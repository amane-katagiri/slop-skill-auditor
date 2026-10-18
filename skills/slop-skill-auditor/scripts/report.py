#!/usr/bin/env python3
import argparse
import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import LOC_RE, LocationResolver, load_json, write_json  # noqa: E402

SCHEMA = "slop-skill-auditor/audit@1"

CATEGORIES = ["instruction", "knowledge", "procedure", "tool", "algorithm", "environment"]
SUBSTITUTABILITY = ["base_model", "short_instruction", "unique_value", "external_capability", "undetermined"]
IMPLEMENTATION = ["confirmed", "partial", "missing", "contradicted", "not_applicable", "unknown"]
EFFECT = ["experimentally_supported", "not_supported_in_experiment", "unverified", "not_assessable"]
CONSISTENCY = ["consistent", "possibly_exaggerated", "inconsistent", "not_assessable"]
CONFIDENCE = ["high", "medium", "low"]
AXES = {
    "implementation": IMPLEMENTATION,
    "effect": EFFECT,
    "consistency": CONSISTENCY,
    "unique_value": SUBSTITUTABILITY,
    "confidence": CONFIDENCE,
}
BASES = ["implementation_confirmed", "inference", "experiment_observed", "unconfirmed"]
CONCLUSIONS = [
    "clear_unique_value",
    "unique_value_unverified",
    "likely_replaceable_by_short_prompt",
    "claims_exceed_evidence",
    "insufficient_information",
    "possible_negative_effect",
]
MINIMAL_PROMPT_MAX_CHARS = 600

LABELS = {
    "ja": {
        "instruction": "指示", "knowledge": "知識", "procedure": "手順",
        "tool": "ツール", "algorithm": "アルゴリズム", "environment": "環境",
        "base_model": "素のモデルで足りそう",
        "short_instruction": "短い指示で足りそう",
        "unique_value": "スキル固有の価値あり",
        "external_capability": "外部の仕組みが必要",
        "undetermined": "判断できない",
        "implementation.confirmed": "実装を確認できた",
        "implementation.partial": "実装は一部だけ",
        "implementation.missing": "実装が見つからない",
        "implementation.contradicted": "実装が説明と食い違う",
        "implementation.not_applicable": "実装で確かめる種類の主張ではない",
        "implementation.unknown": "実装を確かめられなかった",
        "effect.experimentally_supported": "実験で裏付けられた",
        "effect.not_supported_in_experiment": "実験では裏付けられなかった",
        "effect.unverified": "測定はない",
        "effect.not_assessable": "確かめようがない",
        "consistency.consistent": "実装と根拠に見合っている",
        "consistency.possibly_exaggerated": "実装と根拠に比べてやや強い",
        "consistency.inconsistent": "実装や根拠と合わない",
        "consistency.not_assessable": "判断できない",
        "confidence.high": "高", "confidence.medium": "中", "confidence.low": "低",
        "implementation_confirmed": "実装で確認",
        "inference": "推測",
        "experiment_observed": "実験で観測",
        "unconfirmed": "未確認",
        "clear_unique_value": "導入する価値がはっきりしている",
        "unique_value_unverified": "独自の価値はあるが効果は未確認",
        "likely_replaceable_by_short_prompt": "簡単な指示で代わりになる可能性が高い",
        "claims_exceed_evidence": "宣伝に対して実装や根拠が足りない",
        "insufficient_information": "判断材料が足りない",
        "possible_negative_effect": "悪影響や過剰な制約の可能性",
        "kind.github_repo": "GitHubのリポジトリ", "kind.github_tree": "GitHubのフォルダURL",
        "kind.github_blob": "GitHubのファイルURL", "kind.local_dir": "手元のフォルダ", "kind.local_skill_md": "手元のSKILL.md",
        "dirty.True": "あり", "dirty.False": "なし",
    },
    "en": {
        "instruction": "Instruction", "knowledge": "Knowledge", "procedure": "Procedure",
        "tool": "Tool", "algorithm": "Algorithm", "environment": "Environment",
        "base_model": "The base model is likely enough",
        "short_instruction": "A short instruction is likely enough",
        "unique_value": "Has skill-specific value",
        "external_capability": "Needs an external capability",
        "undetermined": "Cannot tell",
        "implementation.confirmed": "implementation found",
        "implementation.partial": "only partly implemented",
        "implementation.missing": "no implementation found",
        "implementation.contradicted": "the implementation contradicts the description",
        "implementation.not_applicable": "not something code can show",
        "implementation.unknown": "could not be checked",
        "effect.experimentally_supported": "supported by an experiment",
        "effect.not_supported_in_experiment": "not supported by an experiment",
        "effect.unverified": "not measured",
        "effect.not_assessable": "cannot be checked",
        "consistency.consistent": "in line with the implementation and evidence",
        "consistency.possibly_exaggerated": "somewhat stronger than the implementation and evidence",
        "consistency.inconsistent": "does not match the implementation or evidence",
        "consistency.not_assessable": "cannot be judged",
        "confidence.high": "high", "confidence.medium": "medium", "confidence.low": "low",
        "implementation_confirmed": "confirmed in code",
        "inference": "inference",
        "experiment_observed": "observed in an experiment",
        "unconfirmed": "unconfirmed",
        "clear_unique_value": "Clearly worth installing",
        "unique_value_unverified": "Has its own value, but the effect is unconfirmed",
        "likely_replaceable_by_short_prompt": "A simple instruction can likely replace it",
        "claims_exceed_evidence": "The claims go beyond the implementation and evidence",
        "insufficient_information": "Not enough information to judge",
        "possible_negative_effect": "May do harm or over-constrain",
        "kind.github_repo": "GitHub repository", "kind.github_tree": "GitHub folder URL",
        "kind.github_blob": "GitHub file URL", "kind.local_dir": "local folder", "kind.local_skill_md": "local SKILL.md",
        "dirty.True": "yes", "dirty.False": "no",
    },
}

