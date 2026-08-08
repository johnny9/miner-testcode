from __future__ import annotations

import unittest
from types import SimpleNamespace

from miner_testcode.testcase import MinerTestCase, validation_test


class ValidationTestSelectionTest(unittest.TestCase):
    def test_skips_unselected_pr_and_runs_selected_pr(self) -> None:
        class ValidationSelectionCase(MinerTestCase):
            @validation_test(1849, 1900)
            def test_related_prs(self) -> None:
                pass

        case = ValidationSelectionCase("test_related_prs")
        case._context = SimpleNamespace(validation_prs=frozenset())  # type: ignore[assignment]
        with self.assertRaisesRegex(unittest.SkipTest, "#1849, #1900"):
            case.setUp()

        case._context = SimpleNamespace(  # type: ignore[assignment]
            validation_prs=frozenset({1900})
        )
        case.setUp()

    def test_rejects_invalid_pr_declarations(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validation_test()
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validation_test(0)


if __name__ == "__main__":
    unittest.main()
