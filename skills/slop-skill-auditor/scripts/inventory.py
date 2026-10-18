#!/usr/bin/env python3
import argparse
import hashlib
import os
import posixpath
import re
import stat
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (  # noqa: E402
    INVENTORY_SCHEMA, LINE_SCAN_MAX, MD_EXTS, PLUGIN_DIR, SKIP_DIRS, TEXT_MAX_BYTES, code_fence_map, collect_refs,
    decode_text, dump_json, is_repo_doc, is_review_seed, is_script_path, load_json, parse_frontmatter, plugin_scan,
    reference_closure, scan_lines, sha256_file, skill_label, source_lines, split_lines, walk_files, write_json,
)

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KNOWN_KEYS = {
    "name", "description", "license", "compatibility", "metadata", "allowed-tools", "version", "model",
    "disable-model-invocation", "user-invocable", "argument-hint", "context", "agent", "hooks", "when_to_use",
    "paths", "effort",
}
BODY_MAX_LINES = 500
EXCERPT_MAX = 160
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
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh", "shell-session", "terminal"}
INJECTION_RES = [
    ("ignore_instructions", re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+)?(?:of\s+)?(?:your\s+|the\s+|my\s+)?"
        r"(?:(?:previous|prior|above|earlier|preceding|other|existing|original|system)\s*)?(?:instructions|rules|directions|guidelines|prompts?)\b",
        re.I)),
    ("you_are_now", re.compile(r"\byou\s+are\s+now\b", re.I)),
    ("addresses_auditor", re.compile(
        r"\b(?:auditor|evaluator|grader)s?\b.{0,300}\b(?:must|should|rate|score)\b|\breviewers?\b.{0,300}\b(?:rate|score)\b|"
        r"\b(?:note|message|attention|instructions?)\s+(?:to|for)\s+(?:any\s+|all\s+|the\s+)?(?:ai|llm|auditors?|reviewers?|evaluators?|models?|agents?|assistants?)\b",
        re.I)),
    ("system_prompt", re.compile(r"\bsystem\s+prompt\b", re.I)),
    ("hide_from_user", re.compile(r"\bdo\s+not\s+(?:tell|inform|mention\s+(?:this\s+)?to|reveal\s+(?:this\s+)?to|alert)\s+(?:the\s+)?user", re.I)),
    ("rate_this_skill", re.compile(r"\b(?:rate|score|mark|classify|grade)\s+(?:this|the)\s+skill\s+(?:as|with|at)\b", re.I)),
]
AI_ADDRESS_RE = re.compile(r"\b(?:ai|assistant|agent|llm|model|claude|gpt|auditor|reviewer|evaluator)s?\b", re.I)
IMPERATIVE_RE = re.compile(r"\b(?:ignore|must|should|do not|don't|run|execute|rate|score|say|tell|pretend|always|never|verify)\b", re.I)


def excerpt(text):
    text = text.strip()
    return text if len(text) <= EXCERPT_MAX else text[: EXCERPT_MAX - 1] + "…"


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
    if st.st_size > TEXT_MAX_BYTES:
        info.update(kind="binary", sha256=sha256_file(full))
        return info, None
    with open(full, "rb") as f:
        data = f.read()
    info["sha256"] = hashlib.sha256(data).hexdigest()
    text = decode_text(data)
    if text is None:
        info["kind"] = "binary"
        return info, None
    lines = split_lines(text)
    info["kind"] = "text"
    info["lines"] = len(lines)
    if lines and lines[0].startswith("#!"):
        info["shebang"] = excerpt(lines[0])
    return info, lines