HEADINGS = {
    "ja": {
        "title": "スキル監査レポート", "conclusion": "結論", "summary": "要約",
        "purpose": "目的", "actual_behavior": "実際にしていること",
        "unique_capabilities": "独自の実装や外部の仕組み", "expected_value": "期待できる効果",
        "claim_consistency": "宣伝との一致", "open_questions": "まだ分からないこと",
        "behavior": "このスキルがしていること", "activation": "いつ使われるか",
        "claims": "宣伝の確認", "necessity": "スキルなしで足りるか",
        "base_model": "素のモデルでできそうなこと", "short_instruction": "短い指示で足りそうなこと",
        "unique_value": "スキル固有の価値", "redundant": "不要・重複していそうな部分", "unconfirmed": "確かめられていないこと",
        "minimal_prompt": "比較用の短い指示", "minimal_prompt_intro": "目的だけを普通に頼んだ場合の指示。実験ではこれをスキルと比べる。",
        "dropped": "この指示では再現できないこと",
        "experiments": "実験", "not_run": "対照実験はしていない。実験で確かめるとよい点:",
        "results": "結果", "not_embedded": "監査フォルダの外なので埋め込まない",
        "concerns": "気になった点", "conditions_limits": "監査の条件と限界", "conditions": "条件",
        "limitations": "限界", "not_reviewed": "読んでいないファイル",
        "col_feature": "機能", "col_subst": "スキルなしで足りるか", "col_confidence": "確かさ",
        "axis_implementation": "仕組み", "axis_effect": "効果", "axis_consistency": "宣伝の強さ",
        "axis_unique_value": "代わりになるか", "axis_confidence": "確かさ",
        "tally": "付録: 判定の集計", "axis": "観点", "value": "判定", "count": "件数",
        "categories": "種類", "external": "使う外部の仕組み", "effect": "期待される効果", "rationale": "判断の理由",
        "none": "なし", "source": "出典", "mechanism": "説明されている仕組み", "claimed_effect": "主張している効果",
        "impl_note": "実装との照合", "exp_note": "実験", "notes": "補足", "evidence": "根拠",
        "task_idea": "方法の案", "target_claims": "対象の宣伝",
        "input": "入力", "kind": "取得方法", "repo": "リポジトリ", "commit": "commit", "dirty": "未コミットの変更",
        "skill_subpath": "スキルの場所", "fetched_at": "取得日時", "fingerprint": "ファイルの指紋",
        "model": "監査したモデル", "runtime": "実行環境", "date": "監査日",
        "sep": "、", "end": "。", "lines": "{}行", "colon": ": ", "q": "「{}」",
    },
    "en": {
        "title": "Skill Audit Report", "conclusion": "Conclusion", "summary": "Summary",
        "purpose": "Purpose", "actual_behavior": "What it actually does",
        "unique_capabilities": "Own implementation and external capabilities", "expected_value": "Expected benefit",
        "claim_consistency": "Match with its claims", "open_questions": "Still unknown",
        "behavior": "What the skill does", "activation": "When it is used",
        "claims": "Checking the claims", "necessity": "Is the skill needed?",
        "base_model": "Likely doable by the base model", "short_instruction": "Likely covered by a short instruction",
        "unique_value": "Skill-specific value", "redundant": "Likely unnecessary or duplicated", "unconfirmed": "Not confirmed",
        "minimal_prompt": "Short instruction for comparison",
        "minimal_prompt_intro": "What a user would write to ask for the same goal. Experiments compare the skill against this.",
        "dropped": "What this instruction cannot reproduce",
        "experiments": "Experiments", "not_run": "No controlled experiment was run. Points worth testing:",
        "results": "Results", "not_embedded": "outside the audit directory; not embedded",
        "concerns": "Concerns", "conditions_limits": "Audit conditions and limits", "conditions": "Conditions",
        "limitations": "Limits", "not_reviewed": "Files not read",
        "col_feature": "Feature", "col_subst": "Needed beyond the model?", "col_confidence": "Confidence",
        "axis_implementation": "Mechanism", "axis_effect": "Effect", "axis_consistency": "Strength of the claim",
        "axis_unique_value": "Replaceable?", "axis_confidence": "Confidence",
        "tally": "Appendix: tally of ratings", "axis": "Aspect", "value": "Rating", "count": "Count",
        "categories": "Kind", "external": "External capabilities", "effect": "Expected effect", "rationale": "Why",
        "none": "none", "source": "Source", "mechanism": "Stated mechanism", "claimed_effect": "Claimed effect",
        "impl_note": "Against the implementation", "exp_note": "Experiment", "notes": "Notes", "evidence": "Evidence",
        "task_idea": "How", "target_claims": "Claims",
        "input": "Input", "kind": "Fetched as", "repo": "Repository", "commit": "Commit", "dirty": "Uncommitted changes",
        "skill_subpath": "Skill location", "fetched_at": "Fetched at", "fingerprint": "File fingerprint",
        "model": "Audited by model", "runtime": "Runtime", "date": "Audit date",
        "sep": "; ", "end": ".", "lines": "lines {}", "colon": ": ", "q": "“{}”",
    },
}


