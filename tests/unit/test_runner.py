from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from miner_testcode.artifacts import RunArtifacts
from miner_testcode.provenance import ResolvedTestCode
from miner_testcode.results import TestCodeRecord
from miner_testcode.runner import MiningTestResult
from miner_testcode.telemetry import STANDARD_MINING_METRICS, TelemetryCapture


class ResultMarkerTest(unittest.TestCase):
    def test_success_marker_names_the_test_method(self) -> None:
        class ExampleCase(unittest.TestCase):
            def test_named_feature(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = RunArtifacts(root, "run")
            capture = TelemetryCapture(
                STANDARD_MINING_METRICS,
                event_path=artifacts.path / "telemetry.jsonl",
                started_at=100.0,
            )
            test = ExampleCase("test_named_feature")
            test._context = SimpleNamespace(  # type: ignore[attr-defined]
                device_config=SimpleNamespace(publication_name="Gamma")
            )
            test.device = SimpleNamespace(telemetry=capture)  # type: ignore[attr-defined]
            result = MiningTestResult(
                io.StringIO(),
                True,
                0,
                artifacts=artifacts,
                test_code=ResolvedTestCode(
                    root=Path(__file__).resolve().parents[2],
                    record=TestCodeRecord(
                        repository="owner/miner-testcode",
                        commit_sha="a" * 40,
                        url="https://github.com/owner/miner-testcode",
                    ),
                ),
            )

            result.startTest(test)
            result.addSuccess(test)
            markers = capture.to_dict()["markers"]

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["label"], "test_named_feature passed")
        self.assertEqual(markers[0]["status"], "good")


if __name__ == "__main__":
    unittest.main()