def skill_md_facts(lines, fm, fences=None):
    blocks, inside = fences or code_fence_map(lines)
    headings = []
    for idx, ln in enumerate(lines, 1):
        if idx in inside or (fm["present"] and fm["line_end"] and idx <= fm["line_end"]):
            continue
        m = re.match(r"\s{0,3}(#{1,6})\s+(.*)", ln)
        if m:
            headings.append({"level": len(m.group(1)), "text": m.group(2).rstrip().rstrip("#").rstrip(), "line": idx})
    body_start = fm["line_end"] if fm.get("line_end") else 0
    body = lines[body_start:]
    return {
        "lines": len(lines),
        "body_lines": len(body),
        "words": len(" ".join(body).split()),
        "headings": headings,
        "code_blocks": [{"lang": b[0] or None, "line_start": b[1], "line_end": b[2]} for b in blocks],
    }


def scan_indicators(rel, lines, blocks=()):
    out = []
    for idx, ln in enumerate(lines, 1):
        for kind, rx in INDICATOR_RES:
            m = rx.search(ln)
            if m:
                out.append({"path": rel, "line": idx, "kind": kind, "match": excerpt(m.group(0)), "excerpt": excerpt(ln)})
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
    pos = idx = 1
    start = text.find("<!--")
    while start != -1:
        end = text.find("-->", start + 4)
        if end == -1:
            break
        body = text[start + 4:end]
        if AI_ADDRESS_RE.search(body) and IMPERATIVE_RE.search(body):
            idx += text.count("\n", pos - 1, start)
            pos = start + 1
            if idx not in out:
                out[idx] = {"path": rel, "line": idx, "rule": "html_comment_to_ai", "excerpt": excerpt(" ".join(body.split()))}
        start = text.find("<!--", end + 3)
    return [out[k] for k in sorted(out)]


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")
TRIGGER_RE = re.compile(r"\buse\s+(?:it\s+|this\s+(?:skill\s+)?)?(?:when|for|if|whenever)\b|\bwhen\s+(?:the\s+)?user|\btrigger|とき|時に|場合|依頼", re.I)
NON_SKILL_DIRS = {"tests", "test", "fixtures", "examples"}
MISSING_SCOPE = (
    "only references made from review_targets (SKILL.md, files reachable from it, files under scripts/ or bin/, executables, "
    "root README*); files inside any tests/, test/, fixtures/ or examples/ directory are excluded unless reachable from "
    "SKILL.md; slash-less backtick/bare_path targets are never reported as missing; targets outside the skill directory that "
    "exist in the source tree are resolved as scope 'external' (@repo/...) instead"
)
TOKEN_METHOD = "chars/4 for ASCII-dominant text, chars/1.5 for CJK-dominant text (estimate only)"


