#!/usr/bin/env python3
import argparse
import hashlib
import os
import posixpath
import re
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    INVENTORY_SCHEMA, dump_json, load_json, parse_frontmatter, read_text, sha256_file, split_lines, write_json,
)

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KNOWN_KEYS = {
    "name", "description", "license", "compatibility", "metadata", "allowed-tools", "version", "model",
    "disable-model-invocation", "user-invocable", "argument-hint", "context", "agent", "hooks", "when_to_use",
    "paths", "effort",
}
BODY_MAX_LINES = 500
EXCERPT_MAX = 160
FILE_EXTS = {
    "md", "markdown", "txt", "rst", "py", "sh", "bash", "zsh", "fish", "ps1", "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "csv", "tsv", "xml", "html", "htm", "css", "scss",
    "sql", "rb", "go", "rs", "java", "kt", "swift", "c", "h", "cc", "cpp", "hpp", "lua", "pl", "php", "r",
    "ipynb", "pdf", "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "docx", "xlsx", "pptx", "zip", "tar", "gz",
    "tmpl", "tpl", "j2", "jinja", "mustache", "hbs", "env", "lock", "dockerfile", "mdx", "proto", "graphql", "wasm",
}
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"'`)\]]+")
MD_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
INVOKE_RE = re.compile(
    r"(?=(?<![\w./-])(?:python3?|bash|sh|zsh|node|deno run|bun|ruby|perl|uv run|npx|pwsh|source|\.)\s+"
    r"(?:-[\w-]+(?:=\S+)?\s+)*([^\s`'\"|;&<>()]+))"
)
INCLUDE_RE = re.compile(r"(?:^|(?<=[\s(\[]))@([\w.~/-]+)")
BARE_RE = re.compile(r"(?<![\w/.@`~$-])((?:\.{1,2}/)?[\w.-]+(?:/[\w.-]+)+)")
BARE_NAME_RE = re.compile(
    r"(?<![\w/.@`~$<-])([\w-][\w.-]*\.(?:%s))(?![\w/.>-])" % "|".join(sorted(FILE_EXTS, key=len, reverse=True)), re.I)
LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
REF_KIND_ORDER = ["markdown_link", "invocation", "include", "backtick", "bare_path"]

INDICATOR_RES = [
    ("url", re.compile(r"\bhttps?://[^\s<>\"'`)\]]+")),
    ("mcp_tool", re.compile(r"\bmcp__\w+|\bMCP\s+server", re.I)),
    ("env_var", re.compile(
        r"\$\{[A-Z][A-Z0-9_]{2,}[}:]|\$[A-Z][A-Z0-9_]{2,}\b|\bexport\s+[A-Z][A-Z0-9_]{2,}=|\b[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET|PASSWORD)\b|\bos\.environ\b|\bos\.getenv\b|\bprocess\.env\b|\bDeno\.env\b|\bENV\[|\bgetenv\(")),
    ("network_code", re.compile(
        r"\brequests\.(?:get|post|put|patch|delete|head|request|Session)\b|\burllib\.request\b|\burlopen\(|"
        r"\bhttpx\b|\baiohttp\b|\bhttp\.client\b|\bfetch\(|\baxios\b|\bcurl\b|\bwget\b|\bsocket\.|"
        r"\bInvoke-WebRequest\b|\bInvoke-RestMethod\b")),
    ("subprocess", re.compile(
        r"\bsubprocess\b|\bos\.system\(|\bos\.popen\(|\bos\.exec\w*\(|\bchild_process\b|\bexecSync\(|\bspawnSync\(|"
        r"(?<![\w.])exec\(|(?<![\w.])eval\(|\bspawn\(")),
    ("package_install", re.compile(
        r"\bpip3?\s+install\b|\bpython3?\s+-m\s+pip\s+install\b|\bnpm\s+(?:install|i|add)\b|\bpnpm\s+(?:add|install)\b|"
        r"\byarn\s+add\b|\buv\s+(?:add|pip\s+install|tool\s+install)\b|\bbrew\s+install\b|\bapt(?:-get)?\s+install\b|"
        r"\bcargo\s+install\b|\bgo\s+install\b|\bgem\s+install\b|\bnpx\s+-y\b|\bpipx\s+install\b")),
]
MD_EXTS = (".md", ".markdown", ".mdx")
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh", "shell-session", "terminal"}
INJECTION_RES = [
    ("ignore_instructions", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?(?:of\s+)?(?:your\s+|the\s+|my\s+)?"
        r"(?:previous|prior|above|earlier|preceding|other|existing|original|system)?\s*(?:instructions|rules|directions|guidelines|prompts?)\b",
        re.I)),
    ("you_are_now", re.compile(r"\byou\s+are\s+now\b", re.I)),
    ("addresses_auditor", re.compile(
        r"\b(?:auditor|evaluator|grader)s?\b.*\b(?:must|should|rate|score)\b|\breviewers?\b.*\b(?:rate|score)\b|"
        r"\b(?:note|message|attention|instructions?)\s+(?:to|for)\s+(?:any\s+|all\s+|the\s+)?(?:ai|llm|auditors?|reviewers?|evaluators?|models?|agents?|assistants?)\b",
        re.I)),
    ("system_prompt", re.compile(r"\bsystem\s+prompt\b", re.I)),
    ("hide_from_user", re.compile(r"\bdo\s+not\s+(?:tell|inform|mention\s+(?:this\s+)?to|reveal\s+(?:this\s+)?to|alert)\s+(?:the\s+)?user", re.I)),
    ("rate_this_skill", re.compile(r"\b(?:rate|score|mark|classify|grade)\s+(?:this|the)\s+skill\s+(?:as|with|at)\b", re.I)),
]
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
AI_ADDRESS_RE = re.compile(r"\b(?:ai|assistant|agent|llm|model|claude|gpt|auditor|reviewer|evaluator)s?\b", re.I)
IMPERATIVE_RE = re.compile(r"\b(?:ignore|must|should|do not|don't|run|execute|rate|score|say|tell|pretend|always|never|verify)\b", re.I)


