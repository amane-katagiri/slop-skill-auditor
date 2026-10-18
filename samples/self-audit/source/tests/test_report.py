import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
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
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.audit = self.tmp / "audit"
        src = self.audit / "source"
        shutil.copytree(FIXTURES / "script-tool", src)
        (self.audit / "provenance.json").write_text(json.dumps({
            "schema": "slop-skill-auditor/provenance@1", "input": str(FIXTURES / "script-tool"),
            "kind": "local_dir", "source_root": "source", "skill_subpath": ".", "candidates": ["."],
            "docs": [], "repo_url": None, "commit": None, "dirty": None, "ref_requested": None,
            "fetched_at": "2026-10-18T00:00:00Z", "tools": {},
        }))
        subprocess.run([sys.executable, "-I", str(SCRIPTS / "inventory.py"), str(self.audit)], check=True, capture_output=True)

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

    def test_diff_identical_is_stable(self):
        self.write(VALID)
        d = report.diff(self.audit / "audit.json", self.audit / "audit.json")
        self.assertEqual(d["cited_files_jaccard"], 1.0)
        self.assertEqual(d["cited_lines_jaccard"], 1.0)


if __name__ == "__main__":
    unittest.main()
