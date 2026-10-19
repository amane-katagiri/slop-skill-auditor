from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from exp_plan import DEFAULT_TOOLS, ExperimentError, experiment_root
from exp_tree import inside

ENV_ALLOW = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "LOGNAME",
    "CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
)
AUTH_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
FAKE_BACKEND = Path(__file__).resolve().parents[3] / "tests" / "fake_backend.py"
OUTPUT_READ_LIMIT = 1 << 20
COMMAND_FSIZE_LIMIT = 64 << 20
TOOLS_DIR = Path(__file__).resolve().parent
TOOLS_MOUNT_NAME = ".slop-tools"
REQUIRED_LAUNCH_FLAGS = ("--strict-mcp-config", "--no-session-persistence")
SYSTEM_RO_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")
TMPFS_DIRS = ("/tmp", "/run", "/var", "/mnt", "/home")
UNIX_SOCKET_DENY_READ = ("/run", "/var/run", "/tmp")


def oauth_credentials_present() -> bool:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return (config / ".credentials.json").is_file()


def child_env() -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    if not oauth_credentials_present():
        env |= {k: os.environ[k] for k in AUTH_ENV if k in os.environ}
    return env


def resolve_backend_argv(argv: list[str]) -> list[str]:
    spec = os.environ.get("SLOP_EXPERIMENT_BACKEND", "")
    if not spec:
        return argv
    if not spec.startswith("fake:") or Path(spec[len("fake:"):]).resolve() != FAKE_BACKEND:
        raise ExperimentError(f"SLOP_EXPERIMENT_BACKEND may only point to the test backend {FAKE_BACKEND}")
    return [sys.executable, "-I", "-S", str(FAKE_BACKEND)] + argv[1:]


def safe_rmtree(path: Path, parent: Path) -> None:
    if path.is_symlink() or not inside(path, parent):
        raise ExperimentError(f"refusing to delete {path}: not inside {parent}")
    shutil.rmtree(path)


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


class FileSizeLimitExceeded(Exception):
    pass


def read_capped(f, limit: int, tail: bool = False) -> str:
    size = f.seek(0, os.SEEK_END)
    f.seek(max(0, size - limit) if tail else 0)
    return f.read(limit).decode("utf-8", "replace")


def run_process(argv, stdin_text, cwd, timeout, stdout_path=None, env=None, out_limit=OUTPUT_READ_LIMIT, fsize_limit=None):
    # Output goes to files, not pipes: a descendant that escaped the process group could hold a pipe open forever.
    with (open(stdout_path, "w+b") if stdout_path else tempfile.TemporaryFile()) as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=out_f, stderr=err_f,
            env=env if env is not None else child_env(), start_new_session=True,
            preexec_fn=(lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))) if fsize_limit else None,
        )
        lock = threading.Lock()
        done = timed_out = False

        def kill():
            nonlocal timed_out
            with lock:
                if done:
                    return
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # A timer instead of communicate(timeout=...): Popen.wait(timeout) polls with sleeps of up to 50 ms per call.
        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            proc.communicate(stdin_text.encode("utf-8"))
        finally:
            with lock:
                done = True
            timer.cancel()
        if fsize_limit and not timed_out and (proc.returncode in (-signal.SIGXFSZ, 128 + signal.SIGXFSZ)
                                              or max(out_f.seek(0, os.SEEK_END), err_f.seek(0, os.SEEK_END)) >= fsize_limit):
            raise FileSizeLimitExceeded(f"stdout/stderr or a written file reached the {fsize_limit >> 20} MB file size limit")
        out = None if stdout_path else read_capped(out_f, out_limit)
        return proc.returncode, out, read_capped(err_f, out_limit, tail=True), timed_out


def claude_version() -> str | None:
    argv = resolve_backend_argv(["claude", "--version"])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=child_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def tools_mount() -> str:
    return str(Path.home() / TOOLS_MOUNT_NAME)


def python_prefixes() -> list[str]:
    home = Path.home().resolve()
    system = [Path(d) for d in SYSTEM_RO_DIRS]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        exe = Path(d) / "python3"
        if not os.path.isabs(d) or not os.access(exe, os.X_OK):
            continue
        prefixes = {p.parent.parent for p in (exe.absolute(), exe.resolve())}
        if any(p == home or home in p.parents or len(p.parts) < 3 and not any(p == s or s in p.parents for s in system) for p in prefixes):
            continue
        prefixes = sorted((p for p in prefixes if not any(p == s or s in p.parents for s in system)), key=lambda p: len(p.parts))
        return [str(p) for i, p in enumerate(prefixes) if not any(q in p.parents for q in prefixes[:i])]
    return []


def bwrap_argv(workdir: Path) -> list[str]:
    wd = str(workdir)
    argv = ["bwrap", "--unshare-all", "--new-session", "--die-with-parent", "--cap-drop", "ALL"]
    for d in SYSTEM_RO_DIRS:
        if os.path.islink(d):
            argv += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    for d in TMPFS_DIRS:
        argv += ["--tmpfs", d]
    for prefix in python_prefixes():
        argv += ["--ro-bind", prefix, prefix]
    argv += ["--tmpfs", str(Path.home()), "--ro-bind", str(TOOLS_DIR), tools_mount(),
             "--bind", wd, wd, "--chdir", wd, "--dev", "/dev", "--proc", "/proc"]
    return argv


