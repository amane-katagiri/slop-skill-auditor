#!/usr/bin/env python3
"""Controlled A/B/C experiments for slop-skill-auditor.

experiment_type "capability": A = baseline (skill absent), B = minimal instruction taken from the
audit (skill absent), C = full skill installed as a project skill and force-loaded via `/<name>`.
experiment_type "activation": only C, automatic selection, tasks marked should_activate true/false.

Subcommands: init, validate, freeze, selftest, run, blind, grade, aggregate.

Check and metric commands run in the trial workdir inside bwrap with no network and an empty $HOME.
This skill's scripts directory is mounted read-only inside that sandbox and exposed as $SLOP_TOOLS,
e.g. a metric command: python3 -I "$SLOP_TOOLS/text_metrics.py" edit-ratio draft.md out.md
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import platform
import random
import re
import secrets
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_json, write_json  # noqa: E402
from exp_grading import (  # noqa: E402
    DIFF_LIMIT_CHARS, GRADING_FILE_LIMIT_CHARS, REDACTED, fixture_digest, grading_files_digest, judge_prompt, redact,
    redaction_terms, run_judge, unified_tree_diff,
)
from exp_plan import (  # noqa: E402
    CONDITIONS, EXPERIMENT_TYPES, FORCING_METHOD, LOCK_SCHEMA, ExperimentError, claude_config_problems,
    experiment_root, fixture_fingerprints, load_plan, mcp_for, now_iso, plan_conditions, plan_hash, plan_infos,
    plan_paths, plan_template, reference_fingerprints, require_lock, resolve_path, skill_has_scripts,
    skill_name_from_dir, validate_plan,
)
from exp_render import render_activation_md, render_md  # noqa: E402
from exp_sandbox import (  # noqa: E402
    FileSizeLimitExceeded, bwrap_status, build_checked_argv, build_settings, claude_version, isolation_deny_dirs, make_workdir,
    resolve_backend_argv, run_process, safe_rmtree, sandbox_run, stdin_payload, workdir_base,
)
from exp_stats import (  # noqa: E402
    BOOTSTRAP_RESAMPLES, MIN_N, PARETO_DEFINITIONS, UNSTABLE_MARGIN, UNSTABLE_MIN_N, band_label, bootstrap_diff_ci,
    derived_seed, describe, diff_or_none, fisher_exact, item_scores, mean_or_null, metric_direction_note,
    metric_values, min_attainable_p, pareto_label, rate, scores_per_run, wilson,
)
from exp_tree import (  # noqa: E402
    EMPTY_SHA256, UnsafeDestination, copy_tree, files_changed, inside, safe_copy_file, safe_overlay, sha256_bytes, skip_git,
    skip_installed_skills, tree_fingerprint,
)

RESULTS_SCHEMA = "slop-skill-auditor/experiment-results@1"
SELFTEST_SCHEMA = "slop-skill-auditor/experiment-selftest@3"
SELFTEST_SUMMARY_KEYS = ("sandbox_ok", "file_isolation_ok", "write_isolation_ok", "unsandboxed_bash_denied", "hooks_disabled", "ran_at")
PAIRS = (("C", "A"), ("C", "B"), ("B", "A"))
CHECK_TIMEOUT_S = 300
REFERENCE_COMMAND_TIMEOUT_S = 60
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def save_json(path: Path, data) -> None:
    write_json(path, data, sort_keys=False)


def compose_prompt(plan: dict, task: dict, condition: str) -> str:
    prompt = task["prompt"]
    if plan["experiment_type"] == "activation":
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
    tool_errors = set()
    texts = []
    user_texts = []
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
                bucket = user_texts if ev.get("type") == "user" else texts
                if isinstance(content, str):
                    bucket.append(content)
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
                        if item.get("is_error"):
                            tool_errors.add(item.get("tool_use_id"))
                    elif item.get("type") == "text":
                        bucket.append(item.get("text", ""))
    return {"result": result, "tool_uses": tool_uses, "tool_results": tool_results, "tool_errors": tool_errors, "texts": texts,
            "user_texts": user_texts, "init_skills": init_skills}


def skill_activation(parsed: dict, skill_name: str) -> tuple[bool, list[str]]:
    evidence = []
    skill_md = f".claude/skills/{skill_name}/SKILL.md"
    for tu in parsed["tool_uses"]:
        if tu.get("id") not in parsed["tool_results"] or tu.get("id") in parsed["tool_errors"]:
            continue
        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
        skill, path = inp.get("skill"), inp.get("file_path")
        if tu.get("name") == "Skill" and isinstance(skill, str) and (
                skill == skill_name or skill.endswith(":" + skill_name) or skill.split()[:1] == [skill_name]):
            evidence.append(f"Skill tool_use {inp}")
        elif tu.get("name") == "Read" and isinstance(path, str) and (path == skill_md or path.endswith("/" + skill_md)):
            evidence.append(f"Read {skill_md}")
    if any(f"<command-name>/{skill_name}</command-name>" in text for text in parsed["user_texts"]):
        evidence.append("slash command expanded (<command-name> marker)")
    return bool(evidence), evidence[:10]


SELFTEST_PROMPT = """This is an automated isolation self-test. Perform each step exactly once, in order, using exactly the tool named. Do not retry, do not try alternatives, and continue even if a step fails.
1. Bash tool: cat {probe}
2. Read tool: {probe}
3. Bash tool: curl -s -o /dev/null -m 10 -w 'CURL_STATUS=%{{http_code}}' https://example.com
4. Write tool: create {wd}/write_ok.txt with content WRITE_OK
5. Bash tool: echo BASH_OK > {wd}/bash_ok.txt
6. Bash tool: ls -a ~
7. Bash tool: cat {exp_probe} {skill_probe}
8. Read tool: {exp_probe}
9. Grep tool: pattern SLOPPROBE in path {skill_dir}
10. Glob tool: pattern *.txt in path {wd}
11. Bash tool: echo WPROBE > {bash_write_experiment_dir}
12. Bash tool: echo WPROBE > {bash_write_temp_sibling}
13. Write tool: create {write_tool_experiment_dir} with content WPROBE
14. Write tool: create {write_tool_temp_sibling} with content WPROBE
Then reply DONE."""
HOOK_SKILL = "slop-hook-probe"
HOOK_EVENTS_SKILL = ("PreToolUse", "PostToolUse", "Stop")
HOOK_EVENTS_PROJECT = ("SessionStart", "UserPromptSubmit", "PreToolUse")


def hook_entry(marker: Path) -> list[dict]:
    return [{"matcher": "*", "hooks": [{"type": "command", "command": f"touch {marker}"}]}]


def install_hook_probes(wd: Path) -> list[Path]:
    markers = []
    skill_hooks = []
    for ev in HOOK_EVENTS_SKILL:
        m = wd / f"hook_fired_skill_{ev}"
        markers.append(m)
        skill_hooks += [f"  {ev}:", "    - matcher: \"*\"", "      hooks:", "        - type: command", f"          command: \"touch {m}\""]
    skill = wd / ".claude" / "skills" / HOOK_SKILL
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("\n".join(
        ["---", f"name: {HOOK_SKILL}", "description: Isolation self-test helper. Follow the user's numbered steps.", "hooks:", *skill_hooks,
         "---", "Follow the numbered steps in the user's message exactly.", ""]), encoding="utf-8")
    project = {}
    for ev in HOOK_EVENTS_PROJECT:
        m = wd / f"hook_fired_project_{ev}"
        markers.append(m)
        project[ev] = hook_entry(m)
    (wd / ".claude" / "settings.json").write_text(json.dumps({"hooks": project}), encoding="utf-8")
    return markers


def _step_result(parsed, tool, needle):
    for tu in parsed["tool_uses"]:
        if tu.get("name") != tool:
            continue
        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
        if any(isinstance(v, str) and needle in v for v in inp.values()):
            return parsed["tool_results"].get(tu.get("id"), "")
    return None


def write_probe_steps(parsed: dict, markers: dict) -> dict:
    steps = {}
    for key, (tool, marker) in markers.items():
        if marker.exists():
            steps[key] = "allowed"
        else:
            steps[key] = "blocked" if _step_result(parsed, tool, str(marker)) is not None else "not_attempted"
    return steps


def write_isolation(steps: dict, sandbox: bool) -> bool:
    if any(v == "allowed" for v in steps.values()):
        return False
    return all(v == "blocked" for k, v in steps.items() if sandbox or k.startswith("write_tool_"))


def selftest_probe(model: str, sandbox: bool, exp_dir: Path, skill_dir: Path, timeout: int = 300, keep: Path | None = None) -> dict:
    wd = make_workdir("slop-selftest-")
    sib = make_workdir("slop-selftest-sib-")
    token = "SLOPPROBE" + secrets.token_hex(8)
    tag = secrets.token_hex(4)
    probe = Path.home() / f".slop-sbx-probe-{tag}.txt"
    exp_probe = exp_dir / f".slop-selftest-probe-{tag}.txt"
    skill_probe = skill_dir / f".slop-selftest-probe-{tag}.txt"
    write_markers = {
        "bash_write_experiment_dir": ("Bash", exp_dir / f".slop-wprobe-bash-{tag}"),
        "bash_write_temp_sibling": ("Bash", sib / f".slop-wprobe-bash-{tag}"),
        "write_tool_experiment_dir": ("Write", exp_dir / f".slop-wprobe-tool-{tag}"),
        "write_tool_temp_sibling": ("Write", sib / f".slop-wprobe-tool-{tag}"),
    }
    try:
        for f in (probe, exp_probe, skill_probe):
            f.write_text(token + "\n", encoding="utf-8")
        markers = install_hook_probes(wd)
        settings = build_settings(wd, sandbox, deny_bash=not sandbox, deny_dirs=isolation_deny_dirs(exp_dir, skill_dir, wd))
        probe_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Skill"]
        argv = build_checked_argv(
            {"model": model, "effort": None}, settings, allowed_tools=probe_tools, model=model,
            tools=probe_tools, budget=0.5,
        )
        transcript = wd.parent / (wd.name + ".jsonl")
        prompt = f"/{HOOK_SKILL} " + SELFTEST_PROMPT.format(probe=probe, wd=wd, exp_probe=exp_probe, skill_probe=skill_probe, skill_dir=skill_dir,
                                                                  **{k: m for k, (_, m) in write_markers.items()})
        code, _, err, timed_out = run_process(resolve_backend_argv(argv), stdin_payload(prompt), wd, timeout, stdout_path=transcript)
        parsed = parse_stream(transcript)
        raw = transcript.read_text(encoding="utf-8", errors="replace") if transcript.exists() else ""
        if keep is not None:
            keep.write_text(raw, encoding="utf-8")
        transcript.unlink(missing_ok=True)
        res = parsed["result"] or {}
        fired = sorted(m.name for m in markers if m.exists())
        out = {
            "sandbox_settings": sandbox, "bash_denied_by_rule": not sandbox, "exit_code": code, "timed_out": timed_out,
            "cost_usd": res.get("total_cost_usd"), "stderr_head": err.strip()[:500],
            "result_head": str(res.get("result") or "")[:300],
            "started": bool(parsed["tool_uses"]),
            "hook_skill_loaded": f"<command-name>/{HOOK_SKILL}</command-name>" in "".join(parsed["user_texts"]),
            "hooks_fired": fired,
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
            "bash_read_experiment_and_skill_source": blocked(_step_result(parsed, "Bash", str(exp_probe))),
            "read_tool_experiment_dir": blocked(_step_result(parsed, "Read", str(exp_probe))),
            "grep_tool_skill_source": blocked(_step_result(parsed, "Grep", str(skill_dir))),
        }
        w = wd / "write_ok.txt"
        b = wd / "bash_ok.txt"
        steps["write_tool_workdir"] = "ok" if w.is_file() and "WRITE_OK" in w.read_text(errors="replace") else "failed"
        steps["bash_write_workdir"] = "ok" if b.is_file() else "failed"
        glob = _step_result(parsed, "Glob", str(wd))
        steps["glob_tool_workdir"] = "ok" if glob and "write_ok.txt" in glob else "failed"
        wsteps = write_probe_steps(parsed, write_markers)
        steps |= wsteps
        out["steps"] = steps
        out["write_isolation_ok"] = write_isolation(wsteps, sandbox)
        out["token_leaked"] = leak
        out["hooks_disabled"] = not fired and out["hook_skill_loaded"]
        file_tools = ("read_tool_home_file", "read_tool_experiment_dir", "grep_tool_skill_source")
        if sandbox:
            out["ok"] = (not leak and out["hooks_disabled"] and out["write_isolation_ok"] and all(steps[k] == "blocked" for k in (*file_tools, "bash_read_home_file", "bash_network", "bash_list_home",
                                                                                          "bash_read_experiment_and_skill_source"))
                         and all(steps[k] == "ok" for k in ("write_tool_workdir", "bash_write_workdir", "glob_tool_workdir")))
        else:
            out["ok"] = (not leak and out["hooks_disabled"] and out["write_isolation_ok"] and all(steps[k] == "blocked" for k in file_tools)
                         and steps["write_tool_workdir"] == "ok" and steps["bash_write_workdir"] == "failed")
        if not out["hook_skill_loaded"]:
            out["reason"] = "probe skill did not load; hook test inconclusive"
        elif not out["ok"]:
            out["reason"] = "expected isolation not observed (see steps, hooks_fired, token_leaked, write_isolation_ok)"
        return out
    finally:
        for f in (probe, exp_probe, skill_probe, *(m for _, m in write_markers.values())):
            f.unlink(missing_ok=True)
        shutil.rmtree(wd, ignore_errors=True)
        shutil.rmtree(sib, ignore_errors=True)


def selftest_environment(version: str | None) -> dict:
    canonical = Path("/slop-canonical/wd")
    shapes = [build_settings(canonical, sb, deny_bash=not sb, deny_dirs=["/slop-canonical/deny"]) for sb in (True, False)]
    try:
        base = str(workdir_base())
    except ExperimentError as e:
        base = f"unavailable: {e}"
    env = {
        "claude_version": version, "platform": platform.platform(), "tmpdir": os.environ.get("TMPDIR"), "workdir_base": base,
        "bwrap": bool(shutil.which("bwrap")), "socat": bool(shutil.which("socat")),
        "settings_sha256": sha256_bytes(json.dumps(shapes, sort_keys=True).encode()),
        "prompt_sha256": sha256_bytes(SELFTEST_PROMPT.encode()),
    }
    return env | {"key": sha256_bytes(json.dumps(env, sort_keys=True).encode())}


def selftest_cache_valid(cached, env: dict) -> bool:
    if not isinstance(cached, dict) or cached.get("schema") != SELFTEST_SCHEMA or cached.get("environment") != env:
        return False
    sb, perm = cached.get("sandbox"), cached.get("permissions_only")
    if not (isinstance(sb, dict) and isinstance(perm, dict)):
        return False
    flags = ("sandbox_ok", "file_isolation_ok", "unsandboxed_bash_denied", "hooks_disabled", "write_isolation_ok")
    if not all(isinstance(cached.get(k), bool) for k in flags):
        return False
    return (cached["sandbox_ok"] == (sb.get("ok") is True) and cached["file_isolation_ok"] == (perm.get("ok") is True)
            and cached["write_isolation_ok"] == (sb.get("write_isolation_ok") is True and perm.get("write_isolation_ok") is True))


def do_selftest(exp_dir: Path, model: str = "haiku", force: bool = False) -> dict:
    path = plan_paths(exp_dir)["selftest"]
    version = claude_version()
    env = selftest_environment(version)
    if path.is_file() and not force:
        try:
            cached = load_json(path)
        except (json.JSONDecodeError, UnicodeDecodeError):
            cached = None
        if selftest_cache_valid(cached, env):
            return cached
    skill_dir = resolve_path(exp_dir, load_plan(exp_dir)["skill"]["dir"])
    sb = selftest_probe(model, sandbox=True, exp_dir=exp_dir, skill_dir=skill_dir, keep=path.with_name("selftest_sandbox.jsonl"))
    perm = selftest_probe(model, sandbox=False, exp_dir=exp_dir, skill_dir=skill_dir, keep=path.with_name("selftest_permissions.jsonl"))
    data = {
        "schema": SELFTEST_SCHEMA, "ran_at": now_iso(), "claude_version": version, "model": model,
        "platform": env["platform"], "bwrap": env["bwrap"], "socat": env["socat"], "environment": env,
        "sandbox": sb, "permissions_only": perm,
        "sandbox_ok": sb.get("ok") is True,
        "file_isolation_ok": perm.get("ok") is True,
        "write_isolation_ok": sb.get("write_isolation_ok") is True and perm.get("write_isolation_ok") is True,
        "unsandboxed_bash_denied": perm.get("steps", {}).get("bash_write_workdir") == "failed" and perm.get("started", False),
        "hooks_disabled": bool(sb.get("hooks_disabled")) and bool(perm.get("hooks_disabled")),
        "cost_usd": round(sum(x.get("cost_usd") or 0 for x in (sb, perm)), 6),
    }
    save_json(path, data)
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
        m = load_json(path)
        if m.get("plan_sha256") != lock["plan_sha256"]:
            raise ExperimentError("trials.json belongs to a different plan hash")
        if not all(isinstance(t.get("trial_id"), str) and re.fullmatch(r"[0-9a-f]{8}", t["trial_id"]) for t in m["trials"]):
            raise ExperimentError("trials.json has malformed trial ids")
        return m["trials"]
    trials = build_trials(plan)
    if persist:
        save_json(path, {"plan_sha256": lock["plan_sha256"], "created_at": now_iso(), "seed": plan["seed"], "trials": trials})
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


def run_metrics(task: dict, workdir: Path, bwrap_ok: bool, bwrap_reason: str) -> list[dict]:
    out = []
    for m in task.get("metrics", []):
        rec = {"id": m["id"], "direction": m["direction"], "value": None, "exit_code": None, "reason": None}
        if not bwrap_ok:
            rec["reason"] = f"not run: {bwrap_reason}"
            out.append(rec)
            continue
        try:
            r = sandbox_run(m["command"], workdir, CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            rec["reason"] = f"timeout after {CHECK_TIMEOUT_S}s"
            out.append(rec)
            continue
        except FileSizeLimitExceeded as e:
            rec["reason"] = str(e)
            out.append(rec)
            continue
        rec["exit_code"] = r.returncode
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode != 0:
            rec["reason"] = f"exit {r.returncode}: {r.stderr.strip()[-300:]}"
        elif not lines:
            rec["reason"] = "no output"
        else:
            try:
                v = float(lines[-1])
                if math.isfinite(v):
                    rec["value"] = v
                else:
                    rec["reason"] = f"non-finite value {lines[-1]!r}"
            except ValueError:
                rec["reason"] = f"last stdout line is not a number: {lines[-1][:100]!r}"
        out.append(rec)
    return out


def run_checks(task: dict, workdir: Path, bwrap_ok: bool, bwrap_reason: str) -> list[dict]:
    out = []
    for c in task.get("checks", []):
        if not bwrap_ok:
            out.append({"id": c["id"], "status": "not_run", "exit_code": None, "passed": None, "reason": bwrap_reason})
            continue
        try:
            r = sandbox_run(c["command"], workdir, CHECK_TIMEOUT_S)
            out.append({"id": c["id"], "status": "ran", "exit_code": r.returncode, "passed": r.returncode == 0,
                        "stdout_tail": r.stdout[-2000:], "stderr_tail": r.stderr[-2000:]})
        except subprocess.TimeoutExpired:
            out.append({"id": c["id"], "status": "timeout", "exit_code": None, "passed": False})
        except FileSizeLimitExceeded as e:
            out.append({"id": c["id"], "status": "file_size_limit", "exit_code": None, "passed": False, "reason": str(e)})
    return out


class LeakError(ExperimentError):
    pass


def leak_check(workdir: Path, skill_files: dict, workdir_files: dict) -> str | None:
    if (workdir / ".claude" / "skills").exists():
        return "workdir contains .claude/skills/"
    # Empty files hash identically everywhere, so they cannot indicate a copied skill.
    skill_hashes = {h for h in skill_files.values() if not h.startswith("symlink:") and h != EMPTY_SHA256}
    hits = [rel for rel, h in workdir_files.items() if h in skill_hashes]
    if hits:
        return f"workdir files identical to target-skill files: {hits[:5]}"
    return None


def execute_trial(exp_dir: Path, plan: dict, lock: dict, trial: dict, mode: dict, bwrap: tuple[bool, str]) -> dict:
    task = next(t for t in plan["tasks"] if t["id"] == trial["task_id"])
    cond = trial["condition"]
    tdir = plan_paths(exp_dir)["trials"] / trial["trial_id"]
    if tdir.exists():
        safe_rmtree(tdir, plan_paths(exp_dir)["trials"])
    tdir.mkdir(parents=True)
    wd = make_workdir("slop-trial-")
    skill_name = plan["skill"]["name"]
    skill_dir = resolve_path(exp_dir, plan["skill"]["dir"])

    def abort(error: str, message: str):
        save_json(tdir / "leak_error.json", {"trial_id": trial["trial_id"], "condition": cond, "error": error, "at": now_iso()})
        raise LeakError(f"trial {trial['trial_id']} ({cond}) aborted before launch: {message}")
    try:
        fx = resolve_path(exp_dir, task.get("fixture_dir"))
        if fx:
            copy_tree(fx, wd)
        if cond == "C":
            dst = wd / ".claude" / "skills" / skill_name
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(skill_dir, dst, skip_names=(".git",))
        before = tree_fingerprint(wd, skip_installed_skills)["files"]
        if cond == "C":
            leak = "not_applicable"
        elif problem := leak_check(wd, lock["skill_files"], before):
            abort(problem, problem)
        else:
            leak = "passed"
        config = claude_config_problems(wd, skill_name if cond == "C" else None)
        config += [str(a / n) for a in wd.parents for n in ("CLAUDE.md", "CLAUDE.local.md") if (a / n).exists()]
        if config:
            abort(f"Claude Code config in workdir: {config[:5]}", f"workdir would load Claude Code config {config[:5]}")
        prompt = compose_prompt(plan, task, cond)
        deny = isolation_deny_dirs(exp_dir, skill_dir, wd)
        settings = build_settings(wd, mode["sandbox"], deny_bash=mode["bash_denied"], deny_dirs=deny)
        mcp = mcp_for(plan, exp_dir, cond)
        argv = build_checked_argv(plan["backend"], settings, mcp)
        (tdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        save_json(tdir / "command.json", {"argv": argv, "cwd": str(wd)})
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
        tool_calls = Counter(tu.get("name", "?") for tu in parsed["tool_uses"])
        activated, evidence = skill_activation(parsed, skill_name)
        checks = run_checks(task, wd, *bwrap)
        metrics = run_metrics(task, wd, *bwrap)
        after = tree_fingerprint(wd, skip_installed_skills)["files"]
        changed, kinds = files_changed(before, after)
        workspace_skipped = copy_tree(wd, tdir / "workspace", exclude_skills=True, capped=True)
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
            "forced": plan["experiment_type"] == "capability" and cond == "C",
            "should_activate": task.get("should_activate"),
            "leak_check": leak,
            "checks": checks, "metrics": metrics, "files_changed": changed, "files_change_kind": kinds,
            "prompt_sha256": sha256_bytes(prompt.encode()),
            "workspace_not_copied": workspace_skipped,
            "isolation": {"mode": mode["name"], "sandbox": mode["sandbox"], "allow_unsandboxed": mode["allow_unsandboxed"],
                          "bash_effectively_denied": mode["bash_denied"], "hooks_disabled": True,
                          "mcp_unsandboxed": mcp is not None},
            "error": error,
        }
        save_json(tdir / "result.json", result)
        return result
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def decide_mode(plan: dict, exp_dir: Path, allow_unsandboxed: bool, dry_run: bool) -> dict:
    pol = sandbox_policy(plan, exp_dir)
    mode = {"required": pol["required"], "reasons": pol["reasons"], "allow_unsandboxed": allow_unsandboxed, "bash_denied": False}
    if not pol["required"]:
        mode.update(name="permissions-only", sandbox=False)
        if dry_run:
            return mode
        st = do_selftest(exp_dir, model="haiku")
        if not (st.get("file_isolation_ok") and st.get("hooks_disabled") and st["permissions_only"].get("write_isolation_ok")):
            raise ExperimentError("permission-rule isolation selftest failed (file/write isolation or disabled hooks); refusing to run")
        return mode
    if dry_run:
        st_path = plan_paths(exp_dir)["selftest"]
        ok = st_path.is_file() and load_json(st_path).get("sandbox_ok")
        mode.update(name="sandbox" if ok else "sandbox (selftest pending/failed)", sandbox=True)
        return mode
    st = do_selftest(exp_dir, model="haiku")
    if st.get("sandbox_ok") and st["sandbox"].get("write_isolation_ok"):
        mode.update(name="sandbox", sandbox=True)
        return mode
    if not allow_unsandboxed:
        reason = (st.get("sandbox") or {}).get("reason", "unknown")
        raise ExperimentError(
            "sandbox required (" + "; ".join(pol["reasons"]) + ") but sandbox selftest failed: " + reason
            + ". Fix the sandbox, or rerun with --allow-unsandboxed (Bash is then denied by an explicit deny rule, "
            "file tools stay restricted to the workdir)."
        )
    if not (st.get("file_isolation_ok") and st["permissions_only"].get("write_isolation_ok")):
        raise ExperimentError("permission-rule isolation selftest also failed; refusing to run even with --allow-unsandboxed")
    mode.update(name="unsandboxed (Bash denied by rule)", sandbox=False, bash_denied=True)
    return mode


def reference_key(task_id: str, ref_id: str) -> str:
    return f"ref:{task_id}/{ref_id}"


def apply_reference(ref: dict, wd: Path, exp_dir: Path, bwrap: tuple[bool, str]) -> str | None:
    kind = ref["kind"]
    try:
        if kind == "copy":
            src = (wd / ref["from"]).resolve()
            if wd.resolve() not in src.parents:
                return "copy 'from' escapes the workdir"
            if not src.exists():
                return f"'from' {ref['from']!r} not found in the fixture copy"
            if src.is_dir():
                safe_overlay(src, wd, Path(ref["to"]))
            else:
                safe_copy_file(src, wd, Path(ref["to"]))
            return None
        if kind == "files":
            safe_overlay(resolve_path(exp_dir, ref["dir"]), wd)
            return None
    except UnsafeDestination as e:
        return f"refusing to write through a symlink in the fixture copy: {e}"
    if not bwrap[0]:
        return f"refusing to run reference command without the no-network sandbox: {bwrap[1]}"
    try:
        r = sandbox_run(ref["command"], wd, REFERENCE_COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"reference command timed out after {REFERENCE_COMMAND_TIMEOUT_S}s"
    except FileSizeLimitExceeded as e:
        return f"reference command stopped: {e}"
    if r.returncode != 0:
        return f"reference command exited {r.returncode}: {r.stderr.strip()[-300:]}"
    return None


def materialise_reference(exp_dir: Path, task: dict, ref: dict, bwrap: tuple[bool, str]) -> dict:
    rdir = exp_dir / "references" / task["id"] / ref["id"]
    if rdir.exists():
        safe_rmtree(rdir, exp_dir / "references")
    rdir.mkdir(parents=True)
    wd = make_workdir("slop-ref-")
    try:
        fx = resolve_path(exp_dir, task.get("fixture_dir"))
        if fx:
            copy_tree(fx, wd)
        before = tree_fingerprint(wd)["files"]
        error = apply_reference(ref, wd, exp_dir, bwrap)
        checks = metrics = []
        if error is None:
            checks = run_checks(task, wd, *bwrap)
            metrics = run_metrics(task, wd, *bwrap)
        changed, kinds = files_changed(before, tree_fingerprint(wd)["files"])
        copy_tree(wd, rdir / "workspace", exclude_skills=True, capped=True)
        result = {
            "trial_id": reference_key(task["id"], ref["id"]), "reference_id": ref["id"], "task_id": task["id"],
            "condition": f"REF:{ref['id']}", "kind": ref["kind"], "description": ref.get("description"),
            "created_at": now_iso(), "final_text": ref.get("final_text", ""),
            "final_text_note": None if "final_text" in ref else "reference defines no final_text; output.md is empty",
            "checks": checks, "metrics": metrics, "files_changed": changed, "files_change_kind": kinds,
            "model_called": False, "error": error,
        }
        save_json(rdir / "result.json", result)
        return result
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def load_references(exp_dir: Path) -> dict:
    out = {}
    root = exp_dir / "references"
    if root.is_dir():
        for rp in sorted(root.glob("*/*/result.json")):
            r = load_json(rp)
            out[r["trial_id"]] = r
    return out


def cmd_run(exp_dir: Path, dry_run: bool, only: list[str] | None, allow_unsandboxed: bool, allow_unsandboxed_mcp: bool = False) -> int:
    plan, lock = require_lock(exp_dir, check_inputs=True)
    if plan["backend"].get("mcp_config") and not allow_unsandboxed_mcp and not dry_run:
        raise ExperimentError("backend.mcp_config is set: MCP servers run outside the sandbox with the trial environment and network. "
                              "Rerun with --allow-unsandboxed-mcp only if you accept that.")
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
        print(f"# experiment_type: {plan['experiment_type']}")
        print(f"# prompt composition: {json.dumps(plan.get('prompt_composition'))}")
        print("# stdin is one stream-json user message wrapping the prompt below")
        for t in trials:
            wd = Path("/<workdir>")
            argv = build_checked_argv(plan["backend"], build_settings(wd, mode["sandbox"], deny_bash=mode["bash_denied"]),
                                      mcp_for(plan, exp_dir, t["condition"]))
            print(f"\n## trial {t['trial_id']} order={t['order']} task={t['task_id']} condition={t['condition']}")
            print("workdir contents: fixture" + (f" + .claude/skills/{plan['skill']['name']}/" if t["condition"] == "C" else ""))
            print("argv: " + json.dumps(argv))
            print("stdin prompt:\n" + compose_prompt(plan, tasks[t["task_id"]], t["condition"]))
        for task in plan["tasks"]:
            for ref in task.get("reference_outputs") or []:
                spec = {k: v for k, v in ref.items() if k not in ("id", "description", "final_text")}
                print(f"\n## reference {ref['id']} task={task['id']} (no model call): {json.dumps(spec)}")
        return 0
    bw = bwrap_status()
    if not bw[0]:
        print(f"warning: checks will not run: {bw[1]}", file=sys.stderr)
    run_log = exp_dir / "run_log.jsonl"
    with open(run_log, "a", encoding="utf-8") as lf:
        lf.write(json.dumps({"at": now_iso(), "mode": mode, "only": only, "bwrap": bw[0]}) + "\n")
    done = skipped = 0
    leaks = []
    ref_errors = []
    refs_done = 0
    if not only and plan["experiment_type"] == "capability":
        for task in plan["tasks"]:
            for ref in task.get("reference_outputs") or []:
                done_path = exp_dir / "references" / task["id"] / ref["id"] / "result.json"
                if done_path.is_file() and not load_json(done_path).get("error"):
                    continue
                r = materialise_reference(exp_dir, task, ref, bw)
                refs_done += 1
                if r["error"]:
                    ref_errors.append(r["trial_id"])
                    print(f"error: reference {r['trial_id']}: {r['error']}", file=sys.stderr)
    for t in trials:
        if (plan_paths(exp_dir)["trials"] / t["trial_id"] / "result.json").is_file():
            skipped += 1
            continue
        try:
            r = execute_trial(exp_dir, plan, lock, t, mode, bw)
        except LeakError as e:
            leaks.append(t["trial_id"])
            print(f"error: {e}", file=sys.stderr)
            continue
        done += 1
        print(f"[{t['order']}] {t['trial_id']} done: exit={r['exit_code']} cost={r['total_cost_usd']} error={r['error']}", file=sys.stderr)
    print(json.dumps({"ran": done, "skipped_existing": skipped, "leak_aborted": leaks, "references_materialised": refs_done,
                      "reference_errors": ref_errors, "mode": mode["name"]}))
    return 4 if leaks or ref_errors else 0


def cmd_blind(exp_dir: Path, force: bool) -> int:
    plan, lock = require_lock(exp_dir)
    if plan["experiment_type"] == "activation":
        print("activation experiment: nothing to blind (activation is measured from transcripts); run aggregate")
        return 0
    gdir = plan_paths(exp_dir)["grading"]
    if gdir.exists():
        if not force:
            raise ExperimentError(f"{gdir} already exists; use --force to rebuild (existing grades are discarded)")
        safe_rmtree(gdir, exp_dir)
    results = load_results(exp_dir)
    if not results:
        raise ExperimentError("no completed trials to blind")
    workspaces = {tid: plan_paths(exp_dir)["trials"] / tid / "workspace" for tid in results}
    references = {k: v for k, v in load_references(exp_dir).items() if not v.get("error")}
    for k, r in references.items():
        workspaces[k] = exp_dir / "references" / r["task_id"] / r["reference_id"] / "workspace"
    results = results | references
    skill_name = plan["skill"]["name"]
    tasks = {t["id"]: t for t in plan["tasks"]}
    trial_ids = list(results)
    random.SystemRandom().shuffle(trial_ids)
    used = set(results)
    key, redactions = {}, {}
    files_seen = {t["id"]: {} for t in plan["tasks"]}
    for trial_id in trial_ids:
        r = results[trial_id]
        item_id = secrets.token_hex(4)
        while item_id in used:
            item_id = secrets.token_hex(4)
        used.add(item_id)
        idir = gdir / "items" / item_id
        idir.mkdir(parents=True)
        out_text, n1 = redact(r.get("final_text") or "", skill_name)
        task = tasks[r["task_id"]]
        workspace = workspaces[trial_id]
        (idir / "output.md").write_text(out_text, encoding="utf-8")
        if task.get("grading_files"):
            files_text, matched = grading_files_digest(workspace, task["grading_files"])
            files_text, n2 = redact(files_text, skill_name)
            (idir / "files.md").write_text(files_text, encoding="utf-8")
            files_seen[task["id"]][item_id] = matched
        else:
            fx = resolve_path(exp_dir, task.get("fixture_dir"))
            diff_text, n2 = redact(unified_tree_diff(fx, workspace), skill_name)
            if len(diff_text) > DIFF_LIMIT_CHARS:
                diff_text = diff_text[:DIFF_LIMIT_CHARS] + f"\n[diff truncated at {DIFF_LIMIT_CHARS} characters]\n"
            (idir / "diff.patch").write_text(diff_text, encoding="utf-8")
        save_json(idir / "item.json", {"item_id": item_id, "task_id": r["task_id"]})
        key[item_id] = trial_id
        redactions[item_id] = n1 + n2
    save_json(gdir / "key.json", {
        "plan_sha256": lock["plan_sha256"], "created_at": now_iso(), "items": key,
        "reference_items": sorted(i for i, src in key.items() if src in references),
        "redaction": {"applied": True, "terms": redaction_terms(skill_name), "replacement": REDACTED,
                      "case_insensitive": True, "counts": redactions},
        "note": "Do not show this file to the judge.",
    })
    rubric_doc = {}
    for t in plan["tasks"]:
        entry = {"prompt": t["prompt"], "rubric": t["rubric"]}
        if t.get("grading_files"):
            entry["judge_sees"] = {"item_files": ["output.md", "files.md"], "grading_files": t["grading_files"],
                                   "max_chars_per_file": GRADING_FILE_LIMIT_CHARS, "files_seen_per_item": files_seen[t["id"]]}
        else:
            entry["judge_sees"] = {"item_files": ["output.md", "diff.patch"], "max_chars": DIFF_LIMIT_CHARS}
        rubric_doc[t["id"]] = entry
    save_json(gdir / "rubric.json", rubric_doc)
    for t in plan["tasks"]:
        (gdir / "inputs").mkdir(exist_ok=True)
        (gdir / "inputs" / f"{t['id']}.md").write_text(fixture_digest(resolve_path(exp_dir, t.get("fixture_dir"))), encoding="utf-8")
    print(json.dumps({"items": len(key), "redacted_items": sum(1 for v in redactions.values() if v), "dir": str(gdir)}))
    return 0


def cmd_grade(exp_dir: Path) -> int:
    plan, lock = require_lock(exp_dir)
    if plan["experiment_type"] == "activation":
        print("activation experiment: nothing to grade (no rubric); run aggregate")
        return 0
    gdir = plan_paths(exp_dir)["grading"]
    if not (gdir / "key.json").is_file():
        raise ExperimentError("run `blind` first")
    rubrics = load_json(gdir / "rubric.json")
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
        rec = load_json(gpath) if gpath.is_file() else None
        idir = gdir / "items" / item_id
        task_id = load_json(idir / "item.json")["task_id"]
        if rec is None:
            rec = {"item_id": item_id, "task_id": task_id, "mode": "llm-judge", "judge_model": plan["grading"]["judge_model"], "runs": []}
        rec["runs"] = [r for r in rec["runs"] if r.get("error") is None]
        rubric = rubrics[task_id]["rubric"]
        inputs_path = gdir / "inputs" / f"{task_id}.md"
        files_mode = (idir / "files.md").is_file()
        evidence = (idir / ("files.md" if files_mode else "diff.patch")).read_text(encoding="utf-8")
        output = (idir / "output.md").read_text(encoding="utf-8")
        inputs = inputs_path.read_text(encoding="utf-8") if inputs_path.is_file() else ""
        while len(rec["runs"]) < runs:
            r = run_judge(plan, rubric, judge_prompt(rubrics[task_id]["prompt"], rubric, output, evidence, inputs, files_mode))
            r["run"] = len(rec["runs"])
            total_cost += r.get("total_cost_usd") or 0
            if r["error"]:
                rec.setdefault("failed_runs", []).append(r)
                print(f"{item_id}: judge error: {r['error']}", file=sys.stderr)
                break
            rec["runs"].append(r)
        save_json(gpath, rec)
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
        known = set(items)
        with open(filled, newline="", encoding="utf-8") as f:
            for line, row in enumerate(csv.DictReader(f), 2):
                item_id, task_id = row.get("item_id"), row.get("task_id")
                if item_id not in known:
                    raise ExperimentError(f"{filled}:{line}: unknown item_id {item_id!r}")
                if task_id not in rubrics:
                    raise ExperimentError(f"{filled}:{line}: unknown task_id {task_id!r}")
                rubric = rubrics[task_id]["rubric"]
                scores, bad = {}, []
                for c in rubric:
                    v = (row.get(c["id"]) or "").strip()
                    if v.lstrip("-").isdigit() and int(v) in c["scale"]:
                        scores[c["id"]] = int(v)
                    elif c.get("optional") and v.lower() in ("n/a", "na", "null"):
                        scores[c["id"]] = None
                    else:
                        bad.append(c["id"])
                save_json(grades_dir / f"{item_id}.json", {
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
            task_id = load_json(gdir / "items" / item_id / "item.json")["task_id"]
            own = {c["id"] for c in rubrics[task_id]["rubric"]}
            w.writerow([item_id, task_id, *["" if c in own else "n/a" for c in criteria], ""])
    print("Manual grading:")
    print(f"  1. For each item in {gdir / 'items'}, read output.md and diff.patch (or files.md when the task sets grading_files).")
    print(f"  2. Score it against {gdir / 'rubric.json'} (task prompt + rubric), using {gdir / 'inputs'} for the task's initial files. Do not open key.json.")
    print(f"  3. Fill {template} and save it as {filled}. Optional criteria may be scored n/a.")
    print("  4. Run `grade` again to import the scores.")
    return 0


def load_results(exp_dir: Path) -> dict:
    out = {}
    tdir = plan_paths(exp_dir)["trials"]
    if tdir.is_dir():
        for p in sorted(tdir.iterdir()):
            rp = p / "result.json"
            if rp.is_file():
                out[p.name] = load_json(rp)
    return out


def common_header(plan, lock, results, manifest, selftest, grading=None, judge_models=(), judge_cost=0.0) -> dict:
    models, cost, starts, modes, allow, mcp = set(), 0.0, [], set(), set(), False
    for r in results.values():
        models.update((r.get("model_usage") or {}).keys())
        cost += r.get("total_cost_usd") or 0
        starts.append(r["started_at"])
        iso = r.get("isolation") or {}
        modes.add(iso.get("mode"))
        allow.add(iso.get("allow_unsandboxed"))
        mcp = mcp or bool(iso.get("mcp_unsandboxed"))
    return {
        "schema": RESULTS_SCHEMA, "experiment_type": plan["experiment_type"], "forcing_method": lock.get("forcing_method"),
        "generated_at": now_iso(),
        "plan_sha256": lock["plan_sha256"], "frozen_at": lock.get("frozen_at"), "env": lock.get("env"),
        "skill": {"name": plan["skill"]["name"], "fingerprint": lock.get("skill_fingerprint")},
        "backend": plan["backend"],
        **({"grading": grading} if grading is not None else {}),
        "models_reported": {"trials": sorted(models), "judge": sorted(judge_models)},
        "dates": {"first_trial_started": min(starts) if starts else None, "last_trial_started": max(starts) if starts else None},
        "trials": {"planned": len(manifest), "completed": len(results)},
        "cost_usd": {"trials": round(cost, 6), "judge": round(judge_cost, 6), "selftest": selftest.get("cost_usd") if selftest else None},
        "isolation": {"run_modes": sorted(m for m in modes if m), "allow_unsandboxed_used": True in allow, "unsandboxed_mcp_used": mcp,
                      "selftest": {k: selftest.get(k) for k in SELFTEST_SUMMARY_KEYS} if selftest else None},
    }


def isolation_limitation(header) -> str:
    st = header["isolation"]["selftest"]
    return (f"Isolation: run modes {header['isolation']['run_modes']}; --allow-unsandboxed used: {header['isolation']['allow_unsandboxed_used']}; "
            f"sandbox selftest ok: {st.get('sandbox_ok') if st else 'not run'}.")


def aggregate_activation(plan, lock, results, manifest, selftest) -> dict:
    out = common_header(plan, lock, results, manifest, selftest)
    per_task, counts = [], {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    errored = []
    for t in plan["tasks"]:
        rs = [r for r in results.values() if r["task_id"] == t["id"]]
        valid = [r for r in rs if not (r.get("error") or r.get("is_error"))]
        valid_ids = {r["trial_id"] for r in valid}
        errored += [r["trial_id"] for r in rs if r["trial_id"] not in valid_ids]
        k = sum(bool(r.get("skill_activated")) for r in valid)
        pos = bool(t["should_activate"])
        counts["tp" if pos else "fp"] += k
        counts["fn" if pos else "tn"] += len(valid) - k
        disc = [r.get("skill_discovered") for r in rs if r.get("skill_discovered") is not None]
        ar = rate(k, len(valid), "valid trials")
        per_task.append({
            "task_id": t["id"], "should_activate": pos,
            "unstable": ar["value"] is not None and ar["n"] >= UNSTABLE_MIN_N and abs(ar["value"] - 0.5) <= UNSTABLE_MARGIN,
            "n_planned": sum(1 for m in manifest if m["task_id"] == t["id"]), "n_completed": len(rs), "n_valid": len(valid),
            "activation_rate": ar,
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
    unstable = [t["task_id"] for t in per_task if t["unstable"]]
    if unstable:
        out["limitations"].append(f"Task(s) {unstable} activated in {0.5 - UNSTABLE_MARGIN:.0%}-{0.5 + UNSTABLE_MARGIN:.0%} of trials (flagged unstable): selection is close to a coin flip there.")
    return out


def load_grading(exp_dir: Path) -> tuple[dict, dict | None, dict]:
    gdir = plan_paths(exp_dir)["grading"]
    key, redaction, grades = {}, None, {}
    if (gdir / "key.json").is_file():
        kd = load_json(gdir / "key.json")
        key = {v: k for k, v in kd["items"].items()}
        redaction = kd.get("redaction")
    if (gdir / "grades").is_dir():
        for p in (gdir / "grades").glob("*.json"):
            grades[p.stem] = load_json(p)
    return key, redaction, grades


def check_success(task: dict, rs: list[dict]) -> tuple[dict, tuple[int, int] | None]:
    if not task.get("checks"):
        return {"value": None, "reason": "task defines no checks"}, None
    ran = [r for r in rs if r["checks"] and all(c.get("passed") is not None for c in r["checks"])]
    passed = sum(all(c["passed"] for c in r["checks"]) for r in ran)
    success = rate(passed, len(ran), "trials with checks run" if rs else "completed trials")
    if not ran and rs:
        success["reason"] = "checks not run"
    return success, (passed, len(ran))


def capability_cell(task: dict, cond: str, planned: list[dict], rs_all: list[dict], key: dict, grades: dict,
                    missing: list[dict], c_not_loaded: list[dict]) -> tuple[dict, dict]:
    rubric = task["rubric"]
    rs = [r for r in rs_all if not (cond == "C" and not r.get("skill_activated"))]
    compared = {r["trial_id"] for r in rs}
    crit_vals = {c["id"]: [] for c in rubric}
    na_counts = {c["id"]: 0 for c in rubric if c.get("optional")}
    tot, norm = [], []
    for r in rs_all:
        item = key.get(r["trial_id"])
        sc = item_scores(grades.get(item), rubric) if item else None
        if r["trial_id"] not in compared:
            c_not_loaded.append({"trial_id": r["trial_id"], "task_id": task["id"], "total_score": sc["total"] if sc else None,
                                 "reason": "skill not loaded despite forcing" + ("" if sc and sc["total"] is not None else
                                                                                 f"; score missing: {sc['reason'] if sc else 'not blinded'}"),
                                 "error": r.get("error")})
            continue
        if sc is None:
            missing.append({"trial_id": r["trial_id"], "reason": "not blinded (no grading item)"})
            continue
        for k, v in sc["per"].items():
            if v is not None:
                crit_vals[k].append(v)
        for k in sc["na"]:
            na_counts[k] += 1
        if sc["total"] is None:
            missing.append({"trial_id": r["trial_id"], "item_id": item, "reason": sc["reason"]})
        else:
            tot.append(sc["total"])
            if sc["normalized"] is not None:
                norm.append(sc["normalized"])
    success, passes = check_success(task, rs)
    costs = mean_or_null([r.get("total_cost_usd") for r in rs], "cost")
    tokens = {k: mean_or_null([(r.get("usage") or {}).get(k) for r in rs], k) for k in USAGE_KEYS}
    metrics = {m["id"]: (m["direction"], *metric_values(rs, m["id"])) for m in task.get("metrics") or []}
    cell = {
        "task_id": task["id"], "condition": cond, "n_planned": len(planned), "n_completed": len(rs_all),
        "n_in_comparison": len(rs), "n_excluded_not_loaded": len(rs_all) - len(rs),
        "n_scored": len(tot),
        "success_rate": success,
        "criteria": {k: describe(v) for k, v in crit_vals.items()},
        "criteria_na_counts": na_counts,
        "total_score": describe(tot),
        "normalized_score": describe(norm),
        "max_total_score": sum(max(c["scale"]) for c in rubric),
        "duration_s": mean_or_null([r.get("duration_s") for r in rs], "duration"),
        "tokens": tokens,
        "cost_usd": costs,
        "num_turns": mean_or_null([r.get("num_turns") for r in rs], "num_turns"),
        "skill_activation_rate": rate(sum(bool(r.get("skill_activated")) for r in rs_all), len(rs_all), "completed trials"),
        "error_count": sum(1 for r in rs if r.get("error") or r.get("is_error") or (r.get("exit_code") not in (0, None))),
        "metrics": {mid: describe(vals) | {"direction": d, "n_null": len(reasons), "null_reasons": reasons[:5]}
                    for mid, (d, vals, reasons) in metrics.items()},
    }
    arm = {"tot": tot, "norm": norm, "pass": passes, "cost": costs.get("value"),
           "tokens": {k: tokens[k].get("value") for k in USAGE_KEYS}, "metrics": {mid: vals for mid, (_, vals, _) in metrics.items()},
           "na_seen": any(na_counts.values())}
    return cell, arm


def pairwise_entry(task: dict, a: str, b: str, A: dict, B: dict, band: float, seed: int) -> dict:
    task_id = task["id"]
    x, y = A["tot"], B["tot"]
    entry = {"task_id": task_id, "comparison": f"{a}-{b}", "n_a": len(x), "n_b": len(y),
             "insufficient_n": len(x) < MIN_N or len(y) < MIN_N, "neutral_band": band}
    if not x or not y:
        entry.update(mean_diff=None, ci95=None, reason="an arm has no scored trials")
    else:
        entry["mean_diff"] = statistics.fmean(x) - statistics.fmean(y)
        entry["ci95"] = list(bootstrap_diff_ci(x, y, derived_seed(seed, task_id, a, b)))
        entry["ci_method"] = f"percentile bootstrap, {BOOTSTRAP_RESAMPLES} resamples, arms resampled independently"
    entry["normalized_mean_diff"] = (statistics.fmean(A["norm"]) - statistics.fmean(B["norm"])) if A["norm"] and B["norm"] else None
    entry["label"] = band_label(entry["mean_diff"], entry["ci95"], band, entry["insufficient_n"])
    dc = diff_or_none(A["cost"], B["cost"])
    dtok = {k: diff_or_none(A["tokens"][k], B["tokens"][k]) for k in USAGE_KEYS}
    dtok_total = sum(dtok.values()) if all(v is not None for v in dtok.values()) else None
    entry["cost_delta_usd"] = dc
    entry["token_delta"] = dtok | {"total_all_buckets": dtok_total}
    entry["pareto_cost"] = pareto_label(entry["mean_diff"], dc, band)
    entry["pareto_tokens"] = pareto_label(entry["mean_diff"], dtok_total, band)
    if A["pass"] is not None:
        (ka, na), (kb, nb) = A["pass"], B["pass"]
        if na and nb:
            entry["checks"] = {"pass_a": ka, "n_a": na, "pass_b": kb, "n_b": nb,
                               "rate_a": ka / na, "rate_b": kb / nb, "ci95_a": wilson(ka, na), "ci95_b": wilson(kb, nb),
                               "fisher_p_two_sided": fisher_exact(ka, na, kb, nb),
                               "min_attainable_p": min_attainable_p(na, nb)}
        else:
            entry["checks"] = {"reason": "checks not run in one or both arms"}
    entry["metrics"] = {}
    for m in task.get("metrics") or []:
        mx, my = A["metrics"][m["id"]], B["metrics"][m["id"]]
        me = {"direction": m["direction"], "interpretation": metric_direction_note(m["direction"], a, b), "n_a": len(mx), "n_b": len(my)}
        if mx and my:
            me["mean_diff"] = statistics.fmean(mx) - statistics.fmean(my)
            me["ci95"] = list(bootstrap_diff_ci(mx, my, derived_seed(seed, task_id, a, b, "metric", m["id"])))
        else:
            me.update(mean_diff=None, ci95=None, reason="an arm has no metric values")
        entry["metrics"][m["id"]] = me
    return entry


def reference_summary(exp_dir: Path, tasks: dict, cells: dict, key: dict, grades: dict) -> tuple[list, list, list]:
    references, flags, saturated = [], [], []
    ref_results = load_references(exp_dir)
    for task_id, task in tasks.items():
        for ref in task.get("reference_outputs") or []:
            rk = reference_key(task_id, ref["id"])
            r = ref_results.get(rk)
            row = {"task_id": task_id, "reference_id": ref["id"], "kind": ref["kind"], "description": ref.get("description"),
                   "total_score": None, "normalized_score": None, "criteria": {}, "checks_passed": None, "metrics": {}, "reason": None}
            if r is None:
                row["reason"] = "not materialised (run `run`)"
            elif r.get("error"):
                row["reason"] = f"materialisation failed: {r['error']}"
            else:
                if r.get("checks") and all(c.get("passed") is not None for c in r["checks"]):
                    row["checks_passed"] = all(c["passed"] for c in r["checks"])
                row["metrics"] = {m.get("id"): m.get("value") for m in r.get("metrics") or []}
                item = key.get(rk)
                if item is None:
                    row["reason"] = "not blinded (run `blind --force`)"
                else:
                    sc = item_scores(grades.get(item), task["rubric"])
                    row.update(total_score=sc["total"], normalized_score=sc["normalized"], criteria=sc["per"], reason=sc["reason"])
                    runs = scores_per_run(grades.get(item))
                    sat = [c["id"] for c in task["rubric"]
                           if runs and all(isinstance(x.get(c["id"]), int) and x[c["id"]] == max(c["scale"]) for x in runs)]
                    if sat:
                        saturated.append({"task_id": task_id, "reference_id": ref["id"], "criteria": sat, "gradings": len(runs)})
            references.append(row)
            if row["total_score"] is not None:
                means = {c: cells[f"{task_id}/{c}"]["total_score"]["mean"] for c in CONDITIONS}
                beats = [c for c in CONDITIONS if means[c] is not None and row["total_score"] >= means[c]]
                if beats:
                    flags.append({"task_id": task_id, "reference_id": ref["id"], "reference_total": row["total_score"], "beats_or_ties": beats})
    return references, flags, saturated


def capability_limitations(plan, header, results, manifest, cells, reference_flags, na_seen, missing, c_not_loaded) -> list[str]:
    n_values = sorted({c["n_scored"] for c in cells.values()})
    grading = plan["grading"]
    limitations = [
        f"Sample size: scored trials per task x condition = {n_values}; pairs with n<{MIN_N} per arm are flagged insufficient_n and their CIs are unreliable.",
        f"Single backend model configuration: {plan['backend']['model']} (reported model ids: {header['models_reported']['trials'] or 'none'}); results may not transfer to other models or agents.",
        (f"Scores come from an LLM judge ({grading['judge_model']}, {grading.get('judge_runs', 1)} run(s) per item); judge bias and variance are not calibrated against humans."
         if grading["mode"] == "llm-judge" else "Scores come from manual human grading."),
        "Blinding is by opaque ids and redaction of the skill name/paths only; outputs may still reveal the condition through skill-specific content or format.",
        isolation_limitation(header),
        "Built-in Claude Code skills and the default system prompt are present in all conditions equally.",
        "Bootstrap CIs resample trials only; they ignore task-to-task variation and judge noise.",
        "Trials are not paired across arms, so check pass rates are compared with an unpaired Fisher exact test; min_attainable_p shows the smallest p these arm sizes allow.",
        "Labels (better/worse/within_neutral_band/inconclusive, Pareto) are descriptive summaries of point estimates and CIs, not conclusions; Pareto labels use point estimates only.",
    ]
    for f in reference_flags:
        limitations.append(f"Task {f['task_id']}: the degenerate reference '{f['reference_id']}' scores >= condition(s) {f['beats_or_ties']}; the rubric does not separate those conditions from it.")
    if not any(t.get("reference_outputs") for t in plan["tasks"]):
        note = plan["analysis"].get("reference_note")
        limitations.append("No degenerate reference outputs were graded" + (f" (auditor's note: {note})." if note else "; the rubric's floor for trivial outputs is unknown."))
    if na_seen:
        limitations.append("Some optional criteria were scored N/A; raw totals then have different maxima across items, so compare normalized scores too.")
    if header["isolation"]["unsandboxed_mcp_used"]:
        limitations.append("MCP servers from backend.mcp_config ran outside the sandbox (--allow-unsandboxed-mcp).")
    if any((r.get("isolation") or {}).get("bash_effectively_denied") for r in results.values()):
        limitations.append("Bash was denied in these trials (no verified sandbox), so skills that rely on running scripts could not exercise them.")
    if missing:
        limitations.append(f"{len(missing)} trial(s) lack a total score; see missing[].")
    if c_not_loaded:
        limitations.append(f"{len(c_not_loaded)} C trial(s) did not load the skill despite forcing and are excluded from the comparison; see c_not_loaded[].")
    if len(results) < len(manifest):
        limitations.append(f"Only {len(results)} of {len(manifest)} planned trials completed.")
    return limitations


def aggregate_capability(exp_dir, plan, lock, results, manifest, selftest) -> dict:
    key, redaction, grades = load_grading(exp_dir)
    tasks = {t["id"]: t for t in plan["tasks"]}
    band = plan["analysis"]["neutral_band"]
    judge_models, judge_cost = set(), 0.0
    for g in grades.values():
        for r in g.get("runs", []) + g.get("failed_runs", []):
            judge_models.update((r.get("model_usage") or {}).keys())
            judge_cost += r.get("total_cost_usd") or 0
    cells, arms, missing, c_not_loaded = {}, {}, [], []
    for task_id, task in tasks.items():
        for cond in CONDITIONS:
            planned = [m for m in manifest if m["task_id"] == task_id and m["condition"] == cond]
            rs_all = [r for r in results.values() if r["task_id"] == task_id and r["condition"] == cond]
            cells[f"{task_id}/{cond}"], arms[(task_id, cond)] = capability_cell(task, cond, planned, rs_all, key, grades, missing, c_not_loaded)
    pairwise = [pairwise_entry(task, a, b, arms[(task_id, a)], arms[(task_id, b)], band, plan["seed"])
                for task_id, task in tasks.items() for a, b in PAIRS]
    references, reference_flags, saturated = reference_summary(exp_dir, tasks, cells, key, grades)
    out = common_header(plan, lock, results, manifest, selftest, plan["grading"], judge_models, judge_cost)
    na_seen = any(arm["na_seen"] for arm in arms.values())
    out |= {
        "redaction": redaction,
        "analysis": {"neutral_band": band, "pareto_definitions": PARETO_DEFINITIONS},
        "cells": cells, "pairwise": pairwise, "references": references, "reference_flags": reference_flags,
        "saturated_criteria": saturated,
        "missing": missing, "c_not_loaded": c_not_loaded,
        "limitations": capability_limitations(plan, out, results, manifest, cells, reference_flags, na_seen, missing, c_not_loaded),
    }
    return out


def cmd_aggregate(exp_dir: Path) -> int:
    plan, lock = require_lock(exp_dir)
    paths = plan_paths(exp_dir)
    results = load_results(exp_dir)
    manifest = load_json(paths["manifest"])["trials"] if paths["manifest"].is_file() else []
    selftest = load_json(paths["selftest"]) if paths["selftest"].is_file() else None
    summary = {"results": str(exp_dir / "results.json"), "markdown": str(exp_dir / "results.md")}
    if plan["experiment_type"] == "activation":
        out = aggregate_activation(plan, lock, results, manifest, selftest)
        md = render_activation_md(out)
    else:
        out = aggregate_capability(exp_dir, plan, lock, results, manifest, selftest)
        md = render_md(out, {t["id"]: t for t in plan["tasks"]})
        summary["missing"] = len(out["missing"])
    save_json(exp_dir / "results.json", out)
    (exp_dir / "results.md").write_text(md, encoding="utf-8")
    print(json.dumps(summary))
    return 0


def cmd_init(exp_dir: Path, skill_dir: Path, skill_name: str | None, exp_type: str = "capability",
             audit_dir: Path | None = None) -> int:
    skill_dir = skill_dir.resolve()
    root = experiment_root(exp_dir)
    if not inside(skill_dir, root):
        raise ExperimentError(f"--skill-dir must lie inside {root} (the audit dir when the experiment is <audit_dir>/experiments/<id>); "
                              "fetch or copy the skill there first")
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
    save_json(exp_dir / "plan.json", plan)
    print(f"wrote {exp_dir / 'plan.json'}; fill in tasks, rubric and conditions.B.minimal_instruction, then validate and freeze")
    return 0


def check_plan(exp_dir: Path) -> tuple[dict, list[str], list[str], list[str]]:
    plan = load_plan(exp_dir)
    errors, warnings = validate_plan(plan, exp_dir)
    infos = plan_infos(plan) if isinstance(plan, dict) else []
    for i in infos:
        print(f"info: {i}", file=sys.stderr)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    return plan, errors, warnings, infos


def cmd_validate(exp_dir: Path) -> int:
    _, errors, warnings, infos = check_plan(exp_dir)
    print(json.dumps({"ok": not errors, "errors": len(errors), "warnings": len(warnings), "infos": len(infos)}))
    return 1 if errors else 0


def cmd_freeze(exp_dir: Path) -> int:
    plan, errors, warnings, _ = check_plan(exp_dir)
    if errors:
        return 1
    paths = plan_paths(exp_dir)
    h = plan_hash(exp_dir)
    if paths["lock"].is_file():
        old = load_json(paths["lock"])
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
        "reference_fingerprints": reference_fingerprints(plan, exp_dir),
        "env": {"claude_version": claude_version(), "python": platform.python_version(), "platform": platform.platform(),
                "bwrap_present": bool(shutil.which("bwrap")), "socat_present": bool(shutil.which("socat"))},
        "warnings": warnings,
    }
    if plan["experiment_type"] == "capability":
        lock["forcing_method"] = FORCING_METHOD
    if plan["skill"].get("sha256") and plan["skill"]["sha256"] != skill_fp["sha256"]:
        print("warning: skill files changed since init; the lock records the current fingerprint", file=sys.stderr)
    save_json(paths["lock"], lock)
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
                        ("grade", "grade blinded items (LLM judge or manual CSV)"), ("aggregate", "compute results.json and results.md")):
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
    p.add_argument("--allow-unsandboxed-mcp", action="store_true", help="permit backend.mcp_config (its servers are not sandboxed)")
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
            print(json.dumps({k: st.get(k) for k in ("sandbox_ok", "file_isolation_ok", "write_isolation_ok", "unsandboxed_bash_denied", "hooks_disabled", "cost_usd")}
                             | {"sandbox_reason": st["sandbox"].get("reason"), "path": str(plan_paths(exp_dir)["selftest"])}))
            return 0 if st.get("sandbox_ok") else 3
        if a.cmd == "run":
            return cmd_run(exp_dir, a.dry_run, a.only, a.allow_unsandboxed, a.allow_unsandboxed_mcp)
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