def excerpt(text):
    text = text.strip()
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 1] + "…"


def walk_files(root):
    entries = []
    dirs = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            dirs.add(rel_dir.replace(os.sep, "/"))
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                entries.append(os.path.normpath(os.path.join(rel_dir, d)).replace(os.sep, "/"))
        for f in filenames:
            entries.append(os.path.normpath(os.path.join(rel_dir, f)).replace(os.sep, "/"))
    return sorted(entries), dirs


def describe_file(root, rel):
    full = os.path.join(root, rel)
    st = os.lstat(full)
    ext = posixpath.splitext(rel)[1].lower()
    info = {"path": rel, "size": st.st_size, "ext": ext, "executable": bool(st.st_mode & 0o111),
            "shebang": None, "lines": None}
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(full)
        info.update(kind="symlink", sha256=hashlib.sha256(("symlink:" + target).encode()).hexdigest(),
                    link_target=target, executable=False)
        return info, None
    if not stat.S_ISREG(st.st_mode):
        info.update(kind="other", sha256=hashlib.sha256(b"").hexdigest())
        return info, None
    info["sha256"] = sha256_file(full)
    text = read_text(full)
    if text is None:
        info["kind"] = "binary"
        return info, None
    lines = split_lines(text)
    info["kind"] = "text"
    info["lines"] = len(lines)
    if lines and lines[0].startswith("#!"):
        info["shebang"] = excerpt(lines[0])
    return info, lines


def code_fence_map(lines):
    blocks, inside = [], set()
    fence = None
    for idx, ln in enumerate(lines, 1):
        s = ln.lstrip()
        m = re.match(r"^(`{3,}|~{3,})(.*)$", s)
        if fence is None:
            if m:
                fence = (m.group(1), (m.group(2).strip().split() or [""])[0].strip("{}.").lower(), idx)
                inside.add(idx)
        else:
            inside.add(idx)
            if m and m.group(1)[0] == fence[0][0] and len(m.group(1)) >= len(fence[0]) and not m.group(2).strip():
                blocks.append((fence[1], fence[2], idx))
                fence = None
    if fence is not None:
        blocks.append((fence[1], fence[2], len(lines)))
    return blocks, inside


