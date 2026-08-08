from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import yaml

from miner_testcode.errors import ConfigError
from miner_testcode.orchestrator.config import ConfigStore, validate_config
from miner_testcode.orchestrator.database import OrchestratorDatabase
from miner_testcode.orchestrator.engine import OrchestratorEngine, Planner
from miner_testcode.orchestrator.events import cron_matches, paths_match
from miner_testcode.orchestrator.qa_status import GatePublisher


def configuration(root: Path) -> dict:
    return {
        "schema_version": 1,
        "controller": {"state_dir": str(root / "state")},
        "qa_status": {"enabled": False},
        "repositories": {
            "firmware": {
                "repository": "owner/firmware",
                "pushes": {"branches": ["main", "master"]},
                "pull_requests": {
                    "base_branches": ["main", "master"],
                    "trusted_contributors": ["alice"],
                },
            }
        },
        "test_modules": {
            "smoke": {
                "pattern": "test_smoke.py",
                "device_types": ["bitaxe_bonanza"],
                "required_interfaces": ["api"],
            },
            "regression": {
                "pattern": "test_regression.py",
                "device_types": ["bitaxe_bonanza"],
            },
        },
        "lab": {
            "hosts": {"local": {"transport": "local"}},
            "devices": {
                "bonanza": {
                    "name": "Bonanza",
                    "type": "bitaxe_bonanza",
                    "host": "local",
                    "addresses": {"api": "http://bitaxe.local"},
                    "usb": {"serial_path": "/dev/serial/by-id/example"},
                    "tags": ["bonanza"],
                }
            },
            "setups": {
                "bench": {
                    "host": "local",
                    "platform_key": "bitaxe-bonanza-1002",
                    "runner_profile": "runner.toml",
                    "devices": {"miner": "bonanza"},
                }
            },
        },
        "gates": {
            "firmware-smoke": {
                "repository": "firmware",
                "triggers": {"pushes": True, "pull_requests": True, "schedules": []},
                "changes": {"include": ["src/**"], "exclude": ["doc/**"]},
                "test_modules": ["smoke", "regression"],
                "targets": {"setups": ["bench"]},
                "required": "all",
            }
        },
    }


class ConfigStoreTest(unittest.TestCase):
    def test_validates_references_and_writes_revision_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "orchestrator.yaml"
            path.write_text(yaml.safe_dump(configuration(root), sort_keys=False))
            store = ConfigStore(path)
            original = store.snapshot
            updated = configuration(root)
            updated["gates"]["firmware-smoke"]["name"] = "Firmware qualification"
            replacement = store.replace(updated, expected_revision=original.revision)

            self.assertNotEqual(original.revision, replacement.revision)
            self.assertEqual(ConfigStore(path).snapshot.revision, replacement.revision)
            self.assertEqual(len(list((root / ".orchestrator-backups").glob("*.bak"))), 1)
            with self.assertRaisesRegex(ConfigError, "revision"):
                store.replace(updated, expected_revision=original.revision)

    def test_rejects_plaintext_secrets_and_broken_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = configuration(Path(directory))
            document["qa_status"]["token"] = "secret"
            with self.assertRaisesRegex(ConfigError, "plaintext secrets"):
                validate_config(document)

            document = configuration(Path(directory))
            document["gates"]["firmware-smoke"]["targets"]["setups"] = ["missing"]
            with self.assertRaisesRegex(ConfigError, "unknown setup"):
                validate_config(document)


