import copy
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "slop-skill-auditor"
SCRIPTS = SKILL_ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "skills"
sys.path.insert(0, str(SCRIPTS))

import report  # noqa: E402

VALID = {
    "schema": report.SCHEMA,
    "auditor": {"model": "test-model", "runtime": "unittest", "date": "2026-10-18"},
    "summary": {
        "purpose": "Profile CSV files",
        "actual_behavior": "Runs a bundled script that computes column statistics",
        "unique_capabilities": "Deterministic statistics script",
        "expected_value": "Exact numbers without mental arithmetic",
        "claim_consistency": "Mechanism matches README",
        "open_questions": [],
    },
    "activation": {"description": "description mentions CSV profiling", "evidence": ["SKILL.md:1-4"]},
    "features": [{
        "id": "F1",
        "name": "CSV profile",
        "categories": ["tool", "algorithm"],
        "actual_processing": "computes stats",
        "locations": ["scripts/csv_profile.py:1-3", "SKILL.md:2"],
        "external_capabilities": [],
        "expected_effect": "exact stats",
        "substitutability": "unique_value",
        "basis": "implementation_confirmed",
        "confidence": "high",
        "rationale": "code",
    }],
    "claims": [{
        "id": "C1",
        "source": "README.md:1",
        "quote": "profiles CSV",
        "claimed_effect": "exact stats",
        "mechanism": "script",
        "implementation_evidence": ["scripts/csv_profile.py:1"],
        "implementation_note": "present",
        "experimental_evidence": None,
        "experimental_evidence_locations": [],
        "experiment_ref": None,
        "assessment": {
            "implementation": "confirmed",
            "effect": "unverified",
            "consistency": "consistent",
            "unique_value": "unique_value",
            "confidence": "medium",
        },
        "unique_value_basis": "inference",
        "notes": "",
    }],
    "necessity": {"base_model": [], "short_instruction": [], "unique_value": ["script"], "redundant": [], "unconfirmed": []},
    "minimal_prompt": {"text": "Summarise each column of this CSV.", "derived_from": ["README.md:1"], "dropped": ["deterministic script"]},
    "security_notes": [],
    "experiments": {"status": "not_run", "runs": [], "recommended": [{"claim_ids": ["C1"], "hypothesis": "h", "task_idea": "t"}]},
    "conclusion": {"verdicts": ["unique_value_unverified"], "reasons": ["script exists, effect not measured"]},
    "limitations": [],
}


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = Path(tempfile.mkdtemp()) / "audit"
        shutil.copytree(FIXTURES / "script-tool", cls.template / "source")
        (cls.template / "provenance.json").write_text(json.dumps({
            "schema": "slop-skill-auditor/provenance@1", "input": str(FIXTURES / "script-tool"),
            "kind": "local_dir", "source_root": "source", "skill_subpath": ".", "candidates": ["."],
            "docs": [], "repo_url": None, "commit": None, "dirty": None, "ref_requested": None,
            "fetched_at": "2026-10-18T00:00:00Z", "tools": {},
        }))
        subprocess.run([sys.executable, "-I", str(SCRIPTS / "inventory.py"), str(cls.template)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.template.parent)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audit = self.tmp / "audit"
        shutil.copytree(self.template, self.audit, symlinks=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, data):
        (self.audit / "audit.json").write_text(json.dumps(data, ensure_ascii=False))

    def errors(self, data):
        self.write(data)
        return report.check(self.audit).errors

    def test_valid_audit_passes_and_renders(self):
        self.assertEqual(self.errors(VALID), [])
        md = report.render(self.audit, "ja")
        for heading in ("## Summary", "## Implementation", "## Claims", "## Necessity", "## Experimental Results", "## Conclusion"):
            self.assertIn(heading, md)
        self.assertIn("実験未実施", md)
        self.assertIn("判定の集計", md)
        self.assertIn("宣伝との整合", md)
        self.assertIn("fingerprint", md)
        self.assertIn("Claims", report.render(self.audit, "en"))

    def test_location_out_of_range_is_error(self):
        d = copy.deepcopy(VALID)
        d["features"][0]["locations"] = ["SKILL.md:99999"]
        self.assertTrue(any("bad location" in e for e in self.errors(d)))

    def test_location_to_missing_file_is_error(self):
        d = copy.deepcopy(VALID)
        d["features"][0]["locations"] = ["scripts/nope.py:1"]
        self.assertTrue(any("bad location" in e for e in self.errors(d)))

    def test_feature_without_location_needs_unconfirmed(self):
        d = copy.deepcopy(VALID)
        d["features"][0]["locations"] = []
        self.assertTrue(any("without locations" in e for e in self.errors(d)))
        d["features"][0]["basis"] = "unconfirmed"
        self.assertEqual(self.errors(d), [])

    def test_experimentally_supported_without_experiment_is_error(self):
        d = copy.deepcopy(VALID)
        d["claims"][0]["assessment"]["effect"] = "experimentally_supported"
        self.assertTrue(any("experimentally_supported" in e for e in self.errors(d)))

    def test_axes_are_independent(self):
        d = copy.deepcopy(VALID)
        d["claims"][0]["assessment"].update({"implementation": "confirmed", "effect": "unverified", "consistency": "possibly_exaggerated"})
        self.assertEqual(self.errors(d), [])

    def test_missing_implementation_needs_referencing_line(self):
        d = copy.deepcopy(VALID)
        d["claims"][0]["assessment"]["implementation"] = "missing"
        d["claims"][0]["implementation_evidence"] = []
        self.assertTrue(any("implementation_evidence" in e for e in self.errors(d)))

    def test_minimal_prompt_must_come_from_readme_or_description(self):
        d = copy.deepcopy(VALID)
        d["minimal_prompt"]["derived_from"] = ["scripts/csv_profile.py:1"]
        self.assertTrue(any("not a README" in e for e in self.errors(d)))
        d["minimal_prompt"]["derived_from"] = ["SKILL.md:2"]
        self.assertEqual(self.errors(d), [])
        (self.audit / "source" / "scripts" / "README.md").write_text("nested readme\n")
        d["minimal_prompt"]["derived_from"] = ["scripts/README.md:1"]
        self.assertTrue(any("not a README" in e for e in self.errors(d)), "only the skill-root README counts")
        d["minimal_prompt"]["derived_from"] = []
        self.assertTrue(any("derived_from" in e for e in self.errors(d)))

    def test_experiment_observed_basis_without_results_is_error(self):
        d = copy.deepcopy(VALID)
        d["features"][0]["basis"] = "experiment_observed"
        self.assertTrue(any("experiment_observed" in e for e in self.errors(d)))

    def test_experiment_ref_with_results_passes(self):
        d = copy.deepcopy(VALID)
        exp = self.audit / "experiments" / "e1"
        exp.mkdir(parents=True)
        (exp / "results.json").write_text("{}")
        (exp / "results.md").write_text("| cond | score |\n|---|---|\n| A | 1 |\n")
        d["experiments"] = {"status": "run", "runs": [{"id": "e1", "results_file": "experiments/e1/results.json", "summary": "s"}], "recommended": []}
        d["claims"][0]["assessment"]["effect"] = "experimentally_supported"
        d["claims"][0].update({"experiment_ref": "e1", "unique_value_basis": "experiment_observed"})
        self.assertEqual(self.errors(d), [])
        self.assertIn("results.md", report.render(self.audit, "ja"))

    def test_not_run_with_experiment_ref_is_error(self):
        d = copy.deepcopy(VALID)
        d["claims"][0]["experiment_ref"] = "e1"
        self.assertTrue(any("experiment_ref" in e for e in self.errors(d)))

    def test_unknown_labels_are_errors(self):
        d = copy.deepcopy(VALID)
        d["features"][0]["substitutability"] = "probably_fine"
        d["claims"][0]["assessment"]["consistency"] = "false"
        d["conclusion"]["verdicts"] = ["great"]
        self.assertEqual(len(self.errors(d)), 3)

    def test_render_refuses_on_errors(self):
        d = copy.deepcopy(VALID)
        d["features"] = []
        self.write(d)
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "report.py"), "render", str(self.audit)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.audit / "report.md").exists())

    def test_skeleton_then_check_reports_missing_content(self):
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "report.py"), "skeleton", str(self.audit)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(report.check(self.audit).errors)

    def test_unreviewed_target_warns_until_listed(self):
        d = copy.deepcopy(VALID)
        d["claims"][0]["source"] = "SKILL.md:1"
        d["minimal_prompt"]["derived_from"] = ["SKILL.md:2"]
        self.write(d)
        warnings = report.check(self.audit).warnings
        self.assertTrue(any("README.md" in w and "not_reviewed" in w for w in warnings), warnings)
        d["not_reviewed"] = [{"path": "README.md", "reason": "one-line README"}]
        self.write(d)
        self.assertFalse([w for w in report.check(self.audit).warnings if "review_targets" in w])

    def run_with_results(self, d, results_file):
        d["experiments"] = {"status": "run", "runs": [{"id": "e1", "results_file": results_file, "summary": "s"}], "recommended": []}
        d["claims"][0]["assessment"]["effect"] = "experimentally_supported"
        d["claims"][0].update({"experiment_ref": "e1", "unique_value_basis": "experiment_observed"})
        return d

    def test_results_file_outside_audit_dir_is_rejected(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "r.json").write_text("{}")
        (outside / "r.md").write_text("SECRET")
        (self.audit / "experiments").mkdir()
        (self.audit / "experiments" / "link.json").symlink_to(outside / "r.json")
        for rf in (str(outside / "r.json"), "../outside/r.json", "experiments/link.json"):
            with self.subTest(rf=rf):
                errs = self.errors(self.run_with_results(copy.deepcopy(VALID), rf))
                self.assertTrue(any("inside the audit directory" in e for e in errs), errs)
                self.assertNotIn("SECRET", report.render(self.audit, "en"))

    def test_results_md_symlink_outside_is_not_embedded(self):
        exp = self.audit / "experiments" / "e1"
        exp.mkdir(parents=True)
        (exp / "results.json").write_text("{}")
        (self.tmp / "secret.md").write_text("SECRET")
        (exp / "results.md").symlink_to(self.tmp / "secret.md")
        self.assertEqual(self.errors(self.run_with_results(copy.deepcopy(VALID), "experiments/e1/results.json")), [])
        md = report.render(self.audit, "en")
        self.assertNotIn("SECRET", md)
        self.assertIn("not embedded", md)

    def test_untrusted_text_cannot_inject_markdown_or_html(self):
        inv_path = self.audit / "inventory.json"
        inv = json.loads(inv_path.read_text())
        inv["frontmatter"]["parsed"]["name"] = "evil<script>alert(1)</script>\n# fake heading"
        inv_path.write_text(json.dumps(inv))
        exp = self.audit / "experiments" / "e1"
        exp.mkdir(parents=True)
        (exp / "results.json").write_text("{}")
        (exp / "results.md").write_text("| a |\n|---|\n| 1 |\n<img src=x onerror=alert(1)>\n</details>\n")
        d = self.run_with_results(copy.deepcopy(VALID), "experiments/e1/results.json")
        d["minimal_prompt"]["text"] = "Do this:\n```\n# Conclusion\n````"
        d["features"][0]["name"] = "a\n## Fake | x"
        d["conclusion"]["reasons"] = ["## Fake conclusion", "line\n\n# injected <b>"]
        self.assertEqual(self.errors(d), [])
        md = report.render(self.audit, "en")
        lines = md.splitlines()
        self.assertEqual(lines[0], "# Skill Audit Report: evil&lt;script&gt;alert(1)&lt;/script&gt; # fake heading")
        self.assertNotIn("<script>", md)
        self.assertNotIn("<img", md)
        self.assertNotIn("<b>", md)
        self.assertNotIn("# fake heading", lines[1:])
        self.assertEqual(md.count("</details>"), 1)
        self.assertIn("| a |\n|---|\n| 1 |\n&lt;img src=x onerror=alert(1)&gt;", md)
        i = lines.index("`````text")
        self.assertEqual(lines[i + 1:i + 6], ["Do this:", "```", "# Conclusion", "````", "`````"])
        self.assertIn("| F1 a<br>## Fake \\| x |", md)
        self.assertIn("- \\## Fake conclusion", lines)
        self.assertIn("- line # injected &lt;b&gt;", lines)
        self.assertFalse([ln for ln in lines if ln.lstrip("#").startswith((" Fake", " fake", " injected"))])

    def test_locations_with_backticks_render_as_code(self):
        self.assertEqual(report._code("a`b"), "``a`b``")
        self.assertEqual(report._code("`x` <y>"), "`` `x` <y> ``")
        self.assertEqual(report._locs(["SKILL.md:1", "x``y:2"]), "`SKILL.md:1`, ```x``y:2```")
        self.assertEqual(report._code("a\n# b"), "`a # b`")

    def test_diff_identical_is_stable(self):
        self.write(VALID)
        d = report.diff(self.audit / "audit.json", self.audit / "audit.json")
        self.assertEqual(d["cited_files_jaccard"], 1.0)
        self.assertEqual(d["cited_lines_jaccard"], 1.0)


    def test_diff_of_two_audits(self):
        b = copy.deepcopy(VALID)
        b["features"][0].update(locations=["scripts/csv_profile.py:2-5"], substitutability="short_instruction")
        b["claims"][0]["implementation_evidence"] = []
        b["claims"][0]["assessment"]["consistency"] = "possibly_exaggerated"
        b["conclusion"]["verdicts"] = ["likely_replaceable_by_short_prompt"]
        self.write(VALID)
        (self.tmp / "b.json").write_text(json.dumps(b))
        d = report.diff(self.audit / "audit.json", self.tmp / "b.json")
        self.assertEqual(d["cited_files_jaccard"], 0.5)
        self.assertEqual(d["cited_lines_jaccard"], round(2 / 6, 3))
        self.assertEqual(d["substitutability"], [{"unique_value": 1}, {"short_instruction": 1}])
        self.assertEqual(d["claim_assessments"]["consistency"], [{"consistent": 1}, {"possibly_exaggerated": 1}])
        self.assertEqual(d["conclusion"], [["unique_value_unverified"], ["likely_replaceable_by_short_prompt"]])

    def test_diff_handles_huge_ranges(self):
        a, b = copy.deepcopy(VALID), copy.deepcopy(VALID)
        a["features"][0]["locations"] = ["SKILL.md:1-1000000000", "SKILL.md:5-2"]
        b["features"][0]["locations"] = ["SKILL.md:500000001-1500000000"]
        a["claims"][0]["implementation_evidence"] = b["claims"][0]["implementation_evidence"] = []
        (self.tmp / "a.json").write_text(json.dumps(a))
        (self.tmp / "b.json").write_text(json.dumps(b))
        t = time.monotonic()
        d = report.diff(self.tmp / "a.json", self.tmp / "b.json")
        self.assertLess(time.monotonic() - t, 1)
        self.assertEqual(d["cited_lines_jaccard"], round(5e8 / 1.5e9, 3))

    def test_check_reports_malformed_structure(self):
        d = copy.deepcopy(VALID)
        d["features"].append("F2")
        d["claims"].append(["C2"])
        d["claims"][0]["assessment"] = "high"
        d["security_notes"] = ["note"]
        d["summary"] = "text"
        d["experiments"]["recommended"] = ["x"]
        errors = self.errors(d)
        for needle in ("features[1]: must be an object", "claims[1]: must be an object", "claims[C1].assessment: must be an object",
                       "security_notes[0]: must be an object", "summary: must be an object", "experiments.recommended[0]: must be an object"):
            self.assertIn(needle, errors)
        self.write([1])
        self.assertEqual(report.check(self.audit).errors, ["audit.json: top level must be an object"])

if __name__ == "__main__":
    unittest.main()
