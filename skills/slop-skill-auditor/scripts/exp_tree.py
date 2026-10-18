from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from _common import sha256_file

WORKSPACE_FILE_MAX_BYTES = 20 * 1024 * 1024
WORKSPACE_TOTAL_MAX_BYTES = 200 * 1024 * 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside(path: Path, root: Path) -> bool:
    path, root = path.resolve(), root.resolve()
    return path != root and root in path.parents


def skip_git(rel: str) -> bool:
    return rel == ".git" or rel.startswith(".git/")


def skip_installed_skills(rel: str) -> bool:
    return rel == ".claude/skills" or rel.startswith(".claude/skills/")


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
        elif (st := p.lstat()).st_size > WORKSPACE_FILE_MAX_BYTES:
            files[rel] = f"oversize:{st.st_size}:{st.st_mtime_ns}"
        else:
            files[rel] = sha256_file(p)
    combined = sha256_bytes(json.dumps(files, sort_keys=True).encode())
    return {"sha256": combined, "files": files}


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


def copy_tree(src: Path, dst: Path, exclude_skills: bool = False, skip_names=(), capped: bool = False) -> list[str]:
    src_root = src.resolve()
    skipped = []
    total = 0

    def ign(d, names):
        nonlocal total
        dpath = Path(d)
        # copytree would block on FIFOs and choke on sockets/devices an agent may leave behind.
        out = {n for n in names if not ((dpath / n).is_symlink() or (dpath / n).is_dir() or (dpath / n).is_file())}
        here = dpath.resolve()
        if exclude_skills and here == src_root / ".claude" and "skills" in names:
            out.add("skills")
        if here == src_root:
            out |= {n for n in names if n in skip_names}
        if capped:
            for n in sorted(set(names) - out):
                f = dpath / n
                if f.is_symlink() or not f.is_file():
                    continue
                size = f.lstat().st_size
                if size > WORKSPACE_FILE_MAX_BYTES or total + size > WORKSPACE_TOTAL_MAX_BYTES:
                    out.add(n)
                    skipped.append(f.relative_to(src).as_posix())
                else:
                    total += size
        return out
    # symlinks=True copies links as links so an agent-created link to e.g. ~/.ssh is never followed.
    shutil.copytree(src, dst, symlinks=True, ignore=ign, dirs_exist_ok=True)
    return skipped