class SchedulingTest(unittest.TestCase):
    def test_cron_and_change_filters(self) -> None:
        when = datetime(2026, 8, 8, 3, 17, tzinfo=UTC)
        self.assertTrue(cron_matches("17 3 * * *", when))
        self.assertTrue(cron_matches("*/17 3 * * 6", when))
        self.assertFalse(cron_matches("18 3 * * *", when))
        self.assertTrue(paths_match(["src/main.c"], {"include": ["src/**"]}))
        self.assertFalse(
            paths_match(
                ["doc/design.md"],
                {"include": ["**"], "exclude": ["doc/**"]},
            )
        )

    def test_plans_one_assignment_per_setup_and_module_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config(configuration(root))
            database = OrchestratorDatabase(root / "state.sqlite3")
            event, _ = database.create_event(
                event_key="push:1",
                repository_id="firmware",
                trigger_type="push",
                commit_sha="a" * 40,
                branch="main",
                changed_paths=["src/main.c"],
            )
            planner = Planner(database)
            self.assertEqual(planner.plan(config), 1)
            self.assertEqual(planner.plan(config), 0)
            runs = database.list_gate_runs()
            assignments = database.assignments(runs[0]["id"])
            database.close()

        self.assertEqual(len(assignments), 2)
        self.assertEqual({item["module_id"] for item in assignments}, {"smoke", "regression"})
        self.assertEqual({item["platform_key"] for item in assignments}, {"bitaxe-bonanza-1002"})

    def test_executes_manual_gate_and_reads_existing_child_result_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fake-miner-test"
            script.write_text(
                """#!/usr/bin/env python3
import json, os
from pathlib import Path
metadata = json.loads(os.environ["MINER_TEST_ORCHESTRATION_METADATA"])
assert metadata["gate_id"] == "firmware-smoke"
pointer = Path(os.environ["MINER_TEST_RESULT_POINTER"])
pointer.parent.mkdir(parents=True, exist_ok=True)
pointer.write_text(json.dumps({
    "status": "passed",
    "publishers": [{
        "name": "mining_qa_status", "success": True,
        "url": "https://qa.example/results/child-result-id"
    }]
}))
""",
                encoding="utf-8",
            )
            script.chmod(0o700)
            document = configuration(root)
            document["lab"]["hosts"]["local"]["miner_test"] = str(script)
            (root / "runner.toml").write_text("", encoding="utf-8")
            path = root / "orchestrator.yaml"
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            store = ConfigStore(path)
            database = OrchestratorDatabase(root / "state.sqlite3")
            engine = OrchestratorEngine(store, database)
            run = engine.manual_run("firmware-smoke", "a" * 40, "main")
            while engine.tick():
                pass
            completed = database.gate_run(run["id"])

            self.assertEqual(completed["status"], "passed")
            self.assertEqual(
                {item["qa_result_id"] for item in completed["assignments"]},
                {"child-result-id"},
            )
            self.assertTrue(
                all(Path(item["result_pointer"]).is_file() for item in completed["assignments"])
            )
            database.close()

    def test_resource_leases_are_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config(configuration(root))
            database = OrchestratorDatabase(root / "state.sqlite3")
            event, _ = database.create_event(
                event_key="manual:1",
                repository_id="firmware",
                trigger_type="manual",
                commit_sha="b" * 40,
            )
            Planner(database).plan(config)
            first, second = database.assignments(database.list_gate_runs()[0]["id"])
            self.assertTrue(database.acquire(first["id"], ["device:bonanza"]))
            self.assertFalse(database.acquire(second["id"], ["device:bonanza"]))
            database.finish_assignment(first["id"], status="passed")
            self.assertTrue(database.acquire(second["id"], ["device:bonanza"]))
            database.close()


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def json_request(self, method, url, body, *, token=None, headers=None, timeout=20):
        self.calls.append({"method": method, "url": url, "body": body, "token": token})
        if url.endswith("/results"):
            return {"linked": True}
        return {"run": {"id": "gate-qa-id"}}

    def put_file(self, *args, **kwargs):
        raise AssertionError("gate publisher must not upload child artifacts")


class GatePublisherTest(unittest.TestCase):
    def test_publishes_only_gate_and_links_existing_result(self) -> None:
        transport = FakeTransport()
        publisher = GatePublisher(
            {
                "enabled": True,
                "base_url": "https://qa.example",
                "token_env": "TEST_QA_TOKEN",
            },
            transport=transport,
        )
        run = {
            "id": "gate-run-1",
            "gate_id": "firmware-smoke",
            "repository_id": "firmware",
            "commit_sha": "a" * 40,
            "branch": "main",
            "pr_number": None,
            "trigger_type": "push",
            "definition_digest": "b" * 64,
            "required_policy": "all",
            "status": "passed",
            "summary": "1/1 assignments passed",
            "started_at": 1.0,
            "finished_at": 2.0,
        }
        assignment = {
            "id": "assignment-1",
            "setup_id": "bench",
            "module_id": "smoke",
            "platform_key": "bitaxe-bonanza-1002",
            "status": "passed",
            "qa_result_id": "child-id",
            "qa_result_url": "https://qa.example/results/child-id",
        }
        with mock.patch.dict("os.environ", {"TEST_QA_TOKEN": "secret"}):
            published = publisher.publish_run(
                run,
                gate={"name": "Firmware smoke"},
                repository={"repository": "owner/firmware"},
                assignments=[assignment],
            )
            publisher.link_result("gate-qa-id", assignment, "child-id")

        self.assertEqual(published["id"], "gate-qa-id")
        self.assertEqual(transport.calls[0]["body"]["platforms"], ["bitaxe-bonanza-1002"])
        self.assertEqual(transport.calls[1]["body"]["result_id"], "child-id")
        self.assertEqual(len(transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
