from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from miner_testcode.errors import ConfigError
from miner_testcode.orchestrator.firmware import (
    FirmwareDeployer,
    FirmwareDeploymentError,
    GithubActionsArtifactFetcher,
)


def artifact_archive(firmware: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("esp-miner.bin", firmware)
    return output.getvalue()


class GithubActionsArtifactTest(unittest.TestCase):
    def test_fetches_exact_sha_and_verifies_archive_digest(self) -> None:
        firmware = b"firmware" * 10_000
        archive = artifact_archive(firmware)
        run = {
            "id": 91,
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.example/actions/runs/91",
        }
        artifact = {
            "id": 92,
            "name": "esp-miner.bin",
            "expired": False,
            "archive_download_url": "https://api.github.example/artifacts/92/zip",
            "digest": f"sha256:{hashlib.sha256(archive).hexdigest()}",
        }
        fetcher = GithubActionsArtifactFetcher()
        config = {
            "workflow": "build.yml",
            "artifact_name": "esp-miner.bin",
            "filename": "esp-miner.bin",
            "token_env": "TEST_GITHUB_TOKEN",
            "wait_timeout": 1,
            "poll_seconds": 0.01,
            "max_bytes": 1024 * 1024,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"TEST_GITHUB_TOKEN": "token"}
        ), mock.patch.object(
            fetcher,
            "_json",
            side_effect=[{"workflow_runs": [run]}, {"artifacts": [artifact]}],
        ), mock.patch.object(fetcher, "_download", return_value=archive):
            result = fetcher.fetch(
                "owner/firmware",
                "a" * 40,
                config,
                Path(directory),
            )
            written = Path(result["firmware_path"])

            self.assertEqual(written.read_bytes(), firmware)
            self.assertEqual(result["workflow_run_id"], 91)
            self.assertEqual(result["firmware_sha256"], hashlib.sha256(firmware).hexdigest())

    def test_rejects_archive_digest_mismatch(self) -> None:
        firmware = b"firmware" * 10_000
        archive = artifact_archive(firmware)
        fetcher = GithubActionsArtifactFetcher()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"TEST_GITHUB_TOKEN": "token"}
        ), mock.patch.object(
            fetcher,
            "_json",
            side_effect=[
                {
                    "workflow_runs": [
                        {
                            "id": 91,
                            "head_sha": "a" * 40,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                },
                {
                    "artifacts": [
                        {
                            "id": 92,
                            "name": "esp-miner.bin",
                            "expired": False,
                            "archive_download_url": "https://example.invalid/artifact.zip",
                            "digest": f"sha256:{'0' * 64}",
                        }
                    ]
                },
            ],
        ), mock.patch.object(fetcher, "_download", return_value=archive):
            with self.assertRaisesRegex(FirmwareDeploymentError, "digest"):
                fetcher.fetch(
                    "owner/firmware",
                    "a" * 40,
                    {
                        "workflow": "build.yml",
                        "artifact_name": "esp-miner.bin",
                        "filename": "esp-miner.bin",
                        "token_env": "TEST_GITHUB_TOKEN",
                        "wait_timeout": 1,
                        "poll_seconds": 0.01,
                        "max_bytes": 1024 * 1024,
                    },
                    Path(directory),
                )


class FakeFetcher:
    def __init__(self, firmware_path: Path) -> None:
        self.firmware_path = firmware_path
        self.calls = 0

    def fetch(self, repository, commit_sha, config, cache_root):
        self.calls += 1
        return {
            "source_repository": repository,
            "commit_sha": commit_sha,
            "workflow_run_id": 10,
            "workflow_run_url": "https://github.example/actions/runs/10",
            "artifact_id": 11,
            "artifact_name": "esp-miner.bin",
            "archive_sha256": "b" * 64,
            "firmware_sha256": hashlib.sha256(self.firmware_path.read_bytes()).hexdigest(),
            "firmware_size": self.firmware_path.stat().st_size,
            "firmware_path": str(self.firmware_path),
        }


class FirmwareDeployerTest(unittest.TestCase):
    def configuration(self) -> dict:
        return {
            "repositories": {
                "firmware": {
                    "repository": "owner/firmware",
                    "artifacts": {"ota": {"filename": "esp-miner.bin"}},
                }
            },
            "gates": {
                "gate": {
                    "deployment": {
                        "artifact": "ota",
                        "device_roles": ["miner"],
                        "reboot_timeout": 30,
                    }
                }
            },
            "lab": {
                "setups": {"bench": {"devices": {"miner": "gamma"}}},
                "devices": {
                    "gamma": {
                        "name": "Bitaxe Gamma 602",
                        "addresses": {"api": "http://gamma.local"},
                        "expected": {"board_version": "602"},
                    }
                },
            },
        }

    def test_deploys_once_and_reuses_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            firmware = root / "esp-miner.bin"
            firmware.write_bytes(b"firmware" * 10_000)
            fetcher = FakeFetcher(firmware)
            deployer = FirmwareDeployer(fetcher)  # type: ignore[arg-type]
            before = {"boardVersion": "602", "version": "old"}
            after = {"boardVersion": "602", "version": "new"}
            with mock.patch.object(
                deployer, "_device_info", return_value=before
            ), mock.patch.object(deployer, "_ota") as ota, mock.patch.object(
                deployer, "_wait_for_reboot", return_value=after
            ):
                first = deployer.ensure(
                    {
                        "id": "run",
                        "gate_id": "gate",
                        "repository_id": "firmware",
                        "commit_sha": "a" * 40,
                    },
                    "bench",
                    self.configuration(),
                    root / "state",
                )
                second = deployer.ensure(
                    {
                        "id": "run",
                        "gate_id": "gate",
                        "repository_id": "firmware",
                        "commit_sha": "a" * 40,
                    },
                    "bench",
                    self.configuration(),
                    root / "state",
                )

            self.assertEqual(first, second)
            self.assertEqual(first["status"], "passed")
            self.assertNotIn("firmware_path", first["artifact"])
            self.assertEqual(fetcher.calls, 1)
            ota.assert_called_once()

    def test_refuses_wrong_board_before_ota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            firmware = root / "esp-miner.bin"
            firmware.write_bytes(b"firmware" * 10_000)
            deployer = FirmwareDeployer(FakeFetcher(firmware))  # type: ignore[arg-type]
            with mock.patch.object(
                deployer,
                "_device_info",
                return_value={"boardVersion": "601", "version": "old"},
            ), mock.patch.object(deployer, "_ota") as ota:
                with self.assertRaisesRegex(ConfigError, "refusing OTA"):
                    deployer.ensure(
                        {
                            "id": "run",
                            "gate_id": "gate",
                            "repository_id": "firmware",
                            "commit_sha": "a" * 40,
                        },
                        "bench",
                        self.configuration(),
                        root / "state",
                    )
            ota.assert_not_called()


if __name__ == "__main__":
    unittest.main()
