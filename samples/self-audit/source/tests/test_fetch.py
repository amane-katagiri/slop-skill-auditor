import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import fetch_target  # noqa: E402
from fetch_target import FetchError, parse_github_url, parse_ls_remote, split_ref_path  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
LS_REMOTE = "\n".join([
    SHA + "\tHEAD",
    SHA + "\trefs/heads/main",
    SHA + "\trefs/heads/feature/x",
    SHA + "\trefs/heads/feature/x/y",
    SHA + "\trefs/tags/v1.0",
    SHA + "\trefs/tags/v1.0^{}",
    SHA + "\trefs/pull/1/head",
])


def run(script, *args, cwd=None):
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / script), *map(str, args)],
                          cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.hooksPath=/dev/null",
                    "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class GithubUrlTests(unittest.TestCase):
    def test_repo_forms(self):
        for url in ["https://github.com/o/r", "https://github.com/o/r/", "https://github.com/o/r.git",
                    "http://www.github.com/o/r?tab=readme#x"]:
            info = parse_github_url(url)
            self.assertEqual(info["kind"], "github_repo", url)
            self.assertEqual(info["repo_url"], "https://github.com/o/r.git")
            self.assertIsNone(info["rest"])

    def test_tree_and_blob(self):
        info = parse_github_url("https://github.com/anthropics/skills/tree/main/skills/pdf")
        self.assertEqual((info["kind"], info["owner"], info["repo"], info["rest"]),
                         ("github_tree", "anthropics", "skills", "main/skills/pdf"))
        info = parse_github_url("https://github.com/o/r/blob/feature/x/a/b/SKILL.md")
        self.assertEqual((info["kind"], info["rest"]), ("github_blob", "feature/x/a/b/SKILL.md"))

    def test_non_github_and_bad(self):
        self.assertIsNone(parse_github_url("https://gitlab.com/o/r"))
        with self.assertRaises(FetchError):
            parse_github_url("https://github.com/o")
        with self.assertRaises(FetchError):
            parse_github_url("https://github.com/o/r/issues/1")

    def test_ls_remote_parse(self):
        self.assertEqual(parse_ls_remote(LS_REMOTE), ["feature/x", "feature/x/y", "main", "v1.0"])

    def test_ref_with_slash_longest_wins(self):
        refs = parse_ls_remote(LS_REMOTE)
        self.assertEqual(split_ref_path("feature/x/y/skills/a", refs), ("feature/x/y", "skills/a", False))
        self.assertEqual(split_ref_path("feature/x/skills/a", refs), ("feature/x", "skills/a", False))
        self.assertEqual(split_ref_path("main", refs), ("main", "", False))
        self.assertEqual(split_ref_path("v1.0/skills/a/", refs), ("v1.0", "skills/a", False))
        self.assertEqual(split_ref_path(SHA + "/skills/a", []), (SHA, "skills/a", True))
        with self.assertRaises(FetchError):
            split_ref_path("nope/skills/a", refs)

    def test_offline_rejects_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = run("fetch_target.py", "https://github.com/o/r/tree/main/x", "--out", tmp, "--offline")
            self.assertEqual(p.returncode, 2)
            self.assertIn("--offline", p.stderr)
            self.assertFalse(os.path.exists(os.path.join(tmp, "source")))


class LocalFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def copy_fixture(self, rel):
        dst = os.path.join(self.tmp, "in", os.path.basename(rel))
        shutil.copytree(FIXTURES / rel, dst)
        return dst

    def test_multi_skill_requires_choice(self):
        repo = self.copy_fixture("repos/multi-skill")
        out = os.path.join(self.tmp, "audit")
        p = run("fetch_target.py", repo, "--out", out)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("skills/alpha", p.stdout)
        self.assertIn("skills/beta", p.stdout)
        prov = json.loads(Path(out, "provenance.json").read_text())
        self.assertEqual(prov["candidates"], ["skills/alpha", "skills/beta"])
        self.assertIsNone(prov["skill_subpath"])
        self.assertFalse(os.path.exists(os.path.join(out, "source")))

        p = run("fetch_target.py", repo, "--out", out, "--skill", "skills/alpha")
        self.assertEqual(p.returncode, 0, p.stderr)
        prov = json.loads(Path(out, "provenance.json").read_text())
        self.assertEqual(prov["kind"], "local_dir")
        self.assertEqual(prov["skill_subpath"], ".")
        self.assertEqual(prov["skill_dir_name"], "alpha")
        self.assertIsNone(prov["commit"])
        self.assertIsNone(prov["dirty"])

    def test_git_repo_root_is_copied(self):
        repo = self.copy_fixture("repos/multi-skill")
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "init")
        Path(repo, "README.md").write_text("changed\n")
        out = os.path.join(self.tmp, "audit")
        p = run("fetch_target.py", os.path.join(repo, "skills", "beta", "SKILL.md"), "--out", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        prov = json.loads(Path(out, "provenance.json").read_text())
        self.assertEqual(prov["kind"], "local_skill_md")
        self.assertEqual(prov["skill_subpath"], "skills/beta")
        self.assertEqual(prov["candidates"], ["skills/alpha", "skills/beta"])
        self.assertRegex(prov["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(prov["dirty"])
        self.assertFalse(os.path.exists(os.path.join(out, "source", ".git")))
        self.assertTrue(os.path.isfile(os.path.join(out, "source", "README.md")))

        p = run("inventory.py", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        inv = json.loads(Path(out, "inventory.json").read_text())
        self.assertEqual(inv["skill_root"], "source/skills/beta")
        self.assertEqual(inv["source_context"]["other_skills"], ["skills/alpha"])
        self.assertEqual(inv["source_context"]["repo_docs"], ["@repo/README.md"])

    def test_refuses_nonempty_source_without_force(self):
        skill = self.copy_fixture("skills/short-generic")
        out = os.path.join(self.tmp, "audit")
        self.assertEqual(run("fetch_target.py", skill, "--out", out).returncode, 0)
        p = run("fetch_target.py", skill, "--out", out)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--force", p.stderr)
        self.assertEqual(run("fetch_target.py", skill, "--out", out, "--force").returncode, 0)

    def test_docs_are_copied_with_safe_names(self):
        skill = self.copy_fixture("skills/short-generic")
        doc = os.path.join(self.tmp, "my notes (v2).md")
        Path(doc).write_text("# notes\n")
        out = os.path.join(self.tmp, "audit")
        p = run("fetch_target.py", skill, "--out", out, "--doc", doc, "--doc", doc, "--offline")
        self.assertEqual(p.returncode, 0, p.stderr)
        prov = json.loads(Path(out, "provenance.json").read_text())
        self.assertEqual([d["saved_as"] for d in prov["docs"]], ["docs/my_notes_v2_.md", "docs/my_notes_v2_-2.md"])
        p = run("fetch_target.py", skill, "--out", out, "--force", "--doc", "https://example.invalid/x.md", "--offline")
        self.assertEqual(p.returncode, 2)

    def test_symlink_escaping_target_is_not_copied(self):
        skill = self.copy_fixture("skills/short-generic")
        secret = os.path.join(self.tmp, "secret.txt")
        Path(secret).write_text("secret\n")
        os.symlink(secret, os.path.join(skill, "leak.txt"))
        out = os.path.join(self.tmp, "audit")
        self.assertEqual(run("fetch_target.py", skill, "--out", out).returncode, 0)
        self.assertFalse(os.path.lexists(os.path.join(out, "source", "leak.txt")))
        prov = json.loads(Path(out, "provenance.json").read_text())
        self.assertEqual([s["path"] for s in prov["skipped"]], ["leak.txt"])


class SafeNameTests(unittest.TestCase):
    def test_safe_doc_name(self):
        used = set()
        self.assertEqual(fetch_target.safe_doc_name("../../etc/passwd", used), "etc_passwd")
        self.assertEqual(fetch_target.safe_doc_name("", used), "doc")


if __name__ == "__main__":
    unittest.main()
