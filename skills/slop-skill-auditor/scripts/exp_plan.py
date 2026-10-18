from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
from pathlib import Path

from _common import is_script_file, load_json, parse_frontmatter, sha256_file, split_lines
from exp_stats import MIN_N, UNSTABLE_MIN_N
from exp_tree import inside, iter_tree, skip_git, tree_fingerprint

PLAN_SCHEMA = "slop-skill-auditor/experiment-plan@1"
LOCK_SCHEMA = "slop-skill-auditor/experiment-lock@1"
LOCK_KEYS = ("plan_sha256", "skill_fingerprint", "skill_files", "fixture_fingerprints", "reference_fingerprints")
CONDITIONS = ("A", "B", "C")
EXPERIMENT_TYPES = ("capability", "activation")
FORCING_METHOD = {
    "name": "slash-command",
    "prompt": "'/<skill-name> ' + task.prompt",
    "evidence": "stream-json input with --replay-user-messages; the replayed user message contains <command-name>/<skill-name></command-name> only when Claude Code resolved and expanded the skill",
    "verified": "claude 2.1.302 -p, model haiku: installed skill expanded and its mandated format applied; an unknown /name produced no marker",
}
AUDIT_SOURCE = "audit.json:minimal_prompt"
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Skill"]
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
B_OVERLAP_WARN = 0.30
METRIC_DIRECTIONS = ("higher_better", "lower_better", "informational")
REFERENCE_KINDS = ("copy", "files", "command")
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class ExperimentError(Exception):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_skill_md(skill_dir: Path) -> tuple[dict, list[str]] | None:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    text = md.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    return fm, split_lines(text)[fm["line_end"] or 0:]


def skill_name_from_dir(skill_dir: Path) -> str:
    md = read_skill_md(skill_dir)
    name = md[0]["parsed"].get("name") if md else None
    return name.strip() if isinstance(name, str) and name.strip() else skill_dir.name


def skill_body_lines(skill_dir: Path) -> list[str]:
    md = read_skill_md(skill_dir)
    lines = []
    for ln in md[1] if md else []:
        s = " ".join(ln.split())
        if len(s) >= 8 and not re.fullmatch(r"[`\-=*#|:> ]+", s):
            lines.append(s)
    return lines


def skill_has_scripts(skill_dir: Path) -> list[str]:
    found = []
    for rel, kind, p in iter_tree(skill_dir, skip_git):
        if kind != "file":
            continue
        shebang = False
        try:
            with open(p, "rb") as f:
                shebang = f.read(2) == b"#!"
        except OSError:
            pass
        if is_script_file(rel, os.access(p, os.X_OK), shebang):
            found.append(rel)
    return found


def experiment_root(exp_dir: Path) -> Path:
    audit = exp_dir.parent.parent
    if exp_dir.parent.name == "experiments" and ((audit / "audit.json").is_file() or (audit / "provenance.json").is_file()):
        return audit.resolve()
    return exp_dir.resolve()


def resolve_path(exp_dir: Path, value) -> Path | None:
    if not value:
        return None
    if not isinstance(value, str) or value.startswith("~"):
        raise ExperimentError(f"path {value!r} must be a string without '~'")
    p = Path(value)
    p = (p if p.is_absolute() else exp_dir / p).resolve()
    root = experiment_root(exp_dir)
    if p != root and not inside(p, root):
        raise ExperimentError(f"path {value!r} resolves outside the experiment root {root} (the audit dir, or the experiment dir)")
    return p


def claude_config_problems(root: Path, skill_name: str | None = None) -> list[str]:
    allowed = f".claude/skills/{skill_name}" if skill_name else None
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        for name in dirnames + filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            if allowed and (rel == allowed or rel in (".claude", ".claude/skills") and not os.path.islink(os.path.join(dirpath, name))):
                continue
            if name == ".claude" or name.lower() in ("claude.md", "claude.local.md") or (allowed and rel_dir in (".claude", ".claude/skills")):
                found.append(rel)
        dirnames[:] = [d for d in dirnames if os.path.normpath(os.path.join(rel_dir, d)).replace(os.sep, "/") != allowed]
    return sorted(found)


