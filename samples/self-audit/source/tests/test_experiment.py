import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FAKE = ROOT / "tests" / "fake_backend.py"
sys.path.insert(0, str(SCRIPTS))

import experiment as ex  # noqa: E402

SKILL_NAME = "toy-fmt"
SKILL_MD = """---
name: toy-fmt
description: Formats status reports in the toy format. Use for status reports.
---
# Toy format

Every report must start with the line TOY-HEADER exactly.
Every report must end with the line TOY-FOOTER exactly.
List each finding as a numbered item with a severity tag.
Never use more than five findings in a single report.
"""
MINIMAL = "Write status reports with a fixed header line and footer line."


def call(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ex.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exp-test-"))
        self.skill = self.tmp / "skill"
        self.skill.mkdir()
        (self.skill / "SKILL.md").write_text(SKILL_MD)
        self.fixture = self.tmp / "fixture"
        self.fixture.mkdir()
        (self.fixture / "notes.txt").write_text("login bug fixed\n")
        self.audit = self.tmp / "audit"
        self.audit.mkdir()
        (self.audit / "audit.json").write_text(json.dumps({"minimal_prompt": {"text": MINIMAL}}))
        self.exp = self.tmp / "exp"
        env = {"SLOP_EXPERIMENT_BACKEND": f"fake:{FAKE}", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("FAKE_NO_LOAD", "FAKE_SANDBOX_FAIL"):
            os.environ.pop(k, None)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def plan(self):
        return json.loads((self.exp / "plan.json").read_text())

    def save(self, plan):
        (self.exp / "plan.json").write_text(json.dumps(plan, indent=2))

    def init_capability(self, n=2, tools=("Read", "Write", "Edit", "Glob", "Grep", "Skill")):
        code, _, err = call("init", self.exp, "--skill-dir", self.skill, "--audit-dir", self.audit)
        self.assertEqual(code, 0, err)
        p = self.plan()
        p["backend"]["tools"] = list(tools)
        p["trials_per_condition"] = n
        p["tasks"] = [{
            "id": "t1", "hypothesis": "the skill enforces the header", "claim_ids": ["c1"],
            "prompt": "Write a status report from notes.txt", "fixture_dir": str(self.fixture),
            "rubric": [
                {"id": "fmt", "description": "header and footer present", "scale": [0, 1, 2, 3], "anchors": {"0": "none", "3": "both"}},
                {"id": "acc", "description": "accurate", "scale": [0, 1, 2], "anchors": {"0": "wrong", "2": "right"}},
            ],
            "checks": [{"id": "wrote", "command": "test -f out.txt", "description": "an output file exists"}],
        }]
        p["grading"]["judge_model"] = "haiku"
        self.save(p)
        return p

    def freeze(self):
        code, _, err = call("freeze", self.exp)
        self.assertEqual(code, 0, err)


class PlanTests(Base):
    def test_init_copies_audit_minimal_prompt(self):
        self.init_capability()
        b = self.plan()["conditions"]["B"]
        self.assertEqual(b["minimal_instruction"], MINIMAL)
        self.assertEqual(b["source"], "audit.json:minimal_prompt")
        self.assertNotIn("c_invocation", self.plan()["conditions"]["C"])
        self.assertEqual(self.plan()["experiment_type"], "capability")

    def test_validate_errors_when_b_differs_from_audit(self):
        self.init_capability()
        self.assertEqual(call("validate", self.exp)[0], 0)
        (self.audit / "audit.json").write_text(json.dumps({"minimal_prompt": {"text": "changed"}}))
        code, _, err = call("validate", self.exp)
        self.assertEqual(code, 1)
        self.assertIn("differs from the current audit.json", err)

    def test_validate_warns_on_copied_skill_body(self):
        p = self.init_capability()
        p["conditions"]["B"] = {"minimal_instruction": SKILL_MD.split("---", 2)[2]}
        self.save(p)
        code, _, err = call("validate", self.exp)
        self.assertEqual(code, 0)
        self.assertIn("SKILL.md body lines", err)

    def test_validate_template_fails(self):
        call("init", self.exp, "--skill-dir", self.skill)
        code, _, err = call("validate", self.exp)
        self.assertEqual(code, 1)
        self.assertIn("minimal_instruction must be non-empty", err)
        self.assertIn("prompt must be non-empty", err)

    def test_activation_needs_positive_and_negative(self):
        call("init", self.exp, "--skill-dir", self.skill, "--type", "activation")
        p = self.plan()
        p["tasks"] = [{"id": "p", "should_activate": True, "prompt": "zebra report", "fixture_dir": None, "checks": []}]
        self.save(p)
        code, _, err = call("validate", self.exp)
        self.assertEqual(code, 1)
        self.assertIn("should_activate:false", err)

    def test_prompt_composition(self):
        p = self.init_capability()
        task = p["tasks"][0]
        self.assertEqual(ex.compose_prompt(p, task, "A"), task["prompt"])
        self.assertEqual(ex.compose_prompt(p, task, "B"), MINIMAL + "\n\n" + task["prompt"])
        self.assertEqual(ex.compose_prompt(p, task, "C"), f"/{SKILL_NAME} " + task["prompt"])
        p["experiment_type"] = "activation"
        self.assertEqual(ex.compose_prompt(p, task, "C"), task["prompt"])

    def test_modification_after_freeze_aborts(self):
        p = self.init_capability()
        self.freeze()
        p["notes"] = "edited"
        self.save(p)
        for cmd in ("run", "blind", "grade", "aggregate"):
            code, _, err = call(cmd, self.exp)
            self.assertEqual(code, 2, cmd)
            self.assertIn("changed after freeze", err)

    def test_skill_change_after_freeze_aborts_run(self):
        self.init_capability()
        self.freeze()
        (self.skill / "extra.md").write_text("x")
        code, _, err = call("run", self.exp)
        self.assertEqual(code, 2)
        self.assertIn("skill directory changed", err)

    def test_dry_run_writes_nothing(self):
        self.init_capability(n=1)
        self.freeze()
        code, out, _ = call("run", self.exp, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--replay-user-messages", out)
        self.assertIn(f"/{SKILL_NAME} Write a status report", out)
        self.assertFalse((self.exp / "trials.json").exists())
        self.assertFalse((self.exp / "trials").exists())

    def test_cli_runs_isolated(self):
        self.init_capability()
        r = subprocess.run([sys.executable, "-I", str(SCRIPTS / "experiment.py"), "validate", str(self.exp)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class RunTests(Base):
    def run_all(self, *extra):
        code, out, err = call("run", self.exp, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out.strip().splitlines()[-1])

    def results(self):
        return {r["trial_id"]: r for r in ex.load_results(self.exp).values()}

    def test_end_to_end_capability(self):
        self.init_capability(n=2)
        self.freeze()
        lock = json.loads((self.exp / "plan.lock.json").read_text())
        self.assertEqual(lock["forcing_method"]["name"], "slash-command")
        self.assertEqual(self.run_all()["ran"], 6)
        res = self.results()
        self.assertEqual(sorted(r["condition"] for r in res.values()), ["A", "A", "B", "B", "C", "C"])
        manifest = json.loads((self.exp / "trials.json").read_text())["trials"]
        self.assertEqual([t["order"] for t in manifest], list(range(6)))
        bw_ok = ex.bwrap_status()[0]
        for tid, r in res.items():
            ws = self.exp / "trials" / tid / "workspace"
            self.assertTrue((self.exp / "trials" / tid / "transcript.jsonl").is_file())
            self.assertFalse((ws / "env_leak.txt").exists(), "parent session env leaked to child")
            self.assertFalse((ws / ".claude" / "skills").exists())
            seen = (ws / "seen_skills.txt").read_text().split()
            self.assertEqual(seen, [SKILL_NAME] if r["condition"] == "C" else [])
            self.assertEqual(r["skill_activated"], r["condition"] == "C")
            self.assertIn("out.txt", r["files_changed"])
            self.assertEqual(r["usage"]["output_tokens"], 50)
            self.assertEqual(r["checks"][0]["passed"], True if bw_ok else None)
        self.assertEqual(call("blind", self.exp)[0], 0)
        self.assertEqual(call("grade", self.exp)[0], 0)
        code, _, err = call("aggregate", self.exp)
        self.assertEqual(code, 0, err)
        out = json.loads((self.exp / "results.json").read_text())
        self.assertEqual(out["experiment_type"], "capability")
        self.assertEqual(out["forcing_method"]["name"], "slash-command")
        self.assertEqual(out["models_reported"]["trials"], ["fake-model-1"])
        self.assertEqual(out["models_reported"]["judge"], ["fake-judge-1"])
        self.assertEqual(out["cells"]["t1/C"]["total_score"]["mean"], 5)
        self.assertEqual(out["cells"]["t1/A"]["total_score"]["mean"], 0)
        ca = next(p for p in out["pairwise"] if p["comparison"] == "C-A")
        self.assertEqual(ca["mean_diff"], 5)
        self.assertTrue(ca["insufficient_n"])
        self.assertEqual(out["c_not_loaded"], [])
        md = (self.exp / "results.md").read_text()
        self.assertIn("## Limitations", md)

    def test_resume_skips_completed(self):
        self.init_capability(n=1)
        self.freeze()
        self.assertEqual(self.run_all()["ran"], 3)
        victim = next(iter(self.results()))
        (self.exp / "trials" / victim / "result.json").unlink()
        summary = self.run_all()
        self.assertEqual((summary["ran"], summary["skipped_existing"]), (1, 2))
        self.assertIn(victim, self.results())

    def test_only_filter(self):
        self.init_capability(n=1)
        self.freeze()
        call("run", self.exp, "--dry-run")
        self.run_all()
        first = next(iter(self.results()))
        (self.exp / "trials" / first / "result.json").unlink()
        other = [t for t in self.results()]
        (self.exp / "trials" / other[0] / "result.json").unlink()
        self.assertEqual(self.run_all("--only", first)["ran"], 1)
        self.assertEqual(call("run", self.exp, "--only", "deadbeef")[0], 2)

    def test_blinding_hides_conditions_and_redacts(self):
        self.init_capability(n=1)
        self.freeze()
        self.run_all()
        self.assertEqual(call("blind", self.exp)[0], 0)
        g = self.exp / "grading"
        key = json.loads((g / "key.json").read_text())
        self.assertEqual(sorted(key["items"].values()), sorted(self.results()))
        self.assertTrue(key["redaction"]["applied"])
        self.assertTrue(set(key["items"]).isdisjoint(self.results()))
        rubric = json.loads((g / "rubric.json").read_text())
        self.assertNotIn("condition", json.dumps(rubric))
        redacted = 0
        for item in (g / "items").iterdir():
            meta = json.loads((item / "item.json").read_text())
            self.assertEqual(set(meta), {"item_id", "task_id"})
            for f in ("output.md", "diff.patch"):
                text = (item / f).read_text()
                self.assertNotIn(SKILL_NAME, text)
                self.assertNotIn("SKILL.md", text)
                self.assertNotIn(".claude/skills", text)
                redacted += ex.REDACTED in text
            self.assertIn("+++ b/out.txt", (item / "diff.patch").read_text())
        self.assertGreater(redacted, 0)
        self.assertIn("login bug fixed", (g / "inputs" / "t1.md").read_text())
        prompt = ex.judge_prompt("p", rubric["t1"]["rubric"], "out", "diff", (g / "inputs" / "t1.md").read_text())
        self.assertIn("<task_inputs>", prompt)
        self.assertIn("notes.txt", prompt)
        self.assertEqual(call("blind", self.exp)[0], 2)

    def test_aggregate_with_missing_grades(self):
        self.init_capability(n=1)
        self.freeze()
        self.run_all()
        call("blind", self.exp)
        code, _, err = call("aggregate", self.exp)
        self.assertEqual(code, 0, err)
        out = json.loads((self.exp / "results.json").read_text())
        cell = out["cells"]["t1/A"]
        self.assertIsNone(cell["total_score"]["mean"])
        self.assertEqual(cell["total_score"]["reason"], "no values")
        self.assertTrue(all(m["reason"] == "no grade file" for m in out["missing"]))
        self.assertEqual(len(out["missing"]), 3)
        for p in out["pairwise"]:
            self.assertIsNone(p["mean_diff"])
            self.assertIsNone(p["ci95"])
            self.assertIn("no scored trials", p["reason"])

    def test_c_not_loaded_is_excluded(self):
        self.init_capability(n=1)
        self.freeze()
        os.environ["FAKE_NO_LOAD"] = "1"
        self.run_all()
        call("blind", self.exp)
        call("grade", self.exp)
        call("aggregate", self.exp)
        out = json.loads((self.exp / "results.json").read_text())
        self.assertEqual(len(out["c_not_loaded"]), 1)
        self.assertEqual(out["cells"]["t1/C"]["n_completed"], 1)
        self.assertEqual(out["cells"]["t1/C"]["n_in_comparison"], 0)
        self.assertIsNone(next(p for p in out["pairwise"] if p["comparison"] == "C-A")["mean_diff"])

    def test_sandbox_required_refuses_then_allows_unsandboxed(self):
        self.init_capability(n=1, tools=ex.DEFAULT_TOOLS)
        self.freeze()
        os.environ["FAKE_SANDBOX_FAIL"] = "1"
        code, _, err = call("run", self.exp)
        self.assertEqual(code, 2)
        self.assertIn("--allow-unsandboxed", err)
        st = json.loads((self.exp / "selftest.json").read_text())
        self.assertFalse(st["sandbox_ok"])
        self.assertIn("socat", st["sandbox"]["reason"])
        self.assertTrue(st["file_isolation_ok"])
        self.assertTrue(st["unsandboxed_bash_denied"])
        self.run_all("--allow-unsandboxed")
        for r in self.results().values():
            self.assertTrue(r["isolation"]["allow_unsandboxed"])
            self.assertTrue(r["isolation"]["bash_effectively_denied"])
        self.assertFalse(list(Path.home().glob(".slop-sbx-probe-*")))

    def test_activation_experiment(self):
        call("init", self.exp, "--skill-dir", self.skill, "--type", "activation")
        p = self.plan()
        p["backend"]["tools"] = ["Read", "Glob", "Skill"]
        p["trials_per_condition"] = 2
        p["tasks"] = [
            {"id": "pos", "should_activate": True, "prompt": "Write a zebra status report", "fixture_dir": None, "checks": []},
            {"id": "neg", "should_activate": False, "prompt": "Tell me a joke", "fixture_dir": None, "checks": []},
        ]
        self.save(p)
        self.assertEqual(call("validate", self.exp)[0], 0)
        self.freeze()
        self.assertEqual(self.run_all()["ran"], 4)
        self.assertTrue(all(r["condition"] == "C" for r in self.results().values()))
        code, out, _ = call("blind", self.exp)
        self.assertEqual(code, 0)
        self.assertIn("nothing to blind", out)
        self.assertIn("nothing to grade", call("grade", self.exp)[1])
        self.assertEqual(call("aggregate", self.exp)[0], 0)
        res = json.loads((self.exp / "results.json").read_text())
        self.assertEqual(res["experiment_type"], "activation")
        a = res["activation"]
        self.assertEqual(a["counts"], {"tp": 2, "fn": 0, "fp": 0, "tn": 2})
        self.assertEqual(a["true_positive_rate"]["value"], 1.0)
        self.assertEqual(a["false_positive_rate"]["value"], 0.0)


class StatsTests(unittest.TestCase):
    def test_bootstrap_deterministic(self):
        x, y = [3, 4, 5, 4, 2], [1, 2, 2, 3, 1]
        a = ex.bootstrap_diff_ci(x, y, 42)
        self.assertEqual(a, ex.bootstrap_diff_ci(x, y, 42))
        self.assertLessEqual(a[0], 2.2)
        self.assertGreaterEqual(a[1], 2.2)
        self.assertEqual(ex.derived_seed(1, "t", "C", "A"), ex.derived_seed(1, "t", "C", "A"))
        self.assertNotEqual(ex.derived_seed(1, "t", "C", "A"), ex.derived_seed(1, "t", "C", "B"))

    def test_describe_small_n(self):
        d = ex.describe([2.0])
        self.assertIsNone(d["stdev"])
        self.assertIn("n>=2", d["reason"])


if __name__ == "__main__":
    unittest.main()