def est_tokens(text):
    chars = len(text)
    dense = len("".join(text.split()))
    cjk = len(CJK_RE.findall(text))
    divisor = 1.5 if dense and cjk * 2 > dense else 4.0
    return chars, int(-(-chars // divisor))


def cost_of(texts):
    chars = tokens = 0
    for t in texts:
        c, k = est_tokens(t)
        chars += c
        tokens += k
    return {"chars": chars, "est_tokens": tokens}


def is_script(info):
    return is_script_path(info["path"], info["executable"], info["shebang"])


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
        m = TRIGGER_RE.search(desc)
        add("description_has_trigger_context", "info", bool(m),
            "trigger phrase %r" % m.group(0) if m else "description does not say when to use the skill")
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
    files, texts = [], {}
    for rel in paths:
        info, lines = describe_file(skill_root, rel)
        files.append(info)
        if lines is not None:
            texts[rel] = lines

    fences = {rel: code_fence_map(lines) for rel, lines in texts.items() if rel.lower().endswith(MD_EXTS)}
    skill_lines = texts.get("SKILL.md")
    skill_text = "\n".join(skill_lines) if skill_lines is not None else ""
    fm = parse_frontmatter(skill_text)
    skill_md = skill_md_facts(skill_lines, fm, fences["SKILL.md"]) if skill_lines is not None else None

    scan, truncated = {}, []
    for rel in sorted(texts):
        scan[rel], long_lines = scan_lines(texts[rel])
        if long_lines:
            truncated.append({"path": rel, "count": len(long_lines), "first_line": long_lines[0]})
    references = collect_refs(paths, dirs, texts)

    info_by_path = {f["path"]: f for f in files}
    seeds = [p for p in paths if is_review_seed(p, info_by_path[p]["executable"], info_by_path[p]["shebang"])]
    plugin = None
    if provenance is not None:
        root, skill_sub = os.path.join(audit_dir, provenance["source_root"]), provenance["skill_subpath"]
        prel = provenance.get("plugin_root")
        if prel and os.path.isdir(os.path.join(root, prel, PLUGIN_DIR)) and not os.path.islink(os.path.join(root, prel, PLUGIN_DIR)):
            plugin = plugin_scan(root, prel)
    else:
        root, skill_sub = skill_root, "."
    closure = reference_closure(root, skill_sub, paths, dirs, references, seeds, plugin["files"] if plugin else ())
    reachable = closure["reachable"]

    external_files, ext_texts = [], {}
    for p, reason in closure["external"].items():
        lines = source_lines(root, p)
        full = os.path.join(root, p)
        st = os.lstat(full)
        loc = "@repo/" + p
        entry = {"path": loc, "reason": reason, "size": st.st_size, "sha256": sha256_file(full),
                 "kind": "text" if lines or st.st_size == 0 else "binary", "lines": len(lines) if lines is not None else None}
        if entry["kind"] == "binary":
            entry["lines"] = None
        else:
            ext_texts[loc] = lines
        external_files.append(entry)
    external_files.sort(key=lambda e: e["path"])

    def exempt(p):
        base = posixpath.basename(p).upper()
        return p == "SKILL.md" or base.startswith("README") or base.startswith("LICENSE")

    unreferenced = [p for p in paths if p not in reachable and not exempt(p)]

    reach_text = sorted(p for p in reachable if p in texts and p != "SKILL.md")
    reach_scripts = [p for p in reach_text if is_script(info_by_path[p])]
    ext_text = sorted(ext_texts)
    desc_val = (fm["parsed"] or {}).get("description") if isinstance(fm["parsed"], dict) else None
    context_cost = {
        "method": TOKEN_METHOD,
        "skill_md": cost_of([skill_text] if skill_lines is not None else []),
        "frontmatter_description": cost_of([desc_val] if isinstance(desc_val, str) else []),
        "reachable_text": dict(files=reach_text, **cost_of("\n".join(texts[p]) for p in reach_text)),
        "scripts": dict(files=reach_scripts, **cost_of("\n".join(texts[p]) for p in reach_scripts)),
        "external_text": dict(files=ext_text, **cost_of("\n".join(ext_texts[p]) for p in ext_text)),
    }
    review_targets = sorted(
        p for p in paths
        if p == "SKILL.md" or p in reachable or is_script(info_by_path[p])
        or ("/" not in p and p.upper().startswith("README"))
    ) + [e["path"] for e in external_files]

    missing = {}
    missing_from = {
        p for p in review_targets
        if p in reachable or p == "SKILL.md" or not NON_SKILL_DIRS.intersection(p.split("/")[:-1])
    }
    for ref in sorted(references, key=lambda r: (r["from"] != "SKILL.md", r["from"], r["line"])):
        if ref["from"] not in missing_from:
            continue
        if ref["exists"] or ("/" not in ref["target"] and ref["kind"] in ("backtick", "bare_path")):
            continue
        key = ref["resolved"] or ref["target"]
        if key not in missing:
            missing[key] = {"target": ref["target"], "resolved": ref["resolved"], "from": ref["from"], "line": ref["line"], "count": 0}
        missing[key]["count"] += 1

    indicators, injections = [], []
    for rel in sorted(texts):
        indicators.extend(scan_indicators(rel, scan[rel], fences[rel][0] if rel in fences else ()))
        injections.extend(scan_injection(rel, scan[rel], "\n".join(texts[rel])))
    for loc in ext_text:
        lines = scan_lines(ext_texts[loc])[0]
        blocks = code_fence_map(ext_texts[loc])[0] if loc.lower().endswith(MD_EXTS) else ()
        indicators.extend(scan_indicators(loc, lines, blocks))
        injections.extend(scan_injection(loc, lines, "\n".join(ext_texts[loc])))
    indicators.sort(key=lambda x: (x["path"], x["line"], x["kind"]))

    fingerprint = hashlib.sha256(("".join("%s\t%s\n" % (f["path"], f["sha256"]) for f in files)
                                  + "".join("%s\t%s\n" % (e["path"], e["sha256"]) for e in external_files)).encode()).hexdigest()

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
        "missing_references_scope": MISSING_SCOPE,
        "unreferenced_files": unreferenced,
        "external_files": external_files,
        "external_references": closure["external_refs"],
        "external_truncated": closure["truncated"],
        "external_indicators": indicators,
        "injection_candidates": injections,
        "truncated_lines": {"max_chars": LINE_SCAN_MAX, "files": truncated},
        "fingerprint": fingerprint,
        "context_cost": context_cost,
        "review_targets": review_targets,
    }
    if plugin is not None:
        inv["plugin"] = {"root": plugin["root"], "manifests": plugin["manifests"], "hooks": plugin["hooks"],
                         "mcp_servers": plugin["mcp_servers"]}
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
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS) if depth < 2 else []
        for f in filenames:
            rel = f if rel_dir == "." else rel_dir + "/" + f
            in_skill = sub == "." or rel == sub or rel.startswith(sub + "/")
            if in_skill:
                continue
            if is_repo_doc(rel_dir, f) and os.path.isfile(os.path.join(dirpath, f)) and not os.path.islink(os.path.join(dirpath, f)):
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
    kinds = Counter(f["kind"] for f in inv["files"])
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
    for e in inv["external_files"]:
        out.append("external:    %s (%s)" % (e["path"], e["reason"]))
    if inv["external_truncated"]:
        out.append("external:    more files outside the skill are referenced than were followed")
    plugin = inv.get("plugin")
    if plugin:
        out.append("plugin:      %s (%d manifests)" % (plugin["root"], len(plugin["manifests"])))
        for h in plugin["hooks"]:
            out.append("  hook %s [%s]: %s" % (h["event"], h["matcher"] or "*", (h["command"] or h["type"] or "-")[:120]))
        for s in plugin["mcp_servers"]:
            out.append("  mcp %s: %s" % (s["name"], " ".join(x for x in [s["command"] or s["url"] or "-"] + s["args"])[:120]))
    if inv["unreferenced_files"]:
        out.append("unreferenced: %s" % ", ".join(inv["unreferenced_files"]))
    counts = Counter(i["kind"] for i in inv["external_indicators"])
    out.append("indicators:  %s" % (", ".join("%s %d" % kv for kv in sorted(counts.items())) or "none"))
    for t in inv["truncated_lines"]["files"]:
        out.append("long lines:  %s: %d lines over %d chars, scanned only up to that length (first at line %d)" % (
            t["path"], t["count"], inv["truncated_lines"]["max_chars"], t["first_line"]))
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
    cc = inv["context_cost"]
    out.append("cost:        SKILL.md ~%d tokens, description ~%d tokens, reachable files ~%d tokens in %d files (scripts ~%d), "
               "outside the skill ~%d tokens in %d files (estimate)" % (
                   cc["skill_md"]["est_tokens"], cc["frontmatter_description"]["est_tokens"], cc["reachable_text"]["est_tokens"],
                   len(cc["reachable_text"]["files"]), cc["scripts"]["est_tokens"], cc["external_text"]["est_tokens"],
                   len(cc["external_text"]["files"])))
    out.append("review targets: %d (%s)" % (len(inv["review_targets"]), ", ".join(inv["review_targets"])))
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
            label = skill_label(prov)
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
