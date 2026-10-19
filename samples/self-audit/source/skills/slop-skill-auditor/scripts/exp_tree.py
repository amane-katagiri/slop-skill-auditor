from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
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


class UnsafeDestination(Exception):
    pass


def safe_mkdirs(root: Path, rel: Path) -> Path:
    cur = root
    for part in rel.parts:
        if part == ".":
            continue
        cur = cur / part
        try:
            st = cur.lstat()
        except FileNotFoundError:
            cur.mkdir()
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafeDestination(f"{cur.relative_to(root).as_posix()} is a symlink or not a directory")
    return cur


def safe_copy_file(src: Path, root: Path, rel: Path) -> None:
    parent = safe_mkdirs(root, rel.parent)
    dst = parent / rel.name
    try:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise UnsafeDestination(f"{rel.as_posix()}: cannot write ({e.strerror}; symlink or directory in the way)") from None
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out)
        os.fchmod(out.fileno(), stat.S_IMODE(os.stat(src).st_mode))


def safe_overlay(src: Path, root: Path, rel_dst: Path = Path(".")) -> None:
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = rel_dst / Path(dirpath).relative_to(src)
        safe_mkdirs(root, rel_dir)
        for name in sorted(dirnames + filenames):
            p = Path(dirpath) / name
            if p.is_symlink():
                try:
                    os.symlink(os.readlink(p), root / rel_dir / name)
                except FileExistsError:
                    raise UnsafeDestination(f"{(rel_dir / name).as_posix()} already exists; refusing to replace it with a symlink") from None
            elif p.is_file():
                safe_copy_file(p, root, rel_dir / name)
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
