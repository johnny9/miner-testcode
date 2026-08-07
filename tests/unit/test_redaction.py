from __future__ import annotations

import unittest
from pathlib import Path

from miner_testcode.redaction import redact_bytes, redact_text


class RedactionTest(unittest.TestCase):
    def test_removes_pool_identities_and_keyed_secrets(self) -> None:
        data = (
            b"stratumUser=bc1qabcdefghijklmnopqrstuvwxyz0123456789.worker "
            b"stratumPassword:secret poolUser='npub1abcdefghijklmnopqrstuvwxyz0123456789' "
            b"ssid='private-network' mac=10:20:30:40:50:60 ip=192.168.1.44"
        )
        redacted = redact_bytes(data)
        self.assertNotIn(b"bc1q", redacted)
        self.assertNotIn(b"npub1", redacted)
        self.assertNotIn(b"secret", redacted)
        self.assertNotIn(b"private-network", redacted)
        self.assertNotIn(b"10:20:30:40:50:60", redacted)
        self.assertNotIn(b"192.168.1.44", redacted)
        self.assertIn(b"<redacted", redacted)

    def test_redacts_traceback_text_before_publication(self) -> None:
        value = "AssertionError: poolUser=npub1abcdefghijklmnopqrstuvwxyz0123456789.worker"
        redacted = redact_text(value)
        self.assertNotIn("npub1", redacted)
        self.assertIn("<redacted", redacted)

    def test_rewrites_repository_paths_and_removes_other_local_paths(self) -> None:
        project = Path("/home/alice/work/miner-testcode")
        artifacts = project / "artifacts/run-1"
        value = (
            'File "/home/alice/work/miner-testcode/tests/e2e/test_smoke.py", line 12\n'
            "artifacts=/home/alice/work/miner-testcode/artifacts/run-1/runner.log\n"
            "serial=/dev/serial/by-id/private-device"
        )
        redacted = redact_text(
            value,
            project_root=project,
            artifact_root=artifacts,
        )
        self.assertIn('File "tests/e2e/test_smoke.py", line 12', redacted)
        self.assertIn("artifacts=<artifacts>/runner.log", redacted)
        self.assertIn("serial=<local-path>", redacted)
        self.assertNotIn("/home/alice", redacted)
        self.assertNotIn("/dev/", redacted)

    def test_replaces_private_device_labels_without_changing_words(self) -> None:
        redacted = redact_text(
            "device=lab logger=miner.lab.test collaborate",
            replacements={"lab": "Bonanza device"},
        )
        self.assertEqual(
            redacted,
            "device=Bonanza device logger=miner.Bonanza device.test collaborate",
        )
