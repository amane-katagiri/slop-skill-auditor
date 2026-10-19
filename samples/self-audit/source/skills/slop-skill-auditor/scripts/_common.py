import hashlib
import json
import os
import posixpath
import re
import stat
from collections import deque

PROVENANCE_SCHEMA = "slop-skill-auditor/provenance@1"
INVENTORY_SCHEMA = "slop-skill-auditor/inventory@1"
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
TEXT_MAX_BYTES = 5 * 1024 * 1024
SCRIPT_EXTS = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd", ".lua", ".go", ".rs",
}

LOC_RE = re.compile(r"^(.+?):(\d+)(?:-(\d+))?$")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(data, sort_keys=True):
    return json.dumps(data, sort_keys=sort_keys, ensure_ascii=False, indent=2) + "\n"


def write_json(path, data, sort_keys=True):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_json(data, sort_keys))
    os.replace(tmp, path)


def split_lines(text):
    if text == "":
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_text(data):
    if b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("\ufeff") else text


def read_text(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size > TEXT_MAX_BYTES:
        return None
    with open(path, "rb") as f:
        return decode_text(f.read())


def is_script_file(path, executable, shebang):
    return bool(executable or shebang or os.path.splitext(path)[1].lower() in SCRIPT_EXTS)


def is_repo_doc(rel_dir, name):
    up = name.upper()
    return up.startswith("README") or up.startswith("CHANGELOG") or (
        posixpath.basename(rel_dir) == "docs" and name.lower().endswith(".md"))


def _within(base, path):
    base = os.path.realpath(base)
    real = os.path.realpath(path)
    return real == base or real.startswith(base + os.sep)


def skill_label(prov):
    if not prov.get("source_root") or prov.get("skill_subpath") is None:
        raise ValueError("provenance.json has no resolved skill; rerun fetch_target.py with --skill")
    return posixpath.normpath(posixpath.join(prov["source_root"], prov["skill_subpath"]))


def audit_roots(audit_dir):
    audit_dir = os.path.abspath(audit_dir)
    prov_path = os.path.join(audit_dir, "provenance.json")
    if not os.path.isfile(prov_path):
        raise ValueError("no provenance.json in %s" % audit_dir)
    prov = load_json(prov_path)
    skill_root = os.path.normpath(os.path.join(audit_dir, skill_label(prov)))
    source_root = os.path.normpath(os.path.join(audit_dir, prov["source_root"]))
    return skill_root, source_root, os.path.join(audit_dir, "docs")


def count_lines(path):
    n, last = 0, b"\n"
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    return n + (last != b"\n")


class LocationResolver:
    def __init__(self, audit_dir):
        self.audit_dir = audit_dir
        self._roots = None
        self._line_counts = {}

    def roots(self):
        if self._roots is None:
            self._roots = audit_roots(self.audit_dir)
        return self._roots

    def line_count(self, path):
        if path not in self._line_counts:
            self._line_counts[path] = count_lines(path)
        return self._line_counts[path]

    def __call__(self, loc):
        if not isinstance(loc, str) or not loc.strip():
            raise ValueError("empty location")
        loc = loc.strip()
        m = LOC_RE.match(loc)
        if m:
            rel, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        else:
            rel, start, end = loc, None, None
        skill_root, source_root, docs_root = self.roots()
        if rel.startswith("@repo/"):
            base, rel = source_root, rel[len("@repo/"):]
        elif rel.startswith("@docs/"):
            base, rel = docs_root, rel[len("@docs/"):]
        else:
            base = skill_root
        if not rel or rel.startswith("/") or os.path.isabs(rel):
            raise ValueError("bad path in location: %r" % loc)
        path = os.path.normpath(os.path.join(base, rel))
        if not _within(base, path):
            raise ValueError("location escapes its root: %r" % loc)
        if not os.path.isfile(path):
            raise ValueError("no such file: %r" % loc)
        if start is None:
            return path, None, None
        n = self.line_count(path)
        if start < 1 or end < start or end > n:
            raise ValueError("line range %d-%d out of range for %r (%d lines)" % (start, end, loc, n))
        return path, start, end


def resolve_location(audit_dir, loc):
    return LocationResolver(audit_dir)(loc)


_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:(?:\s+(.*)|\s*)$")
_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?$")


class _FrontmatterParser:
    def __init__(self, lines, first_lineno):
        self.lines = list(lines)
        self.first = first_lineno
        self.errors = []

    def lineno(self, i):
        return self.first + i

    def err(self, i, msg):
        self.errors.append({"line": self.lineno(i), "message": msg})

    @staticmethod
    def indent_of(text):
        return len(text) - len(text.lstrip(" "))

    def significant(self, i):
        while i < len(self.lines):
            s = self.lines[i].strip()
            if s and not s.startswith("#"):
                return i
            i += 1
        return i

    def parse(self):
        i = 0
        result = {}
        while True:
            i = self.significant(i)
            if i >= len(self.lines):
                break
            mapping, i2 = self.parse_mapping(i, 0)
            for k, v in mapping.items():
                result.setdefault(k, v)
            if i2 == i:
                self.err(i, "cannot parse line")
                i2 = i + 1
            i = i2
        return result

    def parse_mapping(self, i, indent, depth=0):
        out = {}
        while True:
            i = self.significant(i)
            if i >= len(self.lines):
                return out, i
            text = self.lines[i]
            if "\t" in text[: len(text) - len(text.lstrip())]:
                self.err(i, "tab in indentation")
                i += 1
                continue
            ind = self.indent_of(text)
            if ind < indent:
                return out, i
            if ind > indent:
                self.err(i, "unexpected indentation")
                i += 1
                continue
            content = text[ind:]
            if content == "-" or content.startswith("- "):
                if depth == 0 and indent == 0:
                    self.err(i, "list item outside of a key")
                    i += 1
                    continue
                return out, i
            m = _KEY_RE.match(content)
            if not m:
                self.err(i, "expected 'key: value'")
                i += 1
                continue
            key = m.group(1)
            value, i = self.parse_value(m.group(2) or "", i, indent, depth)
            if key in out:
                self.err(i - 1, "duplicate key %r" % key)
            else:
                out[key] = value

    def parse_list(self, i, indent, depth):
        items = []
        while True:
            i = self.significant(i)
            if i >= len(self.lines):
                return items, i
            text = self.lines[i]
            ind = self.indent_of(text)
            content = text[ind:]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                return items, i
            after = content[1:].lstrip(" ")
            offset = ind + (len(content) - len(after))
            if after and _KEY_RE.match(after) and not after.startswith(("'", '"')):
                self.lines[i] = " " * offset + after
                value, i = self.parse_mapping(i, offset, depth + 1)
            else:
                value, i = self.parse_value(after, i, indent, depth + 1, in_list=True)
            items.append(value)

    def parse_value(self, rest, i, parent_indent, depth, in_list=False):
        rest = rest.strip()
        if rest == "" or rest.startswith("#"):
            j = self.significant(i + 1)
            if j < len(self.lines):
                text = self.lines[j]
                ind = self.indent_of(text)
                content = text[ind:]
                is_item = content == "-" or content.startswith("- ")
                if ind > parent_indent or (ind == parent_indent and is_item and not in_list):
                    if is_item:
                        return self.parse_list(j, ind, depth)
                    if ind > parent_indent:
                        if depth >= 4:
                            self.err(j, "nesting too deep")
                        return self.parse_mapping(j, ind, depth + 1)
            return None, i + 1
        if rest[0] in "|>":
            return self.parse_block_scalar(rest, i, parent_indent)
        if rest[0] in "\"'":
            val, tail = self.parse_quoted(rest, i)
            if tail.strip() and not tail.strip().startswith("#"):
                self.err(i, "unexpected text after quoted string")
            return val, i + 1
        if rest[0] == "[":
            return self.parse_flow_list(rest, i), i + 1
        if rest[0] == "{":
            return self.parse_flow_map(rest, i), i + 1
        plain = _strip_comment(rest)
        j = i + 1
        parts = [plain]
        pending_blank = 0
        while j < len(self.lines):
            text = self.lines[j]
            if not text.strip():
                pending_blank += 1
                j += 1
                continue
            if self.indent_of(text) <= parent_indent:
                break
            stripped = _strip_comment(text.strip())
            parts.append(("\n" * pending_blank) if pending_blank else " ")
            parts.append(stripped)
            pending_blank = 0
            j += 1
        if len(parts) > 1:
            return "".join(parts).strip(), j - pending_blank
        return _convert_plain(plain), i + 1

    def parse_quoted(self, rest, i):
        q = rest[0]
        buf = []
        k = 1
        while k < len(rest):
            c = rest[k]
            if q == "'" and c == "'":
                if rest[k + 1: k + 2] == "'":
                    buf.append("'")
                    k += 2
                    continue
                return "".join(buf), rest[k + 1:]
            if q == '"' and c == "\\" and k + 1 < len(rest):
                e = rest[k + 1]
                if e == "u" and re.match(r"[0-9a-fA-F]{4}", rest[k + 2: k + 6] or ""):
                    buf.append(chr(int(rest[k + 2: k + 6], 16)))
                    k += 6
                    continue
                buf.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/"}.get(e, "\\" + e))
                k += 2
                continue
            if q == '"' and c == '"':
                return "".join(buf), rest[k + 1:]
            buf.append(c)
            k += 1
        self.err(i, "unterminated quoted string (multi-line quoted scalars are not supported)")
        return "".join(buf), ""

    def _split_flow(self, inner, i):
        items, buf, quote = [], [], None
        for c in inner:
            if quote:
                buf.append(c)
                if c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
                buf.append(c)
            elif c == ",":
                items.append("".join(buf).strip())
                buf = []
            elif c in "[]{}":
                self.err(i, "nested flow collections are not supported")
                buf.append(c)
            else:
                buf.append(c)
        if "".join(buf).strip():
            items.append("".join(buf).strip())
        return items

    def _flow_scalar(self, s, i):
        if s[:1] in ("'", '"'):
            return self.parse_quoted(s, i)[0]
        return _convert_plain(s)

    def parse_flow_list(self, rest, i):
        rest = _strip_comment(rest)
        if not rest.endswith("]"):
            self.err(i, "unterminated or multi-line flow list")
            return rest
        return [self._flow_scalar(x, i) for x in self._split_flow(rest[1:-1], i) if x != ""]

    def parse_flow_map(self, rest, i):
        rest = _strip_comment(rest)
        if not rest.endswith("}"):
            self.err(i, "unterminated or multi-line flow mapping")
            return rest
        out = {}
        for item in self._split_flow(rest[1:-1], i):
            if ":" not in item:
                self.err(i, "bad flow mapping item %r" % item)
                continue
            k, v = item.split(":", 1)
            out[self._flow_scalar(k.strip(), i)] = self._flow_scalar(v.strip(), i)
        return out

    def parse_block_scalar(self, header, i, parent_indent):
        m = re.match(r"^([|>])([1-9]?)([+-]?)([1-9]?)\s*(#.*)?$", header)
        if not m:
            self.err(i, "bad block scalar header %r" % header)
            return header, i + 1
        style = m.group(1)
        chomp = m.group(3)
        explicit = m.group(2) or m.group(4)
        j = i + 1
        raw = []
        content_indent = parent_indent + int(explicit) if explicit else None
        while j < len(self.lines):
            text = self.lines[j]
            if not text.strip():
                raw.append("")
                j += 1
                continue
            ind = self.indent_of(text)
            if content_indent is None:
                if ind <= parent_indent:
                    break
                content_indent = ind
            if ind < content_indent:
                break
            raw.append(text[content_indent:])
            j += 1
        trailing = 0
        while raw and raw[-1] == "":
            raw.pop()
            trailing += 1
        if style == "|":
            body = "\n".join(raw)
        else:
            body = raw[0] if raw else ""
            for k in range(1, len(raw)):
                prev, cur = raw[k - 1], raw[k]
                if cur == "":
                    body += "\n"
                    continue
                if prev == "":
                    pass
                elif prev[:1] in (" ", "\t") or cur[:1] in (" ", "\t"):
                    body += "\n"
                else:
                    body += " "
                body += cur
        if chomp == "-":
            value = body
        elif chomp == "+":
            value = body + "\n" * (1 + trailing) if raw else "\n" * trailing
        else:
            value = body + "\n" if raw else ""
        return value, j


def _strip_comment(s):
    m = re.search(r"\s#", s)
    return (s[: m.start()] if m else s).rstrip()


def _convert_plain(s):
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(s):
        try:
            return int(s)
        except ValueError:
            return s
    if _FLOAT_RE.match(s):
        try:
            return float(s)
        except ValueError:
            return s
    return s


def parse_frontmatter(text):
    lines = split_lines(text)
    out = {"present": False, "line_start": None, "line_end": None, "raw": None, "parsed": {}, "errors": []}
    if not lines or lines[0].rstrip() != "---":
        return out
    out["present"] = True
    out["line_start"] = 1
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() in ("---", "..."):
            end = idx
            break
    if end is None:
        out["errors"].append({"line": 1, "message": "frontmatter is not closed with '---'"})
        return out
    out["line_end"] = end + 1
    body = lines[1:end]
    out["raw"] = "\n".join(body)
    try:
        parser = _FrontmatterParser(body, 2)
        out["parsed"] = parser.parse()
        out["errors"].extend(parser.errors)
    except Exception as e:  # the parser must never take the inventory down
        out["errors"].append({"line": 1, "message": "parser failure: %s" % e})
    out["errors"].sort(key=lambda e: (e["line"], e["message"]))
    return out


FILE_EXTS = {
    "md", "markdown", "txt", "rst", "py", "sh", "bash", "zsh", "fish", "ps1", "js", "mjs", "cjs", "ts", "tsx", "jsx",
    "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "csv", "tsv", "xml", "html", "htm", "css", "scss",
    "sql", "rb", "go", "rs", "java", "kt", "swift", "c", "h", "cc", "cpp", "hpp", "lua", "pl", "php", "r",
    "ipynb", "pdf", "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "docx", "xlsx", "pptx", "zip", "tar", "gz",
    "tmpl", "tpl", "j2", "jinja", "mustache", "hbs", "env", "lock", "dockerfile", "mdx", "proto", "graphql", "wasm",
}
LINE_SCAN_MAX = 4000
URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s<>\"'`)\]]+")
MD_LINK_RE = re.compile(r"!?\[[^\[\]\n]*\]\(\s*<?([^)\s>\[]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
INVOKE_RE = re.compile(
    r"(?=(?<![\w./-])(?:python3?|bash|sh|zsh|node|deno run|bun|ruby|perl|uv run|npx|pwsh|source|\.)\s+"
    r"(?:-[\w-]+(?:=\S+)?\s+){0,8}([^\s`'\"|;&<>()]+))"
)
INCLUDE_RE = re.compile(r"(?:^|(?<=[\s(\[]))@([\w.~/-]+)")
BARE_RE = re.compile(r"(?<![\w/.@`~$-])((?:\.{1,2}/)?[\w.-]+(?:/[\w.-]+)+)")
BARE_NAME_RE = re.compile(
    r"(?<![\w/.@`~$<-])([\w-][\w.-]*\.(?:%s))(?![\w/.>-])" % "|".join(sorted(FILE_EXTS, key=len, reverse=True)), re.I)
LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
REF_KIND_ORDER = ["markdown_link", "invocation", "include", "backtick", "bare_path"]

MD_EXTS = (".md", ".markdown", ".mdx")


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


def clean_target(raw, top_dirs):
    t = raw.strip().strip("\"'")
    t = t.rstrip(".,;:)!?]")
    t = LINE_SUFFIX_RE.sub("", t)
    t = t.split("#", 1)[0].split("?", 1)[0]
    if not t or URL_RE.match(t) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t):
        return None
    if t.startswith(("/", "~", "$", "-", "@")) or re.search(r"[<>{}$*?|\\\[\]=]", t):
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