class Findings:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")


def _load_optional(path):
    return load_json(path) if path.exists() else None


def _check_loc(resolve, loc, where, f):
    if not isinstance(loc, str) or not loc:
        f.error(where, f"location must be a non-empty string, got {loc!r}")
        return
    try:
        resolve(loc)
    except ValueError as e:
        f.error(where, f"bad location {loc!r}: {e}")


def _purpose_source(loc, frontmatter):
    if not isinstance(loc, str) or ":" not in loc:
        return False
    path, rng = loc.rsplit(":", 1)
    top = path.removeprefix("@repo/")
    if path.startswith("@docs/") or ("/" not in top and top.upper().startswith("README")):
        return True
    if path != "SKILL.md" or not frontmatter.get("present"):
        return False
    start, _, end = rng.partition("-")
    try:
        return int(end or start) <= int(frontmatter.get("line_end", 0))
    except ValueError:
        return False


def _inside(audit_dir, path):
    return path.resolve().is_relative_to(audit_dir.resolve())


def _enum(value, allowed, where, f):
    if value not in allowed:
        f.error(where, f"{value!r} is not one of {allowed}")


def _text(obj, key, where, f, required=True):
    v = obj.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            f.error(where, f"missing text field {key!r}")
        return ""
    if not isinstance(v, str):
        f.error(where, f"{key!r} must be a string")
        return ""
    return v


def _list(obj, key, where, f):
    v = obj.get(key, [])
    if v is None:
        return []
    if not isinstance(v, list):
        f.error(where, f"{key!r} must be a list")
        return []
    return v


def _obj(value, where, f):
    if not value:
        return {}
    if not isinstance(value, dict):
        f.error(where, "must be an object")
        return {}
    return value


def _objects(items, where, f):
    for i, item in enumerate(items):
        if isinstance(item, dict):
            yield i, item
        else:
            f.error(f"{where}[{i}]", "must be an object")


