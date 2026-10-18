import json
import os
import re
import sys
from pathlib import Path


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")


def result_line(text, cost=0.01):
    return {
        "type": "result", "subtype": "success", "is_error": False, "result": text,
        "total_cost_usd": cost, "duration_ms": 10, "duration_api_ms": 8, "num_turns": 2,
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "modelUsage": {"fake-model-1": {"inputTokens": 100, "outputTokens": 50}},
    }


def judge(args, prompt):
    schema = json.loads(args[args.index("--json-schema") + 1])
    answer = re.split(r"<final_answer_[0-9a-f]+>", prompt, maxsplit=1)[-1]
    special = "SPECIAL" in answer
    scores = {}
    for cid, spec in schema["properties"]["scores"]["properties"].items():
        if "anyOf" in spec:
            if os.environ.get("FAKE_JUDGE_NULL_OPTIONAL"):
                scores[cid] = None
                continue
            spec = spec["anyOf"][0]
        scores[cid] = max(spec["enum"]) if special else min(spec["enum"])
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": "",
        "structured_output": {"scores": scores, "rationale": "fake"},
        "total_cost_usd": 0.001, "modelUsage": {"fake-judge-1": {}},
    }))


def selftest(cwd, prompt, sandbox):
    emit({"type": "system", "subtype": "init", "skills": []})
    if os.environ.get("FAKE_FIRE_HOOKS"):
        for entries in json.loads((cwd / ".claude" / "settings.json").read_text())["hooks"].values():
            Path(entries[0]["hooks"][0]["command"].split()[-1]).touch()
    m = re.search(r"Write tool: create (\S+) with content WRITE_OK", prompt)
    uses = [
        ("Bash", {"command": re.search(r"Bash tool: (cat \S+)", prompt).group(1)}),
        ("Read", {"file_path": re.search(r"Read tool: (\S+)", prompt).group(1)}),
        ("Bash", {"command": "curl -s https://example.com"}),
        ("Write", {"file_path": m.group(1), "content": "WRITE_OK"}),
        ("Bash", {"command": "echo BASH_OK > bash_ok.txt"}),
        ("Bash", {"command": "ls -a ~"}),
        ("Bash", {"command": re.search(r"Bash tool: (cat \S+ \S+)", prompt).group(1)}),
        ("Read", {"file_path": re.search(r"8\. Read tool: (\S+)", prompt).group(1)}),
        ("Grep", {"pattern": "SLOPPROBE", "path": re.search(r"in path (\S+)", prompt).group(1)}),
        ("Glob", {"pattern": "*.txt", "path": str(cwd)}),
    ]
    for i, (name, inp) in enumerate(uses):
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})
        if name == "Write":
            Path(m.group(1)).write_text("WRITE_OK")
            out = "File created"
        elif name == "Bash" and inp["command"].startswith("echo BASH_OK") and sandbox:
            (cwd / "bash_ok.txt").write_text("BASH_OK")
            out = ""
        elif name == "Glob":
            out = "\n".join(str(p) for p in cwd.glob("*.txt"))
        else:
            out = "Permission denied"
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}})
    emit(result_line("DONE", cost=0.002))


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print("0.0.0-fake (Claude Code)")
        return 0
    raw = sys.stdin.read()
    if "--json-schema" in args:
        judge(args, raw)
        return 0
    prompt = json.loads(raw.splitlines()[0])["message"]["content"]
    settings = json.loads(args[args.index("--settings") + 1])
    if os.environ.get("FAKE_SANDBOX_FAIL") and "sandbox" in settings:
        sys.stderr.write("Error: sandbox required but unavailable: socat not installed\n")
        return 1
    cwd = Path.cwd()
    if "isolation self-test" in prompt:
        selftest(cwd, prompt, "sandbox" in settings)
        return 0
    skills_dir = cwd / ".claude" / "skills"
    skills = sorted(p.name for p in skills_dir.iterdir()) if skills_dir.is_dir() else []
    emit({"type": "system", "subtype": "init", "skills": skills})
    emit({"type": "user", "message": {"role": "user", "content": prompt}, "isReplay": True})
    (cwd / "seen_skills.txt").write_text("\n".join(skills) + "\n")
    leaked = [k for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID") if k in os.environ]
    if leaked:
        (cwd / "env_leak.txt").write_text(",".join(leaked))
    (cwd / "out.txt").write_text(prompt + "\n")
    loaded = False
    for name in skills:
        if prompt.startswith(f"/{name} ") and not os.environ.get("FAKE_NO_LOAD"):
            emit({"type": "user", "message": {"role": "user", "content": f"<command-name>/{name}</command-name>"}, "isReplay": True})
            loaded = True
        elif "zebra" in prompt.lower():
            emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": name}}]}})
            emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "s1", "content": "Launching skill"}]}})
            loaded = True
    text = f"SPECIAL answer mentioning {skills[0]} and SKILL.md" if loaded else "plain answer"
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    emit(result_line(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