def plan_conditions(plan: dict) -> tuple[str, ...]:
    return ("C",) if plan["experiment_type"] == "activation" else CONDITIONS


def mcp_for(plan: dict, exp_dir: Path, condition: str) -> Path | None:
    backend = plan["backend"]
    if backend.get("mcp_config") and condition in backend["mcp_config_conditions"]:
        return resolve_path(exp_dir, backend["mcp_config"])
    return None


def read_audit_minimal_prompt(audit_dir: Path) -> str | None:
    path = audit_dir / "audit.json"
    if not path.is_file():
        return None
    try:
        text = (load_json(path).get("minimal_prompt") or {}).get("text")
    except (json.JSONDecodeError, AttributeError):
        return None
    return text if isinstance(text, str) and text.strip() else None


def plan_template(skill_dir: Path, name: str, exp_type: str = "capability", audit_dir: Path | None = None) -> dict:
    plan = {
        "schema": PLAN_SCHEMA,
        "experiment_type": exp_type,
        "skill": {"dir": str(skill_dir), "name": name, "sha256": tree_fingerprint(skill_dir, skip_git)["sha256"]},
        "backend": {
            "name": "claude-code", "model": "sonnet", "effort": None, "tools": list(DEFAULT_TOOLS),
            "max_budget_usd_per_trial": 1.0, "timeout_s": 900, "mcp_config": None,
            "mcp_config_conditions": list(CONDITIONS) if exp_type == "capability" else ["C"],
        },
        "trials_per_condition": 3,
        "seed": secrets.randbelow(2**31),
        "notes": "",
    }
    if exp_type == "activation":
        plan["conditions"] = {"C": {}}
        plan["prompt_composition"] = {"C": "task.prompt (automatic skill selection, no hint)"}
        plan["tasks"] = [
            {"id": "pos1", "should_activate": True, "hypothesis": "", "claim_ids": [], "prompt": "", "fixture_dir": None, "checks": []},
            {"id": "neg1", "should_activate": False, "hypothesis": "", "claim_ids": [], "prompt": "", "fixture_dir": None, "checks": []},
        ]
        return plan
    b = {"minimal_instruction": ""}
    if audit_dir is not None:
        audit_dir = audit_dir.expanduser().resolve()
        text = read_audit_minimal_prompt(audit_dir)
        if text is not None:
            b = {"minimal_instruction": text, "source": AUDIT_SOURCE, "audit_path": str(audit_dir / "audit.json")}
    plan["conditions"] = {"A": {}, "B": b, "C": {}}
    plan["prompt_composition"] = {
        "A": "task.prompt",
        "B": "conditions.B.minimal_instruction + '\\n\\n' + task.prompt",
        "C": FORCING_METHOD["prompt"] + " (skill force-loaded)",
    }
    plan["tasks"] = [{
        "id": "t1", "hypothesis": "", "claim_ids": [], "prompt": "", "fixture_dir": None,
        "rubric": [{"id": "r1", "description": "", "scale": [0, 1, 2, 3], "anchors": {"0": "", "3": ""}}],
        "checks": [],
    }]
    plan["grading"] = {"mode": "llm-judge", "judge_model": "opus", "judge_runs": 1}
    plan["analysis"] = {"neutral_band": 0.5}
    return plan


def plan_paths(exp_dir: Path) -> dict:
    return {
        "plan": exp_dir / "plan.json", "lock": exp_dir / "plan.lock.json",
        "selftest": exp_dir / "selftest.json", "trials": exp_dir / "trials",
        "manifest": exp_dir / "trials.json", "grading": exp_dir / "grading",
    }


def load_plan(exp_dir: Path) -> dict:
    p = plan_paths(exp_dir)["plan"]
    if not p.is_file():
        raise ExperimentError(f"{p} not found (run init first)")
    try:
        return load_json(p)
    except json.JSONDecodeError as e:
        raise ExperimentError(f"plan.json is not valid JSON: {e}")


