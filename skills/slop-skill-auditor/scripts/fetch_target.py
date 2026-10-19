#!/usr/bin/env python3
import argparse
import datetime
import http.client
import ipaddress
import os
import posixpath
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import EXTERNAL_MAX_FILES, PROVENANCE_SCHEMA, SKIP_DIRS, is_repo_doc, skill_externals, write_json  # noqa: E402

EXIT_ERROR = 2
EXIT_AMBIGUOUS = 3
LOCAL_REPO_MAX_BYTES = 50 * 1024 * 1024
DOC_MAX_BYTES = 10 * 1024 * 1024
DOC_TIMEOUT = 30
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=user",
    "-c", "submodule.recurse=false",
    "-c", "core.fsmonitor=false",
]
CLONE_ONLY = ["-c", "core.symlinks=false"]
GIT_HEAD_MAX_BYTES = 4096
GIT_PACKED_MAX_BYTES = 8 * 1024 * 1024
DOC_DEADLINE = 120
DOC_MAX_REDIRECTS = 5
GIT_NETWORK_TIMEOUT = 300


class FetchError(Exception):
    pass


def git_env():
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    return env


def run_git(args, cwd=None, check=True, extra=(), deadline=None):
    argv = ["git"] + GIT_HARDENING + list(extra) + args
    timeout = None if deadline is None else deadline - time.monotonic()
    if timeout is not None and timeout <= 0:
        raise FetchError("git network operations exceeded %d s" % GIT_NETWORK_TIMEOUT)
    # A new session so a timeout also kills git's transport helpers (git-remote-https etc.).
    proc = subprocess.Popen(argv, cwd=cwd, env=git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise FetchError("git %s exceeded the %d s limit for git network operations" % (" ".join(args[:2]), GIT_NETWORK_TIMEOUT))
    result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check and result.returncode != 0:
        raise FetchError("git %s failed: %s" % (" ".join(args[:2]), err.strip()))
    return result


def parse_github_url(url):
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise FetchError("GitHub URL has no owner/repo: %s" % url)
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    for name in (owner, repo):
        if not re.match(r"^[A-Za-z0-9_.-]+$", name) or name in (".", ".."):
            raise FetchError("invalid GitHub owner/repo in %s" % url)
    info = {"owner": owner, "repo": repo, "repo_url": "https://github.com/%s/%s.git" % (owner, repo), "rest": None}
    if len(parts) == 2:
        info["kind"] = "github_repo"
        return info
    if parts[2] in ("tree", "blob") and len(parts) >= 4:
        info["kind"] = "github_tree" if parts[2] == "tree" else "github_blob"
        info["rest"] = "/".join(parts[3:])
        return info
    raise FetchError("unsupported GitHub URL form: %s" % url)


def parse_ls_remote(output):
    refs = set()
    for line in output.splitlines():
        if "\t" not in line:
            continue
        name = line.split("\t", 1)[1].strip()
        if name.endswith("^{}"):
            name = name[:-3]
        for prefix in ("refs/heads/", "refs/tags/"):
            if name.startswith(prefix):
                refs.add(name[len(prefix):])
    return sorted(refs)


def split_ref_path(rest, refs):
    rest = rest.strip("/")
    first = rest.split("/", 1)[0]
    if SHA_RE.match(first):
        return first, rest[len(first):].strip("/"), True
    best = None
    for ref in refs:
        if rest == ref or rest.startswith(ref + "/"):
            if best is None or len(ref) > len(best):
                best = ref
    if best is None:
        raise FetchError("could not find a branch or tag matching the start of %r" % rest)
    return best, rest[len(best):].strip("/"), False


def find_skill_dirs(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if "SKILL.md" in filenames:
            rel = os.path.relpath(dirpath, root)
            found.append("." if rel == "." else rel.replace(os.sep, "/"))
    return sorted(found)


def candidates_under(candidates, sub):
    if sub == ".":
        return candidates
    return sorted("." if c == sub else c[len(sub) + 1:] for c in candidates if c == sub or c.startswith(sub + "/"))


def choose_skill(base, skill_opt, candidates):
    if skill_opt:
        sub = os.path.normpath(skill_opt).replace(os.sep, "/")
        if sub.startswith("../") or sub == ".." or os.path.isabs(sub):
            raise FetchError("--skill must be a relative path inside the target")
        cur = base
        for part in sub.split("/"):
            cur = os.path.join(cur, part)
            if os.path.islink(cur):
                raise FetchError("--skill path goes through a symlink: %s" % skill_opt)
        if not os.path.isfile(os.path.join(base, sub, "SKILL.md")):
            raise FetchError("--skill %s has no SKILL.md (candidates: %s)" % (skill_opt, ", ".join(candidates) or "none"))
        return sub, candidates, False
    if os.path.isfile(os.path.join(base, "SKILL.md")):
        return ".", candidates, False
    if len(candidates) == 1:
        return candidates[0], candidates, False
    if not candidates:
        raise FetchError("no SKILL.md found under %s" % base)
    return None, candidates, True


def find_git_root(path):
    cur = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def rel_join(*parts):
    return posixpath.normpath(posixpath.join(*parts))


def copy_file(src, dst, budget):
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as fin:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fout:
            while True:
                chunk = fin.read(1024 * 1024)
                if not chunk:
                    break
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise FetchError("local copy exceeds %d bytes" % LOCAL_REPO_MAX_BYTES)
                fout.write(chunk)
    return True


def copy_entry(full, dst, rel, skipped, budget):
    if os.path.islink(full):
        skipped.append({"path": rel, "reason": "symlink (not followed)"})
    elif not copy_file(full, dst, budget):
        skipped.append({"path": rel, "reason": "not a regular file"})


def copy_tree(src, dst, prefix, skipped, budget):
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src).replace(os.sep, "/")
        keep = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS:
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                skipped.append({"path": rel_join(prefix, rel_dir, d), "reason": "symlink (not followed)"})
                continue
            keep.append(d)
        dirnames[:] = keep
        os.makedirs(os.path.join(dst, rel_dir), exist_ok=True)
        for f in sorted(filenames):
            copy_entry(os.path.join(dirpath, f), os.path.join(dst, rel_dir, f), rel_join(prefix, rel_dir, f), skipped, budget)


def copy_repo_docs(root, dst, skill_rel, skipped, budget):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        keep = []
        for d in sorted(dirnames) if depth < 2 else []:
            rel = rel_join(rel_dir, d)
            if d in SKIP_DIRS or rel == skill_rel:
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                skipped.append({"path": rel, "reason": "symlink (not followed)"})
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            if is_repo_doc(rel_dir, f):
                rel = rel_join(rel_dir, f)
                copy_entry(os.path.join(dirpath, f), os.path.join(dst, rel), rel, skipped, budget)


def record_externals(prov, root, skill_sub, skill_dir):
    closure, plugin = skill_externals(root, skill_sub, skill_dir, EXTERNAL_MAX_FILES)
    prov["plugin_root"] = plugin["root"] if plugin else None
    prov["external_files"] = sorted(({"path": p, "reason": r} for p, r in closure["external"].items()), key=lambda e: e["path"])
    if closure["truncated"]:
        prov["notes"].append("more than %d files outside the skill directory are referenced; the rest were left out" % EXTERNAL_MAX_FILES)
    return [e["path"] for e in prov["external_files"]]


def read_small(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = f.read(limit + 1)
    return None if len(data) > limit else data.decode("utf-8", "replace")


def resolve_ref(dirs, ref):
    for _ in range(5):
        if not ref.startswith("refs/") or ".." in ref.split("/"):
            return None
        val = None
        for d in dirs:
            val = read_small(os.path.join(d, *ref.split("/")), GIT_HEAD_MAX_BYTES)
            if val is not None:
                break
        if val is None:
            for d in dirs:
                packed = read_small(os.path.join(d, "packed-refs"), GIT_PACKED_MAX_BYTES)
                for line in (packed or "").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref and SHA_RE.match(parts[0]):
                        return parts[0]
            return None
        val = val.strip()
        if not val.startswith("ref:"):
            return val if SHA_RE.match(val) else None
        ref = val[4:].strip()
    return None


def local_git_info(root, notes):
    gitdir = os.path.join(root, ".git")
    if os.path.isfile(gitdir):
        text = read_small(gitdir, GIT_HEAD_MAX_BYTES) or ""
        if not text.startswith("gitdir:"):
            gitdir = None
        else:
            gitdir = os.path.join(root, text[7:].strip())
    commit = None
    if gitdir:
        dirs = [gitdir]
        common = read_small(os.path.join(gitdir, "commondir"), GIT_HEAD_MAX_BYTES)
        if common and common.strip():
            dirs.append(os.path.join(gitdir, common.strip()))
        head = (read_small(os.path.join(gitdir, "HEAD"), GIT_HEAD_MAX_BYTES) or "").strip()
        commit = resolve_ref(dirs, head[4:].strip()) if head.startswith("ref:") else (head if SHA_RE.match(head) else None)
    if commit is None:
        notes.append("commit could not be read from .git")
    notes.append("dirty not computed (git is not run on local inputs)")
    return commit


def ls_remote(repo_url, deadline=None):
    return run_git(["ls-remote", "--heads", "--tags", repo_url], deadline=deadline).stdout


def clone(repo_url, dest, ref, is_sha, deadline=None):
    base = ["clone", "--depth", "1", "--no-recurse-submodules", "--single-branch"]
    if ref is None:
        run_git(base + ["--", repo_url, dest], extra=CLONE_ONLY, deadline=deadline)
    elif not is_sha:
        run_git(base + ["--branch", ref, "--", repo_url, dest], extra=CLONE_ONLY, deadline=deadline)
    else:
        run_git(["init", "-q", dest], extra=CLONE_ONLY)
        run_git(["config", "core.symlinks", "false"], cwd=dest)
        shallow = run_git(["fetch", "-q", "--depth", "1", "--no-recurse-submodules", repo_url, ref], cwd=dest, check=False, deadline=deadline)
        if shallow.returncode == 0:
            run_git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=dest, deadline=deadline)
        else:
            run_git(["fetch", "-q", "--no-recurse-submodules", repo_url, "+refs/heads/*:refs/remotes/origin/*"], cwd=dest, deadline=deadline)
            run_git(["checkout", "-q", "--detach", ref], cwd=dest, deadline=deadline)
    return run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()


def tree_exceeds(root, limit):
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not (dirpath == root and d == ".git")]
        for name in filenames:
            total += os.lstat(os.path.join(dirpath, name)).st_size
            if total > limit:
                return True
    return False


