"""Run a fresh, independent Claude Code agent that audits a target with this skill installed.

Used by the live acceptance tests and to produce the samples:
    python3 tests/live/agent_audit.py <local-dir-or-github-url> --work <dir> [--model opus] [--note "..."]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHILD_ENV_DROP = (
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ATTENDED",
)
READ_ONLY_BASH = [f"Bash({c} *)" for c in ("python3 -I", "ls", "cat", "head", "tail", "wc", "grep", "file", "stat")]
SKILL_COPY_IGNORE = shutil.ignore_patterns("tests", "samples", "__pycache__", ".git", "slop-audits")


def agent_audit(target, work, model="sonnet", note="", copy_ignore=None, budget_usd=5, timeout=1800):
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(ROOT, work / ".claude" / "skills" / "slop-skill-auditor", ignore=SKILL_COPY_IGNORE)
    if "://" in str(target):
        target_arg = str(target)
    else:
        src = Path(target).resolve()
        shutil.copytree(src, work / "target" / src.name, ignore=copy_ignore)
        target_arg = f"./target/{src.name}"
    # The static audit needs no sandbox: only `python3 -I` and read-only commands are allowed and dontAsk
    # denies everything else, while denied attempts still show up in the transcript.
    settings = {"permissions": {"allow": READ_ONLY_BASH, "deny": ["WebFetch", "WebSearch"]}}
    prompt = f"/slop-skill-auditor {target_arg} のスキルを監査して。audit_dir は ./audit にして、report.md まで作って。対照実験はしないで。{note}"
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    cmd = [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
        "--model", model, "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(READ_ONLY_BASH + ["Read", "Write", "Edit", "Glob", "Grep", "Skill"]),
        "--tools", "Bash,Read,Write,Edit,Glob,Grep,Skill",
        "--settings", json.dumps(settings), "--max-budget-usd", str(budget_usd),
    ]
    proc = subprocess.run(cmd, input=prompt, cwd=work, env=env, capture_output=True, text=True, timeout=timeout)
    (work / "transcript.jsonl").write_text(proc.stdout)
    (work / "stderr.txt").write_text(proc.stderr)
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