EXTERNAL_MAX_FILES = 200
PLUGIN_DIR = ".claude-plugin"
PLUGIN_ROOT_VARS = ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT")
PLUGIN_FIELD_MAX = 500


def is_script_path(path, executable, shebang):
    return is_script_file(path, executable, shebang) or path.split("/", 1)[0] in ("scripts", "bin")


def scan_lines(lines):
    long_lines = [i for i, ln in enumerate(lines, 1) if len(ln) > LINE_SCAN_MAX]
    return ([ln[:LINE_SCAN_MAX] for ln in lines] if long_lines else lines), long_lines


def refs_of(rel, lines, top_dirs):
    fenced = code_fence_map(lines)[1] if rel.lower().endswith(MD_EXTS) else frozenset()
    return extract_refs(rel, scan_lines(lines)[0], top_dirs, fenced)


def collect_refs(paths, dirs, texts):
    file_set = set(paths)
    top_dirs = {d.split("/", 1)[0] for d in dirs}
    lower_map, base_map = {}, {}
    for p in paths:
        lower_map.setdefault(p.lower(), []).append(p)
        base_map.setdefault(posixpath.basename(p).lower(), []).append(p)
    refs = []
    for rel in sorted(texts):
        for ref in refs_of(rel, texts[rel], top_dirs):
            ref["resolved"], ref["exists"], ref["resolution"] = resolve_ref(ref, file_set, dirs, lower_map, base_map)
            if ref["kind"] == "bare_path" and "/" not in ref["target"] and not ref["exists"]:
                continue
            refs.append(ref)
    return refs