def safe_relpath(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not value.startswith("/") and ".." not in Path(value).parts


def positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


class _Report:
    def __init__(self, exp_dir: Path):
        self.exp_dir = exp_dir
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.E = self.errors.append
        self.W = self.warnings.append

    def rp(self, value):
        try:
            return resolve_path(self.exp_dir, value)
        except ExperimentError as e:
            self.E(str(e))
            return None


def validate_plan(plan: dict, exp_dir: Path) -> tuple[list[str], list[str]]:
    if not isinstance(plan, dict):
        return ["plan is not an object"], []
    r = _Report(exp_dir)
    E = r.E
    if plan.get("schema") != PLAN_SCHEMA:
        E(f"schema must be {PLAN_SCHEMA!r}")
    skill_dir = _validate_skill(plan.get("skill") or {}, r)
    exp_type = plan.get("experiment_type")
    valid_type = exp_type in EXPERIMENT_TYPES
    if not valid_type:
        exp_type = "capability"
    activation = exp_type == "activation"
    conds = ("C",) if activation else CONDITIONS
    backend = plan.get("backend") or {}
    _validate_backend(backend, conds, r)
    if not valid_type:
        E(f"experiment_type must be one of {list(EXPERIMENT_TYPES)}")
    conditions = plan.get("conditions") or {}
    if set(conditions) != set(conds):
        E(f"conditions must have exactly {list(conds)} for experiment_type {exp_type!r}")
    if not activation:
        _validate_condition_b(conditions.get("B") or {}, skill_dir, r)
    n = plan.get("trials_per_condition")
    if not positive_int(n):
        E("trials_per_condition must be a positive integer")
    elif n < MIN_N and not activation:
        r.W(f"trials_per_condition={n} < {MIN_N}: pairwise comparisons will be flagged insufficient_n")
    if not isinstance(plan.get("seed"), int) or isinstance(plan.get("seed"), bool):
        E("seed must be an integer")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        E("tasks must be a non-empty list")
        tasks = []
    check_task = _validate_activation_task if activation else _validate_capability_task
    seen = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            E(f"tasks[{i}] is not an object")
            continue
        where, fixture = _validate_task_common(t, i, seen, r)
        check_task(t, where, fixture, r)
        _validate_checks(t.get("checks", []), where, E)
    if activation:
        _validate_activation_plan([t for t in tasks if isinstance(t, dict)], n, r)
    else:
        _validate_grading(plan.get("grading") or {}, backend, r)
        analysis = plan.get("analysis")
        band = analysis.get("neutral_band") if isinstance(analysis, dict) else None
        if not isinstance(band, (int, float)) or isinstance(band, bool) or band < 0:
            E("analysis.neutral_band must be a non-negative number (rubric-total points)")
    return r.errors, r.warnings


def _validate_skill(skill: dict, r: _Report) -> Path | None:
    skill_dir = r.rp(skill.get("dir"))
    if not skill_dir or not skill_dir.is_dir():
        r.E(f"skill.dir does not exist: {skill.get('dir')}")
    elif not (skill_dir / "SKILL.md").is_file():
        r.E("skill.dir has no SKILL.md")
    name = skill.get("name")
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        r.E("skill.name must be a non-empty name without '/'")
    return skill_dir


def _validate_backend(backend: dict, conds: tuple[str, ...], r: _Report) -> None:
    E = r.E
    if backend.get("name") != "claude-code":
        E("backend.name must be 'claude-code'")
    if not isinstance(backend.get("model"), str) or not backend.get("model"):
        E("backend.model must be a non-empty string")
    if backend.get("effort") is not None and backend.get("effort") not in VALID_EFFORTS:
        E(f"backend.effort must be null or one of {sorted(VALID_EFFORTS)}")
    tools = backend.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) and t and "," not in t for t in tools):
        E("backend.tools must be a list of tool names")
    elif "Skill" not in tools:
        r.W("backend.tools lacks 'Skill': condition C can only activate the skill by reading files")
    for key in ("max_budget_usd_per_trial", "timeout_s"):
        v = backend.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            E(f"backend.{key} must be a positive number")
    mcp = backend.get("mcp_config")
    if mcp is None:
        return
    mp = r.rp(mcp)
    if not mp or not mp.is_file():
        E(f"backend.mcp_config file not found: {mcp}")
    r.W("backend.mcp_config: MCP servers run outside the sandbox with the trial environment; `run` needs --allow-unsandboxed-mcp")
    selected = backend.get("mcp_config_conditions")
    if not isinstance(selected, list) or not set(selected) <= set(conds):
        E("backend.mcp_config_conditions must be a subset of ['A','B','C']")
    elif set(selected) != set(conds):
        r.W("mcp_config applies to only some conditions: tool availability differs between arms")