def skill_md_facts(lines, fm):
    blocks, inside = code_fence_map(lines)
    headings = []
    for idx, ln in enumerate(lines, 1):
        if idx in inside or (fm["present"] and fm["line_end"] and idx <= fm["line_end"]):
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", ln)
        if m:
            headings.append({"level": len(m.group(1)), "text": m.group(2), "line": idx})
    body_start = fm["line_end"] if fm.get("line_end") else 0
    body = lines[body_start:]
    return {
        "lines": len(lines),
        "body_lines": len(body),
        "words": len(" ".join(body).split()),
        "headings": headings,
        "code_blocks": [{"lang": b[0] or None, "line_start": b[1], "line_end": b[2]} for b in blocks],
    }


def clean_target(raw, top_dirs):
    t = raw.strip().strip("\"'")
    t = t.rstrip(".,;:)!?]")
    t = LINE_SUFFIX_RE.sub("", t)
    t = t.split("#", 1)[0].split("?", 1)[0]
    if not t or URL_RE.match(t) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t):
        return None
    if t.startswith(("/", "~", "$", "-")) or re.search(r"[<>{}$*?|\\\[\]=]", t):
        return None
    while t.startswith("./"):
        t = t[2:]
    if not t or t in (".", ".."):
        return None
    return t


def path_like(t, top_dirs, strict):
    last = t.rstrip("/").rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last.strip(".") else ""
    if ext in FILE_EXTS:
        return True
    first = t.split("/", 1)[0]
    if "/" in t and (first in top_dirs or first == ".."):
        return True
    if not strict and t.endswith("/") and t.count("/") >= 1 and re.match(r"^[\w.-]+(/[\w.-]+)*/$", t):
        return True
    return False


def extract_refs(rel, lines, top_dirs, fenced=frozenset()):
    found = {}

    def add(lineno, raw, kind, strict=True):
        t = clean_target(raw, top_dirs)
        if t is None or not path_like(t, top_dirs, strict):
            return
        key = (lineno, t)
        if key not in found or REF_KIND_ORDER.index(kind) < REF_KIND_ORDER.index(found[key]):
            found[key] = kind

    for idx, ln in enumerate(lines, 1):
        for m in MD_LINK_RE.finditer(ln):
            add(idx, m.group(1), "markdown_link", strict=False)
        no_url = URL_RE.sub(" ", ln)
        for m in INVOKE_RE.finditer(no_url):
            add(idx, m.group(1), "invocation")
        for m in INCLUDE_RE.finditer(no_url):
            add(idx, m.group(1), "include")
        for m in BACKTICK_RE.finditer(no_url):
            toks = m.group(1).split()
            if toks:
                add(idx, toks[0], "backtick")
        if idx not in fenced:
            for m in BARE_RE.finditer(no_url):
                add(idx, m.group(1), "bare_path")
            for m in BARE_NAME_RE.finditer(no_url):
                add(idx, m.group(1), "bare_path")
    return [{"from": rel, "line": k[0], "target": k[1], "kind": v} for k, v in sorted(found.items())]


def resolve_ref(ref, files, dirs, lower_map, base_map):
    """Return (resolved, exists, resolution). Case-insensitive and unique-basename matches are
    reported separately because they are inferences, not exact paths."""
    from_dir = posixpath.dirname(ref["from"])
    target = ref["target"]
    is_dir_ref = target.endswith("/")
    options = []
    for base in ([from_dir, ""] if from_dir else [""]):
        p = posixpath.normpath(posixpath.join(base, target))
        if p == ".." or p.startswith("../"):
            continue
        if p not in options:
            options.append(p)
    for p in options:
        if (p in files and not is_dir_ref) or p in dirs:
            return p, True, "path"
    if not is_dir_ref:
        for p in options:
            hits = lower_map.get(p.lower(), [])
            if len(hits) == 1:
                return hits[0], True, "case_insensitive"
        if "/" not in target:
            hits = base_map.get(target.lower(), [])
            if len(hits) == 1:
                return hits[0], True, "basename"
    return (options[0] if options else None), False, None


def scan_indicators(rel, lines, is_markdown):
    out = []
    for idx, ln in enumerate(lines, 1):
        for kind, rx in INDICATOR_RES:
            m = rx.search(ln)
            if m:
                out.append({"path": rel, "line": idx, "kind": kind, "match": excerpt(m.group(0)), "excerpt": excerpt(ln)})
    if is_markdown:
        blocks, _ = code_fence_map(lines)
        for lang, start, end in blocks:
            if lang not in SHELL_LANGS:
                continue
            for idx in range(start + 1, end):
                s = lines[idx - 1].strip()
                if s.startswith(("$ ", "% ", "> ")):
                    s = s[2:].strip()
                if not s or s.startswith("#"):
                    continue
                toks = s.split()
                while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
                    toks.pop(0)
                if toks and toks[0] in ("sudo", "env", "exec", "time", "nohup") and len(toks) > 1:
                    toks.pop(0)
                if toks and re.match(r"^[\w./@+-]+$", toks[0]):
                    out.append({"path": rel, "line": idx, "kind": "cli_command", "match": toks[0], "excerpt": excerpt(lines[idx - 1])})
    return out


