import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "slop-skill-auditor"
SCRIPTS = SKILL_ROOT / "scripts"
SELF_SAMPLE = ROOT / "samples" / "self-audit"


def inventory_of_self():
    r = subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "inventory.py"), "--skill-dir", str(SKILL_ROOT), "--json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(r.stdout)


class SelfAuditStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inv = inventory_of_self()

    def test_spec_checks_have_no_errors(self):
        failed = [c for c in self.inv["spec_checks"] if c["level"] == "error" and not c["ok"]]
        self.assertEqual(failed, [])

    def test_everything_skill_md_points_to_exists(self):
        self.assertTrue([r for r in self.inv["references"] if r["from"] == "SKILL.md" and r["exists"]])
        self.assertEqual([m["target"] for m in self.inv["missing_references"] if m["from"] == "SKILL.md"], [])

    def test_every_reference_doc_is_reachable_from_skill_md(self):
        referenced = {r["resolved"] for r in self.inv["references"] if r["from"] == "SKILL.md" and r["exists"]}
        docs = {f["path"] for f in self.inv["files"] if f["path"].startswith("references/")}
        self.assertEqual(docs - referenced, set())

    def test_skill_md_stays_small(self):
        self.assertLess(self.inv["skill_md"]["lines"], 120)

    def test_inventory_is_stable_across_runs(self):
        self.assertEqual(self.inv["fingerprint"], inventory_of_self()["fingerprint"])


@unittest.skipUnless((SELF_SAMPLE / "audit.json").exists(), "self-audit sample not generated")
class SelfAuditSampleTest(unittest.TestCase):
    def setUp(self):
        self.audit = json.loads((SELF_SAMPLE / "audit.json").read_text(encoding="utf-8"))

    def test_sample_passes_report_check(self):
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "report.py"), "check", str(SELF_SAMPLE)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_asks_whether_criteria_are_replaceable_by_a_short_prompt(self):
        self.assertTrue(self.audit["minimal_prompt"]["text"].strip())
        self.assertTrue(self.audit["minimal_prompt"]["dropped"])
        ratings = {f["substitutability"] for f in self.audit["features"]}
        self.assertTrue(ratings & {"base_model", "short_instruction", "undetermined"},
                        "a self-audit that rates every feature as unique value is not credible")

    def test_does_not_claim_its_own_effect_is_proven_without_experiment(self):
        if self.audit["experiments"]["status"] == "run":
            return
        for c in self.audit["claims"]:
            self.assertNotEqual(c["assessment"]["effect"], "experimentally_supported", c["id"])
        self.assertNotIn("clear_unique_value", self.audit["conclusion"]["verdicts"])


if __name__ == "__main__":
    unittest.main()