def sandbox_env() -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "C.UTF-8"), "SLOP_TOOLS": tools_mount()}


def sandbox_run(command: str, workdir: Path, timeout: int) -> subprocess.CompletedProcess:
    argv = bwrap_argv(workdir) + ["sh", "-c", command]
    code, out, err, timed_out = run_process(argv, "", workdir, timeout, env=sandbox_env(), fsize_limit=COMMAND_FSIZE_LIMIT)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    return subprocess.CompletedProcess(argv, code, out, err)


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


def isolation_deny_dirs(exp_dir: Path, skill_dir: Path, workdir: Path) -> list[str]:
    dirs = {experiment_root(exp_dir), exp_dir.resolve(), skill_dir.resolve(), Path("/mnt")}
    dirs |= {p for p in workdir.parent.iterdir() if p.name.startswith("slop-") and p != workdir}
    for d in dirs:
        if d == workdir or d in workdir.parents:
            raise ExperimentError(f"trial workdir {workdir} lies inside {d}, which trials must not read; set TMPDIR elsewhere")
    return sorted(str(d) for d in dirs)


def sandbox_tmpdir() -> Path:
    return Path(os.environ.get("TMPDIR") or "/tmp") / f"claude-{os.getuid()}"


def build_settings(workdir: Path, sandbox: bool, deny_bash: bool = False, deny_dirs=(), unix_sockets: bool = False,
                   allow_bash: bool = False) -> dict:
    wd = str(workdir)
    # Claude Code permission paths need a leading '//' to be absolute; '/x' is relative to the settings root.
    settings = {
        "disableAllHooks": True,
        "permissions": {
            "allow": [f"Read(/{wd}/**)", f"Edit(/{wd}/**)", f"Write(/{wd}/**)", "Skill"],
            "deny": ["Read(~/**)", "Edit(~/**)", "WebFetch", "WebSearch"]
                    + [f"{tool}(/{d}/**)" for d in deny_dirs for tool in ("Read", "Edit", "Write")],
        },
    }
    if deny_bash:
        # dontAsk alone still auto-approves "safe" Bash commands (verified: echo > workdir file ran); deny wins over that.
        settings["permissions"]["deny"].append("Bash")
    if sandbox:
        settings["sandbox"] = {
            "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            # The sandbox's default writable set includes Claude's temp dir, where experiments often live.
            "filesystem": {"denyRead": ["~/", *deny_dirs], "allowWrite": [wd], "denyWrite": list(deny_dirs)},
        }
        if allow_bash:
            # autoAllowBashIfSandboxed still leaves compound commands to a prompt, which dontAsk turns into a denial.
            settings["permissions"]["allow"].append("Bash")
        if unix_sockets:
            # seccomp cannot filter sockets by path, so hide every directory where host sockets live instead.
            tmp = sandbox_tmpdir()
            reopened = [d for d in deny_dirs if inside(Path(d), tmp)]
            if reopened:
                raise ExperimentError(f"unix_sockets would re-open {reopened} through {tmp}; move them out of Claude's temp dir")
            settings["sandbox"]["network"]["allowAllUnixSockets"] = True
            settings["sandbox"]["filesystem"]["denyRead"] += list(UNIX_SOCKET_DENY_READ)
            settings["sandbox"]["filesystem"]["allowRead"] = [wd, str(tmp)]
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


def assert_launch_safe(argv: list[str], allowed_tools) -> None:
    def value(flag):
        return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None
    problems = []
    if value("--setting-sources") != "project":
        problems.append("--setting-sources project missing")
    problems += [f"{f} missing" for f in REQUIRED_LAUNCH_FLAGS if f not in argv]
    try:
        hooks_off = json.loads(value("--settings") or "{}").get("disableAllHooks") is True
    except (json.JSONDecodeError, AttributeError):
        hooks_off = False
    if not hooks_off:
        problems.append("--settings lacks disableAllHooks: true")
    if value("--permission-mode") != "dontAsk":
        problems.append("--permission-mode is not dontAsk")
    tools = value("--tools")
    if tools is None:
        problems.append("--tools missing")
    else:
        extra = {t for t in tools.split(",") if t} - set(allowed_tools)
        if extra:
            problems.append(f"--tools has tools outside the plan: {sorted(extra)}")
    if problems:
        raise ExperimentError("refusing to launch backend: " + "; ".join(problems))


def build_checked_argv(backend: dict, settings: dict, mcp_config: Path | None = None, allowed_tools=None, **kw) -> list[str]:
    argv = build_backend_command(backend, settings, mcp_config, **kw)
    assert_launch_safe(argv, backend.get("tools", DEFAULT_TOOLS) if allowed_tools is None else allowed_tools)
    return argv


def build_judge_command(judge_model: str, schema: dict, budget: float) -> list[str]:
    return [
        "claude", "-p", "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--tools", "",
        "--model", judge_model,
        "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
        "--permission-mode", "dontAsk", "--max-budget-usd", str(budget),
        "--settings", json.dumps({"disableAllHooks": True}),
    ]


def stdin_payload(prompt: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