def scan_injection(rel, lines, text):
    out = {}
    for idx, ln in enumerate(lines, 1):
        for rule, rx in INJECTION_RES:
            if rx.search(ln):
                out.setdefault(idx, {"path": rel, "line": idx, "rule": rule, "excerpt": excerpt(ln)})
                break
    for m in HTML_COMMENT_RE.finditer(text):
        body = m.group(1)
        if AI_ADDRESS_RE.search(body) and IMPERATIVE_RE.search(body):
            idx = text.count("\n", 0, m.start()) + 1
            if idx not in out:
                out[idx] = {"path": rel, "line": idx, "rule": "html_comment_to_ai", "excerpt": excerpt(" ".join(body.split()))}
    return [out[k] for k in sorted(out)]


def spec_checks(fm, skill_md, dir_name):
    checks = []

    def add(cid, level, ok, detail):
        checks.append({"id": cid, "level": level, "ok": bool(ok), "detail": detail})

    add("skill_md_present", "error", skill_md is not None, "SKILL.md found" if skill_md is not None else "SKILL.md missing at skill root")
    add("frontmatter_present", "error", fm["present"] and fm["line_end"] is not None,
        "frontmatter lines %s-%s" % (fm["line_start"], fm["line_end"]) if fm["line_end"] else "no closed '---' frontmatter at line 1")
    add("frontmatter_parse", "error", not fm["errors"],
        "no parse errors" if not fm["errors"] else "; ".join("line %d: %s" % (e["line"], e["message"]) for e in fm["errors"]))
    parsed = fm["parsed"] if isinstance(fm["parsed"], dict) else {}
    name = parsed.get("name")
    add("name_present", "error", isinstance(name, str) and name.strip() != "",
        "name: %r" % (name,) if name is not None else "name missing")
    if isinstance(name, str) and name:
        add("name_format", "error", bool(NAME_RE.match(name)),
            "matches ^[a-z0-9]+(-[a-z0-9]+)*$" if NAME_RE.match(name) else "%r is not lowercase-hyphenated" % name)
        add("name_length", "error", len(name) <= 64, "%d chars (max 64)" % len(name))
        add("name_matches_dir", "warn", name == dir_name, "name %r vs directory %r" % (name, dir_name))
    desc = parsed.get("description")
    add("description_present", "error", isinstance(desc, str) and desc.strip() != "",
        "description present" if isinstance(desc, str) and desc.strip() else "description missing or not a string")
    if isinstance(desc, str):
        add("description_length", "error", len(desc) <= 1024, "%d chars (max 1024)" % len(desc))
    if skill_md is not None:
        add("body_length", "warn", skill_md["body_lines"] <= BODY_MAX_LINES,
            "%d body lines (recommended max %d)" % (skill_md["body_lines"], BODY_MAX_LINES))
    unknown = sorted(k for k in parsed if k not in KNOWN_KEYS)
    add("unknown_frontmatter_keys", "info", not unknown, "unknown keys: %s" % ", ".join(unknown) if unknown else "none")
    return checks


