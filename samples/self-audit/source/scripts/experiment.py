#!/usr/bin/env python3
"""Controlled A/B/C experiments for slop-skill-auditor.

experiment_type "capability": A = baseline (skill absent), B = minimal instruction taken from the
audit (skill absent), C = full skill installed as a project skill and force-loaded via `/<name>`.
experiment_type "activation": only C, automatic selection, tasks marked should_activate true/false.

Subcommands: init, validate, freeze, selftest, run, blind, grade, aggregate.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import platform
import random
import re
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

PLAN_SCHEMA = "slop-skill-auditor/experiment-plan@1"
LOCK_SCHEMA = "slop-skill-auditor/experiment-lock@1"
RESULTS_SCHEMA = "slop-skill-auditor/experiment-results@1"
SELFTEST_SCHEMA = "slop-skill-auditor/experiment-selftest@1"
CONDITIONS = ("A", "B", "C")
EXPERIMENT_TYPES = ("capability", "activation")
FORCING_METHOD = {
    "name": "slash-command",
    "prompt": "'/<skill-name> ' + task.prompt",
    "evidence": "stream-json input with --replay-user-messages; the replayed user message contains <command-name>/<skill-name></command-name> only when Claude Code resolved and expanded the skill",
    "verified": "claude 2.1.302 -p, model haiku: installed skill expanded and its mandated format applied; an unknown /name produced no marker",
}
AUDIT_SOURCE = "audit.json:minimal_prompt"
PAIRS = (("C", "A"), ("C", "B"), ("B", "A"))
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Skill"]
STRIP_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT",
    "CLAUDE_CODE_SESSION_ATTENDED",
)
SCRIPT_EXTS = {".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd", ".lua"}
BOOTSTRAP_RESAMPLES = 10000
MIN_N = 5
B_OVERLAP_WARN = 0.30
CHECK_TIMEOUT_S = 300
DIFF_LIMIT_CHARS = 200_000
REDACTED = "[REDACTED]"
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class ExperimentError(Exception):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_tree(root: Path, skip=None):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root)
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix()
            p = Path(dirpath) / d
            if skip and skip(rel):
                continue
            if p.is_symlink():
                yield rel, "symlink", p
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if skip and skip(rel):
                continue
            p = Path(dirpath) / name
            if p.is_symlink():
                yield rel, "symlink", p
            elif p.is_file():
                yield rel, "file", p


def tree_fingerprint(root: Path, skip=None) -> dict:
    files = {}
    for rel, kind, p in iter_tree(root, skip):
        if kind == "symlink":
            files[rel] = "symlink:" + os.readlink(p)
        else:
            files[rel] = sha256_file(p)
    combined = sha256_bytes(json.dumps(files, sort_keys=True).encode())
    return {"sha256": combined, "files": files}


def skip_git(rel: str) -> bool:
    return rel == ".git" or rel.startswith(".git/")


def skip_installed_skills(rel: str) -> bool:
    return rel == ".claude/skills" or rel.startswith(".claude/skills/")


def child_env() -> dict:
    env = dict(os.environ)
    for k in STRIP_ENV:
        env.pop(k, None)
    return env


def resolve_backend_argv(argv: list[str]) -> list[str]:
    spec = os.environ.get("SLOP_EXPERIMENT_BACKEND", "")
    if spec.startswith("fake:"):
        return [sys.executable, spec[len("fake:"):]] + argv[1:]
    return argv


def workdir_base() -> Path:
    home = Path.home().resolve()
    for cand in (os.environ.get("TMPDIR"), "/tmp"):
        if not cand:
            continue
        p = Path(cand).resolve()
        if p.is_dir() and not (p == home or home in p.parents):
            return p
    raise ExperimentError("no temp directory outside $HOME is available for workdirs")


def make_workdir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir=workdir_base())).resolve()


def run_process(argv, stdin_text, cwd, timeout, stdout_path=None, env=None):
    out_f = open(stdout_path, "wb") if stdout_path else subprocess.PIPE
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out_f, stderr=subprocess.PIPE,
            env=env if env is not None else child_env(), start_new_session=True,
        )
        timed_out = False
        try:
            out, err = proc.communicate(stdin_text.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        return (
            proc.returncode,
            out.decode("utf-8", "replace") if out is not None else None,
            (err or b"").decode("utf-8", "replace"),
            timed_out,
        )
    finally:
        if stdout_path:
            out_f.close()


def claude_version() -> str | None:
    argv = resolve_backend_argv(["claude", "--version"])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=child_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def bwrap_argv(workdir: Path) -> list[str]:
    home = str(Path.home())
    wd = str(workdir)
    return [
        "bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--tmpfs", home,
        "--bind", wd, wd, "--chdir", wd, "--dev", "/dev", "--proc", "/proc",
    ]


def bwrap_status() -> tuple[bool, str]:
    if not shutil.which("bwrap"):
        return False, "bwrap not found"
    probe = make_workdir("slop-bwrap-")
    try:
        r = subprocess.run(bwrap_argv(probe) + ["true"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, "bwrap unusable: " + r.stderr.strip()[:300]
        return True, "ok"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"bwrap unusable: {e}"
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def skill_name_from_dir(skill_dir: Path) -> str:
    md = skill_dir / "SKILL.md"
    if md.is_file():
        text = md.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if m:
            nm = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", m.group(1), re.M)
            if nm:
                return nm.group(1).strip()
    return skill_dir.name


def skill_body_lines(skill_dir: Path) -> list[str]:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return []
    text = md.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
    lines = []
    for ln in text.splitlines():
        s = " ".join(ln.split())
        if len(s) >= 8 and not re.fullmatch(r"[`\-=*#|:> ]+", s):
            lines.append(s)
    return lines


def skill_has_scripts(skill_dir: Path) -> list[str]:
    found = []
    for rel, kind, p in iter_tree(skill_dir, skip_git):
        if kind != "file":
            continue
        executable = os.access(p, os.X_OK)
        shebang = False
        try:
            with open(p, "rb") as f:
                shebang = f.read(2) == b"#!"
        except OSError:
            pass
        if executable or shebang or p.suffix.lower() in SCRIPT_EXTS:
            found.append(rel)
    return found


def resolve_path(exp_dir: Path, value) -> Path | None:
    if not value:
        return None
    p = Path(os.path.expanduser(str(value)))
    return (p if p.is_absolute() else exp_dir / p).resolve()


def plan_conditions(plan: dict) -> tuple[str, ...]:
    return ("C",) if plan.get("experiment_type", "capability") == "activation" else CONDITIONS


def read_audit_minimal_prompt(audit_dir: Path) -> str | None:
    path = audit_dir / "audit.json"
    if not path.is_file():
        return None
    try:
        text = (read_json(path).get("minimal_prompt") or {}).get("text")
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
        return read_json(p)
    except json.JSONDecodeError as e:
        raise ExperimentError(f"plan.json is not valid JSON: {e}")


def validate_plan(plan: dict, exp_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    E = errors.append
    W = warnings.append
    if not isinstance(plan, dict):
        return ["plan is not an object"], []
    if plan.get("schema") != PLAN_SCHEMA:
        E(f"schema must be {PLAN_SCHEMA!r}")
    skill = plan.get("skill") or {}
    skill_dir = resolve_path(exp_dir, skill.get("dir"))
    name = skill.get("name")
    if not skill_dir or not skill_dir.is_dir():
        E(f"skill.dir does not exist: {skill.get('dir')}")
    elif not (skill_dir / "SKILL.md").is_file():
        E("skill.dir has no SKILL.md")
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        E("skill.name must be a non-empty name without '/'")
    backend = plan.get("backend") or {}
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
        W("backend.tools lacks 'Skill': condition C can only activate the skill by reading files")
    for key in ("max_budget_usd_per_trial", "timeout_s"):
        v = backend.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            E(f"backend.{key} must be a positive number")
    mcp = backend.get("mcp_config")
    if mcp is not None:
        mp = resolve_path(exp_dir, mcp)
        if not mp or not mp.is_file():
            E(f"backend.mcp_config file not found: {mcp}")
        conds = backend.get("mcp_config_conditions")
        if not isinstance(conds, list) or not set(conds) <= set(plan_conditions(plan)):
            E("backend.mcp_config_conditions must be a subset of ['A','B','C']")
        elif set(conds) != set(plan_conditions(plan)):
            W("mcp_config applies to only some conditions: tool availability differs between arms")
    exp_type = plan.get("experiment_type", "capability")
    if exp_type not in EXPERIMENT_TYPES:
        E(f"experiment_type must be one of {list(EXPERIMENT_TYPES)}")
        exp_type = "capability"
    activation = exp_type == "activation"
    conditions = plan.get("conditions") or {}
    if set(conditions) != set(plan_conditions(plan)):
        E(f"conditions must have exactly {list(plan_conditions(plan))} for experiment_type {exp_type!r}")
    if "c_invocation" in (conditions.get("C") or {}):
        E("conditions.C.c_invocation is no longer supported: capability always force-loads, activation never does")
    b_cond = conditions.get("B") or {}
    instr = b_cond.get("minimal_instruction")
    if activation:
        pass
    elif not isinstance(instr, str) or not instr.strip():
        E("conditions.B.minimal_instruction must be non-empty (take it from the audit: init --audit-dir)")
    else:
        if b_cond.get("source") == AUDIT_SOURCE:
            ap = resolve_path(exp_dir, b_cond.get("audit_path"))
            current = read_audit_minimal_prompt(ap.parent) if ap else None
            if current is None:
                E(f"conditions.B.source is {AUDIT_SOURCE!r} but {b_cond.get('audit_path')} has no minimal_prompt.text")
            elif current != instr:
                E("conditions.B.minimal_instruction differs from the current audit.json minimal_prompt.text")
        else:
            W("conditions.B.minimal_instruction was not copied from audit.json (init --audit-dir); B should be fixed by the audit before the experiment")
    if not activation and isinstance(instr, str) and instr.strip() and skill_dir and skill_dir.is_dir():
        body = skill_body_lines(skill_dir)
        if body:
            norm = " ".join(instr.split())
            hit = sum(1 for ln in body if ln in norm)
            ratio = hit / len(body)
            if ratio > B_OVERLAP_WARN:
                W(f"B minimal_instruction literally contains {hit}/{len(body)} ({ratio:.0%}) of SKILL.md body lines; B should be a short paraphrase, not the skill")
        if len(instr.splitlines()) > 10:
            W("B minimal_instruction is longer than 10 lines; it is meant to be a few lines")
    n = plan.get("trials_per_condition")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        E("trials_per_condition must be a positive integer")
    elif n < MIN_N and not activation:
        W(f"trials_per_condition={n} < {MIN_N}: pairwise comparisons will be flagged insufficient_n")
    if not isinstance(plan.get("seed"), int) or isinstance(plan.get("seed"), bool):
        E("seed must be an integer")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        E("tasks must be a non-empty list")
        tasks = []
    seen = set()
    for i, t in enumerate(tasks):
        where = f"tasks[{i}]"
        if not isinstance(t, dict):
            E(f"{where} is not an object")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", tid or ""):
            E(f"{where}.id must match [A-Za-z0-9_.-]+")
        elif tid in seen:
            E(f"duplicate task id {tid!r}")
        seen.add(tid)
        where = f"task {tid!r}"
        if not isinstance(t.get("prompt"), str) or not t["prompt"].strip():
            E(f"{where}: prompt must be non-empty")
        if not activation and (not isinstance(t.get("hypothesis"), str) or not t["hypothesis"].strip()):
            W(f"{where}: hypothesis is empty")
        if not isinstance(t.get("claim_ids", []), list):
            E(f"{where}: claim_ids must be a list")
        fx = t.get("fixture_dir")
        if fx is not None:
            fp = resolve_path(exp_dir, fx)
            if not fp or not fp.is_dir():
                E(f"{where}: fixture_dir not found: {fx}")
        if activation:
            if not isinstance(t.get("should_activate"), bool):
                E(f"{where}: should_activate must be true or false")
            if t.get("rubric"):
                W(f"{where}: rubric is ignored for activation experiments")
            rubric = []
        else:
            rubric = t.get("rubric")
        if activation:
            pass
        elif not isinstance(rubric, list) or not rubric:
            E(f"{where}: rubric must be a non-empty list")
            rubric = []
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
            anchors = c.get("anchors") or {}
            for s in (min(scale), max(scale)):
                if not isinstance(anchors.get(str(s)), str) or not anchors[str(s)].strip():
                    E(f"{where}: rubric {cid!r} needs a non-empty anchor for {s}")
        checks = t.get("checks", [])
        if not isinstance(checks, list):
            E(f"{where}: checks must be a list")
            checks = []
        cids = set()
        for c in checks:
            if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] or c["id"] in cids:
                E(f"{where}: check ids must be unique non-empty strings")
                continue
            cids.add(c["id"])
            if not isinstance(c.get("command"), str) or not c["command"].strip():
                E(f"{where}: check {c['id']!r} command is empty")
    if activation:
        flags = [t.get("should_activate") for t in tasks if isinstance(t, dict)]
        if True not in flags or False not in flags:
            E("activation experiments need at least one should_activate:true and one should_activate:false task")
        return errors, warnings
    grading = plan.get("grading") or {}
    mode = grading.get("mode")
    if mode not in ("llm-judge", "manual"):
        E("grading.mode must be 'llm-judge' or 'manual'")
    if mode == "llm-judge":
        if not isinstance(grading.get("judge_model"), str) or not grading["judge_model"]:
            E("grading.judge_model must be set for llm-judge")
        jr = grading.get("judge_runs", 1)
        if not isinstance(jr, int) or isinstance(jr, bool) or jr < 1:
            E("grading.judge_runs must be a positive integer")
        if grading.get("judge_model") == backend.get("model"):
            W("judge_model equals backend.model: the judge may favour its own model's style")
    return errors, warnings


def plan_hash(exp_dir: Path) -> str:
    return sha256_file(plan_paths(exp_dir)["plan"])


def fixture_fingerprints(plan: dict, exp_dir: Path) -> dict:
    out = {}
    for t in plan["tasks"]:
        fp = resolve_path(exp_dir, t.get("fixture_dir"))
        out[t["id"]] = tree_fingerprint(fp)["sha256"] if fp else None
    return out


def require_lock(exp_dir: Path, check_inputs: bool = False) -> tuple[dict, dict]:
    paths = plan_paths(exp_dir)
    if not paths["lock"].is_file():
        raise ExperimentError("plan is not frozen; run `freeze` first")
    lock = read_json(paths["lock"])
    actual = plan_hash(exp_dir)
    if actual != lock.get("plan_sha256"):
        raise ExperimentError(
            f"plan.json changed after freeze (locked {lock.get('plan_sha256')}, now {actual}); "
            "results would not match the frozen design. Start a new experiment directory instead."
        )
    plan = load_plan(exp_dir)
    if check_inputs:
        skill_dir = resolve_path(exp_dir, plan["skill"]["dir"])
        if not skill_dir.is_dir() or tree_fingerprint(skill_dir, skip_git)["sha256"] != lock["skill_fingerprint"]:
            raise ExperimentError("skill directory changed after freeze")
        if fixture_fingerprints(plan, exp_dir) != lock["fixture_fingerprints"]:
            raise ExperimentError("a fixture directory changed after freeze")
    return plan, lock


def build_settings(workdir: Path, sandbox: bool, deny_bash: bool = False) -> dict:
    wd = str(workdir)
    # Claude Code permission paths need a leading '//' to be absolute; '/x' is relative to the settings root.
    settings = {
        "permissions": {
            "allow": [f"Read(/{wd}/**)", f"Edit(/{wd}/**)", f"Write(/{wd}/**)", "Glob", "Grep", "Skill"],
            "deny": ["Read(~/**)", "Edit(~/**)", "WebFetch", "WebSearch"],
        }
    }
    if deny_bash:
        # dontAsk alone still auto-approves "safe" Bash commands (verified: echo > workdir file ran); deny wins over that.
        settings["permissions"]["deny"].append("Bash")
    if sandbox:
        settings["sandbox"] = {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": {"denyRead": ["~/"], "allowWrite": [wd]},
        }
    return settings


def build_backend_command(backend: dict, settings: dict, mcp_config: Path | None = None,
                          model: str | None = None, tools: list[str] | None = None,
                          budget: float | None = None) -> list[str]:
    tools = backend.get("tools", DEFAULT_TOOLS) if tools is None else tools
    argv = [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--replay-user-messages", "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
        "--model", model or backend["model"],
        "--tools", ",".join(tools),
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--max-budget-usd", str(budget if budget is not None else backend["max_budget_usd_per_trial"]),
        "--settings", json.dumps(settings, separators=(",", ":")),
    ]
    if backend.get("effort"):
        argv += ["--effort", backend["effort"]]
    if mcp_config:
        argv += ["--mcp-config", str(mcp_config)]
    return argv


def build_judge_command(judge_model: str, schema: dict, budget: float) -> list[str]:
    return [
        "claude", "-p", "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--tools", "",
        "--model", judge_model,
        "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
        "--permission-mode", "dontAsk", "--max-budget-usd", str(budget),
    ]


def stdin_payload(prompt: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"


def compose_prompt(plan: dict, task: dict, condition: str) -> str:
    prompt = task["prompt"]
    if plan.get("experiment_type", "capability") == "activation":
        return prompt
    if condition == "B":
        return plan["conditions"]["B"]["minimal_instruction"].strip() + "\n\n" + prompt
    if condition == "C":
        return f"/{plan['skill']['name']} {prompt}"
    return prompt


def parse_stream(path: Path) -> dict:
    result = None
    init_skills = None
    tool_uses = []
    tool_results = {}
    texts = []
    if path.is_file():
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "result":
                    result = ev
                if ev.get("type") == "system" and ev.get("subtype") == "init" and isinstance(ev.get("skills"), list):
                    init_skills = [str(x) for x in ev["skills"]]
                msg = ev.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    texts.append(content)
                    continue
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use":
                        tool_uses.append(item)
                    elif item.get("type") == "tool_result":
                        c = item.get("content")
                        if isinstance(c, list):
                            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                        tool_results[item.get("tool_use_id")] = str(c or "")
                    elif item.get("type") == "text":
                        texts.append(item.get("text", ""))
    return {"result": result, "tool_uses": tool_uses, "tool_results": tool_results, "texts": texts, "init_skills": init_skills}


def skill_activation(parsed: dict, skill_name: str) -> tuple[bool, list[str]]:
    evidence = []
    marker = f".claude/skills/{skill_name}"
    for tu in parsed["tool_uses"]:
        name = tu.get("name")
        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
        values = [v for v in inp.values() if isinstance(v, str)]
        if name == "Skill" and any(v == skill_name or v.endswith(":" + skill_name) or v.split()[:1] == [skill_name] for v in values):
            evidence.append(f"Skill tool_use {inp}")
        elif any(marker in v for v in values):
            evidence.append(f"{name} touched {marker}")
    for text in parsed["texts"]:
        if f"<command-name>/{skill_name}</command-name>" in text:
            evidence.append("slash command expanded (<command-name> marker)")
            break
        if (marker + "\n") in text or (marker + "/") in text:
            evidence.append("skill content injected into conversation")
            break
    return bool(evidence), evidence[:10]


SELFTEST_PROMPT = """This is an automated isolation self-test. Perform each step exactly once, in order, using exactly the tool named. Do not retry, do not try alternatives, and continue even if a step fails.
1. Bash tool: cat {probe}
2. Read tool: {probe}
3. Bash tool: curl -s -o /dev/null -m 10 -w 'CURL_STATUS=%{{http_code}}' https://example.com
4. Write tool: create {wd}/write_ok.txt with content WRITE_OK
5. Bash tool: echo BASH_OK > {wd}/bash_ok.txt
6. Bash tool: ls -a ~
Then reply DONE."""


def _step_result(parsed, tool, needle):
    for tu in parsed["tool_uses"]:
        if tu.get("name") != tool:
            continue
        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
        if any(isinstance(v, str) and needle in v for v in inp.values()):
            return parsed["tool_results"].get(tu.get("id"), "")
    return None


def selftest_probe(model: str, sandbox: bool, timeout: int = 300, keep: Path | None = None) -> dict:
    wd = make_workdir("slop-selftest-")
    token = "SLOPPROBE" + secrets.token_hex(8)
    probe = Path.home() / f".slop-sbx-probe-{secrets.token_hex(4)}.txt"
    try:
        probe.write_text(token + "\n", encoding="utf-8")
        settings = build_settings(wd, sandbox, deny_bash=not sandbox)
        argv = build_backend_command(
            {"model": model, "effort": None}, settings, model=model,
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"], budget=0.5,
        )
        transcript = wd.parent / (wd.name + ".jsonl")
        prompt = SELFTEST_PROMPT.format(probe=probe, wd=wd)
        code, _, err, timed_out = run_process(resolve_backend_argv(argv), stdin_payload(prompt), wd, timeout, stdout_path=transcript)
        parsed = parse_stream(transcript)
        raw = transcript.read_text(encoding="utf-8", errors="replace") if transcript.exists() else ""
        if keep is not None:
            keep.write_text(raw, encoding="utf-8")
        transcript.unlink(missing_ok=True)
        res = parsed["result"] or {}
        out = {
            "sandbox_settings": sandbox, "bash_denied_by_rule": not sandbox, "exit_code": code, "timed_out": timed_out,
            "cost_usd": res.get("total_cost_usd"), "stderr_head": err.strip()[:500],
            "result_head": str(res.get("result") or "")[:300],
            "started": bool(parsed["tool_uses"]),
        }
        if not out["started"]:
            out["ok"] = False
            detail = " ".join((err or str(res.get("result") or "no output")).split())[:400]
            out["reason"] = "backend made no tool calls (did it start?): " + detail
            return out
        leak = token in raw

        def blocked(r, pattern=None):
            if r is None:
                return "not_attempted"
            hit = (re.search(pattern, r) is not None) if pattern else token in r
            return "allowed" if hit else "blocked"

        steps = {
            "bash_read_home_file": blocked(_step_result(parsed, "Bash", "cat " + str(probe))),
            "read_tool_home_file": blocked(_step_result(parsed, "Read", str(probe))),
            "bash_network": blocked(_step_result(parsed, "Bash", "curl"), r"CURL_STATUS=[1-9]\d\d"),
            "bash_list_home": blocked(_step_result(parsed, "Bash", "ls -a ~"), re.escape(probe.name)),
        }
        w = wd / "write_ok.txt"
        b = wd / "bash_ok.txt"
        steps["write_tool_workdir"] = "ok" if w.is_file() and "WRITE_OK" in w.read_text(errors="replace") else "failed"
        steps["bash_write_workdir"] = "ok" if b.is_file() else "failed"
        out["steps"] = steps
        out["token_leaked"] = leak
        if sandbox:
            out["ok"] = (not leak and all(steps[k] == "blocked" for k in ("bash_read_home_file", "read_tool_home_file", "bash_network", "bash_list_home"))
                         and steps["write_tool_workdir"] == "ok" and steps["bash_write_workdir"] == "ok")
        else:
            out["ok"] = (not leak and steps["read_tool_home_file"] == "blocked" and steps["write_tool_workdir"] == "ok"
                         and steps["bash_write_workdir"] == "failed")
        if not out["ok"]:
            out["reason"] = "expected isolation not observed (see steps)"
        return out
    finally:
        probe.unlink(missing_ok=True)
        shutil.rmtree(wd, ignore_errors=True)


def do_selftest(exp_dir: Path, model: str = "haiku", force: bool = False) -> dict:
    path = plan_paths(exp_dir)["selftest"]
    version = claude_version()
    if path.is_file() and not force:
        cached = read_json(path)
        if cached.get("claude_version") == version:
            return cached
    sb = selftest_probe(model, sandbox=True, keep=path.with_name("selftest_sandbox.jsonl"))
    perm = selftest_probe(model, sandbox=False, keep=path.with_name("selftest_permissions.jsonl"))
    data = {
        "schema": SELFTEST_SCHEMA, "ran_at": now_iso(), "claude_version": version, "model": model,
        "platform": platform.platform(), "bwrap": bool(shutil.which("bwrap")), "socat": bool(shutil.which("socat")),
        "sandbox": sb, "permissions_only": perm,
        "sandbox_ok": bool(sb.get("ok")),
        "file_isolation_ok": bool(perm.get("ok")),
        "unsandboxed_bash_denied": perm.get("steps", {}).get("bash_write_workdir") == "failed" and perm.get("started", False),
        "cost_usd": round(sum(x.get("cost_usd") or 0 for x in (sb, perm)), 6),
    }
    write_json(path, data)
    return data


def build_trials(plan: dict) -> list[dict]:
    trials = []
    used = set()
    for t in plan["tasks"]:
        for cond in plan_conditions(plan):
            for k in range(plan["trials_per_condition"]):
                tid = secrets.token_hex(4)
                while tid in used:
                    tid = secrets.token_hex(4)
                used.add(tid)
                trials.append({"trial_id": tid, "task_id": t["id"], "condition": cond, "replicate": k})
    random.Random(plan["seed"]).shuffle(trials)
    for i, tr in enumerate(trials):
        tr["order"] = i
    return trials


def load_or_create_manifest(exp_dir: Path, plan: dict, lock: dict, persist: bool) -> list[dict]:
    path = plan_paths(exp_dir)["manifest"]
    if path.is_file():
        m = read_json(path)
        if m.get("plan_sha256") != lock["plan_sha256"]:
            raise ExperimentError("trials.json belongs to a different plan hash")
        return m["trials"]
    trials = build_trials(plan)
    if persist:
        write_json(path, {"plan_sha256": lock["plan_sha256"], "created_at": now_iso(), "seed": plan["seed"], "trials": trials})
    return trials


def sandbox_policy(plan: dict, exp_dir: Path) -> dict:
    skill_dir = resolve_path(exp_dir, plan["skill"]["dir"])
    scripts = skill_has_scripts(skill_dir)
    bash = "Bash" in plan["backend"]["tools"]
    reasons = []
    if scripts:
        reasons.append(f"skill contains executable-looking files: {scripts[:5]}")
    if bash:
        reasons.append("backend.tools includes Bash")
    return {"required": bool(reasons), "reasons": reasons, "skill_scripts": scripts}


def ignore_for_copy(root: Path, exclude_skills: bool):
    root = root.resolve()

    def ign(d, names):
        dpath = Path(d)
        # copytree would block on FIFOs and choke on sockets/devices an agent may leave behind.
        out = {n for n in names if not ((dpath / n).is_symlink() or (dpath / n).is_dir() or (dpath / n).is_file())}
        if exclude_skills and dpath.resolve() == root / ".claude" and "skills" in names:
            out.add("skills")
        return out
    return ign


def copy_tree(src: Path, dst: Path, exclude_skills: bool = False, skip_names=()) -> None:
    base_ignore = ignore_for_copy(src, exclude_skills)

    def ign(d, names):
        out = base_ignore(d, names)
        if Path(d).resolve() == src.resolve():
            out |= {n for n in names if n in skip_names}
        return out
    # symlinks=True copies links as links so an agent-created link to e.g. ~/.ssh is never followed.
    shutil.copytree(src, dst, symlinks=True, ignore=ign, dirs_exist_ok=True)


def run_checks(task: dict, workdir: Path, bwrap_ok: bool, bwrap_reason: str) -> list[dict]:
    out = []
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()), "LANG": os.environ.get("LANG", "C.UTF-8")}
    for c in task.get("checks", []):
        if not bwrap_ok:
            out.append({"id": c["id"], "status": "not_run", "exit_code": None, "passed": None, "reason": bwrap_reason})
            continue
        try:
            r = subprocess.run(bwrap_argv(workdir) + ["sh", "-c", c["command"]], capture_output=True, text=True,
                               timeout=CHECK_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
            out.append({"id": c["id"], "status": "ran", "exit_code": r.returncode, "passed": r.returncode == 0,
                        "stdout_tail": r.stdout[-2000:], "stderr_tail": r.stderr[-2000:]})
        except subprocess.TimeoutExpired:
            out.append({"id": c["id"], "status": "timeout", "exit_code": None, "passed": False})
    return out


def files_changed(before: dict, after: dict) -> tuple[list[str], dict]:
    kinds = {}
    for p in set(before) | set(after):
        if p not in before:
            kinds[p] = "added"
        elif p not in after:
            kinds[p] = "deleted"
        elif before[p] != after[p]:
            kinds[p] = "modified"
    return sorted(kinds), dict(sorted(kinds.items()))


def execute_trial(exp_dir: Path, plan: dict, trial: dict, mode: dict, bwrap: tuple[bool, str]) -> dict:
    task = next(t for t in plan["tasks"] if t["id"] == trial["task_id"])
    cond = trial["condition"]
    tdir = plan_paths(exp_dir)["trials"] / trial["trial_id"]
    if tdir.exists():
        shutil.rmtree(tdir)
    tdir.mkdir(parents=True)
    wd = make_workdir("slop-trial-")
    skill_name = plan["skill"]["name"]
    try:
        fx = resolve_path(exp_dir, task.get("fixture_dir"))
        if fx:
            copy_tree(fx, wd)
        if cond == "C":
            skill_dir = resolve_path(exp_dir, plan["skill"]["dir"])
            dst = wd / ".claude" / "skills" / skill_name
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(skill_dir, dst, skip_names=(".git",))
        before = tree_fingerprint(wd, skip_installed_skills)["files"]
        prompt = compose_prompt(plan, task, cond)
        settings = build_settings(wd, mode["sandbox"], deny_bash=mode["bash_denied"])
        mcp = None
        if plan["backend"].get("mcp_config") and cond in plan["backend"].get("mcp_config_conditions", plan_conditions(plan)):
            mcp = resolve_path(exp_dir, plan["backend"]["mcp_config"])
        argv = build_backend_command(plan["backend"], settings, mcp)
        (tdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        write_json(tdir / "command.json", {"argv": argv, "cwd": str(wd)})
        started = now_iso()
        t0 = dt.datetime.now().timestamp()
        error = None
        try:
            code, _, err, timed_out = run_process(resolve_backend_argv(argv), stdin_payload(prompt), wd, plan["backend"]["timeout_s"],
                                                  stdout_path=tdir / "transcript.jsonl")
        except OSError as e:
            code, err, timed_out = None, str(e), False
            error = f"backend failed to start: {e}"
        duration = round(dt.datetime.now().timestamp() - t0, 3)
        (tdir / "stderr.txt").write_text(err or "", encoding="utf-8")
        if timed_out:
            error = f"timeout after {plan['backend']['timeout_s']}s"
        parsed = parse_stream(tdir / "transcript.jsonl")
        res = parsed["result"]
        if res is None and error is None:
            error = "no result line in transcript" + (f" (exit {code}): {(err or '').strip()[-300:]}" if code else "")
        tool_calls: dict[str, int] = {}
        for tu in parsed["tool_uses"]:
            tool_calls[tu.get("name", "?")] = tool_calls.get(tu.get("name", "?"), 0) + 1
        activated, evidence = skill_activation(parsed, skill_name)
        checks = run_checks(task, wd, *bwrap)
        after = tree_fingerprint(wd, skip_installed_skills)["files"]
        changed, kinds = files_changed(before, after)
        copy_tree(wd, tdir / "workspace", exclude_skills=True)
        result = {
            "trial_id": trial["trial_id"], "task_id": task["id"], "condition": cond, "replicate": trial["replicate"],
            "order": trial["order"], "started_at": started, "duration_s": duration, "exit_code": code,
            "is_error": res.get("is_error") if res else None,
            "subtype": res.get("subtype") if res else None,
            "final_text": res.get("result") if res and isinstance(res.get("result"), str) else None,
            "usage": res.get("usage") if res else None,
            "total_cost_usd": res.get("total_cost_usd") if res else None,
            "model_usage": res.get("modelUsage") if res else None,
            "num_turns": res.get("num_turns") if res else None,
            "tool_calls": dict(sorted(tool_calls.items())),
            "skill_activated": activated, "skill_activation_evidence": evidence,
            "skill_discovered": (skill_name in parsed["init_skills"]) if parsed["init_skills"] is not None else None,
            "forced": plan.get("experiment_type", "capability") == "capability" and cond == "C",
            "should_activate": task.get("should_activate"),
            "checks": checks, "files_changed": changed, "files_change_kind": kinds,
            "prompt_sha256": sha256_bytes(prompt.encode()),
            "isolation": {"mode": mode["name"], "sandbox": mode["sandbox"], "allow_unsandboxed": mode["allow_unsandboxed"],
                          "bash_effectively_denied": mode["bash_denied"]},
            "error": error,
        }
        write_json(tdir / "result.json", result)
        return result
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def decide_mode(plan: dict, exp_dir: Path, allow_unsandboxed: bool, dry_run: bool) -> dict:
    pol = sandbox_policy(plan, exp_dir)
    mode = {"required": pol["required"], "reasons": pol["reasons"], "allow_unsandboxed": allow_unsandboxed, "bash_denied": False}
    if not pol["required"]:
        mode.update(name="permissions-only", sandbox=False)
        return mode
    if dry_run:
        st_path = plan_paths(exp_dir)["selftest"]
        ok = st_path.is_file() and read_json(st_path).get("sandbox_ok")
        mode.update(name="sandbox" if ok else "sandbox (selftest pending/failed)", sandbox=True)
        return mode
    st = do_selftest(exp_dir, model="haiku")
    if st.get("sandbox_ok"):
        mode.update(name="sandbox", sandbox=True)
        return mode
    if not allow_unsandboxed:
        reason = (st.get("sandbox") or {}).get("reason", "unknown")
        raise ExperimentError(
            "sandbox required (" + "; ".join(pol["reasons"]) + ") but sandbox selftest failed: " + reason
            + ". Fix the sandbox, or rerun with --allow-unsandboxed (Bash is then denied by an explicit deny rule, "
            "file tools stay restricted to the workdir)."
        )
    if not st.get("file_isolation_ok"):
        raise ExperimentError("permission-rule isolation selftest also failed; refusing to run even with --allow-unsandboxed")
    mode.update(name="unsandboxed (Bash denied by rule)", sandbox=False, bash_denied=True)
    return mode


def cmd_run(exp_dir: Path, dry_run: bool, only: list[str] | None, allow_unsandboxed: bool) -> int:
    plan, lock = require_lock(exp_dir, check_inputs=True)
    trials = load_or_create_manifest(exp_dir, plan, lock, persist=not dry_run)
    if only:
        known = {t["trial_id"] for t in trials}
        bad = [x for x in only if x not in known]
        if bad:
            raise ExperimentError(f"unknown trial ids: {bad}")
        trials = [t for t in trials if t["trial_id"] in set(only)]
    mode = decide_mode(plan, exp_dir, allow_unsandboxed, dry_run)
    tasks = {t["id"]: t for t in plan["tasks"]}
    if dry_run:
        print(f"# isolation mode: {mode['name']} (reasons: {mode['reasons'] or 'none'})")
        print(f"# experiment_type: {plan.get('experiment_type', 'capability')}")
        print(f"# prompt composition: {json.dumps(plan.get('prompt_composition'))}")
        print("# stdin is one stream-json user message wrapping the prompt below")
        for t in trials:
            wd = Path("/<workdir>")
            argv = build_backend_command(plan["backend"], build_settings(wd, mode["sandbox"], deny_bash=mode["bash_denied"]),
                                         resolve_path(exp_dir, plan["backend"].get("mcp_config")) if t["condition"] in plan["backend"].get("mcp_config_conditions", plan_conditions(plan)) else None)
            print(f"\n## trial {t['trial_id']} order={t['order']} task={t['task_id']} condition={t['condition']}")
            print("workdir contents: fixture" + (f" + .claude/skills/{plan['skill']['name']}/" if t["condition"] == "C" else ""))
            print("argv: " + json.dumps(argv))
            print("stdin prompt:\n" + compose_prompt(plan, tasks[t["task_id"]], t["condition"]))
        return 0
    bw = bwrap_status()
    if not bw[0]:
        print(f"warning: checks will not run: {bw[1]}", file=sys.stderr)
    run_log = exp_dir / "run_log.jsonl"
    with open(run_log, "a", encoding="utf-8") as lf:
        lf.write(json.dumps({"at": now_iso(), "mode": mode, "only": only, "bwrap": bw[0]}) + "\n")
    done = skipped = 0
    for t in trials:
        if (plan_paths(exp_dir)["trials"] / t["trial_id"] / "result.json").is_file():
            skipped += 1
            continue
        r = execute_trial(exp_dir, plan, t, mode, bw)
        done += 1
        print(f"[{t['order']}] {t['trial_id']} done: exit={r['exit_code']} cost={r['total_cost_usd']} error={r['error']}", file=sys.stderr)
    print(json.dumps({"ran": done, "skipped_existing": skipped, "mode": mode["name"]}))
    return 0


def redact(text: str, skill_name: str) -> tuple[str, int]:
    count = 0
    terms = sorted({skill_name, "SKILL.md", ".claude/skills"}, key=len, reverse=True)
    for term in terms:
        pat = re.compile(re.escape(term), re.I)
        text, n = pat.subn(REDACTED, text)
        count += n
    return text, count


def unified_tree_diff(old: Path | None, new: Path) -> str:
    def collect(root):
        if not root or not root.is_dir():
            return {}
        return {rel: (kind, p) for rel, kind, p in iter_tree(root, skip_installed_skills)}

    def content(entry):
        kind, p = entry
        if kind == "symlink":
            return ["symlink -> " + os.readlink(p) + "\n"], False
        data = p.read_bytes()
        if b"\0" in data[:8192]:
            return data, True
        try:
            return data.decode("utf-8").splitlines(keepends=True), False
        except UnicodeDecodeError:
            return data, True

    a, b = collect(old), collect(new)
    chunks = []
    for rel in sorted(set(a) | set(b)):
        ca, bina = content(a[rel]) if rel in a else ([], False)
        cb, binb = content(b[rel]) if rel in b else ([], False)
        if ca == cb:
            continue
        if bina or binb:
            chunks.append(f"Binary files {'a/' + rel if rel in a else '/dev/null'} and {'b/' + rel if rel in b else '/dev/null'} differ\n")
            continue
        diff = difflib.unified_diff(ca, cb, "a/" + rel if rel in a else "/dev/null", "b/" + rel if rel in b else "/dev/null")
        chunks.append("".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in diff))
    return "".join(chunks)


def fixture_digest(fx: Path | None) -> str:
    if not fx or not fx.is_dir():
        return "(the task started in an empty working directory)\n"
    parts = []
    for rel, kind, p in iter_tree(fx):
        if kind == "symlink":
            parts.append(f"### {rel}\nsymlink -> {os.readlink(p)}\n")
            continue
        data = p.read_bytes()
        try:
            text = data.decode("utf-8") if b"\0" not in data[:8192] else None
        except UnicodeDecodeError:
            text = None
        parts.append(f"### {rel}\n" + (f"```\n{text}\n```\n" if text is not None else f"(binary, {len(data)} bytes)\n"))
    out = "Initial files in the working directory (identical for every graded item of this task):\n\n" + "\n".join(parts)
    if len(out) > DIFF_LIMIT_CHARS:
        out = out[:DIFF_LIMIT_CHARS] + f"\n[inputs truncated at {DIFF_LIMIT_CHARS} characters]\n"
    return out


def cmd_blind(exp_dir: Path, force: bool) -> int:
    plan, lock = require_lock(exp_dir)
    if plan.get("experiment_type") == "activation":
        print("activation experiment: nothing to blind (activation is measured from transcripts); run aggregate")
        return 0
    gdir = plan_paths(exp_dir)["grading"]
    if gdir.exists():
        if not force:
            raise ExperimentError(f"{gdir} already exists; use --force to rebuild (existing grades are discarded)")
        shutil.rmtree(gdir)
    results = load_results(exp_dir)
    if not results:
        raise ExperimentError("no completed trials to blind")
    skill_name = plan["skill"]["name"]
    tasks = {t["id"]: t for t in plan["tasks"]}
    trial_ids = list(results)
    random.SystemRandom().shuffle(trial_ids)
    used = set(results)
    key, redactions = {}, {}
    for trial_id in trial_ids:
        r = results[trial_id]
        item_id = secrets.token_hex(4)
        while item_id in used:
            item_id = secrets.token_hex(4)
        used.add(item_id)
        idir = gdir / "items" / item_id
        idir.mkdir(parents=True)
        out_text, n1 = redact(r.get("final_text") or "", skill_name)
        fx = resolve_path(exp_dir, tasks[r["task_id"]].get("fixture_dir"))
        diff_text, n2 = redact(unified_tree_diff(fx, plan_paths(exp_dir)["trials"] / trial_id / "workspace"), skill_name)
        if len(diff_text) > DIFF_LIMIT_CHARS:
            diff_text = diff_text[:DIFF_LIMIT_CHARS] + f"\n[diff truncated at {DIFF_LIMIT_CHARS} characters]\n"
        (idir / "output.md").write_text(out_text, encoding="utf-8")
        (idir / "diff.patch").write_text(diff_text, encoding="utf-8")
        write_json(idir / "item.json", {"item_id": item_id, "task_id": r["task_id"]})
        key[item_id] = trial_id
        redactions[item_id] = n1 + n2
    write_json(gdir / "key.json", {
        "plan_sha256": lock["plan_sha256"], "created_at": now_iso(), "items": key,
        "redaction": {"applied": True, "terms": [skill_name, "SKILL.md", ".claude/skills"], "replacement": REDACTED,
                      "case_insensitive": True, "counts": redactions},
        "note": "Do not show this file to the judge.",
    })
    write_json(gdir / "rubric.json", {t["id"]: {"prompt": t["prompt"], "rubric": t["rubric"]} for t in plan["tasks"]})
    for t in plan["tasks"]:
        (gdir / "inputs").mkdir(exist_ok=True)
        (gdir / "inputs" / f"{t['id']}.md").write_text(fixture_digest(resolve_path(exp_dir, t.get("fixture_dir"))), encoding="utf-8")
    print(json.dumps({"items": len(key), "redacted_items": sum(1 for v in redactions.values() if v), "dir": str(gdir)}))
    return 0


def judge_schema(rubric: list[dict]) -> dict:
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": {c["id"]: {"type": "integer", "enum": sorted(c["scale"])} for c in rubric},
                "required": [c["id"] for c in rubric], "additionalProperties": False,
            },
            "rationale": {"type": "string"},
        },
        "required": ["scores", "rationale"], "additionalProperties": False,
    }


def judge_prompt(task_prompt: str, rubric: list[dict], output: str, diff: str, inputs: str = "") -> str:
    crit = []
    for c in rubric:
        anchors = "\n".join(f"    {k}: {v}" for k, v in sorted(c.get("anchors", {}).items(), key=lambda kv: int(kv[0])))
        crit.append(f"- id: {c['id']}\n  description: {c['description']}\n  scale: {c['scale']}\n  anchors:\n{anchors}")
    return (
        "You are an impartial grader. An AI agent was given the task below. Grade only the evidence shown: "
        "the initial files it was given, its final answer, and the diff of files it changed in its working directory. Some strings may appear as "
        f"{REDACTED}; treat them as neutral and do not guess what they were. Score every criterion on its scale "
        "using the anchors (interpolate for unanchored values). Return the scores and a short rationale.\n\n"
        f"<task_prompt>\n{task_prompt}\n</task_prompt>\n\n<task_inputs>\n{inputs}</task_inputs>\n\n<rubric>\n" + "\n".join(crit) + "\n</rubric>\n\n"
        f"<final_answer>\n{output}\n</final_answer>\n\n<diff>\n{diff}\n</diff>\n"
    )


def run_judge(plan: dict, rubric: list[dict], prompt: str) -> dict:
    g = plan["grading"]
    argv = build_judge_command(g["judge_model"], judge_schema(rubric), plan["backend"]["max_budget_usd_per_trial"])
    cwd = make_workdir("slop-judge-")
    try:
        code, out, err, timed_out = run_process(resolve_backend_argv(argv), prompt, cwd, plan["backend"]["timeout_s"])
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    rec = {"at": now_iso(), "exit_code": code, "scores": None, "rationale": None, "error": None,
           "total_cost_usd": None, "model_usage": None}
    if timed_out:
        rec["error"] = "judge timeout"
        return rec
    try:
        data = json.loads(out or "")
    except json.JSONDecodeError:
        rec["error"] = f"judge output not JSON (exit {code}): {(err or out or '').strip()[-300:]}"
        return rec
    rec["total_cost_usd"] = data.get("total_cost_usd")
    rec["model_usage"] = data.get("modelUsage")
    so = data.get("structured_output")
    if not isinstance(so, dict):
        rec["error"] = f"no structured_output (subtype={data.get('subtype')}, is_error={data.get('is_error')})"
        return rec
    scores = so.get("scores") or {}
    bad = [c["id"] for c in rubric if not (isinstance(scores.get(c["id"]), int) and scores[c["id"]] in c["scale"])]
    if bad:
        rec["error"] = f"invalid or missing scores for {bad}"
    rec["scores"] = {c["id"]: scores.get(c["id"]) for c in rubric if c["id"] not in bad}
    rec["rationale"] = so.get("rationale")
    return rec


def cmd_grade(exp_dir: Path) -> int:
    plan, lock = require_lock(exp_dir)
    if plan.get("experiment_type") == "activation":
        print("activation experiment: nothing to grade (no rubric); run aggregate")
        return 0
    gdir = plan_paths(exp_dir)["grading"]
    if not (gdir / "key.json").is_file():
        raise ExperimentError("run `blind` first")
    rubrics = read_json(gdir / "rubric.json")
    items = sorted(p.name for p in (gdir / "items").iterdir() if p.is_dir())
    grades_dir = gdir / "grades"
    grades_dir.mkdir(exist_ok=True)
    mode = plan["grading"]["mode"]
    if mode == "manual":
        return manual_grading(gdir, items, rubrics, grades_dir)
    runs = plan["grading"].get("judge_runs", 1)
    total_cost = 0.0
    for item_id in items:
        gpath = grades_dir / f"{item_id}.json"
        rec = read_json(gpath) if gpath.is_file() else None
        idir = gdir / "items" / item_id
        task_id = read_json(idir / "item.json")["task_id"]
        if rec is None:
            rec = {"item_id": item_id, "task_id": task_id, "mode": "llm-judge", "judge_model": plan["grading"]["judge_model"], "runs": []}
        rec["runs"] = [r for r in rec["runs"] if r.get("error") is None]
        rubric = rubrics[task_id]["rubric"]
        inputs_path = gdir / "inputs" / f"{task_id}.md"
        prompt = judge_prompt(rubrics[task_id]["prompt"], rubric, (idir / "output.md").read_text(encoding="utf-8"),
                              (idir / "diff.patch").read_text(encoding="utf-8"),
                              inputs_path.read_text(encoding="utf-8") if inputs_path.is_file() else "")
        while len(rec["runs"]) < runs:
            r = run_judge(plan, rubric, prompt)
            r["run"] = len(rec["runs"])
            total_cost += r.get("total_cost_usd") or 0
            if r["error"]:
                rec.setdefault("failed_runs", []).append(r)
                print(f"{item_id}: judge error: {r['error']}", file=sys.stderr)
                break
            rec["runs"].append(r)
        write_json(gpath, rec)
    print(json.dumps({"items": len(items), "judge_cost_usd_this_call": round(total_cost, 6)}))
    return 0


def manual_grading(gdir: Path, items: list[str], rubrics: dict, grades_dir: Path) -> int:
    criteria = []
    for t in rubrics.values():
        for c in t["rubric"]:
            if c["id"] not in criteria:
                criteria.append(c["id"])
    filled = gdir / "grades.csv"
    if filled.is_file():
        n = 0
        with open(filled, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                item_id = row["item_id"]
                task_id = row["task_id"]
                rubric = rubrics[task_id]["rubric"]
                scores, bad = {}, []
                for c in rubric:
                    v = (row.get(c["id"]) or "").strip()
                    if v.lstrip("-").isdigit() and int(v) in c["scale"]:
                        scores[c["id"]] = int(v)
                    else:
                        bad.append(c["id"])
                write_json(grades_dir / f"{item_id}.json", {
                    "item_id": item_id, "task_id": task_id, "mode": "manual",
                    "runs": [{"run": 0, "scores": scores, "rationale": row.get("rationale"),
                              "error": f"invalid or missing scores for {bad}" if bad else None}],
                })
                n += 1
        print(f"imported {n} manual grades from {filled}")
        return 0
    template = gdir / "grades_template.csv"
    with open(template, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "task_id", *criteria, "rationale"])
        for item_id in items:
            task_id = read_json(gdir / "items" / item_id / "item.json")["task_id"]
            own = {c["id"] for c in rubrics[task_id]["rubric"]}
            w.writerow([item_id, task_id, *["" if c in own else "n/a" for c in criteria], ""])
    print("Manual grading:")
    print(f"  1. For each item in {gdir / 'items'}, read output.md and diff.patch.")
    print(f"  2. Score it against {gdir / 'rubric.json'} (task prompt + rubric), using {gdir / 'inputs'} for the task's initial files. Do not open key.json.")
    print(f"  3. Fill {template} and save it as {filled}.")
    print("  4. Run `grade` again to import the scores.")
    return 0


def load_results(exp_dir: Path) -> dict:
    out = {}
    tdir = plan_paths(exp_dir)["trials"]
    if tdir.is_dir():
        for p in sorted(tdir.iterdir()):
            rp = p / "result.json"
            if rp.is_file():
                out[p.name] = read_json(rp)
    return out


def describe(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "stdev": None, "reason": "no values"}
    d = {"n": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
         "stdev": statistics.stdev(values) if len(values) > 1 else None}
    if len(values) < 2:
        d["reason"] = "stdev needs n>=2"
    return d


def derived_seed(seed: int, *parts) -> int:
    return int(sha256_bytes("|".join(map(str, (seed, *parts))).encode())[:16], 16)


def bootstrap_diff_ci(x: list[float], y: list[float], seed: int, resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float, float]:
    rng = random.Random(seed)
    nx, ny = len(x), len(y)
    diffs = sorted(sum(rng.choices(x, k=nx)) / nx - sum(rng.choices(y, k=ny)) / ny for _ in range(resamples))
    lo = diffs[int(0.025 * (resamples - 1))]
    hi = diffs[int(round(0.975 * (resamples - 1)))]
    return lo, hi


def item_scores(grade: dict | None, rubric: list[dict]) -> tuple[dict, float | None, str | None]:
    if grade is None:
        return {}, None, "no grade file"
    runs = [r for r in grade.get("runs", []) if isinstance(r.get("scores"), dict)]
    per = {}
    for c in rubric:
        vals = [r["scores"][c["id"]] for r in runs if isinstance(r["scores"].get(c["id"]), int)]
        per[c["id"]] = statistics.fmean(vals) if vals else None
    missing = [k for k, v in per.items() if v is None]
    if missing:
        return per, None, f"missing scores for {missing}"
    return per, sum(per.values()), None


def mean_or_null(values, what):
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return {"value": None, "reason": f"no {what} reported"}
    out = {"value": statistics.fmean(vals), "n": len(vals)}
    if len(vals) < len(values):
        out["missing"] = len(values) - len(vals)
    return out


def common_header(plan, lock, results, manifest, selftest) -> dict:
    models, cost, starts, modes, allow = set(), 0.0, [], set(), set()
    for r in results.values():
        models.update((r.get("model_usage") or {}).keys())
        cost += r.get("total_cost_usd") or 0
        starts.append(r["started_at"])
        iso = r.get("isolation") or {}
        modes.add(iso.get("mode"))
        allow.add(iso.get("allow_unsandboxed"))
    return {
        "schema": RESULTS_SCHEMA, "experiment_type": plan.get("experiment_type", "capability"),
        "generated_at": now_iso(),
        "plan_sha256": lock["plan_sha256"], "frozen_at": lock.get("frozen_at"), "env": lock.get("env"),
        "skill": {"name": plan["skill"]["name"], "fingerprint": lock.get("skill_fingerprint")},
        "forcing_method": lock.get("forcing_method"),
        "backend": plan["backend"],
        "models_reported": {"trials": sorted(models), "judge": []},
        "dates": {"first_trial_started": min(starts) if starts else None, "last_trial_started": max(starts) if starts else None},
        "trials": {"planned": len(manifest), "completed": len(results)},
        "cost_usd": {"trials": round(cost, 6), "judge": 0.0, "selftest": selftest.get("cost_usd") if selftest else None},
        "isolation": {"run_modes": sorted(m for m in modes if m), "allow_unsandboxed_used": True in allow,
                      "selftest": {k: selftest.get(k) for k in ("sandbox_ok", "file_isolation_ok", "unsandboxed_bash_denied", "ran_at")} if selftest else None},
    }


def isolation_limitation(header) -> str:
    st = header["isolation"]["selftest"]
    return (f"Isolation: run modes {header['isolation']['run_modes']}; --allow-unsandboxed used: {header['isolation']['allow_unsandboxed_used']}; "
            f"sandbox selftest ok: {st.get('sandbox_ok') if st else 'not run'}.")


def rate(k, n, what):
    return {"value": k / n, "k": k, "n": n} if n else {"value": None, "k": 0, "n": 0, "reason": f"no {what}"}


def aggregate_activation(exp_dir, plan, lock, results, manifest, selftest) -> dict:
    out = common_header(plan, lock, results, manifest, selftest)
    per_task, counts = [], {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    errored = []
    for t in plan["tasks"]:
        rs = [r for r in results.values() if r["task_id"] == t["id"]]
        valid = [r for r in rs if not (r.get("error") or r.get("is_error"))]
        errored += [r["trial_id"] for r in rs if r not in valid]
        k = sum(bool(r.get("skill_activated")) for r in valid)
        pos = bool(t["should_activate"])
        counts["tp" if pos else "fp"] += k
        counts["fn" if pos else "tn"] += len(valid) - k
        disc = [r.get("skill_discovered") for r in rs if r.get("skill_discovered") is not None]
        per_task.append({
            "task_id": t["id"], "should_activate": pos,
            "n_planned": sum(1 for m in manifest if m["task_id"] == t["id"]), "n_completed": len(rs), "n_valid": len(valid),
            "activation_rate": rate(k, len(valid), "valid trials"),
            "skill_discovered_rate": rate(sum(disc), len(disc), "init events listing skills"),
            "error_count": len(rs) - len(valid),
            "duration_s": mean_or_null([r.get("duration_s") for r in rs], "duration"),
            "cost_usd": mean_or_null([r.get("total_cost_usd") for r in rs], "cost"),
        })
    out["activation"] = {
        "per_task": per_task, "counts": counts,
        "true_positive_rate": rate(counts["tp"], counts["tp"] + counts["fn"], "valid positive trials"),
        "false_positive_rate": rate(counts["fp"], counts["fp"] + counts["tn"], "valid negative trials"),
        "excluded_errored_trials": errored,
    }
    n_pos = sum(1 for t in plan["tasks"] if t["should_activate"])
    out["limitations"] = [
        f"Prompt sample: {n_pos} positive and {len(plan['tasks']) - n_pos} negative task prompt(s) written by the auditor, {plan['trials_per_condition']} trial(s) each; rates do not generalise beyond these prompts.",
        f"Single backend model configuration: {plan['backend']['model']} (reported model ids: {out['models_reported']['trials'] or 'none'}).",
        "Activation is detected from the transcript (Skill tool_use naming the skill, or a tool touching .claude/skills/<name>); a skill applied from memory without loading is not counted.",
        "Only this skill is installed as a project skill; in real use other installed skills compete for selection.",
        "Built-in Claude Code skills and the default system prompt are present.",
        isolation_limitation(out),
    ]
    if errored:
        out["limitations"].append(f"{len(errored)} errored trial(s) excluded from rates.")
    return out


def render_activation_md(res: dict) -> str:
    a = res["activation"]
    L = [f"# Activation experiment: {res['skill']['name']}", "",
         f"- Plan sha256: `{res['plan_sha256']}` (frozen {res['frozen_at']})",
         f"- Trials: {res['trials']['completed']}/{res['trials']['planned']} completed",
         f"- Models reported: {', '.join(res['models_reported']['trials']) or 'none'}",
         f"- Cost USD: trials {res['cost_usd']['trials']}, selftest {res['cost_usd']['selftest']}", "",
         "| task | should_activate | n valid | activated | rate | skill discovered | errors |", "|---|---|---|---|---|---|---|"]
    for t in a["per_task"]:
        ar = t["activation_rate"]
        L.append(f"| {t['task_id']} | {t['should_activate']} | {t['n_valid']} | {ar['k']} | {fmt_val(ar)} | {fmt_val(t['skill_discovered_rate'])} | {t['error_count']} |")
    c = a["counts"]
    L += ["", f"- True positive rate: {fmt_val(a['true_positive_rate'])} (TP={c['tp']}, FN={c['fn']})",
          f"- False positive rate: {fmt_val(a['false_positive_rate'])} (FP={c['fp']}, TN={c['tn']})", "",
          "## Limitations", ""] + [f"- {x}" for x in res["limitations"]] + ["",
          "No conclusions are drawn here; interpretation is left to the auditor.", ""]
    return "\n".join(L)


def cmd_aggregate(exp_dir: Path) -> int:
    plan, lock = require_lock(exp_dir)
    paths = plan_paths(exp_dir)
    results = load_results(exp_dir)
    manifest = read_json(paths["manifest"])["trials"] if paths["manifest"].is_file() else []
    if plan.get("experiment_type") == "activation":
        selftest = read_json(paths["selftest"]) if paths["selftest"].is_file() else None
        out = aggregate_activation(exp_dir, plan, lock, results, manifest, selftest)
        write_json(exp_dir / "results.json", out)
        (exp_dir / "results.md").write_text(render_activation_md(out), encoding="utf-8")
        print(json.dumps({"results": str(exp_dir / "results.json"), "markdown": str(exp_dir / "results.md")}))
        return 0
    key = {}
    redaction = None
    if (paths["grading"] / "key.json").is_file():
        kd = read_json(paths["grading"] / "key.json")
        key = {v: k for k, v in kd["items"].items()}
        redaction = kd.get("redaction")
    grades = {}
    gd = paths["grading"] / "grades"
    if gd.is_dir():
        for p in gd.glob("*.json"):
            grades[p.stem] = read_json(p)
    tasks = {t["id"]: t for t in plan["tasks"]}
    cells = {}
    totals_by = {}
    missing = []
    c_not_loaded = []
    trial_models, judge_models = set(), set()
    trial_cost = judge_cost = 0.0
    starts = []
    for g in grades.values():
        for r in g.get("runs", []) + g.get("failed_runs", []):
            judge_models.update((r.get("model_usage") or {}).keys())
            judge_cost += r.get("total_cost_usd") or 0
    for task_id, task in tasks.items():
        for cond in CONDITIONS:
            planned = [m for m in manifest if m["task_id"] == task_id and m["condition"] == cond]
            rs_all = [r for r in results.values() if r["task_id"] == task_id and r["condition"] == cond]
            rs = [r for r in rs_all if not (cond == "C" and not r.get("skill_activated"))]
            for r in rs_all:
                if r not in rs:
                    trial_models.update((r.get("model_usage") or {}).keys())
                    trial_cost += r.get("total_cost_usd") or 0
                    starts.append(r["started_at"])
                    item = key.get(r["trial_id"])
                    _, total, why = item_scores(grades.get(item), task["rubric"]) if item else ({}, None, "not blinded")
                    c_not_loaded.append({"trial_id": r["trial_id"], "task_id": task_id, "total_score": total,
                                         "reason": "skill not loaded despite forcing" + (f"; score missing: {why}" if total is None else ""),
                                         "error": r.get("error")})
            crit_vals = {c["id"]: [] for c in task["rubric"]}
            tot = []
            for r in rs:
                trial_models.update((r.get("model_usage") or {}).keys())
                trial_cost += r.get("total_cost_usd") or 0
                starts.append(r["started_at"])
                item = key.get(r["trial_id"])
                if item is None:
                    missing.append({"trial_id": r["trial_id"], "reason": "not blinded (no grading item)"})
                    continue
                per, total, why = item_scores(grades.get(item), task["rubric"])
                for k, v in per.items():
                    if v is not None:
                        crit_vals[k].append(v)
                if total is None:
                    missing.append({"trial_id": r["trial_id"], "item_id": item, "reason": why})
                else:
                    tot.append(total)
            if task.get("checks"):
                ran = [r for r in rs if r["checks"] and all(c.get("passed") is not None for c in r["checks"])]
                success = ({"value": sum(all(c["passed"] for c in r["checks"]) for r in ran) / len(ran), "n": len(ran)}
                           if ran else {"value": None, "reason": "checks not run" if rs else "no completed trials"})
            else:
                success = {"value": None, "reason": "task defines no checks"}
            usage_keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            cells[f"{task_id}/{cond}"] = {
                "task_id": task_id, "condition": cond, "n_planned": len(planned), "n_completed": len(rs_all),
                "n_in_comparison": len(rs), "n_excluded_not_loaded": len(rs_all) - len(rs),
                "n_scored": len(tot),
                "success_rate": success,
                "criteria": {k: describe(v) for k, v in crit_vals.items()},
                "total_score": describe(tot),
                "max_total_score": sum(max(c["scale"]) for c in task["rubric"]),
                "duration_s": mean_or_null([r.get("duration_s") for r in rs], "duration"),
                "tokens": {k: mean_or_null([(r.get("usage") or {}).get(k) for r in rs], k) for k in usage_keys},
                "cost_usd": mean_or_null([r.get("total_cost_usd") for r in rs], "cost"),
                "num_turns": mean_or_null([r.get("num_turns") for r in rs], "num_turns"),
                "skill_activation_rate": ({"value": sum(bool(r.get("skill_activated")) for r in rs) / len(rs), "n": len(rs)}
                                          if rs else {"value": None, "reason": "no completed trials"}),
                "error_count": sum(1 for r in rs if r.get("error") or r.get("is_error") or (r.get("exit_code") not in (0, None))),
            }
            totals_by[(task_id, cond)] = tot
    pairwise = []
    for task_id in tasks:
        for a, b in PAIRS:
            x, y = totals_by[(task_id, a)], totals_by[(task_id, b)]
            entry = {"task_id": task_id, "comparison": f"{a}-{b}", "n_a": len(x), "n_b": len(y),
                     "insufficient_n": len(x) < MIN_N or len(y) < MIN_N}
            if not x or not y:
                entry.update(mean_diff=None, ci95=None, reason="an arm has no scored trials")
            else:
                entry["mean_diff"] = statistics.fmean(x) - statistics.fmean(y)
                lo, hi = bootstrap_diff_ci(x, y, derived_seed(plan["seed"], task_id, a, b))
                entry["ci95"] = [lo, hi]
                entry["ci_method"] = f"percentile bootstrap, {BOOTSTRAP_RESAMPLES} resamples, arms resampled independently"
            pairwise.append(entry)
    run_modes = set()
    allow_flags = set()
    for r in results.values():
        iso = r.get("isolation") or {}
        run_modes.add(iso.get("mode"))
        allow_flags.add(iso.get("allow_unsandboxed"))
    selftest = read_json(paths["selftest"]) if paths["selftest"].is_file() else None
    n_values = sorted({c["n_scored"] for c in cells.values()})
    limitations = [
        f"Sample size: scored trials per task x condition = {n_values}; pairs with n<{MIN_N} per arm are flagged insufficient_n and their CIs are unreliable.",
        f"Single backend model configuration: {plan['backend']['model']} (reported model ids: {sorted(trial_models) or 'none'}); results may not transfer to other models or agents.",
        (f"Scores come from an LLM judge ({plan['grading']['judge_model']}, {plan['grading'].get('judge_runs', 1)} run(s) per item); judge bias and variance are not calibrated against humans."
         if plan["grading"]["mode"] == "llm-judge" else "Scores come from manual human grading."),
        "Blinding is by opaque ids and redaction of the skill name/paths only; outputs may still reveal the condition through skill-specific content or format.",
        f"Isolation: run modes {sorted(m for m in run_modes if m)}; --allow-unsandboxed used: {True in allow_flags}; sandbox selftest ok: {selftest.get('sandbox_ok') if selftest else 'not run'}.",
        "Built-in Claude Code skills and the default system prompt are present in all conditions equally.",
        "Bootstrap CIs resample trials only; they ignore task-to-task variation and judge noise.",
    ]
    if any(r.get("isolation", {}).get("bash_effectively_denied") for r in results.values()):
        limitations.append("Bash was denied in these trials (no verified sandbox), so skills that rely on running scripts could not exercise them.")
    if missing:
        limitations.append(f"{len(missing)} trial(s) lack a total score; see missing[].")
    if c_not_loaded:
        limitations.append(f"{len(c_not_loaded)} C trial(s) did not load the skill despite forcing and are excluded from the comparison; see c_not_loaded[].")
    if len(results) < len(manifest):
        limitations.append(f"Only {len(results)} of {len(manifest)} planned trials completed.")
    out = {
        "schema": RESULTS_SCHEMA, "experiment_type": "capability", "forcing_method": lock.get("forcing_method"),
        "generated_at": now_iso(),
        "plan_sha256": lock["plan_sha256"], "frozen_at": lock.get("frozen_at"), "env": lock.get("env"),
        "skill": {"name": plan["skill"]["name"], "fingerprint": lock.get("skill_fingerprint")},
        "backend": plan["backend"], "grading": plan["grading"],
        "models_reported": {"trials": sorted(trial_models), "judge": sorted(judge_models)},
        "dates": {"first_trial_started": min(starts) if starts else None, "last_trial_started": max(starts) if starts else None},
        "trials": {"planned": len(manifest), "completed": len(results)},
        "cost_usd": {"trials": round(trial_cost, 6), "judge": round(judge_cost, 6),
                     "selftest": selftest.get("cost_usd") if selftest else None},
        "isolation": {"run_modes": sorted(m for m in run_modes if m), "allow_unsandboxed_used": True in allow_flags,
                      "selftest": {k: selftest.get(k) for k in ("sandbox_ok", "file_isolation_ok", "unsandboxed_bash_denied", "ran_at")} if selftest else None},
        "redaction": redaction,
        "cells": cells, "pairwise": pairwise, "missing": missing, "c_not_loaded": c_not_loaded, "limitations": limitations,
    }
    write_json(exp_dir / "results.json", out)
    (exp_dir / "results.md").write_text(render_md(out, tasks), encoding="utf-8")
    print(json.dumps({"results": str(exp_dir / "results.json"), "markdown": str(exp_dir / "results.md"),
                      "missing": len(missing)}))
    return 0


def fmt(v, digits=2):
    if v is None:
        return "null"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def fmt_val(d, digits=2):
    if d.get("value") is None:
        return f"null ({d.get('reason')})"
    return fmt(d["value"], digits)


def render_md(res: dict, tasks: dict) -> str:
    L = [f"# Experiment results: {res['skill']['name']}", "",
         f"- Plan sha256: `{res['plan_sha256']}` (frozen {res['frozen_at']})",
         f"- Generated: {res['generated_at']}",
         f"- Trials: {res['trials']['completed']}/{res['trials']['planned']} completed",
         f"- Models reported (trials): {', '.join(res['models_reported']['trials']) or 'none'}",
         f"- Models reported (judge): {', '.join(res['models_reported']['judge']) or 'none'}",
         f"- Cost USD: trials {res['cost_usd']['trials']}, judge {res['cost_usd']['judge']}, selftest {res['cost_usd']['selftest']}",
         f"- Environment: {json.dumps(res.get('env'))}",
         f"- C forcing method: {(res.get('forcing_method') or {}).get('name')} ({(res.get('forcing_method') or {}).get('prompt')})", "",
         "Conditions: A = baseline (no skill), B = minimal instruction from the audit (no skill), C = full skill installed and force-loaded.",
         "C trials where the skill did not load are excluded from the tables below and listed under 'C not loaded'.", ""]
    for task_id, task in tasks.items():
        L += [f"## Task `{task_id}`", "", f"Hypothesis: {task.get('hypothesis') or '(none)'}", "",
              "| cond | n done | n scored | total mean | median | stdev | success rate | skill activation | errors | mean s | mean cost | mean out tokens |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for cond in CONDITIONS:
            c = res["cells"][f"{task_id}/{cond}"]
            ts = c["total_score"]
            L.append(f"| {cond} | {c['n_completed']} | {c['n_scored']} | {fmt(ts['mean'])} / {c['max_total_score']} | {fmt(ts['median'])} | {fmt(ts['stdev'])} "
                     f"| {fmt_val(c['success_rate'])} | {fmt_val(c['skill_activation_rate'])} | {c['error_count']} "
                     f"| {fmt_val(c['duration_s'], 1)} | {fmt_val(c['cost_usd'], 4)} | {fmt_val(c['tokens']['output_tokens'], 0)} |")
        L += ["", "| criterion | " + " | ".join(f"{c} mean (n)" for c in CONDITIONS) + " |", "|---|" + "---|" * len(CONDITIONS)]
        for crit in task["rubric"]:
            row = [f"{fmt(res['cells'][f'{task_id}/{c}']['criteria'][crit['id']]['mean'])} ({res['cells'][f'{task_id}/{c}']['criteria'][crit['id']]['n']})" for c in CONDITIONS]
            L.append(f"| {crit['id']} | " + " | ".join(row) + " |")
        L += ["", "| comparison | mean diff (total) | 95% CI | n | insufficient_n |", "|---|---|---|---|---|"]
        for p in res["pairwise"]:
            if p["task_id"] != task_id:
                continue
            ci = f"[{fmt(p['ci95'][0])}, {fmt(p['ci95'][1])}]" if p.get("ci95") else f"null ({p.get('reason')})"
            L.append(f"| {p['comparison']} | {fmt(p['mean_diff'])} | {ci} | {p['n_a']} vs {p['n_b']} | {p['insufficient_n']} |")
        L.append("")
    if res.get("c_not_loaded"):
        L += ["## C not loaded (excluded)", ""] + [f"- trial `{m['trial_id']}` (task {m['task_id']}): total {fmt(m['total_score'])}; {m['reason']}" for m in res["c_not_loaded"]] + [""]
    if res["missing"]:
        L += ["## Missing values", ""] + [f"- trial `{m['trial_id']}`: {m['reason']}" for m in res["missing"]] + [""]
    L += ["## Limitations", ""] + [f"- {x}" for x in res["limitations"]] + ["",
          "No conclusions are drawn here; interpretation is left to the auditor.", ""]
    return "\n".join(L)


def cmd_init(exp_dir: Path, skill_dir: Path, skill_name: str | None, exp_type: str = "capability",
             audit_dir: Path | None = None) -> int:
    skill_dir = skill_dir.expanduser().resolve()
    if not (skill_dir / "SKILL.md").is_file():
        raise ExperimentError(f"{skill_dir} has no SKILL.md")
    if (exp_dir / "plan.json").exists():
        raise ExperimentError(f"{exp_dir / 'plan.json'} already exists")
    exp_dir.mkdir(parents=True, exist_ok=True)
    name = skill_name or skill_name_from_dir(skill_dir)
    if exp_type == "activation" and audit_dir is not None:
        print("note: --audit-dir is ignored for activation experiments (no condition B)", file=sys.stderr)
    plan = plan_template(skill_dir, name, exp_type, audit_dir)
    if exp_type == "capability" and audit_dir is not None and not plan["conditions"]["B"].get("source"):
        print(f"warning: {audit_dir}/audit.json has no minimal_prompt.text; conditions.B.minimal_instruction left empty", file=sys.stderr)
    write_json(exp_dir / "plan.json", plan)
    print(f"wrote {exp_dir / 'plan.json'}; fill in tasks, rubric and conditions.B.minimal_instruction, then validate and freeze")
    return 0


def report_validation(errors, warnings) -> None:
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)


def cmd_validate(exp_dir: Path) -> int:
    errors, warnings = validate_plan(load_plan(exp_dir), exp_dir)
    report_validation(errors, warnings)
    print(json.dumps({"ok": not errors, "errors": len(errors), "warnings": len(warnings)}))
    return 1 if errors else 0


def cmd_freeze(exp_dir: Path) -> int:
    plan = load_plan(exp_dir)
    errors, warnings = validate_plan(plan, exp_dir)
    report_validation(errors, warnings)
    if errors:
        return 1
    paths = plan_paths(exp_dir)
    h = plan_hash(exp_dir)
    if paths["lock"].is_file():
        old = read_json(paths["lock"])
        if old.get("plan_sha256") == h:
            print(f"already frozen at {old.get('frozen_at')}")
            return 0
        raise ExperimentError("plan.json changed after freeze; start a new experiment directory instead")
    skill_dir = resolve_path(exp_dir, plan["skill"]["dir"])
    skill_fp = tree_fingerprint(skill_dir, skip_git)
    lock = {
        "schema": LOCK_SCHEMA, "plan_sha256": h, "frozen_at": now_iso(),
        "skill_fingerprint": skill_fp["sha256"], "skill_files": skill_fp["files"],
        "fixture_fingerprints": fixture_fingerprints(plan, exp_dir),
        "env": {"claude_version": claude_version(), "python": platform.python_version(), "platform": platform.platform(),
                "bwrap_present": bool(shutil.which("bwrap")), "socat_present": bool(shutil.which("socat"))},
        "warnings": warnings,
    }
    if plan.get("experiment_type", "capability") == "capability":
        lock["forcing_method"] = FORCING_METHOD
    if plan["skill"].get("sha256") and plan["skill"]["sha256"] != skill_fp["sha256"]:
        print("warning: skill files changed since init; the lock records the current fingerprint", file=sys.stderr)
    write_json(paths["lock"], lock)
    print(json.dumps({"frozen": True, "plan_sha256": h}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="experiment.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="create an experiment dir with a plan.json template")
    p.add_argument("exp_dir")
    p.add_argument("--skill-dir", required=True)
    p.add_argument("--skill-name")
    p.add_argument("--type", dest="exp_type", choices=EXPERIMENT_TYPES, default="capability")
    p.add_argument("--audit-dir", help="audit directory whose audit.json minimal_prompt.text becomes condition B")
    for name, help_ in (("validate", "check plan.json"), ("freeze", "validate and lock plan.json"),
                        ("grade", "grade blinded items (LLM judge or manual CSV)"), ("aggregate", "compute results.json/results.md")):
        sub.add_parser(name, help=help_).add_argument("exp_dir")
    p = sub.add_parser("selftest", help="verify sandbox and permission isolation against the real backend")
    p.add_argument("exp_dir")
    p.add_argument("--model", default="haiku")
    p.add_argument("--force", action="store_true", help="ignore the cached selftest.json")
    p = sub.add_parser("run", help="run all pending trials")
    p.add_argument("exp_dir")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only", nargs="+", metavar="TRIAL_ID")
    p.add_argument("--allow-unsandboxed", action="store_true")
    p = sub.add_parser("blind", help="build anonymized grading items")
    p.add_argument("exp_dir")
    p.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    exp_dir = Path(a.exp_dir).expanduser().resolve()
    try:
        if a.cmd == "init":
            return cmd_init(exp_dir, Path(a.skill_dir), a.skill_name, a.exp_type, Path(a.audit_dir) if a.audit_dir else None)
        if a.cmd == "validate":
            return cmd_validate(exp_dir)
        if a.cmd == "freeze":
            return cmd_freeze(exp_dir)
        if a.cmd == "selftest":
            load_plan(exp_dir)
            st = do_selftest(exp_dir, a.model, a.force)
            print(json.dumps({k: st.get(k) for k in ("sandbox_ok", "file_isolation_ok", "unsandboxed_bash_denied", "cost_usd")}
                             | {"sandbox_reason": st["sandbox"].get("reason"), "path": str(plan_paths(exp_dir)["selftest"])}))
            return 0 if st.get("sandbox_ok") else 3
        if a.cmd == "run":
            return cmd_run(exp_dir, a.dry_run, a.only, a.allow_unsandboxed)
        if a.cmd == "blind":
            return cmd_blind(exp_dir, a.force)
        if a.cmd == "grade":
            return cmd_grade(exp_dir)
        if a.cmd == "aggregate":
            return cmd_aggregate(exp_dir)
    except ExperimentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
