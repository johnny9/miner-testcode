from __future__ import annotations

import unittest

from miner_testcode.redaction import redact_bytes, redact_text


class RedactionTest(unittest.TestCase):
    def test_removes_pool_identities_and_keyed_secrets(self) -> None:
        data = (
            b"stratumUser=bc1qabcdefghijklmnopqrstuvwxyz0123456789.worker "
            b"stratumPassword:secret poolUser='npub1abcdefghijklmnopqrstuvwxyz0123456789'"
        )
        redacted = redact_bytes(data)
        self.assertNotIn(b"bc1q", redacted)
        self.assertNotIn(b"npub1", redacted)
        self.assertNotIn(b"secret", redacted)
        self.assertIn(b"<redacted", redacted)

    def test_redacts_traceback_text_before_publication(self) -> None:
        value = "AssertionError: poolUser=npub1abcdefghijklmnopqrstuvwxyz0123456789.worker"
        redacted = redact_text(value)
        self.assertNotIn("npub1", redacted)
        self.assertIn("<redacted", redacted)
