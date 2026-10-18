import hashlib
import json
import os
import re
import stat

PROVENANCE_SCHEMA = "slop-skill-auditor/provenance@1"
INVENTORY_SCHEMA = "slop-skill-auditor/inventory@1"
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
TEXT_MAX_BYTES = 5 * 1024 * 1024

_LOC_RE = re.compile(r"^(.+?):(\d+)(?:-(\d+))?$")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def write_json(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_json(data))
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


def read_text(path):
    """Return decoded text, or None if the file looks binary or is too large."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_size > TEXT_MAX_BYTES:
        return None
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("﻿") else text


def _within(base, path):
    base = os.path.realpath(base)
    real = os.path.realpath(path)
    return real == base or real.startswith(base + os.sep)


def audit_roots(audit_dir):
    """Return (skill_root, source_root, docs_root) as absolute paths for an audit dir."""
    audit_dir = os.path.abspath(audit_dir)
    prov_path = os.path.join(audit_dir, "provenance.json")
    if not os.path.isfile(prov_path):
        raise ValueError("no provenance.json in %s" % audit_dir)
    prov = load_json(prov_path)
    if not prov.get("source_root") or prov.get("skill_subpath") is None:
        raise ValueError("provenance.json has no resolved skill (ambiguous candidates?)")
    source_root = os.path.normpath(os.path.join(audit_dir, prov["source_root"]))
    skill_root = os.path.normpath(os.path.join(source_root, prov["skill_subpath"]))
    return skill_root, source_root, os.path.join(audit_dir, "docs")


def resolve_location(audit_dir, loc):
    """Resolve `path:LINE`, `path:START-END` or bare `path` (skill-root relative,
    `@repo/` for source root, `@docs/` for user docs) to (abs_path, start, end).
    A bare path returns (abs_path, None, None)."""
    if not isinstance(loc, str) or not loc.strip():
        raise ValueError("empty location")
    loc = loc.strip()
    m = _LOC_RE.match(loc)
    if m:
        rel, start, end = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
    else:
        rel, start, end = loc, None, None
    skill_root, source_root, docs_root = audit_roots(audit_dir)
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
    n, last = 0, b"\n"
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            n += chunk.count(b"\n")
            last = chunk[-1:]
    n += last != b"\n"
    if start < 1 or end < start or end > n:
        raise ValueError("line range %d-%d out of range for %r (%d lines)" % (start, end, loc, n))
    return path, start, end


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
