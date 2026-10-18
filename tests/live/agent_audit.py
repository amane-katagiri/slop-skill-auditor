"""Run a fresh, independent Claude Code agent that audits a target with this skill installed.

Used by the live acceptance tests and to produce the samples:
    python3 tests/live/agent_audit.py <local-dir-or-github-url> --work <dir> [--model opus] [--note "..."]
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "slop-skill-auditor"
SKILL_REL = ".claude/skills/slop-skill-auditor"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from exp_sandbox import child_env, make_workdir  # noqa: E402

READ_ONLY_COMMANDS = ("ls", "cat", "head", "tail", "wc", "grep", "file", "stat")
SKILL_COPY_IGNORE = shutil.ignore_patterns("__pycache__")


def agent_settings(wd: Path) -> dict:
    deny_dirs = sorted({"/mnt", *(str(p) for p in wd.parent.iterdir() if p.name.startswith("slop-") and p != wd)})
    return {
        "disableAllHooks": True,
        "permissions": {
            "allow": [f"Bash(python3 -I {SKILL_REL}/scripts/*)", f"Bash(python3 -I {wd}/{SKILL_REL}/scripts/*)"]
                     + [f"Bash({c} *)" for c in READ_ONLY_COMMANDS]
                     + [f"Read(/{wd}/**)", f"Write(/{wd}/**)", f"Edit(/{wd}/**)", "Skill"],
            "deny": ["Bash(*../*)", "Read(~/**)", "Edit(~/**)", "WebFetch", "WebSearch"]
                    + [f"{t}(/{d}/**)" for d in deny_dirs for t in ("Read", "Edit")],
        },
        "sandbox": {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": False,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": {"denyRead": ["~/", *deny_dirs], "allowWrite": [str(wd)]},
        },
    }


def agent_audit(target, work, model="sonnet", note="", copy_ignore=None, budget_usd=5, timeout=1800):
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    # The agent's sandbox denies reading $HOME, so it runs in a temp dir outside $HOME and the result is copied to `work`.
    wd = make_workdir("slop-agent-audit-")
    try:
        shutil.copytree(SKILL_ROOT, wd / SKILL_REL, ignore=SKILL_COPY_IGNORE)
        if "://" in str(target):
            fetch = subprocess.run([sys.executable, "-I", str(SKILL_ROOT / "scripts" / "fetch_target.py"), str(target), "--out", str(wd / "audit")],
                                   env=child_env(), capture_output=True, text=True, timeout=600)
            if fetch.returncode != 0:
                raise RuntimeError(f"fetch_target failed: {fetch.stderr.strip()[-500:]}")
            target_arg = f"{target}（取得済み: audit_dir ./audit に provenance.json と source/ がある。fetch_target.py は再実行しないで）"
        else:
            src = Path(target).resolve()
            shutil.copytree(src, wd / "target" / src.name, ignore=copy_ignore, symlinks=True)
            target_arg = f"./target/{src.name}"
        prompt = f"/slop-skill-auditor {target_arg} のスキルを監査して。audit_dir は ./audit にして、report.md まで作って。対照実験はしないで。{note}"
        cmd = [
            "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
            "--model", model, "--permission-mode", "dontAsk",
            "--tools", "Bash,Read,Write,Edit,Glob,Grep,Skill",
            "--settings", json.dumps(agent_settings(wd)), "--max-budget-usd", str(budget_usd),
        ]
        proc = subprocess.run(cmd, input=prompt, cwd=wd, env=child_env(), capture_output=True, text=True, timeout=timeout)
        (wd / "transcript.jsonl").write_text(proc.stdout)
        (wd / "stderr.txt").write_text(proc.stderr)
        shutil.copytree(wd, work, symlinks=True)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    events = [json.loads(l) for l in proc.stdout.splitlines() if l.startswith("{")]
    commands = []
    for ev in events:
        msg = ev.get("message")
        for item in (msg.get("content") if isinstance(msg, dict) else None) or []:
            if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") == "Bash":
                commands.append(item.get("input", {}).get("command", ""))
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    return work, commands, result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("target")
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--note", default="")
    p.add_argument("--budget-usd", type=float, default=5)
    args = p.parse_args()
    work, commands, result = agent_audit(args.target, args.work, args.model, args.note, budget_usd=args.budget_usd)
    print(json.dumps({
        "work": str(work),
        "subtype": result.get("subtype"),
        "cost_usd": result.get("total_cost_usd"),
        "turns": result.get("num_turns"),
        "models": list((result.get("modelUsage") or {}).keys()),
        "bash_commands": commands,
    }, ensure_ascii=False, indent=2))
    return 0 if (work / "audit" / "report.md").exists() else 1


if __name__ == "__main__":
    sys.exit(main())