def _validate_condition_b(b_cond: dict, skill_dir: Path | None, r: _Report) -> None:
    instr = b_cond.get("minimal_instruction")
    if not isinstance(instr, str) or not instr.strip():
        r.E("conditions.B.minimal_instruction must be non-empty (take it from the audit: init --audit-dir)")
        return
    if b_cond.get("source") == AUDIT_SOURCE:
        ap = r.rp(b_cond.get("audit_path"))
        current = read_audit_minimal_prompt(ap.parent) if ap else None
        if current is None:
            r.E(f"conditions.B.source is {AUDIT_SOURCE!r} but {b_cond.get('audit_path')} has no minimal_prompt.text")
        elif current != instr:
            r.E("conditions.B.minimal_instruction differs from the current audit.json minimal_prompt.text")
    else:
        r.W("conditions.B.minimal_instruction was not copied from audit.json (init --audit-dir); B should be fixed by the audit before the experiment")
    if not skill_dir or not skill_dir.is_dir():
        return
    body = skill_body_lines(skill_dir)
    if body:
        norm = " ".join(instr.split())
        hit = sum(1 for ln in body if ln in norm)
        ratio = hit / len(body)
        if ratio > B_OVERLAP_WARN:
            r.W(f"B minimal_instruction literally contains {hit}/{len(body)} ({ratio:.0%}) of SKILL.md body lines; B should be a short paraphrase, not the skill")
    if len(instr.splitlines()) > 10:
        r.W("B minimal_instruction is longer than 10 lines; it is meant to be a few lines")


def _validate_task_common(t: dict, i: int, seen: set, r: _Report) -> tuple[str, Path | None]:
    E = r.E
    tid = t.get("id")
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        E(f"tasks[{i}].id must match {ID_RE.pattern}")
    elif tid in seen:
        E(f"duplicate task id {tid!r}")
    seen.add(tid)
    where = f"task {tid!r}"
    if not isinstance(t.get("prompt"), str) or not t["prompt"].strip():
        E(f"{where}: prompt must be non-empty")
    if not isinstance(t.get("claim_ids", []), list):
        E(f"{where}: claim_ids must be a list")
    fx = t.get("fixture_dir")
    fp = r.rp(fx)
    if fx is not None:
        if not fp or not fp.is_dir():
            E(f"{where}: fixture_dir not found: {fx}")
        elif problems := claude_config_problems(fp):
            E(f"{where}: fixture_dir contains Claude Code config (.claude/, CLAUDE.md), which the trial agent would load: {problems[:5]}")
    validate_metrics(t.get("metrics"), where, E)
    return where, fp


def _validate_activation_task(t: dict, where: str, fixture: Path | None, r: _Report) -> None:
    if not isinstance(t.get("should_activate"), bool):
        r.E(f"{where}: should_activate must be true or false")
    if t.get("rubric"):
        r.W(f"{where}: rubric is ignored for activation experiments")
    if t.get("reference_outputs") is not None:
        r.W(f"{where}: reference_outputs are ignored for activation experiments")
    if t.get("grading_files") is not None:
        r.W(f"{where}: grading_files is ignored for activation experiments")


def _validate_capability_task(t: dict, where: str, fixture: Path | None, r: _Report) -> None:
    E = r.E
    if not isinstance(t.get("hypothesis"), str) or not t["hypothesis"].strip():
        r.W(f"{where}: hypothesis is empty")
    rubric = t.get("rubric")
    if not isinstance(rubric, list) or not rubric:
        E(f"{where}: rubric must be a non-empty list")
    else:
        _validate_rubric(rubric, where, E)
    refs = t.get("reference_outputs")
    if refs is not None:
        validate_references(refs, where, r.rp, fixture, E)
    gf = t.get("grading_files")
    if gf is not None and (not isinstance(gf, list) or not gf or not all(safe_relpath(x) for x in gf)):
        E(f"{where}: grading_files must be a non-empty list of relative glob patterns without '..'")