def check(audit_dir):
    f = Findings()
    path = audit_dir / "audit.json"
    if not path.exists():
        f.error("audit.json", "not found")
        return f
    try:
        a = load_json(path)
    except json.JSONDecodeError as e:
        f.error("audit.json", f"invalid JSON: {e}")
        return f
    if not isinstance(a, dict):
        f.error("audit.json", "top level must be an object")
        return f
    resolve = LocationResolver(audit_dir)
    inventory = _load_optional(audit_dir / "inventory.json")
    if inventory is None:
        f.warn("inventory.json", "not found; run inventory.py first so the report records the fingerprint")

    if a.get("schema") != SCHEMA:
        f.error("schema", f"expected {SCHEMA!r}")

    summary = _obj(a.get("summary"), "summary", f)
    for key in ("purpose", "actual_behavior", "unique_capabilities", "expected_value", "claim_consistency"):
        _text(summary, key, "summary", f)

    activation = _obj(a.get("activation"), "activation", f)
    _text(activation, "description", "activation", f)
    for loc in _list(activation, "evidence", "activation", f):
        _check_loc(resolve, loc, "activation.evidence", f)

    features = _list(a, "features", "features", f)
    if not features:
        f.error("features", "at least one feature is required")
    seen = set()
    for i, feat in _objects(features, "features", f):
        where = f"features[{feat.get('id', i)}]"
        fid = feat.get("id")
        if not fid or fid in seen:
            f.error(where, "missing or duplicate id")
        seen.add(fid)
        _text(feat, "name", where, f)
        _text(feat, "actual_processing", where, f)
        cats = _list(feat, "categories", where, f)
        if not cats:
            f.error(where, "categories is empty")
        for c in cats:
            _enum(c, CATEGORIES, where + ".categories", f)
        _enum(feat.get("substitutability"), SUBSTITUTABILITY, where + ".substitutability", f)
        _enum(feat.get("basis"), BASES, where + ".basis", f)
        _enum(feat.get("confidence"), CONFIDENCE, where + ".confidence", f)
        locs = _list(feat, "locations", where, f)
        for loc in locs:
            _check_loc(resolve, loc, where + ".locations", f)
        if not locs and feat.get("basis") != "unconfirmed":
            f.error(where, "a feature without locations cannot be rated as implemented; add locations or set basis to 'unconfirmed'")

    experiments = _obj(a.get("experiments"), "experiments", f)
    status = experiments.get("status")
    _enum(status, ["not_run", "run"], "experiments.status", f)
    runs = _list(experiments, "runs", "experiments", f)
    run_ids = set()
    for i, run in _objects(runs, "experiments.runs", f):
        where = f"experiments.runs[{i}]"
        rid = run.get("id")
        rf = run.get("results_file")
        if not rid or not rf:
            f.error(where, "id and results_file are required")
            continue
        if not isinstance(rf, str) or not _inside(audit_dir, audit_dir / rf):
            f.error(where, f"results_file {rf!r} must be a path inside the audit directory")
            continue
        if not (audit_dir / rf).is_file():
            f.error(where, f"results_file {rf!r} is not a file")
            continue
        run_ids.add(rid)
    if status == "not_run" and runs:
        f.error("experiments", "status is 'not_run' but runs are listed")
    if status == "run" and not run_ids:
        f.error("experiments", "status is 'run' but no run has an existing results_file")

    claims = _list(a, "claims", "claims", f)
    seen = set()
    for i, cl in _objects(claims, "claims", f):
        where = f"claims[{cl.get('id', i)}]"
        cid = cl.get("id")
        if not cid or cid in seen:
            f.error(where, "missing or duplicate id")
        seen.add(cid)
        _text(cl, "quote", where, f)
        _text(cl, "claimed_effect", where, f)
        _check_loc(resolve, cl.get("source"), where + ".source", f)
        assessment = _obj(cl.get("assessment"), where + ".assessment", f)
        for axis, allowed in AXES.items():
            _enum(assessment.get(axis), allowed, f"{where}.assessment.{axis}", f)
        _enum(cl.get("unique_value_basis"), BASES, where + ".unique_value_basis", f)
        impl = _list(cl, "implementation_evidence", where, f)
        for loc in impl:
            _check_loc(resolve, loc, where + ".implementation_evidence", f)
        implementation = assessment.get("implementation")
        if implementation in ("confirmed", "partial", "missing", "contradicted") and not impl:
            f.error(where, f"implementation {implementation!r} needs implementation_evidence (for 'missing', cite the line that references the absent part)")
        ext = _list(cl, "experimental_evidence_locations", where, f)
        for loc in ext:
            _check_loc(resolve, loc, where + ".experimental_evidence_locations", f)
        ref = cl.get("experiment_ref")
        if ref is not None and status != "run":
            f.error(where, "experiment_ref is set but experiments.status is not 'run'")
        if ref is not None and ref not in run_ids:
            f.error(where, f"experiment_ref {ref!r} does not match an experiment run with results")
        effect = assessment.get("effect")
        if effect == "experimentally_supported" and ref not in run_ids and not ext:
            f.error(where, "effect 'experimentally_supported' needs an experiment_ref with results or published-data locations")
        if effect == "not_supported_in_experiment" and ref not in run_ids:
            f.error(where, "effect 'not_supported_in_experiment' needs an experiment_ref with results")
        if assessment.get("consistency") == "inconsistent" and implementation == "confirmed" and effect != "not_supported_in_experiment":
            f.warn(where, "consistency 'inconsistent' while the implementation is confirmed; name the concrete contradiction in implementation_note")
        if assessment.get("confidence") == "high" and cl.get("unique_value_basis") == "inference" and assessment.get("unique_value") in ("base_model", "short_instruction"):
            f.warn(where, "high confidence that the claim is replaceable rests only on inference; consider 'medium' or an experiment")

    for kind, items, key in (("features", features, "basis"), ("claims", claims, "unique_value_basis")):
        for item in items:
            if isinstance(item, dict) and item.get(key) == "experiment_observed" and not run_ids:
                f.error(f"{kind}[{item.get('id')}]", f"{key} 'experiment_observed' without any experiment results")

    necessity = _obj(a.get("necessity"), "necessity", f)
    for key in ("base_model", "short_instruction", "unique_value", "redundant", "unconfirmed"):
        _list(necessity, key, "necessity", f)

    mp = _obj(a.get("minimal_prompt"), "minimal_prompt", f)
    text = _text(mp, "text", "minimal_prompt", f)
    if len(text) > MINIMAL_PROMPT_MAX_CHARS:
        f.warn("minimal_prompt", f"text is {len(text)} chars; a minimal prompt should be a few lines a user would naturally write")
    if not _list(mp, "dropped", "minimal_prompt", f):
        f.warn("minimal_prompt", "'dropped' is empty; list what the minimal prompt cannot reproduce (or state that nothing is lost)")
    derived = _list(mp, "derived_from", "minimal_prompt", f)
    if not derived:
        f.error("minimal_prompt", "derived_from is empty; cite the README / description lines the purpose was taken from")
    fm = (inventory or {}).get("frontmatter") or {}
    for loc in derived:
        _check_loc(resolve, loc, "minimal_prompt.derived_from", f)
        if not _purpose_source(loc, fm):
            f.error("minimal_prompt.derived_from", f"{loc!r} is not a README, user doc, or SKILL.md frontmatter line; the minimal prompt must not be distilled from the skill body")

    for i, note in _objects(_list(a, "security_notes", "security_notes", f), "security_notes", f):
        if note.get("location"):
            _check_loc(resolve, note["location"], f"security_notes[{i}]", f)

    conclusion = _obj(a.get("conclusion"), "conclusion", f)
    verdicts = _list(conclusion, "verdicts", "conclusion", f)
    if not verdicts:
        f.error("conclusion", "verdicts is empty")
    for v in verdicts:
        _enum(v, CONCLUSIONS, "conclusion.verdicts", f)
    if not _list(conclusion, "reasons", "conclusion", f):
        f.error("conclusion", "reasons is empty")

    for i, rec in _objects(_list(experiments, "recommended", "experiments", f), "experiments.recommended", f):
        for cid in _list(rec, "claim_ids", f"experiments.recommended[{i}]", f):
            if cid not in seen:
                f.warn(f"experiments.recommended[{i}]", f"unknown claim id {cid!r}")

    not_reviewed = set()
    for i, nr in enumerate(_list(a, "not_reviewed", "not_reviewed", f)):
        if not isinstance(nr, dict) or not nr.get("path") or not nr.get("reason"):
            f.error(f"not_reviewed[{i}]", "each entry needs path and reason")
            continue
        not_reviewed.add(nr["path"])

    if inventory:
        cited = _files_of(_all_locations(a))
        for target in inventory.get("review_targets", []):
            if target not in cited and target not in not_reviewed:
                f.warn("review_targets", f"{target!r} is neither cited as evidence nor listed in not_reviewed; say whether it was reviewed")
        if inventory.get("injection_candidates") and not a.get("security_notes"):
            f.warn("security_notes", "inventory found injection candidates but security_notes is empty; confirm they were reviewed")
        blob = json.dumps(a, ensure_ascii=False)
        for m in inventory.get("missing_references", []):
            target = m.get("target") if isinstance(m, dict) else m
            if target and target not in blob:
                f.warn("missing_references", f"{target!r} is referenced by the skill but missing, and the audit never mentions it")
    return f