def build_inventory(skill_root, skill_root_label, dir_name=None, provenance=None, audit_dir=None):
    skill_root = os.path.abspath(skill_root)
    dir_name = dir_name or os.path.basename(skill_root.rstrip(os.sep))
    paths, dirs = walk_files(skill_root)
    file_set = set(paths)
    top_dirs = {d.split("/", 1)[0] for d in dirs}
    files, texts = [], {}
    for rel in paths:
        info, lines = describe_file(skill_root, rel)
        files.append(info)
        if lines is not None:
            texts[rel] = lines

    skill_lines = texts.get("SKILL.md")
    skill_text = "\n".join(skill_lines) if skill_lines is not None else ""
    fm = parse_frontmatter(skill_text) if skill_lines is not None else parse_frontmatter("")
    skill_md = skill_md_facts(skill_lines, fm) if skill_lines is not None else None

    lower_map, base_map = {}, {}
    for p in paths:
        lower_map.setdefault(p.lower(), []).append(p)
        base_map.setdefault(posixpath.basename(p).lower(), []).append(p)
    references = []
    for rel in sorted(texts):
        fenced = code_fence_map(texts[rel])[1] if rel.lower().endswith(MD_EXTS) else frozenset()
        for ref in extract_refs(rel, texts[rel], top_dirs, fenced):
            ref["resolved"], ref["exists"], ref["resolution"] = resolve_ref(ref, file_set, dirs, lower_map, base_map)
            if ref["kind"] == "bare_path" and "/" not in ref["target"] and not ref["exists"]:
                continue
            references.append(ref)

    missing = {}
    for ref in sorted(references, key=lambda r: (r["from"] != "SKILL.md", r["from"], r["line"])):
        if ref["exists"] or ("/" not in ref["target"] and ref["kind"] in ("backtick", "bare_path")):
            continue
        key = ref["resolved"] or ref["target"]
        if key not in missing:
            missing[key] = {"target": ref["target"], "resolved": ref["resolved"], "from": ref["from"], "line": ref["line"], "count": 0}
        missing[key]["count"] += 1

    reachable = set()
    queue = ["SKILL.md"] if "SKILL.md" in file_set else []
    seen = set(queue)
    by_from = {}
    for ref in references:
        if ref["exists"]:
            by_from.setdefault(ref["from"], []).append(ref["resolved"])
    while queue:
        cur = queue.pop(0)
        for res in by_from.get(cur, []):
            hits = [res] if res in file_set else [p for p in paths if p.startswith(res + "/")]
            for h in hits:
                reachable.add(h)
                if h not in seen:
                    seen.add(h)
                    queue.append(h)

    def exempt(p):
        base = posixpath.basename(p).upper()
        return p == "SKILL.md" or base.startswith("README") or base.startswith("LICENSE")

    unreferenced = [p for p in paths if p not in reachable and not exempt(p)]

    indicators, injections = [], []
    for rel in sorted(texts):
        lines = texts[rel]
        indicators.extend(scan_indicators(rel, lines, rel.lower().endswith(MD_EXTS)))
        injections.extend(scan_injection(rel, lines, "\n".join(lines)))
    indicators.sort(key=lambda x: (x["path"], x["line"], x["kind"]))

    fingerprint = hashlib.sha256("".join("%s\t%s\n" % (f["path"], f["sha256"]) for f in files).encode()).hexdigest()

    inv = {
        "schema": INVENTORY_SCHEMA,
        "skill_root": skill_root_label,
        "skill_name_dir": dir_name,
        "frontmatter": fm,
        "spec_checks": spec_checks(fm, skill_md, dir_name),
        "files": files,
        "skill_md": skill_md,
        "references": references,
        "missing_references": [missing[k] for k in sorted(missing)],
        "unreferenced_files": unreferenced,
        "external_indicators": indicators,
        "injection_candidates": injections,
        "fingerprint": fingerprint,
    }
    if provenance is not None:
        inv["source_context"] = source_context(audit_dir, provenance)
    return inv


def source_context(audit_dir, prov):
    source_root = os.path.join(audit_dir, prov["source_root"])
    sub = prov["skill_subpath"]
    other = sorted(c for c in prov.get("candidates", []) if c != sub)
    repo_docs = []
    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, source_root).replace(os.sep, "/")
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "node_modules", ".venv", "__pycache__")) if depth < 2 else []
        for f in filenames:
            rel = f if rel_dir == "." else rel_dir + "/" + f
            in_skill = sub == "." or rel == sub or rel.startswith(sub + "/")
            if in_skill:
                continue
            up = f.upper()
            is_doc = up.startswith("README") or up.startswith("CHANGELOG") or (
                posixpath.basename(rel_dir) == "docs" and f.lower().endswith(".md"))
            if is_doc and os.path.isfile(os.path.join(dirpath, f)) and not os.path.islink(os.path.join(dirpath, f)):
                repo_docs.append("@repo/" + rel)
    docs_dir = os.path.join(audit_dir, "docs")
    user_docs = sorted("@docs/" + f for f in os.listdir(docs_dir)) if os.path.isdir(docs_dir) else []
    return {"other_skills": other, "repo_docs": sorted(repo_docs), "user_docs": user_docs}


