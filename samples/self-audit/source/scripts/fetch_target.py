#!/usr/bin/env python3
import argparse
import datetime
import os
import posixpath
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import PROVENANCE_SCHEMA, SKIP_DIRS, write_json  # noqa: E402

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
UNSAFE_LOCAL_CONFIG_RE = re.compile(r"fsmonitor|\[\s*(filter|include|includeif)\b|textconv|command|process|clean|smudge", re.I)


class FetchError(Exception):
    pass


def git_env():
    env = dict(os.environ)
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    return env


def run_git(args, cwd=None, check=True, extra=()):
    proc = subprocess.run(
        ["git"] + GIT_HARDENING + list(extra) + args, cwd=cwd, env=git_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if check and proc.returncode != 0:
        raise FetchError("git %s failed: %s" % (" ".join(args[:2]), proc.stderr.strip()))
    return proc


def parse_github_url(url):
    """Classify a GitHub URL. Returns dict(kind, owner, repo, repo_url, rest) where `rest`
    is the undecoded `REF/PATH` remainder for tree/blob URLs (None otherwise)."""
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
    """Split `REF/PATH` using known ref names (longest prefix wins) or a leading 40-hex SHA.
    Returns (ref, path, is_sha)."""
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


def choose_skill(base, skill_opt):
    """Return (skill_subpath relative to base, candidates relative to base, ambiguous)."""
    candidates = find_skill_dirs(base)
    if skill_opt:
        sub = os.path.normpath(skill_opt).replace(os.sep, "/")
        if sub.startswith("../") or sub == ".." or os.path.isabs(sub):
            raise FetchError("--skill must be a relative path inside the target")
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


def tree_size(root, cap):
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
            if total >= cap:
                return total
    return total


def copy_tree(src, dst, skipped):
    """Copy src into dst without following symlinks out of src; symlinked files inside src
    are copied as regular files, other symlinks are skipped and recorded."""
    src_real = os.path.realpath(src)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src)
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d in SKIP_DIRS:
                continue
            if os.path.islink(full):
                skipped.append({"path": os.path.normpath(os.path.join(rel_dir, d)).replace(os.sep, "/"), "reason": "symlinked directory"})
                continue
            keep.append(d)
        dirnames[:] = keep
        os.makedirs(os.path.join(dst, rel_dir), exist_ok=True)
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            rel = os.path.normpath(os.path.join(rel_dir, f)).replace(os.sep, "/")
            target = os.path.join(dst, rel_dir, f)
            if os.path.islink(full):
                real = os.path.realpath(full)
                if not real.startswith(src_real + os.sep) or not os.path.isfile(real):
                    skipped.append({"path": rel, "reason": "symlink outside target or not a file"})
                    continue
                shutil.copy2(real, target)
            elif os.path.isfile(full):
                shutil.copy2(full, target)
            else:
                skipped.append({"path": rel, "reason": "not a regular file"})


def local_git_info(root, notes):
    commit = None
    dirty = None
    cfg = os.path.join(root, ".git", "config")
    try:
        with open(cfg, "r", encoding="utf-8", errors="replace") as f:
            cfg_text = f.read()
    except OSError:
        cfg_text = ""
    if UNSAFE_LOCAL_CONFIG_RE.search(cfg_text):
        notes.append("local .git/config defines filters/includes/commands; git was not run, commit and dirty unknown")
        return None, None
    proc = run_git(["rev-parse", "HEAD"], cwd=root, check=False)
    if proc.returncode == 0:
        commit = proc.stdout.strip()
    proc = run_git(["status", "--porcelain"], cwd=root, check=False)
    if proc.returncode == 0:
        dirty = bool(proc.stdout.strip())
    return commit, dirty


def ls_remote(repo_url):
    return run_git(["ls-remote", "--heads", "--tags", repo_url]).stdout


def clone(repo_url, dest, ref, is_sha):
    base = ["clone", "--depth", "1", "--no-recurse-submodules", "--single-branch"]
    if ref is None:
        run_git(base + ["--", repo_url, dest], extra=CLONE_ONLY)
    elif not is_sha:
        run_git(base + ["--branch", ref, "--", repo_url, dest], extra=CLONE_ONLY)
    else:
        run_git(["init", "-q", dest], extra=CLONE_ONLY)
        run_git(["config", "core.symlinks", "false"], cwd=dest)
        shallow = run_git(["fetch", "-q", "--depth", "1", "--no-recurse-submodules", repo_url, ref], cwd=dest, check=False)
        if shallow.returncode == 0:
            run_git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=dest)
        else:
            run_git(["fetch", "-q", "--no-recurse-submodules", repo_url, "+refs/heads/*:refs/remotes/origin/*"], cwd=dest)
            run_git(["checkout", "-q", "--detach", ref], cwd=dest)
    return run_git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()


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