def safe_doc_name(name, used):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "doc"
    base = base[:100]
    stem, ext = os.path.splitext(base)
    candidate, n = base, 2
    while candidate in used:
        candidate = "%s-%d%s" % (stem, n, ext)
        n += 1
    used.add(candidate)
    return candidate


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("refusing doc URL %s: only http(s) URLs with a host are allowed" % url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except (OSError, ValueError) as e:
        raise FetchError("cannot resolve doc URL host %s: %s" % (parts.hostname, e))
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise FetchError("refusing doc URL %s: host resolves to non-public address %s" % (url, ip))
        ips.append(str(ip))
    if not ips:
        raise FetchError("cannot resolve doc URL host %s" % parts.hostname)
    return parts, port, ips[0]


def pinned_get(url, deadline):
    for _ in range(DOC_MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            raise FetchError("doc %s took longer than %d s" % (url, DOC_DEADLINE))
        parts, port, ip = check_url(url)
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, port, timeout=DOC_TIMEOUT, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=DOC_TIMEOUT)
        # Connect to the address that was validated; the TLS layer still verifies the certificate against conn.host.
        conn._create_connection = lambda addr, timeout=None, source=None, ip=ip: socket.create_connection((ip, addr[1]), timeout, source)
        try:
            conn.request("GET", urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, "")),
                         headers={"User-Agent": "slop-skill-auditor"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                if not resp.getheader("Location"):
                    raise FetchError("doc %s: redirect without Location" % url)
                url = urllib.parse.urljoin(url, resp.getheader("Location"))
                continue
            if resp.status != 200:
                raise FetchError("doc %s: HTTP %d" % (url, resp.status))
            chunks, size = [], 0
            while chunk := resp.read1(64 * 1024):
                if time.monotonic() > deadline:
                    raise FetchError("doc %s took longer than %d s" % (url, DOC_DEADLINE))
                size += len(chunk)
                if size > DOC_MAX_BYTES:
                    raise FetchError("doc %s exceeds %d bytes" % (url, DOC_MAX_BYTES))
                chunks.append(chunk)
            return b"".join(chunks)
        except (OSError, http.client.HTTPException) as e:
            raise FetchError("cannot download doc %s: %s" % (url, e))
        finally:
            conn.close()
    raise FetchError("doc %s: more than %d redirects" % (url, DOC_MAX_REDIRECTS))


def fetch_doc(spec, docs_dir, used, offline):
    parsed = urllib.parse.urlsplit(spec)
    os.makedirs(docs_dir, exist_ok=True)
    if parsed.scheme in ("http", "https"):
        if offline:
            raise FetchError("--offline: cannot download doc %s" % spec)
        data = pinned_get(spec, time.monotonic() + DOC_DEADLINE)
        name = safe_doc_name(os.path.basename(parsed.path) or parsed.netloc, used)
        with open(os.path.join(docs_dir, name), "wb") as f:
            f.write(data)
        return {"input": spec, "saved_as": "docs/" + name}
    if parsed.scheme and len(parsed.scheme) > 1:
        raise FetchError("unsupported doc scheme: %s" % spec)
    if not os.path.isfile(spec):
        raise FetchError("doc not found: %s" % spec)
    if os.path.getsize(spec) > DOC_MAX_BYTES:
        raise FetchError("doc %s exceeds %d bytes" % (spec, DOC_MAX_BYTES))
    name = safe_doc_name(os.path.basename(spec), used)
    shutil.copyfile(spec, os.path.join(docs_dir, name))
    return {"input": spec, "saved_as": "docs/" + name}


def tool_versions():
    try:
        git = subprocess.run(["git", "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
    except OSError:
        git = None
    return {"git": git, "python": sys.version.split()[0]}


def ensure_empty_source(source, force):
    if os.path.lexists(source):
        if os.path.isdir(source) and not os.path.islink(source) and not os.listdir(source):
            os.rmdir(source)
        elif force:
            if os.path.isdir(source) and not os.path.islink(source):
                shutil.rmtree(source)
            else:
                os.remove(source)
        else:
            raise FetchError("%s is not empty (use --force to replace it)" % source)


def default_out(spec):
    hint = re.sub(r"[^A-Za-z0-9._-]+", "-", spec.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")).strip("-.") or "skill"
    if hint == "SKILL.md":
        hint = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.dirname(os.path.abspath(spec)))) or "skill"
    base = os.path.join(tempfile.gettempdir(), "slop-audits")
    os.makedirs(base, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return tempfile.mkdtemp(prefix="%s-%s-" % (hint[:60], stamp), dir=base)


def fetch(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    source = os.path.join(out, "source")
    ensure_empty_source(source, args.force)
    prov = {
        "schema": PROVENANCE_SCHEMA, "input": args.input, "kind": None, "repo_url": None,
        "ref_requested": args.ref, "commit": None, "dirty": None, "source_root": None,
        "skill_subpath": None, "skill_dir_name": None, "candidates": [], "candidates_relative_to": "source_root",
        "docs": [], "skipped": [], "notes": [], "external_files": [], "plugin_root": None,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "tools": tool_versions(),
    }
    gh = parse_github_url(args.input) if re.match(r"^https?://", args.input) else None
    if re.match(r"^[a-z][a-z0-9+.-]*://", args.input, re.I) and gh is None:
        raise FetchError("unsupported URL (only github.com repo/tree/blob URLs): %s" % args.input)

    ambiguous = False
    if gh:
        prov["kind"] = gh["kind"]
        prov["repo_url"] = gh["repo_url"]
        if args.offline:
            raise FetchError("--offline: %s requires network access" % gh["kind"])
        deadline = time.monotonic() + GIT_NETWORK_TIMEOUT
        ref, path, is_sha = args.ref, ".", bool(args.ref and SHA_RE.match(args.ref))
        if gh["rest"] is not None:
            url_ref, path, is_sha = split_ref_path(gh["rest"], [] if SHA_RE.match(gh["rest"].split("/", 1)[0]) else parse_ls_remote(ls_remote(gh["repo_url"], deadline)))
            if args.ref and args.ref != url_ref:
                raise FetchError("--ref %s conflicts with ref %s in the URL" % (args.ref, url_ref))
            ref = url_ref
            path = path or "."
            if gh["kind"] == "github_blob":
                path = posixpath.dirname(path) or "."
            path = posixpath.normpath(path)
            if path == ".." or path.startswith("../") or path.startswith("/"):
                raise FetchError("path in URL escapes the repository: %s" % path)
        prov["ref_requested"] = ref
        try:
            prov["commit"] = clone(gh["repo_url"], source, ref, is_sha, deadline)
            if tree_exceeds(source, LOCAL_REPO_MAX_BYTES):
                raise FetchError("checked-out tree exceeds %d MB (excluding .git); clone deleted" % (LOCAL_REPO_MAX_BYTES >> 20))
        except (FetchError, OSError):
            shutil.rmtree(source, ignore_errors=True)
            raise
        shutil.rmtree(os.path.join(source, ".git"), ignore_errors=True)
        base = os.path.join(source, path)
        if not os.path.isdir(base):
            shutil.rmtree(source)
            raise FetchError("path %s does not exist at %s" % (path, ref or "default branch"))
        everything = find_skill_dirs(source)
        cands = candidates_under(everything, path) if not SKIP_DIRS.intersection(path.split("/")) else find_skill_dirs(base)
        sub, cands, ambiguous = choose_skill(base, args.skill, cands)
        prov["source_root"] = "source"
        prov["candidates"] = everything
        if ambiguous:
            prov["candidates"] = cands
            prov["candidates_relative_to"] = "input"
            prov["source_root"] = None
            shutil.rmtree(source)
        else:
            prov["skill_subpath"] = rel_join(path, sub)
            prov["skill_dir_name"] = gh["repo"] if prov["skill_subpath"] == "." else os.path.basename(prov["skill_subpath"])
            record_externals(prov, source, prov["skill_subpath"], os.path.join(source, prov["skill_subpath"]))
    else:
        target = os.path.abspath(args.input)
        if os.path.isfile(target):
            if os.path.basename(target) != "SKILL.md":
                raise FetchError("file input must be a SKILL.md: %s" % args.input)
            prov["kind"] = "local_skill_md"
            skill_dir = os.path.dirname(target)
            target_cands = None
            if args.skill:
                raise FetchError("--skill cannot be combined with a SKILL.md path")
        elif os.path.isdir(target):
            prov["kind"] = "local_dir"
            target_cands = find_skill_dirs(target)
            sub, cands, ambiguous = choose_skill(target, args.skill, target_cands)
            if ambiguous:
                prov["candidates"] = cands
                prov["candidates_relative_to"] = "input"
            skill_dir = None if ambiguous else os.path.normpath(os.path.join(target, sub))
        else:
            raise FetchError("no such file or directory: %s" % args.input)
        if not ambiguous:
            git_root = find_git_root(skill_dir)
            copy_root = git_root or (target if prov["kind"] == "local_dir" else skill_dir)
            skill_rel = os.path.relpath(skill_dir, copy_root).replace(os.sep, "/")
            if git_root:
                prov["commit"] = local_git_info(git_root, prov["notes"])
            budget = [LOCAL_REPO_MAX_BYTES]
            try:
                copy_tree(skill_dir, os.path.join(source, skill_rel), skill_rel, prov["skipped"], budget)
                if skill_rel != ".":
                    copy_repo_docs(copy_root, source, skill_rel, prov["skipped"], budget)
                for rel in record_externals(prov, copy_root, skill_rel, os.path.join(source, skill_rel)):
                    if not os.path.lexists(os.path.join(source, rel)):
                        copy_file(os.path.join(copy_root, rel), os.path.join(source, rel), budget)
            except (FetchError, OSError):
                shutil.rmtree(source, ignore_errors=True)
                raise
            prov["source_root"] = "source"
            prov["skill_subpath"] = skill_rel
            prov["skill_dir_name"] = os.path.basename(skill_dir)
            prov["candidates"] = target_cands if copy_root == target else find_skill_dirs(copy_root)

    used = set()
    for spec in args.doc or []:
        prov["docs"].append(fetch_doc(spec, os.path.join(out, "docs"), used, args.offline))
    prov["skipped"].sort(key=lambda s: s["path"])
    write_json(os.path.join(out, "provenance.json"), prov)
    return prov, ambiguous


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch a target skill into an audit directory (never executes it).")
    ap.add_argument("input", help="local dir, SKILL.md path, or github.com repo/tree/blob URL")
    ap.add_argument("--out", help="audit directory (default: a new directory under $TMPDIR/slop-audits/)")
    ap.add_argument("--skill", help="skill subpath relative to the input when it holds several skills")
    ap.add_argument("--ref", help="branch, tag or 40-hex commit for a repo URL")
    ap.add_argument("--doc", action="append", help="extra README/doc (path or http(s) URL); repeatable")
    ap.add_argument("--offline", action="store_true", help="fail instead of using the network")
    ap.add_argument("--force", action="store_true", help="replace an existing non-empty source/")
    args = ap.parse_args(argv)
    if not args.out:
        args.out = default_out(args.input)
    print("audit_dir: %s" % os.path.abspath(args.out))
    try:
        prov, ambiguous = fetch(args)
    except (FetchError, OSError) as e:
        print("fetch_target: error: %s" % e, file=sys.stderr)
        return EXIT_ERROR
    if ambiguous:
        print("fetch_target: several skills found; rerun with --skill <one of>:", file=sys.stderr)
        for c in prov["candidates"]:
            print("  %s" % c)
        return EXIT_AMBIGUOUS
    print("kind:      %s" % prov["kind"])
    print("input:     %s" % prov["input"])
    if prov["repo_url"]:
        print("repo:      %s @ %s" % (prov["repo_url"], prov["ref_requested"] or "(default branch)"))
    print("commit:    %s%s" % (prov["commit"] or "-", " (dirty)" if prov["dirty"] else ""))
    print("skill:     %s/%s" % (prov["source_root"], prov["skill_subpath"]))
    if len(prov["candidates"]) > 1:
        print("other skills in source: %d" % (len(prov["candidates"]) - 1))
    for d in prov["docs"]:
        print("doc:       %s -> %s" % (d["input"], d["saved_as"]))
    if prov["plugin_root"]:
        print("plugin:    %s" % prov["plugin_root"])
    for e in prov["external_files"]:
        print("external:  %s (%s)" % (e["path"], e["reason"]))
    for s in prov["skipped"]:
        print("skipped:   %s (%s)" % (s["path"], s["reason"]))
    for n in prov["notes"]:
        print("note:      %s" % n)
    print("wrote %s" % os.path.join(os.path.abspath(args.out), "provenance.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