def _dicts(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _strings(value):
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _field(d, key):
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _evidence_locations(a):
    locs = []
    for ft in _dicts(a.get("features")):
        locs += _strings(ft.get("locations"))
    for c in _dicts(a.get("claims")):
        locs += _strings(c.get("implementation_evidence"))
    return locs


def _all_locations(a):
    locs = _strings(_field(a, "activation").get("evidence")) + _evidence_locations(a)
    for c in _dicts(a.get("claims")):
        locs += _strings([c.get("source")]) + _strings(c.get("experimental_evidence_locations"))
    locs += _strings(_field(a, "minimal_prompt").get("derived_from"))
    locs += _strings([n.get("location") for n in _dicts(a.get("security_notes"))])
    return locs


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _line(s):
    return " ".join(_esc("" if s is None else s).split())


def _item(s):
    t = _line(s)
    return "\\" + t if t[:1] and t[0] in "#>+*=|~`_-" else t


def _cell(s):
    return "<br>".join(" ".join(_esc(x).split()) for x in str(s or "").splitlines()).replace("|", "\\|")


def _code(s):
    t = " ".join(str(s).split())
    tick = "`" * (max(map(len, re.findall("`+", t)), default=0) + 1)
    pad = " " if t[:1] == "`" or t[-1:] == "`" else ""
    return f"{tick}{pad}{t}{pad}{tick}"


def _fence(text):
    return "`" * max(3, max(map(len, re.findall("`+", text)), default=0) + 1)


def _locs(locs):
    return ", ".join(_code(loc) for loc in locs) if locs else ""


def _bullets(items, none):
    if not items:
        return [f"- {none}"]
    return [f"- {_item(x)}" for x in items]


def _evidence(locs, H, lang):
    groups = {}
    for loc in _strings(locs):
        m = LOC_RE.match(loc.strip())
        path, rng = (m.group(1), (m.group(2), m.group(3))) if m else (loc.strip(), None)
        groups.setdefault(path, [])
        if rng and rng not in groups[path]:
            groups[path].append(rng)
    parts = []
    for path, rngs in groups.items():
        if not rngs:
            parts.append(_code(path))
            continue
        dash = "〜" if lang == "ja" else "–"
        nums = ", ".join(s if not e or e == s else f"{s}{dash}{e}" for s, e in rngs)
        label = H["lines"].format(nums)
        if lang == "en" and len(rngs) == 1 and (not rngs[0][1] or rngs[0][1] == rngs[0][0]):
            label = f"line {nums}"
        parts.append(f"{_code(path)} {label}")
    return f"{H['evidence']}{H['colon']}{H['sep'].join(parts)}" if parts else ""


def _short_quote(text, limit=60):
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _sentence(label, text, H):
    t = _line(text)
    if not t:
        return ""
    return f"{label}{H['colon']}{t}" + ("" if t.endswith(("。", ".", "!", "?", "！", "？", "）", ")")) else H["end"])


def _paragraph(parts):
    return [p for p in parts if p]


def render(audit_dir, lang):
    L, H = LABELS[lang], HEADINGS[lang]
    a = load_json(audit_dir / "audit.json")
    prov = _load_optional(audit_dir / "provenance.json") or {}
    inv = _load_optional(audit_dir / "inventory.json") or {}
    name = (inv.get("frontmatter") or {}).get("parsed", {}).get("name") or inv.get("skill_name_dir") or audit_dir.name
    out = [f"# {H['title']}: {_line(name)}", ""]

    def lab(v, prefix=""):
        return L.get(f"{prefix}{v}", L.get(v, v)) if v is not None else "-"

    con = _field(a, "conclusion")
    out += [f"## {H['conclusion']}", ""]
    verdicts = _strings(con.get("verdicts"))
    if verdicts:
        out += [_line(" / ".join(lab(v) for v in verdicts)), ""]
    out += _bullets(con.get("reasons"), H["none"]) + [""]

    s = _field(a, "summary")
    out += [f"## {H['summary']}", ""]
    for key in ("purpose", "actual_behavior", "unique_capabilities", "expected_value", "claim_consistency"):
        if s.get(key):
            out.append(f"- {H[key]}{H['colon']}{_line(s[key])}")
    if s.get("open_questions"):
        out.append(f"- {H['open_questions']}{H['colon'].rstrip()}")
        out += [f"  - {_item(q)}" for q in s["open_questions"]]
    out.append("")

    feats = _dicts(a.get("features"))
    out += [f"## {H['behavior']}", ""]
    act = _field(a, "activation")
    if act.get("description") or act.get("evidence"):
        out += _paragraph([_sentence(H["activation"], act.get("description"), H), _evidence(act.get("evidence"), H, lang)]) + [""]
    if feats:
        out += [f"| {H['col_feature']} | {H['col_subst']} | {H['col_confidence']} |", "|---|---|---|"]
        for ft in feats:
            subst = f"{lab(ft.get('substitutability'))}（{lab(ft.get('basis'))}）" if lang == "ja" else \
                f"{lab(ft.get('substitutability'))} ({lab(ft.get('basis'))})"
            out.append(f"| {_cell(ft.get('name'))} | {_cell(subst)} | {_cell(lab(ft.get('confidence'), 'confidence.'))} |")
        out.append("")
    for ft in feats:
        out += [f"### {_line(ft.get('name'))}", ""]
        cats = H["sep"].join(lab(c) for c in _strings(ft.get("categories")))
        out += _paragraph([
            _line(ft.get("actual_processing")),
            _sentence(H["effect"], ft.get("expected_effect"), H),
            _sentence(H["external"], ", ".join(_strings(ft.get("external_capabilities"))), H),
            _sentence(H["rationale"], ft.get("rationale"), H),
            _sentence(H["categories"], cats, H),
            _evidence(ft.get("locations"), H, lang),
        ]) + [""]

    claims = _dicts(a.get("claims"))
    quotes = {c.get("id"): _short_quote(c.get("quote"), 40) for c in claims}
    out += [f"## {H['claims']}", ""]
    if not claims:
        out += [f"- {H['none']}", ""]
    ex = _field(a, "experiments")
    for c in claims:
        short = _short_quote(c.get("quote"))
        out += [f"### {H['q'].format(_line(short))}", ""]
        src = _evidence([c.get("source")], H, lang).split(H["colon"], 1)[-1] if c.get("source") else ""
        quote_line = f"> {H['q'].format(_line(c.get('quote')))}" if short != " ".join(str(c.get("quote") or "").split()) else ""
        if quote_line or src:
            out += _paragraph([quote_line, f"> {H['source']}{H['colon']}{src}" if src else ""]) + [""]
        ax = _field(c, "assessment")
        for x in AXES:
            value = lab(ax.get(x), f"{x}." if x not in ("unique_value",) else "")
            if x == "unique_value":
                value = f"{value}（{lab(c.get('unique_value_basis'))}）" if lang == "ja" else f"{value} ({lab(c.get('unique_value_basis'))})"
            out.append(f"- {H[f'axis_{x}']}{H['colon']}{_line(value)}{H['end']}")
        out.append("")
        exp = _line(c.get("experimental_evidence") or "")
        if c.get("experiment_ref"):
            exp = f"{exp} ({_code(c['experiment_ref'])})".strip()
        out += _paragraph([
            _sentence(H["claimed_effect"], c.get("claimed_effect"), H),
            _sentence(H["mechanism"], c.get("mechanism"), H),
            _sentence(H["impl_note"], c.get("implementation_note"), H),
            f"{H['exp_note']}{H['colon']}{exp}" if exp else "",
            _sentence(H["notes"], c.get("notes"), H),
            _evidence(_strings(c.get("implementation_evidence")) + _strings(c.get("experimental_evidence_locations")), H, lang),
        ]) + [""]

    nec = _field(a, "necessity")
    out += [f"## {H['necessity']}", ""]
    for key in ("base_model", "short_instruction", "unique_value", "redundant", "unconfirmed"):
        if nec.get(key):
            out += [f"{H[key]}{H['colon'].rstrip()}", ""] + _bullets(nec.get(key), H["none"]) + [""]
    mp = _field(a, "minimal_prompt")
    text = str(mp.get("text", ""))
    out += [f"### {H['minimal_prompt']}", "", H["minimal_prompt_intro"], "", _fence(text) + "text", text, _fence(text), ""]
    out += [f"{H['dropped']}{H['colon'].rstrip()}", ""] + _bullets(mp.get("dropped"), H["none"]) + [""]

    out += [f"## {H['experiments']}", ""]
    if ex.get("status") == "run":
        for run in _dicts(ex.get("runs")):
            out += [f"### {_line(run.get('id'))}", ""]
            if run.get("summary"):
                out += [_item(run["summary"]), ""]
            rf = run.get("results_file")
            md = (audit_dir / rf).with_suffix(".md") if isinstance(rf, str) else None
            if md is None or not _inside(audit_dir, md):
                out += [f"- {H['results']}{H['colon']}{_code(rf)} ({H['not_embedded']})", ""]
                continue
            out += [f"- {H['results']}{H['colon']}{_code(rf)}", ""]
            if md.exists():
                body = _esc(md.read_text(encoding="utf-8").strip())
                out += ["<details><summary>results.md</summary>", "", body, "", "</details>", ""]
    else:
        out += [H["not_run"], ""]
        if not ex.get("recommended"):
            out.append(f"- {H['none']}")
    for r in _dicts(ex.get("recommended")):
        targets = H["sep"].join(H["q"].format(quotes[i]) if quotes.get(i) else str(i) for i in _strings(r.get("claim_ids")))
        tail = f"（{H['target_claims']}{H['colon']}{_line(targets)}）" if lang == "ja" else f" ({H['target_claims']}{H['colon']}{_line(targets)})"
        line = " ".join(x for x in [_line(r.get("hypothesis", "")), _sentence(H["task_idea"], r.get("task_idea"), H)] if x)
        out.append(f"- {line}{tail if targets else ''}")
    out.append("")

    notes = _dicts(a.get("security_notes"))
    out += [f"## {H['concerns']}", ""]
    if not notes:
        out.append(f"- {H['none']}")
    for n in notes:
        parts = [_item(n.get("observation", "")), _line(n.get("handling", "")), _evidence([n.get("location")] if n.get("location") else [], H, lang)]
        out.append("- " + " ".join(p for p in parts if p))
    out.append("")

    auditor = _field(a, "auditor")
    out += [f"## {H['conditions_limits']}", "", f"### {H['conditions']}", ""]
    kind = prov.get("kind")
    rows = [
        ("input", _code(prov.get("input")) if prov.get("input") else None),
        ("kind", _line(L[f"kind.{kind}"]) if f"kind.{kind}" in L else (_code(kind) if kind else None)),
        ("repo", _code(prov.get("repo_url")) if prov.get("repo_url") else None),
        ("commit", _code(prov.get("commit")) if prov.get("commit") else None),
        ("dirty", L.get(f"dirty.{prov.get('dirty')}") if prov.get("dirty") is not None else None),
        ("skill_subpath", _code(prov.get("skill_subpath")) if prov.get("skill_subpath") else None),
        ("fetched_at", _code(prov.get("fetched_at")) if prov.get("fetched_at") else None),
        ("fingerprint", _code(inv.get("fingerprint")) if inv.get("fingerprint") else None),
        ("model", _code(auditor.get("model")) if auditor.get("model") else None),
        ("runtime", _line(auditor.get("runtime")) if auditor.get("runtime") else None),
        ("date", _code(auditor.get("date")) if auditor.get("date") else None),
    ]
    out += [f"- {H[k]}{H['colon']}{v}" for k, v in rows if v] + [""]
    if a.get("limitations"):
        out += [f"### {H['limitations']}", ""] + _bullets(a["limitations"], H["none"]) + [""]
    not_reviewed = _dicts(a.get("not_reviewed"))
    if not_reviewed:
        out += [f"### {H['not_reviewed']}", ""] + [f"- {_code(n.get('path'))}{H['colon']}{_line(n.get('reason'))}" for n in not_reviewed] + [""]

    if claims:
        out += [f"## {H['tally']}", "", f"| {H['axis']} | {H['value']} | {H['count']} |", "|---|---|---|"]
        for x in AXES:
            counts = Counter(_field(c, "assessment").get(x) for c in claims)
            for v in AXES[x]:
                if counts.get(v):
                    out.append(f"| {H[f'axis_{x}']} | {_cell(lab(v, f'{x}.' if x != 'unique_value' else ''))} | {counts[v]} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def skeleton(audit_dir, model, runtime):
    path = audit_dir / "audit.json"
    if path.exists():
        raise SystemExit(f"{path} already exists")
    data = {
        "schema": SCHEMA,
        "auditor": {"model": model, "runtime": runtime, "date": datetime.date.today().isoformat()},
        "summary": {k: "" for k in ("purpose", "actual_behavior", "unique_capabilities", "expected_value", "claim_consistency")} | {"open_questions": []},
        "activation": {"description": "", "evidence": []},
        "features": [],
        "claims": [],
        "necessity": {k: [] for k in ("base_model", "short_instruction", "unique_value", "redundant", "unconfirmed")},
        "minimal_prompt": {"text": "", "derived_from": [], "dropped": []},
        "security_notes": [],
        "experiments": {"status": "not_run", "runs": [], "recommended": []},
        "conclusion": {"verdicts": [], "reasons": []},
        "limitations": [],
        "not_reviewed": [],
    }
    write_json(path, data)
    return path


def _files_of(locs):
    return {loc.rsplit(":", 1)[0] for loc in locs if isinstance(loc, str) and ":" in loc}


def _line_ranges(locs):
    by_path = {}
    for loc in locs:
        if not isinstance(loc, str) or ":" not in loc:
            continue
        path, rng = loc.rsplit(":", 1)
        start, _, end = rng.partition("-")
        try:
            s, e = int(start), int(end or start)
        except ValueError:
            continue
        if s <= e:
            by_path.setdefault(path, []).append((s, e))
    merged = {}
    for path, ranges in by_path.items():
        out = []
        for s, e in sorted(ranges):
            if out and s <= out[-1][1] + 1:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        merged[path] = out
    return merged


def _range_size(ranges):
    return sum(e - s + 1 for path in ranges.values() for s, e in path)


def _range_overlap(x, y):
    total = 0
    for path in x.keys() & y.keys():
        a, b, i, j = x[path], y[path], 0, 0
        while i < len(a) and j < len(b):
            total += max(0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]) + 1)
            if a[i][1] < b[j][1]:
                i += 1
            else:
                j += 1
    return total


