from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miner_testcode.config import ConfigError, load_config
from miner_testcode.runner import _orchestration_metadata, build_parser


class ConfigTest(unittest.TestCase):
    def test_loads_bounded_orchestration_metadata(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MINER_TEST_ORCHESTRATION_METADATA": '{"gate_run_id":"run-1"}'},
        ):
            self.assertEqual(_orchestration_metadata(), {"gate_run_id": "run-1"})

    def test_cli_can_select_validation_prs(self) -> None:
        self.assertEqual(build_parser().parse_args([]).validation_pr, [])
        self.assertEqual(
            build_parser()
            .parse_args(["--validation-pr", "1849", "--validation-pr", "1844"])
            .validation_pr,
            [1849, 1844],
        )

    def test_loads_generic_interfaces_and_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[runner]
tests_dir = "e2e"
validation_prs = [1849, 1844]

[publishers.local]
enabled = true

[[devices]]
name = "lab"
type = "bitaxe_bonanza"

[devices.options]
publication_name = "Bonanza qualification device"

[devices.interfaces.api]
base_url = "${TEST_MINER_URL}"

[tests.public_pool_smoke]
host = "public-pool.io"
""",
                encoding="utf-8",
            )
            os.environ["TEST_MINER_URL"] = "http://bitaxe.local"
            self.addCleanup(os.environ.pop, "TEST_MINER_URL", None)
            config = load_config(path)

        self.assertEqual(config.devices[0].name, "lab")
        self.assertEqual(
            config.devices[0].publication_name, "Bonanza qualification device"
        )
        self.assertEqual(
            config.devices[0].interface("api")["base_url"], "http://bitaxe.local"
        )
        self.assertTrue(config.runner.tests_dir.is_absolute())
        self.assertEqual(config.runner.validation_prs, frozenset({1849, 1844}))
        self.assertTrue(config.publisher_settings("local")["enabled"])
        with self.assertRaises(TypeError):
            config.devices[0].interfaces["new"] = {}  # type: ignore[index]

    def test_rejects_invalid_validation_pr_config(self) -> None:
        invalid_values = ('"1849"', '[1849, false]', '[0]')
        for value in invalid_values:
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "config.toml"
                path.write_text(
                    f"[runner]\nvalidation_prs = {value}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigError, "validation_prs"):
                    load_config(path)

    def test_rejects_duplicate_device_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[[devices]]
name = "same"
type = "bitaxe_bonanza"
[[devices]]
name = "same"
type = "bitaxe_bonanza"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "duplicate device name"):
                load_config(path)

    def test_requires_missing_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                """
[[devices]]
name = "lab"
type = "bitaxe_bonanza"
[devices.interfaces.api]
base_url = "${A_VARIABLE_THAT_IS_NOT_SET}"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "required environment variable"):
                load_config(path)
