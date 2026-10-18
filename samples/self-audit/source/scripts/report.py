#!/usr/bin/env python3
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import load_json, resolve_location, write_json  # noqa: E402

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
        "base_model": "モデル標準能力で代替可能と考えられる",
        "short_instruction": "短い追加指示で代替可能と考えられる",
        "unique_value": "独自の知識・手順・実装が価値を持つ",
        "external_capability": "外部能力が必要",
        "undetermined": "現時点では判断不能",
        "implementation.confirmed": "確認済み",
        "implementation.partial": "一部のみ",
        "implementation.missing": "欠落",
        "implementation.contradicted": "説明と矛盾",
        "implementation.not_applicable": "該当なし",
        "implementation.unknown": "不明",
        "effect.experimentally_supported": "実験で支持",
        "effect.not_supported_in_experiment": "実験で支持されず",
        "effect.unverified": "未検証",
        "effect.not_assessable": "評価不能",
        "consistency.consistent": "整合",
        "consistency.possibly_exaggerated": "誇張の疑い",
        "consistency.inconsistent": "不一致",
        "consistency.not_assessable": "評価不能",
        "confidence.high": "高", "confidence.medium": "中", "confidence.low": "低",
        "implementation_confirmed": "実装確認済み",
        "inference": "推論による仮説",
        "experiment_observed": "実験で観測",
        "unconfirmed": "未確認",
        "clear_unique_value": "導入による独自価値が明確",
        "unique_value_unverified": "独自価値はあるが効果は未検証",
        "likely_replaceable_by_short_prompt": "簡単な指示で代替できる可能性が高い",
        "claims_exceed_evidence": "宣伝に対して実装・根拠が不足",
        "insufficient_information": "判断材料不足",
        "possible_negative_effect": "悪影響・過剰な制約の可能性",
    },
    "en": {
        "instruction": "Instruction", "knowledge": "Knowledge", "procedure": "Procedure",
        "tool": "Tool", "algorithm": "Algorithm", "environment": "Environment",
        "base_model": "Likely replaceable by the base model",
        "short_instruction": "Likely replaceable by a short instruction",
        "unique_value": "Unique knowledge/procedure/implementation adds value",
        "external_capability": "Requires external capability",
        "undetermined": "Undetermined",
        "implementation.confirmed": "Confirmed",
        "implementation.partial": "Partial",
        "implementation.missing": "Missing",
        "implementation.contradicted": "Contradicts description",
        "implementation.not_applicable": "N/A",
        "implementation.unknown": "Unknown",
        "effect.experimentally_supported": "Experimentally supported",
        "effect.not_supported_in_experiment": "Not supported in experiment",
        "effect.unverified": "Unverified",
        "effect.not_assessable": "Not assessable",
        "consistency.consistent": "Consistent",
        "consistency.possibly_exaggerated": "Possibly exaggerated",
        "consistency.inconsistent": "Inconsistent",
        "consistency.not_assessable": "Not assessable",
        "confidence.high": "High", "confidence.medium": "Medium", "confidence.low": "Low",
        "implementation_confirmed": "Confirmed in implementation",
        "inference": "Inference",
        "experiment_observed": "Observed in experiment",
        "unconfirmed": "Unconfirmed",
        "clear_unique_value": "Clear unique value",
        "unique_value_unverified": "Unique value, effect unverified",
        "likely_replaceable_by_short_prompt": "Likely replaceable by a short prompt",
        "claims_exceed_evidence": "Claims exceed implementation/evidence",
        "insufficient_information": "Insufficient information",
        "possible_negative_effect": "Possible negative effect / over-constraint",
    },
}

