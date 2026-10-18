import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "slop-skill-auditor"
SCRIPTS = SKILL_ROOT / "scripts"
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
        self.assertIsNone(prov["dirty"])
        self.assertIn("dirty not computed (git is not run on local inputs)", prov["notes"])
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


class LocalGitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.marker = os.path.join(self.tmp, "PWNED")
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(FIXTURES / "repos" / "multi-skill", self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, stdout=subprocess.PIPE, text=True).stdout.strip()

    def hostile_config(self, gitdir):
        cmd = "touch %s" % self.marker
        with open(os.path.join(gitdir, "config"), "a") as f:
            f.write("[core]\n\tfsmonitor = %s\n[filter \"x\"]\n\tclean = %s\n\tsmudge = %s\n" % (cmd, cmd, cmd))
        Path(self.repo, ".gitattributes").write_text("* filter=x\n")

    def fetch(self):
        out = os.path.join(self.tmp, "audit")
        args = fetch_target.argparse.Namespace(
            input=os.path.join(self.repo, "skills", "beta"), out=out, skill=None, ref=None,
            doc=None, offline=True, force=True)
        with mock.patch.object(fetch_target, "run_git", side_effect=AssertionError("git run on a local input")), \
                mock.patch.object(fetch_target, "tool_versions", return_value={}):
            prov, ambiguous = fetch_target.fetch(args)
        self.assertFalse(ambiguous)
        self.assertFalse(os.path.exists(self.marker))
        self.assertIsNone(prov["dirty"])
        self.assertIn("dirty not computed (git is not run on local inputs)", prov["notes"])
        return prov

    def test_loose_ref_with_hostile_config(self):
        self.hostile_config(os.path.join(self.repo, ".git"))
        self.assertEqual(self.fetch()["commit"], self.sha)

    def test_packed_refs(self):
        git(self.repo, "pack-refs", "--all")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "refs", "heads", "main")))
        self.assertEqual(self.fetch()["commit"], self.sha)

    def test_detached_head(self):
        Path(self.repo, ".git", "HEAD").write_text(self.sha + "\n")
        self.assertEqual(self.fetch()["commit"], self.sha)

    def test_gitfile_pointing_to_hostile_gitdir(self):
        moved = os.path.join(self.tmp, "elsewhere.git")
        shutil.move(os.path.join(self.repo, ".git"), moved)
        Path(self.repo, ".git").write_text("gitdir: ../elsewhere.git\n")
        self.hostile_config(moved)
        self.assertEqual(self.fetch()["commit"], self.sha)

    def test_unreadable_head_gives_null_commit(self):
        Path(self.repo, ".git", "HEAD").write_text("ref: ../../../etc/passwd\n")
        prov = self.fetch()
        self.assertIsNone(prov["commit"])
        self.assertIn("commit could not be read from .git", prov["notes"])


class LocalCopyScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(FIXTURES / "repos" / "multi-skill", self.repo)
        os.mkdir(os.path.join(self.repo, ".git"))
        self.out = os.path.join(self.tmp, "audit")

    def test_only_skill_and_repo_docs_are_copied(self):
        Path(self.repo, ".env").write_text("TOKEN=secret\n")
        os.makedirs(os.path.join(self.repo, "secrets"))
        Path(self.repo, "secrets", "key").write_text("k\n")
        Path(self.repo, "README.md").write_text("# r\n")
        Path(self.repo, "CHANGELOG").write_text("c\n")
        os.makedirs(os.path.join(self.repo, "docs"))
        Path(self.repo, "docs", "x.md").write_text("# x\n")
        Path(self.repo, "docs", "data.json").write_text("{}\n")
        p = run("fetch_target.py", os.path.join(self.repo, "skills", "beta"), "--out", self.out)
        self.assertEqual(p.returncode, 0, p.stderr)
        src = Path(self.out, "source")
        for rel in [".env", "secrets", "docs/data.json", "skills/alpha"]:
            self.assertFalse(os.path.lexists(src / rel), rel)
        for rel in ["README.md", "CHANGELOG", "docs/x.md", "skills/beta/SKILL.md"]:
            self.assertTrue((src / rel).is_file(), rel)
        prov = json.loads(Path(self.out, "provenance.json").read_text())
        self.assertEqual(prov["skill_subpath"], "skills/beta")
        self.assertEqual(prov["candidates"], ["skills/alpha", "skills/beta"])

    def test_symlinks_in_skill_are_not_followed(self):
        skill = os.path.join(self.repo, "skills", "beta")
        outside = os.path.join(self.tmp, "outside.txt")
        Path(outside).write_text("secret\n")
        os.symlink(outside, os.path.join(skill, "out.txt"))
        os.symlink("SKILL.md", os.path.join(skill, "in.md"))
        os.symlink(self.tmp, os.path.join(skill, "dirlink"))
        p = run("fetch_target.py", skill, "--out", self.out)
        self.assertEqual(p.returncode, 0, p.stderr)
        prov = json.loads(Path(self.out, "provenance.json").read_text())
        self.assertEqual({s["path"]: s["reason"] for s in prov["skipped"]},
                         {k: "symlink (not followed)" for k in ["skills/beta/dirlink", "skills/beta/in.md", "skills/beta/out.txt"]})
        for name in ["out.txt", "in.md", "dirlink"]:
            self.assertFalse(os.path.lexists(Path(self.out, "source", "skills", "beta", name)))

    def test_byte_cap_exceeded_fails_and_cleans_up(self):
        Path(self.repo, "skills", "beta", "big.bin").write_bytes(b"x" * 4096)
        args = fetch_target.argparse.Namespace(
            input=os.path.join(self.repo, "skills", "beta"), out=self.out, skill=None, ref=None,
            doc=None, offline=True, force=False)
        with mock.patch.object(fetch_target, "LOCAL_REPO_MAX_BYTES", 1000):
            with self.assertRaisesRegex(FetchError, "exceeds 1000 bytes"):
                fetch_target.fetch(args)
        self.assertFalse(os.path.exists(os.path.join(self.out, "source")))