def fetch_doc(spec, docs_dir, used, offline):
    parsed = urllib.parse.urlsplit(spec)
    os.makedirs(docs_dir, exist_ok=True)
    if parsed.scheme in ("http", "https"):
        if offline:
            raise FetchError("--offline: cannot download doc %s" % spec)
        name = safe_doc_name(os.path.basename(parsed.path) or parsed.netloc, used)
        req = urllib.request.Request(spec, headers={"User-Agent": "slop-skill-auditor"})
        with urllib.request.urlopen(req, timeout=DOC_TIMEOUT) as resp:
            data = resp.read(DOC_MAX_BYTES + 1)
        if len(data) > DOC_MAX_BYTES:
            raise FetchError("doc %s exceeds %d bytes" % (spec, DOC_MAX_BYTES))
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


def join_sub(a, b):
    return posixpath.normpath(posixpath.join(a, b))


def fetch(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    source = os.path.join(out, "source")
    ensure_empty_source(source, args.force)
    prov = {
        "schema": PROVENANCE_SCHEMA, "input": args.input, "kind": None, "repo_url": None,
        "ref_requested": args.ref, "commit": None, "dirty": None, "source_root": None,
        "skill_subpath": None, "skill_dir_name": None, "candidates": [], "candidates_relative_to": "source_root",
        "docs": [], "skipped": [], "notes": [],
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
        ref, path, is_sha = args.ref, ".", bool(args.ref and SHA_RE.match(args.ref))
        if gh["rest"] is not None:
            url_ref, path, is_sha = split_ref_path(gh["rest"], [] if SHA_RE.match(gh["rest"].split("/", 1)[0]) else parse_ls_remote(ls_remote(gh["repo_url"])))
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
        prov["commit"] = clone(gh["repo_url"], source, ref, is_sha)
        shutil.rmtree(os.path.join(source, ".git"), ignore_errors=True)
        base = os.path.join(source, path)
        if not os.path.isdir(base):
            shutil.rmtree(source)
            raise FetchError("path %s does not exist at %s" % (path, ref or "default branch"))
        sub, cands, ambiguous = choose_skill(base, args.skill)
        prov["source_root"] = "source"
        prov["candidates"] = find_skill_dirs(source)
        if ambiguous:
            prov["candidates"] = cands
            prov["candidates_relative_to"] = "input"
            prov["source_root"] = None
            shutil.rmtree(source)
        else:
            prov["skill_subpath"] = join_sub(path, sub)
            prov["skill_dir_name"] = gh["repo"] if prov["skill_subpath"] == "." else os.path.basename(prov["skill_subpath"])
    else:
        target = os.path.abspath(args.input)
        if os.path.isfile(target):
            if os.path.basename(target) != "SKILL.md":
                raise FetchError("file input must be a SKILL.md: %s" % args.input)
            prov["kind"] = "local_skill_md"
            skill_dir = os.path.dirname(target)
            if args.skill:
                raise FetchError("--skill cannot be combined with a SKILL.md path")
        elif os.path.isdir(target):
            prov["kind"] = "local_dir"
            sub, cands, ambiguous = choose_skill(target, args.skill)
            if ambiguous:
                prov["candidates"] = cands
                prov["candidates_relative_to"] = "input"
            skill_dir = None if ambiguous else os.path.normpath(os.path.join(target, sub))
        else:
            raise FetchError("no such file or directory: %s" % args.input)
        if not ambiguous:
            git_root = find_git_root(skill_dir)
            copy_root = skill_dir
            if git_root:
                prov["commit"], prov["dirty"] = local_git_info(git_root, prov["notes"])
                if tree_size(git_root, LOCAL_REPO_MAX_BYTES) < LOCAL_REPO_MAX_BYTES:
                    copy_root = git_root
                else:
                    prov["notes"].append("git repo is larger than 50 MB; copied only the skill directory")
            copy_tree(copy_root, source, prov["skipped"])
            prov["source_root"] = "source"
            prov["skill_subpath"] = os.path.relpath(skill_dir, copy_root).replace(os.sep, "/")
            prov["skill_dir_name"] = os.path.basename(skill_dir)
            prov["candidates"] = find_skill_dirs(source)

    used = set()
    for spec in args.doc or []:
        prov["docs"].append(fetch_doc(spec, os.path.join(out, "docs"), used, args.offline))
    prov["skipped"].sort(key=lambda s: s["path"])
    write_json(os.path.join(out, "provenance.json"), prov)
    return prov, ambiguous


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch a target skill into an audit directory (never executes it).")
    ap.add_argument("input", help="local dir, SKILL.md path, or github.com repo/tree/blob URL")
    ap.add_argument("--out", required=True, help="audit directory")
    ap.add_argument("--skill", help="skill subpath relative to the input when it holds several skills")
    ap.add_argument("--ref", help="branch, tag or 40-hex commit for a repo URL")
    ap.add_argument("--doc", action="append", help="extra README/doc (path or http(s) URL); repeatable")
    ap.add_argument("--offline", action="store_true", help="fail instead of using the network")
    ap.add_argument("--force", action="store_true", help="replace an existing non-empty source/")
    args = ap.parse_args(argv)
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
    for s in prov["skipped"]:
        print("skipped:   %s (%s)" % (s["path"], s["reason"]))
    for n in prov["notes"]:
        print("note:      %s" % n)
    print("wrote %s" % os.path.join(os.path.abspath(args.out), "provenance.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