def _jaccard(x, y):
    return 1.0 if not x and not y else len(x & y) / len(x | y)


def _lines_jaccard(la, lb):
    x, y = _line_ranges(la), _line_ranges(lb)
    inter = _range_overlap(x, y)
    union = _range_size(x) + _range_size(y) - inter
    return 1.0 if not union else inter / union


def _dist(values):
    return dict(sorted(Counter(values).items(), key=lambda kv: str(kv[0])))


def diff(a_path, b_path):
    a, b = load_json(a_path), load_json(b_path)
    la, lb = _evidence_locations(a), _evidence_locations(b)
    return {
        "features": [len(a.get("features", [])), len(b.get("features", []))],
        "claims": [len(a.get("claims", [])), len(b.get("claims", []))],
        "cited_files_jaccard": round(_jaccard(_files_of(la), _files_of(lb)), 3),
        "cited_lines_jaccard": round(_lines_jaccard(la, lb), 3),
        "feature_categories": [sorted({c for f in d.get("features", []) for c in f.get("categories", [])}) for d in (a, b)],
        "substitutability": [_dist(f.get("substitutability") for f in d.get("features", [])) for d in (a, b)],
        "claim_assessments": {x: [_dist((c.get("assessment") or {}).get(x) for c in d.get("claims", [])) for d in (a, b)] for x in AXES},
        "conclusion": [a.get("conclusion", {}).get("verdicts"), b.get("conclusion", {}).get("verdicts")],
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Validate and render slop-skill-auditor audit.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    sk = sub.add_parser("skeleton", help="write an empty audit.json")
    sk.add_argument("audit_dir", type=Path)
    sk.add_argument("--model", default="")
    sk.add_argument("--runtime", default="")
    ck = sub.add_parser("check", help="validate audit.json against the evidence rules")
    ck.add_argument("audit_dir", type=Path)
    ck.add_argument("--strict", action="store_true", help="treat warnings as errors")
    rd = sub.add_parser("render", help="validate, then write report.md")
    rd.add_argument("audit_dir", type=Path)
    rd.add_argument("--lang", choices=sorted(LABELS), default="ja")
    rd.add_argument("--out", type=Path)
    df = sub.add_parser("diff", help="compare the extracted facts of two audit.json files")
    df.add_argument("a", type=Path)
    df.add_argument("b", type=Path)
    args = p.parse_args(argv)

    if args.cmd == "skeleton":
        print(skeleton(args.audit_dir, args.model, args.runtime))
        return 0
    if args.cmd == "diff":
        print(json.dumps(diff(args.a, args.b), ensure_ascii=False, indent=2))
        return 0

    f = check(args.audit_dir)
    for w in f.warnings:
        print(f"WARN  {w}", file=sys.stderr)
    for e in f.errors:
        print(f"ERROR {e}", file=sys.stderr)
    failed = bool(f.errors) or (args.cmd == "check" and args.strict and f.warnings)
    if args.cmd == "check":
        print(f"{len(f.errors)} error(s), {len(f.warnings)} warning(s)")
        return 1 if failed else 0
    if f.errors:
        print("not rendering: fix the errors above", file=sys.stderr)
        return 1
    out = args.out or args.audit_dir / "report.md"
    out.write_text(render(args.audit_dir, args.lang), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
