import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SKILLS = ROOT / "tests" / "fixtures" / "skills"
sys.path.insert(0, str(SCRIPTS))

from _common import parse_frontmatter, resolve_location, write_json  # noqa: E402
from inventory import build_inventory  # noqa: E402

CASES = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())


def inv_of(case):
    return build_inventory(SKILLS / case, case)


def run(script, *args):
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / script), *map(str, args)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def kinds(inv, path=None):
    return {i["kind"] for i in inv["external_indicators"] if path is None or i["path"] == path}


class FrontmatterTests(unittest.TestCase):
    def test_subset(self):
        fm = parse_frontmatter(
            "---\n"
            "name: x  # comment\n"
            "description: >-\n  folded\n  text\n\n  para\n"
            "keep: |+\n  a\n\n"
            "tools: [Read, 'Bash(git:*)']\n"
            "items:\n  - a\n  - \"b\"\n"
            "metadata:\n  version: 1.0\n  author: me\n"
            "flag: true\n"
            "---\nbody\n")
        p = fm["parsed"]
        self.assertEqual(fm["errors"], [])
        self.assertEqual((fm["line_start"], fm["line_end"]), (1, 19))
        self.assertEqual(p["name"], "x")
        self.assertEqual(p["description"], "folded text\npara")
        self.assertEqual(p["keep"], "a\n\n")
        self.assertEqual(p["tools"], ["Read", "Bash(git:*)"])
        self.assertEqual(p["items"], ["a", "b"])
        self.assertEqual(p["metadata"], {"version": 1.0, "author": "me"})
        self.assertIs(p["flag"], True)

    def test_errors_never_raise(self):
        for text in ["---\nname: [unclosed\n---\n", "---\n\tbad: 1\n---\n", "---\nname: x\n",
                     "---\n: nokey\n  - stray\n---\n", "---\nname: \"open\n---\n", "no frontmatter\n", ""]:
            fm = parse_frontmatter(text)
            self.assertIsInstance(fm["errors"], list)
        self.assertFalse(parse_frontmatter("no frontmatter\n")["present"])
        fm = parse_frontmatter("---\nname: x\n")
        self.assertTrue(fm["present"])
        self.assertIsNone(fm["line_end"])
        fm = parse_frontmatter("---\nname: a\nname: b\noops\n---\n")
        self.assertEqual(fm["parsed"], {"name": "a"})
        self.assertEqual([e["line"] for e in fm["errors"]], [3, 4])


class FixtureInventoryTests(unittest.TestCase):
    def test_every_fixture_parses(self):
        for case in CASES:
            inv = inv_of(case)
            self.assertEqual(inv["frontmatter"]["errors"], [], case)
            self.assertEqual(inv["frontmatter"]["parsed"]["name"], case)
            self.assertTrue(all(c["ok"] for c in inv["spec_checks"]), (case, inv["spec_checks"]))
            self.assertEqual(inv["skill_name_dir"], case)

    def test_missing_impl(self):
        inv = inv_of("missing-impl")
        missing = {m["target"]: m for m in inv["missing_references"]}
        self.assertEqual(set(missing), {"scripts/changelog.py", "references/style.md"})
        self.assertEqual(missing["scripts/changelog.py"]["from"], "SKILL.md")

    def test_script_tool(self):
        inv = inv_of("script-tool")
        script = next(f for f in inv["files"] if f["path"] == "scripts/csv_profile.py")
        self.assertTrue(script["executable"])
        self.assertEqual(script["shebang"], "#!/usr/bin/env python3")
        self.assertEqual(script["kind"], "text")
        self.assertEqual(kinds(inv, "scripts/csv_profile.py"), set())
        ref = next(r for r in inv["references"] if r["from"] == "SKILL.md")
        self.assertEqual((ref["kind"], ref["resolved"], ref["exists"]), ("invocation", "scripts/csv_profile.py", True))
        self.assertEqual(inv["unreferenced_files"], [])
        self.assertEqual([b["lang"] for b in inv["skill_md"]["code_blocks"]], ["bash"])

    def test_mcp_api(self):
        inv = inv_of("mcp-api")
        self.assertTrue({"mcp_tool", "env_var", "network_code", "url"} <= kinds(inv))
        self.assertIn("network_code", kinds(inv, "scripts/fetch_alerts.py"))
        self.assertIn("package_install", kinds(inv, "README.md"))
        self.assertIn("mcp__weather__get_forecast", {i["match"] for i in inv["external_indicators"]})

    def test_references_logic(self):
        inv = inv_of("references-logic")
        refs = [r for r in inv["references"] if r["from"] == "SKILL.md"]
        self.assertEqual([(r["resolved"], r["exists"], r["kind"]) for r in refs],
                         [("references/decision-table.md", True, "markdown_link")])
        self.assertEqual(inv["missing_references"], [])
        self.assertEqual(inv["unreferenced_files"], [])

    def test_untrusted_injection(self):
        inv = inv_of("untrusted-injection")
        locs = {(c["path"], c["line"]) for c in inv["injection_candidates"]}
        self.assertIn(("README.md", 5), locs)
        self.assertIn(("SKILL.md", 8), locs)
        self.assertIn("network_code", kinds(inv, "scripts/cleanup.sh"))
        self.assertIn("url", kinds(inv, "scripts/cleanup.sh"))
        script = next(f for f in inv["files"] if f["path"] == "scripts/cleanup.sh")
        self.assertFalse(script["executable"])

    def test_benign_fixtures_have_no_injection_candidates(self):
        for case in CASES:
            if case != "untrusted-injection":
                self.assertEqual(inv_of(case)["injection_candidates"], [], case)

    def test_spec_checks_fail_when_expected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp, "some-dir")
            d.mkdir()
            Path(d, "SKILL.md").write_text("---\nname: Bad_Name\ndescription: " + "x" * 1100 + "\nfoo: 1\n---\n"
                                           + "line\n" * 501)
            checks = {c["id"]: c for c in build_inventory(d, str(d))["spec_checks"]}
            self.assertFalse(checks["name_format"]["ok"])
            self.assertEqual(checks["name_format"]["level"], "error")
            self.assertFalse(checks["name_matches_dir"]["ok"])
            self.assertEqual(checks["name_matches_dir"]["level"], "warn")
            self.assertFalse(checks["description_length"]["ok"])
            self.assertFalse(checks["body_length"]["ok"])
            self.assertFalse(checks["unknown_frontmatter_keys"]["ok"])
            self.assertEqual(checks["unknown_frontmatter_keys"]["level"], "info")
            Path(d, "SKILL.md").write_text("no frontmatter\n")
            checks = {c["id"]: c for c in build_inventory(d, str(d))["spec_checks"]}
            self.assertFalse(checks["frontmatter_present"]["ok"])
            self.assertFalse(checks["name_present"]["ok"])
            self.assertFalse(checks["description_present"]["ok"])

    def test_adhoc_cli_json(self):
        p = run("inventory.py", "--skill-dir", SKILLS / "short-generic", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["schema"], "slop-skill-auditor/inventory@1")
        self.assertNotIn("source_context", data)


class AuditDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.audit = os.path.join(self.tmp, "audit")
        p = run("fetch_target.py", SKILLS / "references-logic", "--out", self.audit)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_deterministic(self):
        outs = []
        for _ in range(2):
            p = run("inventory.py", self.audit)
            self.assertEqual(p.returncode, 0, p.stderr)
            outs.append(Path(self.audit, "inventory.json").read_bytes())
        self.assertEqual(outs[0], outs[1])
        self.assertNotIn(b"fetched_at", outs[0])

    def test_resolve_location(self):
        path, start, end = resolve_location(self.audit, "references/decision-table.md:3-5")
        self.assertTrue(path.endswith(os.path.join("references", "decision-table.md")))
        self.assertEqual((start, end), (3, 5))
        self.assertEqual(resolve_location(self.audit, "SKILL.md:1")[1:], (1, 1))
        self.assertEqual(resolve_location(self.audit, "SKILL.md")[1:], (None, None))
        for bad in ["SKILL.md:0", "SKILL.md:999", "SKILL.md:5-2", "nope.md:1", "../provenance.json:1",
                    "/etc/passwd:1", "@repo/../provenance.json:1", "", "@docs/x.md:1"]:
            with self.assertRaises(ValueError, msg=bad):
                resolve_location(self.audit, bad)

    def test_resolve_location_repo_and_docs(self):
        prov = json.loads(Path(self.audit, "provenance.json").read_text())
        Path(self.audit, "source", "skills").mkdir()
        shutil.move(os.path.join(self.audit, "source", "SKILL.md"), os.path.join(self.audit, "source", "skills", "SKILL.md"))
        Path(self.audit, "source", "README.md").write_text("a\nb\n")
        prov["skill_subpath"] = "skills"
        write_json(os.path.join(self.audit, "provenance.json"), prov)
        Path(self.audit, "docs").mkdir()
        Path(self.audit, "docs", "notes.md").write_text("x\n")
        self.assertEqual(resolve_location(self.audit, "@repo/README.md:2")[1:], (2, 2))
        self.assertEqual(resolve_location(self.audit, "@docs/notes.md:1")[1:], (1, 1))
        self.assertEqual(resolve_location(self.audit, "SKILL.md:1")[1:], (1, 1))
        with self.assertRaises(ValueError):
            resolve_location(self.audit, "@repo/README.md:3")


class UntrustedFixtureGuard(unittest.TestCase):
    def test_no_executable_files_in_untrusted_fixture(self):
        for dirpath, _, filenames in os.walk(SKILLS / "untrusted-injection"):
            for f in filenames:
                mode = os.lstat(os.path.join(dirpath, f)).st_mode
                self.assertFalse(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), os.path.join(dirpath, f))


if __name__ == "__main__":
    unittest.main()
