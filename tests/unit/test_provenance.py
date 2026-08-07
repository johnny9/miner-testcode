from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from miner_testcode.errors import ConfigError
from miner_testcode.provenance import resolve_test_code


def git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process.stdout.strip()


class TestCodeProvenanceTest(unittest.TestCase):
    def test_resolves_exact_published_github_source_and_rejects_dirty_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "--initial-branch=main")
            git(root, "config", "user.name", "Test")
            git(root, "config", "user.email", "test@example.invalid")
            source = root / "tests" / "e2e" / "test_smoke.py"
            source.parent.mkdir(parents=True)
            source.write_text("def test_smoke():\n    pass\n", encoding="utf-8")
            git(root, "add", "--", "tests/e2e/test_smoke.py")
            git(root, "commit", "-m", "Add test")
            git(root, "remote", "add", "origin", "git@github.com:owner/miner-testcode.git")
            git(root, "update-ref", "refs/remotes/origin/main", "HEAD")

            resolved = resolve_test_code(root, require_published=True)
            url = resolved.file_url(source, 1)

            self.assertEqual(resolved.record.repository, "owner/miner-testcode")
            self.assertTrue(resolved.record.published)
            self.assertFalse(resolved.record.dirty)
            self.assertEqual(
                url,
                f"https://github.com/owner/miner-testcode/blob/"
                f"{resolved.record.commit_sha}/tests/e2e/test_smoke.py#L1",
            )

            source.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "uncommitted changes"):
                resolve_test_code(root, require_published=True)