HEADINGS = {
    "ja": {
        "title": "スキル監査レポート", "provenance": "監査対象と条件", "summary": "Summary",
        "purpose": "主な目的", "actual_behavior": "実際に行うこと",
        "unique_capabilities": "独自の実装や外部能力", "expected_value": "期待できる付加価値",
        "claim_consistency": "宣伝との整合性", "open_questions": "重要な疑問点",
        "activation": "起動条件", "implementation": "Implementation", "claims": "Claims",
        "necessity": "Necessity", "base_model": "素のモデルでできそうな部分",
        "short_instruction": "数行の指示で代替できそうな部分", "unique_value": "スキル固有の価値",
        "redundant": "不要または冗長と思われる構成", "unconfirmed": "確証がない部分",
        "minimal_prompt": "最小指示案（B群）", "dropped": "最小指示で再現できないもの",
        "experiments": "Experimental Results", "not_run": "対照実験は未実施。実験で検証することが望ましい主張:",
        "security": "セキュリティ上の注意（副次的な記録）", "conclusion": "Conclusion",
        "limitations": "監査の限界", "feature_details": "機能の詳細",
        "col_feature": "機能", "col_processing": "実際の処理", "col_location": "実装場所",
        "col_subst": "モデルだけで代替できるか", "col_claim": "READMEの主張",
        "col_impl": "実装上の根拠", "col_exp": "実験上の根拠",
        "axis_implementation": "実装", "axis_effect": "効果", "axis_consistency": "宣伝との整合",
        "axis_unique_value": "独自価値", "axis_confidence": "確信度",
        "tally": "判定の集計", "axis": "観点", "value": "判定", "count": "件数",
        "categories": "分類", "external": "外部能力", "effect": "期待される効果", "rationale": "理由",
        "basis": "根拠の種類", "none": "なし", "not_run_cell": "実験未実施",
        "source": "出典", "mechanism": "仕組み", "claimed_effect": "主張されている効果",
    },
    "en": {
        "title": "Skill Audit Report", "provenance": "Target and conditions", "summary": "Summary",
        "purpose": "Purpose", "actual_behavior": "What it actually does",
        "unique_capabilities": "Own implementation / external capabilities", "expected_value": "Expected added value",
        "claim_consistency": "Consistency with claims", "open_questions": "Open questions",
        "activation": "Activation", "implementation": "Implementation", "claims": "Claims",
        "necessity": "Necessity", "base_model": "Likely doable by the base model",
        "short_instruction": "Replaceable by a few lines of instruction", "unique_value": "Skill-specific value",
        "redundant": "Unnecessary or redundant parts", "unconfirmed": "Unconfirmed",
        "minimal_prompt": "Minimal prompt (condition B)", "dropped": "Not reproducible with the minimal prompt",
        "experiments": "Experimental Results", "not_run": "No controlled experiment was run. Claims worth testing:",
        "security": "Security notes (secondary)", "conclusion": "Conclusion",
        "limitations": "Limitations of this audit", "feature_details": "Feature details",
        "col_feature": "Feature", "col_processing": "Actual processing", "col_location": "Location",
        "col_subst": "Replaceable by model alone?", "col_claim": "Claim",
        "col_impl": "Implementation evidence", "col_exp": "Experimental evidence",
        "axis_implementation": "Implementation", "axis_effect": "Effect", "axis_consistency": "Consistency with claim",
        "axis_unique_value": "Unique value", "axis_confidence": "Confidence",
        "tally": "Tally", "axis": "Axis", "value": "Value", "count": "Count",
        "categories": "Categories", "external": "External capabilities", "effect": "Expected effect", "rationale": "Rationale",
        "basis": "Basis", "none": "none", "not_run_cell": "not run",
        "source": "Source", "mechanism": "Mechanism", "claimed_effect": "Claimed effect",
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


def _check_loc(audit_dir, loc, where, f):
    if not isinstance(loc, str) or not loc:
        f.error(where, f"location must be a non-empty string, got {loc!r}")
        return
    try:
        resolve_location(audit_dir, loc)
    except ValueError as e:
        f.error(where, f"bad location {loc!r}: {e}")


def _purpose_source(loc, frontmatter):
    if not isinstance(loc, str) or ":" not in loc:
        return False
    path, rng = loc.rsplit(":", 1)
    name = path.rsplit("/", 1)[-1]
    if path.startswith("@docs/") or name.upper().startswith("README"):
        return True
    if path != "SKILL.md" or not frontmatter.get("present"):
        return False
    start, _, end = rng.partition("-")
    try:
        return int(end or start) <= int(frontmatter.get("line_end", 0))
    except ValueError:
        return False


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
    inventory = _load_optional(audit_dir / "inventory.json")
    if inventory is None:
        f.warn("inventory.json", "not found; run inventory.py first so the report records the fingerprint")

    if a.get("schema") != SCHEMA:
        f.error("schema", f"expected {SCHEMA!r}")

    summary = a.get("summary") or {}
    for key in ("purpose", "actual_behavior", "unique_capabilities", "expected_value", "claim_consistency"):
        _text(summary, key, "summary", f)

    activation = a.get("activation") or {}
    _text(activation, "description", "activation", f)
    for loc in _list(activation, "evidence", "activation", f):
        _check_loc(audit_dir, loc, "activation.evidence", f)

    features = _list(a, "features", "features", f)
    if not features:
        f.error("features", "at least one feature is required")
    seen = set()
    for i, feat in enumerate(features):
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
            _check_loc(audit_dir, loc, where + ".locations", f)
        if not locs and feat.get("basis") != "unconfirmed":
            f.error(where, "a feature without locations cannot be rated as implemented; add locations or set basis to 'unconfirmed'")

    experiments = a.get("experiments") or {}
    status = experiments.get("status")
    _enum(status, ["not_run", "run"], "experiments.status", f)
    runs = _list(experiments, "runs", "experiments", f)
    run_ids = set()
    for i, run in enumerate(runs):
        where = f"experiments.runs[{i}]"
        rid = run.get("id")
        rf = run.get("results_file")
        if not rid or not rf:
            f.error(where, "id and results_file are required")
            continue
        if not (audit_dir / rf).exists():
            f.error(where, f"results_file {rf!r} does not exist")
            continue
        run_ids.add(rid)
    if status == "not_run" and runs:
        f.error("experiments", "status is 'not_run' but runs are listed")
    if status == "run" and not run_ids:
        f.error("experiments", "status is 'run' but no run has an existing results_file")

    claims = _list(a, "claims", "claims", f)
    seen = set()
    for i, cl in enumerate(claims):
        where = f"claims[{cl.get('id', i)}]"
        cid = cl.get("id")
        if not cid or cid in seen:
            f.error(where, "missing or duplicate id")
        seen.add(cid)
        _text(cl, "quote", where, f)
        _text(cl, "claimed_effect", where, f)
        _check_loc(audit_dir, cl.get("source"), where + ".source", f)
        assessment = cl.get("assessment") or {}
        for axis, allowed in AXES.items():
            _enum(assessment.get(axis), allowed, f"{where}.assessment.{axis}", f)
        _enum(cl.get("unique_value_basis"), BASES, where + ".unique_value_basis", f)
        impl = _list(cl, "implementation_evidence", where, f)
        for loc in impl:
            _check_loc(audit_dir, loc, where + ".implementation_evidence", f)
        implementation = assessment.get("implementation")
        if implementation in ("confirmed", "partial", "missing", "contradicted") and not impl:
            f.error(where, f"implementation {implementation!r} needs implementation_evidence (for 'missing', cite the line that references the absent part)")
        ext = _list(cl, "experimental_evidence_locations", where, f)
        for loc in ext:
            _check_loc(audit_dir, loc, where + ".experimental_evidence_locations", f)
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
            if item.get(key) == "experiment_observed" and not run_ids:
                f.error(f"{kind}[{item.get('id')}]", f"{key} 'experiment_observed' without any experiment results")

    necessity = a.get("necessity") or {}
    for key in ("base_model", "short_instruction", "unique_value", "redundant", "unconfirmed"):
        _list(necessity, key, "necessity", f)

    mp = a.get("minimal_prompt") or {}
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
        _check_loc(audit_dir, loc, "minimal_prompt.derived_from", f)
        if not _purpose_source(loc, fm):
            f.error("minimal_prompt.derived_from", f"{loc!r} is not a README, user doc, or SKILL.md frontmatter line; the minimal prompt must not be distilled from the skill body")

    for i, note in enumerate(_list(a, "security_notes", "security_notes", f)):
        if note.get("location"):
            _check_loc(audit_dir, note["location"], f"security_notes[{i}]", f)

    conclusion = a.get("conclusion") or {}
    verdicts = _list(conclusion, "verdicts", "conclusion", f)
    if not verdicts:
        f.error("conclusion", "verdicts is empty")
    for v in verdicts:
        _enum(v, CONCLUSIONS, "conclusion.verdicts", f)
    if not _list(conclusion, "reasons", "conclusion", f):
        f.error("conclusion", "reasons is empty")

    for i, rec in enumerate(_list(experiments, "recommended", "experiments", f)):
        for cid in rec.get("claim_ids", []):
            if cid not in seen:
                f.warn(f"experiments.recommended[{i}]", f"unknown claim id {cid!r}")

    if inventory:
        if inventory.get("injection_candidates") and not a.get("security_notes"):
            f.warn("security_notes", "inventory found injection candidates but security_notes is empty; confirm they were reviewed")
        blob = json.dumps(a, ensure_ascii=False)
        for m in inventory.get("missing_references", []):
            target = m.get("target") if isinstance(m, dict) else m
            if target and target not in blob:
                f.warn("missing_references", f"{target!r} is referenced by the skill but missing, and the audit never mentions it")
    return f


def _cell(s):
    return (s or "").replace("|", "\\|").replace("\n", "<br>")


def _locs(locs):
    return ", ".join(f"`{l}`" for l in locs) if locs else ""


def _bullets(items, none):
    if not items:
        return [f"- {none}"]
    return [f"- {x}" for x in items]


def render(audit_dir, lang):
    L, H = LABELS[lang], HEADINGS[lang]
    a = load_json(audit_dir / "audit.json")
    prov = _load_optional(audit_dir / "provenance.json") or {}
    inv = _load_optional(audit_dir / "inventory.json") or {}
    name = (inv.get("frontmatter") or {}).get("parsed", {}).get("name") or inv.get("skill_name_dir") or audit_dir.name
    out = [f"# {H['title']}: {name}", ""]

    auditor = a.get("auditor") or {}
    out += [f"## {H['provenance']}", ""]
    rows = [
        ("input", prov.get("input")),
        ("kind", prov.get("kind")),
        ("repo", prov.get("repo_url")),
        ("commit", prov.get("commit")),
        ("dirty", prov.get("dirty")),
        ("skill_subpath", prov.get("skill_subpath")),
        ("fetched_at", prov.get("fetched_at")),
        ("fingerprint", inv.get("fingerprint")),
        ("auditor.model", auditor.get("model")),
        ("auditor.runtime", auditor.get("runtime")),
        ("audit date", auditor.get("date")),
    ]
    out += [f"- {k}: `{v}`" for k, v in rows if v not in (None, "")]
    out.append("")

    s = a.get("summary") or {}
    out += [f"## {H['summary']}", ""]
    for key in ("purpose", "actual_behavior", "unique_capabilities", "expected_value", "claim_consistency"):
        out.append(f"- **{H[key]}**: {s.get(key, '')}")
    if s.get("open_questions"):
        out.append(f"- **{H['open_questions']}**:")
        out += [f"  - {q}" for q in s["open_questions"]]
    out.append("")
    act = a.get("activation") or {}
    out += [f"**{H['activation']}**: {act.get('description', '')} {_locs(act.get('evidence', []))}", ""]

    feats = a.get("features") or []
    out += [f"## {H['implementation']}", ""]
    out += [f"| {H['col_feature']} | {H['col_processing']} | {H['col_location']} | {H['col_subst']} |", "|---|---|---|---|"]
    for ft in feats:
        subst = f"{L.get(ft.get('substitutability'), ft.get('substitutability'))} ({L.get(ft.get('basis'), ft.get('basis'))} / {H['axis_confidence']}: {L.get('confidence.' + str(ft.get('confidence')), ft.get('confidence'))})"
        out.append(f"| {ft.get('id')} {_cell(ft.get('name'))} | {_cell(ft.get('actual_processing'))} | {_locs(ft.get('locations', []))} | {_cell(subst)} |")
    out += ["", f"### {H['feature_details']}", ""]
    for ft in feats:
        out.append(f"#### {ft.get('id')} {ft.get('name')}")
        out.append(f"- {H['categories']}: {', '.join(L.get(c, c) for c in ft.get('categories', []))}")
        out.append(f"- {H['external']}: {', '.join(ft.get('external_capabilities') or []) or H['none']}")
        if ft.get("expected_effect"):
            out.append(f"- {H['effect']}: {ft['expected_effect']}")
        if ft.get("rationale"):
            out.append(f"- {H['rationale']}: {ft['rationale']}")
        out.append("")

    claims = a.get("claims") or []
    out += [f"## {H['claims']}", ""]
    if claims:
        out += ["| " + " | ".join([H["col_claim"]] + [H[f"axis_{x}"] for x in AXES]) + " |", "|---" * (len(AXES) + 1) + "|"]
        for c in claims:
            ax = c.get("assessment") or {}
            cells = [_cell(L.get(f"{x}.{ax.get(x)}", L.get(ax.get(x), ax.get(x)))) for x in AXES]
            cells[3] += f"<br>({_cell(L.get(c.get('unique_value_basis'), c.get('unique_value_basis')))})"
            out.append(f"| {c.get('id')}: “{_cell(c.get('quote'))}” `{c.get('source')}` | " + " | ".join(cells) + " |")
        out += ["", f"**{H['tally']}**", "", f"| {H['axis']} | {H['value']} | {H['count']} |", "|---|---|---|"]
        for x in AXES:
            counts = {}
            for c in claims:
                v = (c.get("assessment") or {}).get(x)
                counts[v] = counts.get(v, 0) + 1
            for v in AXES[x]:
                if counts.get(v):
                    out.append(f"| {H[f'axis_{x}']} | {L.get(f'{x}.{v}', L.get(v, v))} | {counts[v]} |")
        out.append("")
        for c in claims:
            exp = c.get("experimental_evidence") or ""
            if c.get("experiment_ref"):
                exp = f"{exp} (exp `{c['experiment_ref']}`)".strip()
            if c.get("experimental_evidence_locations"):
                exp = f"{exp} {_locs(c['experimental_evidence_locations'])}".strip()
            if not exp:
                exp = H["not_run_cell"] if (a.get("experiments") or {}).get("status") != "run" else H["none"]
            impl = " ".join(x for x in [c.get("implementation_note") or "", _locs(c.get("implementation_evidence", []))] if x)
            out.append(f"#### {c.get('id')}")
            for k, v in ((H["claimed_effect"], c.get("claimed_effect")), (H["mechanism"], c.get("mechanism")),
                         (H["col_impl"], impl), (H["col_exp"], exp), ("notes", c.get("notes"))):
                if v:
                    out.append(f"- {k}: {v}")
            out.append("")
    else:
        out += [f"- {H['none']}", ""]

    nec = a.get("necessity") or {}
    out += [f"## {H['necessity']}", ""]
    for key in ("base_model", "short_instruction", "unique_value", "redundant", "unconfirmed"):
        out += [f"**{H[key]}**", ""] + _bullets(nec.get(key), H["none"]) + [""]
    mp = a.get("minimal_prompt") or {}
    out += [f"### {H['minimal_prompt']}", "", "```text", mp.get("text", ""), "```", "", f"**{H['dropped']}**", ""]
    out += _bullets(mp.get("dropped"), H["none"]) + [""]

    ex = a.get("experiments") or {}
    if ex.get("status") == "run":
        out += [f"## {H['experiments']}", ""]
        for run in ex.get("runs", []):
            out += [f"### {run['id']}", ""]
            if run.get("summary"):
                out += [run["summary"], ""]
            out += [f"- results: `{run['results_file']}`", ""]
            md = (audit_dir / run["results_file"]).with_suffix(".md")
            if md.exists():
                body = md.read_text(encoding="utf-8").strip()
                out += ["<details><summary>results.md</summary>", "", body, "", "</details>", ""]
    if ex.get("status") != "run" or ex.get("recommended"):
        if ex.get("status") != "run":
            out += [f"## {H['experiments']}", "", H["not_run"], ""]
        if ex.get("status") != "run" and not ex.get("recommended"):
            out.append(f"- {H['none']}")
        for r in ex.get("recommended", []):
            ids = ", ".join(r.get("claim_ids", []))
            out.append(f"- [{ids}] {r.get('hypothesis', '')} — {r.get('task_idea', '')}")
        out.append("")

    notes = a.get("security_notes") or []
    if notes:
        out += [f"## {H['security']}", ""]
        for n in notes:
            loc = f" `{n['location']}`" if n.get("location") else ""
            out.append(f"- {n.get('observation', '')}{loc} — {n.get('handling', '')}")
        out.append("")

    con = a.get("conclusion") or {}
    out += [f"## {H['conclusion']}", ""]
    out.append("**" + " / ".join(L.get(v, v) for v in con.get("verdicts", [])) + "**")
    out.append("")
    out += _bullets(con.get("reasons"), H["none"]) + [""]
    if a.get("limitations"):
        out += [f"### {H['limitations']}", ""] + _bullets(a["limitations"], H["none"]) + [""]
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
    }
    write_json(path, data)
    return path


def _files_of(locs):
    return {l.rsplit(":", 1)[0] for l in locs if isinstance(l, str) and ":" in l}


def _lines_of(locs):
    out = set()
    for l in locs:
        if not isinstance(l, str) or ":" not in l:
            continue
        path, rng = l.rsplit(":", 1)
        start, _, end = rng.partition("-")
        try:
            s, e = int(start), int(end or start)
        except ValueError:
            continue
        out |= {(path, n) for n in range(s, e + 1)}
    return out


def _jaccard(x, y):
    return 1.0 if not x and not y else len(x & y) / len(x | y)


def diff(a_path, b_path):
    a, b = load_json(a_path), load_json(b_path)
    def locs(d):
        r = []
        for ft in d.get("features", []):
            r += ft.get("locations", [])
        for c in d.get("claims", []):
            r += c.get("implementation_evidence", [])
        return r
    def dist(d, kind, key):
        out = {}
        for x in d.get(kind, []):
            out[x.get(key)] = out.get(x.get(key), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: str(kv[0])))
    def dist_axis(d, axis):
        out = {}
        for c in d.get("claims", []):
            v = (c.get("assessment") or {}).get(axis)
            out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: str(kv[0])))
    la, lb = locs(a), locs(b)
    return {
        "features": [len(a.get("features", [])), len(b.get("features", []))],
        "claims": [len(a.get("claims", [])), len(b.get("claims", []))],
        "cited_files_jaccard": round(_jaccard(_files_of(la), _files_of(lb)), 3),
        "cited_lines_jaccard": round(_jaccard(_lines_of(la), _lines_of(lb)), 3),
        "feature_categories": [sorted({c for f in d.get("features", []) for c in f.get("categories", [])}) for d in (a, b)],
        "substitutability": [dist(a, "features", "substitutability"), dist(b, "features", "substitutability")],
        "claim_assessments": {x: [dist_axis(a, x), dist_axis(b, x)] for x in AXES},
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