def in_dir(path, sub):
    return sub == "." or path == sub or path.startswith(sub + "/")


def safe_rel(path):
    p = posixpath.normpath(path)
    return None if p in (".", "..") or p.startswith(("../", "/")) else p


def source_lines(root, rel):
    parts = rel.split("/")
    if SKIP_DIRS.intersection(parts) or parts[-1].lower().startswith(".env"):
        return None
    cur, st = root, None
    for part in parts:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return None
    if st is None or not stat.S_ISREG(st.st_mode):
        return None
    if st.st_size > TEXT_MAX_BYTES:
        return []
    try:
        fd = os.open(cur, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = decode_text(f.read(TEXT_MAX_BYTES + 1))
    return [] if text is None else split_lines(text)


def _targets(target, bases):
    out = []
    for base in bases:
        p = safe_rel(posixpath.join(base, target))
        if p and p not in out:
            out.append(p)
    return out


def reference_closure(root, skill_sub, skill_files, skill_dirs, skill_refs, skill_seeds=(), plugin_files=(),
                      limit=EXTERNAL_MAX_FILES):
    cache = {}

    def lines_of(p):
        if p not in cache:
            cache[p] = source_lines(root, p)
        return cache[p]

    for ref in skill_refs:
        ref["scope"] = "skill"
        if ref["exists"] or ref["target"].endswith("/"):
            continue
        from_src = safe_rel(posixpath.join(skill_sub, ref["from"]))
        for p in _targets(ref["target"], [posixpath.dirname(from_src), skill_sub]):
            if not in_dir(p, skill_sub) and lines_of(p) is not None:
                ref.update(resolved="@repo/" + p, exists=True, resolution="external", scope="external")
                break

    files, dirs = set(skill_files), set(skill_dirs)
    under = {}
    for p in skill_files:
        parts = p.split("/")
        for i in range(1, len(parts)):
            under.setdefault("/".join(parts[:i]), []).append(p)
    by_from = {}
    for ref in skill_refs:
        if ref["exists"]:
            by_from.setdefault(ref["from"], []).append(ref)

    external, external_refs, reachable, visited = {}, [], set(), set()
    state = {"truncated": False}

    def add_external(p, reason):
        if p in external:
            return True
        if len(external) >= limit:
            state["truncated"] = True
            return False
        external[p] = reason
        return True

    def skill_rel(p):
        return p if skill_sub == "." else p[len(skill_sub) + 1:]

    def walk(starts, mark):
        queue = deque(starts)
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            kind, cur = node
            if kind == "skill":
                for ref in by_from.get(cur, []):
                    if ref["scope"] == "external":
                        p = ref["resolved"][len("@repo/"):]
                        if add_external(p, "referenced_from:%s:%d" % (posixpath.join(skill_sub, cur) if skill_sub != "." else cur, ref["line"])):
                            queue.append(("ext", p))
                        continue
                    for h in [ref["resolved"]] if ref["resolved"] in files else under.get(ref["resolved"], []):
                        if mark:
                            reachable.add(h)
                        queue.append(("skill", h))
                continue
            for ref in refs_of("@repo/" + cur, lines_of(cur) or [], frozenset()):
                if ref["target"].endswith("/"):
                    continue
                for p in _targets(ref["target"], [posixpath.dirname(cur), ""]):
                    if in_dir(p, skill_sub):
                        s = skill_rel(p)
                        if s not in files and s not in dirs:
                            continue
                        hits = [s] if s in files else under.get(s, [])
                        for h in hits:
                            if mark:
                                reachable.add(h)
                            queue.append(("skill", h))
                        resolved = s
                    elif lines_of(p) is not None:
                        if not add_external(p, "referenced_from:%s:%d" % (cur, ref["line"])):
                            break
                        queue.append(("ext", p))
                        resolved = "@repo/" + p
                    else:
                        continue
                    external_refs.append(dict(ref, resolved=resolved, exists=True, resolution="external", scope="external"))
                    break

    for p, reason in plugin_files:
        if not in_dir(p, skill_sub) and lines_of(p) is not None:
            add_external(p, reason)
    walk([("skill", "SKILL.md")] if "SKILL.md" in files else [], True)
    walk([("skill", p) for p in skill_seeds if p in files]
         + [("ext", p) for p, _ in plugin_files if p in external], False)
    external_refs = [r for r in skill_refs if r["scope"] == "external" and r["resolved"][len("@repo/"):] in external] + external_refs
    return {"reachable": reachable, "external": external, "external_refs": external_refs, "truncated": state["truncated"]}


def find_plugin_root(root, skill_sub):
    parts = [] if skill_sub == "." else skill_sub.split("/")
    for i in range(len(parts), -1, -1):
        rel = "/".join(parts[:i]) or "."
        d = os.path.join(root, rel, PLUGIN_DIR)
        if os.path.isdir(d) and not os.path.islink(d):
            return rel
    return None


def _short(v):
    if not isinstance(v, str):
        return None
    return v if len(v) <= PLUGIN_FIELD_MAX else v[: PLUGIN_FIELD_MAX - 1] + "…"


def _hook_entries(obj, source):
    events = obj.get("hooks") if isinstance(obj.get("hooks"), dict) else obj
    out = []
    for event, groups in sorted(events.items()) if isinstance(events, dict) else []:
        for g in groups if isinstance(groups, list) else []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") if isinstance(g.get("hooks"), list) else []:
                if isinstance(h, dict):
                    out.append({"event": _short(event), "matcher": _short(g.get("matcher")), "type": _short(h.get("type")),
                                "command": _short(h.get("command")), "source": "@repo/" + source})
    return out


def _mcp_entries(obj, source):
    servers = obj.get("mcpServers") if isinstance(obj.get("mcpServers"), dict) else obj
    out = []
    for name, cfg in sorted(servers.items()) if isinstance(servers, dict) else []:
        if not isinstance(cfg, dict) or not any(k in cfg for k in ("command", "url", "type")):
            continue
        args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        out.append({"name": _short(name), "type": _short(cfg.get("type")), "command": _short(cfg.get("command")),
                    "args": [_short(a) for a in args[:50] if isinstance(a, str)], "url": _short(cfg.get("url")),
                    "env_keys": sorted(_short(k) for k in env if isinstance(k, str)), "source": "@repo/" + source})
    return out


def _command_targets(text):
    for var in PLUGIN_ROOT_VARS:
        text = text.replace(var, ".")
    return [r["target"] for r in extract_refs("", [text[:LINE_SCAN_MAX]], frozenset())]


def plugin_scan(root, prel):
    files, manifests, hooks, servers = [], [], [], []

    def rel(*parts):
        return safe_rel(posixpath.join(prel, *parts))

    def add(p, reason):
        if not p or source_lines(root, p) is None:
            return False
        if p not in (f for f, _ in files):
            files.append((p, reason))
        return True

    def load(p):
        lines = source_lines(root, p) if p else None
        try:
            return json.loads("\n".join(lines)) if lines else None
        except (ValueError, RecursionError):
            return None

    mdir = os.path.join(root, prel, PLUGIN_DIR)
    for name in sorted(os.listdir(mdir)):
        if name.endswith(".json") and add(rel(PLUGIN_DIR, name), "plugin_manifest"):
            manifests.append("@repo/" + rel(PLUGIN_DIR, name))
    hook_files, mcp_files = [rel("hooks", "hooks.json")], [rel(".mcp.json")]
    manifest_path = rel(PLUGIN_DIR, "plugin.json")
    manifest = load(manifest_path)
    if isinstance(manifest, dict):
        for key, paths, parse in (("hooks", hook_files, _hook_entries), ("mcpServers", mcp_files, _mcp_entries)):
            value = manifest.get(key)
            for v in value if isinstance(value, list) else [value]:
                if isinstance(v, str):
                    paths.append(rel(v))
                elif isinstance(v, dict):
                    (hooks if key == "hooks" else servers).extend(parse(v, manifest_path))
    for paths, reason, parse, sink in ((hook_files, "plugin_hooks", _hook_entries, hooks), (mcp_files, "plugin_mcp", _mcp_entries, servers)):
        for p in paths:
            if p not in (f for f, _ in files) and add(p, reason):
                data = load(p)
                if isinstance(data, dict):
                    sink.extend(parse(data, p))
    for entries, reason in ((hooks, "plugin_hooks"), (servers, "plugin_mcp")):
        for e in entries:
            text = " ".join(x for x in [e.get("command")] + e.get("args", []) if x)
            for t in _command_targets(text):
                add(rel(t), reason)
    return {"root": prel, "files": files, "manifests": manifests, "hooks": hooks, "mcp_servers": servers}


def read_tree(root):
    paths, dirs = walk_files(root)
    texts, meta = {}, {}
    for p in paths:
        full = os.path.join(root, p)
        st = os.lstat(full)
        lines = None
        if stat.S_ISREG(st.st_mode):
            text = read_text(full)
            lines = split_lines(text) if text is not None else None
        if lines is not None:
            texts[p] = lines
        meta[p] = (bool(st.st_mode & 0o111) and stat.S_ISREG(st.st_mode), bool(lines and lines[0].startswith("#!")))
    return paths, dirs, texts, meta


def is_review_seed(path, executable, shebang):
    return path == "SKILL.md" or ("/" not in path and path.upper().startswith("README")) or is_script_path(path, executable, shebang)


def skill_externals(root, skill_sub, skill_dir, limit=EXTERNAL_MAX_FILES):
    paths, dirs, texts, meta = read_tree(skill_dir)
    refs = collect_refs(paths, dirs, texts)
    prel = find_plugin_root(root, skill_sub)
    plugin = plugin_scan(root, prel) if prel else None
    seeds = [p for p in paths if is_review_seed(p, *meta[p])]
    closure = reference_closure(root, skill_sub, paths, dirs, refs, seeds, plugin["files"] if plugin else (), limit)
    return closure, plugin