def _validate_rubric(rubric: list, where: str, E) -> None:
    rids = set()
    for c in rubric:
        if not isinstance(c, dict):
            E(f"{where}: rubric item is not an object")
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid or cid in rids:
            E(f"{where}: rubric ids must be unique non-empty strings")
        rids.add(cid)
        if not isinstance(c.get("description"), str) or not c["description"].strip():
            E(f"{where}: rubric {cid!r} description is empty")
        scale = c.get("scale")
        if not isinstance(scale, list) or len(scale) < 2 or not all(isinstance(s, int) and not isinstance(s, bool) for s in scale):
            E(f"{where}: rubric {cid!r} scale must be a list of >=2 integers")
            continue
        if "optional" in c and not isinstance(c["optional"], bool):
            E(f"{where}: rubric {cid!r} optional must be true or false")
        anchors = c.get("anchors") or {}
        for s in (min(scale), max(scale)):
            if not isinstance(anchors.get(str(s)), str) or not anchors[str(s)].strip():
                E(f"{where}: rubric {cid!r} needs a non-empty anchor for {s}")


def _validate_checks(checks, where: str, E) -> None:
    if not isinstance(checks, list):
        E(f"{where}: checks must be a list")
        return
    cids = set()
    for c in checks:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not ID_RE.fullmatch(c["id"]) or c["id"] in cids:
            E(f"{where}: check ids must be unique and match {ID_RE.pattern}")
            continue
        cids.add(c["id"])
        if not isinstance(c.get("command"), str) or not c["command"].strip():
            E(f"{where}: check {c['id']!r} command is empty")


def _validate_activation_plan(tasks: list[dict], n, r: _Report) -> None:
    flags = [t.get("should_activate") for t in tasks]
    if True not in flags or False not in flags:
        r.E("activation experiments need at least one should_activate:true and one should_activate:false task")
    if flags.count(False) < 2:
        r.W("activation plan has fewer than 2 negative (near-miss) tasks; the false positive rate rests on very few prompts")
    if isinstance(n, int) and n < UNSTABLE_MIN_N:
        r.W(f"activation plan has trials_per_condition={n} < {UNSTABLE_MIN_N}; per-task activation rates cannot be judged stable")


def _validate_grading(grading: dict, backend: dict, r: _Report) -> None:
    mode = grading.get("mode")
    if mode not in ("llm-judge", "manual"):
        r.E("grading.mode must be 'llm-judge' or 'manual'")
    if mode != "llm-judge":
        return
    if not isinstance(grading.get("judge_model"), str) or not grading["judge_model"]:
        r.E("grading.judge_model must be set for llm-judge")
    if not positive_int(grading.get("judge_runs", 1)):
        r.E("grading.judge_runs must be a positive integer")
    if grading.get("judge_model") == backend.get("model"):
        r.W("judge_model equals backend.model: the judge may favour its own model's style")


def validate_metrics(metrics, where: str, E) -> None:
    if metrics is None:
        return
    if not isinstance(metrics, list):
        E(f"{where}: metrics must be a list")
        return
    ids = set()
    for m in metrics:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not ID_RE.fullmatch(m["id"]) or m["id"] in ids:
            E(f"{where}: metric ids must be unique and match {ID_RE.pattern}")
            continue
        ids.add(m["id"])
        if not isinstance(m.get("command"), str) or not m["command"].strip():
            E(f"{where}: metric {m['id']!r} command is empty")
        if m.get("direction") not in METRIC_DIRECTIONS:
            E(f"{where}: metric {m['id']!r} direction must be one of {list(METRIC_DIRECTIONS)}")