class DocUrlSafetyTests(unittest.TestCase):
    PUBLIC = "http://93.184.215.14/x.md"

    def test_check_url_refuses_non_public_and_non_http(self):
        for url in ["file:///etc/passwd", "ftp://93.184.215.14/x", "http://127.0.0.1/x", "http://localhost:8000/x",
                    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://0.0.0.0/", "http://224.0.0.1/"]:
            with self.assertRaises(FetchError, msg=url):
                fetch_target.check_url(url)
        fetch_target.check_url(self.PUBLIC)

    def test_redirect_handler_refuses_unsafe_targets(self):
        handler = fetch_target.SafeRedirectHandler()
        req = urllib.request.Request(self.PUBLIC)
        for url in ["file:///etc/passwd", "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/"]:
            with self.assertRaises(FetchError, msg=url):
                handler.redirect_request(req, None, 302, "Found", {}, url)
        self.assertEqual(handler.redirect_request(req, None, 302, "Found", {}, "https://93.184.215.14/y").full_url,
                         "https://93.184.215.14/y")

    def test_local_http_server_is_refused(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        self.addCleanup(server.server_close)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FetchError, "non-public"):
                fetch_target.fetch_doc("http://127.0.0.1:%d/x.md" % server.server_port, tmp, set(), False)
            self.assertEqual(os.listdir(tmp), [])

    def test_total_deadline(self):
        class Slow:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read1(self, n):
                time.sleep(0.02)
                return b"x"

        opener = mock.Mock()
        opener.open.return_value = Slow()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fetch_target, "DOC_DEADLINE", 0.1), \
                mock.patch.object(fetch_target.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(FetchError, "took longer"):
                fetch_target.fetch_doc(self.PUBLIC, tmp, set(), False)


class DefaultOutTests(unittest.TestCase):
    def test_default_out_is_a_fresh_dir_under_tmp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        src = Path(tmp, "src", "my-skill")
        shutil.copytree(FIXTURES / "skills" / "short-generic", src)
        env = dict(os.environ, TMPDIR=tmp)
        outs = []
        for _ in range(2):
            p = subprocess.run([sys.executable, "-I", str(SCRIPTS / "fetch_target.py"), str(src)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            outs.append(p.stdout.splitlines()[0].removeprefix("audit_dir: "))
        self.assertNotEqual(outs[0], outs[1])
        for out in outs:
            self.assertEqual(Path(out).parent, Path(tmp, "slop-audits"))
            self.assertTrue(Path(out).name.startswith("my-skill-"))
            self.assertTrue(Path(out, "provenance.json").is_file())

    def test_default_out_names_skill_md_input_after_its_directory(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with mock.patch.object(fetch_target.tempfile, "gettempdir", return_value=tmp):
            self.assertTrue(os.path.basename(fetch_target.default_out("/x/abc/SKILL.md")).startswith("abc-"))


class SafeNameTests(unittest.TestCase):
    def test_safe_doc_name(self):
        used = set()
        self.assertEqual(fetch_target.safe_doc_name("../../etc/passwd", used), "etc_passwd")
        self.assertEqual(fetch_target.safe_doc_name("", used), "doc")


if __name__ == "__main__":
    unittest.main()