def summary(inv):
    out = []
    parsed = inv["frontmatter"]["parsed"] or {}
    desc = parsed.get("description")
    desc = " ".join(str(desc).split()) if desc is not None else "-"
    out.append("name:        %s (dir: %s)" % (parsed.get("name", "-"), inv["skill_name_dir"]))
    out.append("description: %s" % (desc if len(desc) <= 200 else desc[:199] + "…"))
    kinds = {}
    for f in inv["files"]:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    out.append("files:       %d (%s)" % (len(inv["files"]), ", ".join("%s %d" % kv for kv in sorted(kinds.items()))))
    if inv["skill_md"]:
        out.append("SKILL.md:    %d lines, %d body words, %d headings, %d code blocks" % (
            inv["skill_md"]["lines"], inv["skill_md"]["words"], len(inv["skill_md"]["headings"]), len(inv["skill_md"]["code_blocks"])))
    scripts = [f for f in inv["files"] if f["executable"] or f["shebang"]]
    for f in scripts:
        out.append("script:      %s%s%s" % (f["path"], " [x]" if f["executable"] else " [not executable]",
                                            " " + f["shebang"] if f["shebang"] else ""))
    for m in inv["missing_references"]:
        out.append("missing ref: %s (first at %s:%d, %dx)" % (m["target"], m["from"], m["line"], m["count"]))
    if inv["unreferenced_files"]:
        out.append("unreferenced: %s" % ", ".join(inv["unreferenced_files"]))
    counts = {}
    for i in inv["external_indicators"]:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    out.append("indicators:  %s" % (", ".join("%s %d" % kv for kv in sorted(counts.items())) or "none"))
    out.append("injection candidates: %d" % len(inv["injection_candidates"]))
    for c in inv["injection_candidates"]:
        out.append("  %s:%d [%s] %s" % (c["path"], c["line"], c["rule"], c["excerpt"][:100]))
    fails = [c for c in inv["spec_checks"] if not c["ok"]]
    for c in fails:
        out.append("spec %-5s %s: %s" % (c["level"], c["id"], c["detail"]))
    if not fails:
        out.append("spec:        all checks pass")
    ctx = inv.get("source_context")
    if ctx:
        out.append("context:     %d other skills, %d repo docs, %d user docs" % (
            len(ctx["other_skills"]), len(ctx["repo_docs"]), len(ctx["user_docs"])))
    out.append("fingerprint: %s" % inv["fingerprint"])
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic inventory of a skill (reads files only, never executes them).")
    ap.add_argument("audit_dir", nargs="?", help="audit directory containing provenance.json")
    ap.add_argument("--skill-dir", help="ad-hoc mode: inventory this directory without provenance")
    ap.add_argument("--json", action="store_true", help="print the JSON instead of the text summary")
    args = ap.parse_args(argv)
    if bool(args.audit_dir) == bool(args.skill_dir):
        ap.error("give exactly one of AUDIT_DIR or --skill-dir")
    try:
        if args.skill_dir:
            if not os.path.isdir(args.skill_dir):
                raise ValueError("not a directory: %s" % args.skill_dir)
            inv = build_inventory(args.skill_dir, os.path.abspath(args.skill_dir))
        else:
            audit_dir = os.path.abspath(args.audit_dir)
            prov = load_json(os.path.join(audit_dir, "provenance.json"))
            if not prov.get("source_root") or prov.get("skill_subpath") is None:
                raise ValueError("provenance.json has no resolved skill; rerun fetch_target.py with --skill")
            label = posixpath.normpath(posixpath.join(prov["source_root"], prov["skill_subpath"]))
            skill_root = os.path.join(audit_dir, label)
            if not os.path.isdir(skill_root):
                raise ValueError("skill root missing: %s" % skill_root)
            inv = build_inventory(skill_root, label, prov.get("skill_dir_name"), prov, audit_dir)
            write_json(os.path.join(audit_dir, "inventory.json"), inv)
    except (OSError, ValueError) as e:
        print("inventory: error: %s" % e, file=sys.stderr)
        return 2
    sys.stdout.write(dump_json(inv) if args.json else summary(inv) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