def validate_references(refs, where: str, rp, fixture: Path | None, E) -> None:
    if not isinstance(refs, list):
        E(f"{where}: reference_outputs must be a list")
        return
    ids = set()
    for r in refs:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not ID_RE.fullmatch(r["id"]) or r["id"] in ids:
            E(f"{where}: reference ids must be unique and match {ID_RE.pattern}")
            continue
        ids.add(r["id"])
        rw = f"{where}: reference {r['id']!r}"
        if not isinstance(r.get("description"), str) or not r["description"].strip():
            E(f"{rw}: description is empty")
        if "final_text" in r and not isinstance(r["final_text"], str):
            E(f"{rw}: final_text must be a string")
        kind = r.get("kind")
        if kind not in REFERENCE_KINDS:
            E(f"{rw}: kind must be one of {list(REFERENCE_KINDS)}")
        elif kind == "copy":
            if not safe_relpath(r.get("from")) or not safe_relpath(r.get("to")):
                E(f"{rw}: copy needs relative 'from' and 'to' paths without '..'")
            elif not fixture or not (fixture / r["from"]).exists():
                E(f"{rw}: 'from' {r.get('from')!r} does not exist in the fixture")
        elif kind == "files":
            d = rp(r.get("dir"))
            if not d or not d.is_dir():
                E(f"{rw}: files dir not found: {r.get('dir')}")
            elif problems := claude_config_problems(d):
                E(f"{rw}: files dir contains Claude Code config (.claude/, CLAUDE.md): {problems[:5]}")
        elif not isinstance(r.get("command"), str) or not r["command"].strip():
            E(f"{rw}: command is empty")


def plan_infos(plan: dict) -> list[str]:
    if plan.get("experiment_type") != "capability":
        return []
    tasks = [t for t in plan.get("tasks") or [] if isinstance(t, dict)]
    analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
    if not any(t.get("reference_outputs") for t in tasks) and not analysis.get("reference_note"):
        return ["no task defines reference_outputs and analysis.reference_note is empty: consider what a degenerate output "
                "(no-op, 'no issues found', a clarifying question) would score; if none is meaningful, say why in analysis.reference_note"]
    return []


def reference_fingerprints(plan: dict, exp_dir: Path) -> dict:
    out = {}
    for t in plan["tasks"]:
        for r in t.get("reference_outputs") or []:
            if r.get("kind") == "files":
                d = resolve_path(exp_dir, r.get("dir"))
                out[f"{t['id']}/{r['id']}"] = tree_fingerprint(d)["sha256"] if d and d.is_dir() else None
    return out


def fixture_fingerprints(plan: dict, exp_dir: Path) -> dict:
    out = {}
    for t in plan["tasks"]:
        fp = resolve_path(exp_dir, t.get("fixture_dir"))
        out[t["id"]] = tree_fingerprint(fp)["sha256"] if fp else None
    return out


def plan_hash(exp_dir: Path) -> str:
    return sha256_file(plan_paths(exp_dir)["plan"])


def require_lock(exp_dir: Path, check_inputs: bool = False) -> tuple[dict, dict]:
    paths = plan_paths(exp_dir)
    if not paths["lock"].is_file():
        raise ExperimentError("plan is not frozen; run `freeze` first")
    try:
        lock = load_json(paths["lock"])
    except json.JSONDecodeError as e:
        raise ExperimentError(f"plan.lock.json is not valid JSON: {e}")
    if not isinstance(lock, dict):
        raise ExperimentError("plan.lock.json is not an object")
    actual = plan_hash(exp_dir)
    if actual != lock.get("plan_sha256"):
        raise ExperimentError(
            f"plan.json changed after freeze (locked {lock.get('plan_sha256')}, now {actual}); "
            "results would not match the frozen design. Start a new experiment directory instead."
        )
    if lock.get("schema") != LOCK_SCHEMA or any(k not in lock for k in LOCK_KEYS):
        raise ExperimentError("plan.lock.json is incomplete or has an unknown schema; freeze the plan again in a new experiment directory")
    plan = load_plan(exp_dir)
    if check_inputs:
        skill_dir = resolve_path(exp_dir, plan["skill"]["dir"])
        if not skill_dir.is_dir() or tree_fingerprint(skill_dir, skip_git)["sha256"] != lock["skill_fingerprint"]:
            raise ExperimentError("skill directory changed after freeze")
        if fixture_fingerprints(plan, exp_dir) != lock["fixture_fingerprints"]:
            raise ExperimentError("a fixture directory changed after freeze")
        if reference_fingerprints(plan, exp_dir) != lock["reference_fingerprints"]:
            raise ExperimentError("a reference_outputs files dir changed after freeze")
    return plan, lock
