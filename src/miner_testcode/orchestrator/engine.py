from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from ..errors import ConfigError
from ..publishers import PublishError
from .config import ConfigStore, config_digest
from .database import OrchestratorDatabase
from .events import EventCollector, paths_match
from .firmware import FirmwareDeployer
from .qa_status import GatePublisher

logger = logging.getLogger(__name__)


def _platform_key(
    setup: Mapping[str, Any], devices: Mapping[str, Mapping[str, Any]]
) -> str:
    configured = setup.get("platform_key")
    if isinstance(configured, str) and configured:
        return configured
    types = sorted({str(devices[item]["type"]) for item in setup["devices"].values()})
    return "+".join(types)


def _definition_digest(gate: Mapping[str, Any]) -> str:
    return config_digest(gate)


class Planner:
    def __init__(self, database: OrchestratorDatabase) -> None:
        self.database = database

    def plan(self, config: Mapping[str, Any]) -> int:
        created = 0
        for event in reversed(self.database.list_events(unplanned=True)):
            requested_gate = event["payload"].get("gate_id")
            for gate_id, gate in config["gates"].items():
                if gate["repository"] != event["repository_id"]:
                    continue
                if requested_gate and requested_gate != gate_id:
                    continue
                trigger = event["trigger_type"]
                if trigger == "push" and not gate["triggers"].get("pushes", True):
                    continue
                if trigger == "pull_request" and not gate["triggers"].get("pull_requests", True):
                    continue
                if trigger in {"push", "pull_request"} and not paths_match(
                    event["changed_paths"], gate.get("changes")
                ):
                    continue
                lab = config["lab"]
                assignments: list[dict[str, str]] = []
                for setup_id in gate["targets"]["setups"]:
                    setup = lab["setups"][setup_id]
                    platform = _platform_key(setup, lab["devices"])
                    for module_id in gate["test_modules"]:
                        assignments.append(
                            {
                                "setup_id": setup_id,
                                "module_id": module_id,
                                "platform_key": platform,
                            }
                        )
                _, inserted = self.database.create_gate_run(
                    gate_id=gate_id,
                    event=event,
                    definition_digest=_definition_digest(gate),
                    required_policy=gate.get("required", "all"),
                    assignments=assignments,
                    config_snapshot=config,
                )
                created += int(inserted)
            self.database.mark_event_planned(event["id"])
        return created


class AssignmentExecutor:
    def __init__(
        self,
        database: OrchestratorDatabase,
        config_store: ConfigStore,
        gate_publisher: GatePublisher,
        firmware_deployer: FirmwareDeployer | None = None,
    ) -> None:
        self.database = database
        self.config_store = config_store
        self.gate_publisher = gate_publisher
        self.firmware_deployer = firmware_deployer or FirmwareDeployer()

    def _resources(self, assignment: Mapping[str, Any], config: Mapping[str, Any]) -> list[str]:
        setup = config["lab"]["setups"][assignment["setup_id"]]
        return [f"device:{item}" for item in setup["devices"].values()]

    def _safe_environment(self, controller: Mapping[str, Any]) -> dict[str, str]:
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "TZ",
            "TMPDIR",
            "MINING_QA_TOKEN",
            "MINER_TEST_POOL_USER",
            "MINER_TEST_POOL_PASSWORD",
            "MINER_TEST_FAKE_STRATUM_PASSWORD",
            "MINER_CURRENT_POOL_PASSWORD",
        }
        allowed.update(controller.get("environment_allowlist", []))
        return {key: value for key, value in os.environ.items() if key in allowed}

    @staticmethod
    def _qa_result(pointer: Mapping[str, Any]) -> tuple[str | None, str | None]:
        for publisher in pointer.get("publishers", []):
            if publisher.get("name") != "mining_qa_status" or not publisher.get("success"):
                continue
            url = publisher.get("url")
            if isinstance(url, str) and url:
                result_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
                return result_id, url
        return None, None

    def execute(self, assignment: Mapping[str, Any]) -> None:
        snapshot = self.config_store.snapshot
        run = self.database.gate_run(assignment["gate_run_id"], include_config=True)
        config = run["config_snapshot"]
        resources = self._resources(assignment, config)
        if not self.database.acquire(assignment["id"], resources):
            return
        gate = config["gates"][run["gate_id"]]
        repository = config["repositories"][run["repository_id"]]
        lab = config["lab"]
        setup = lab["setups"][assignment["setup_id"]]
        host_id = setup.get("host") or lab["devices"][next(iter(setup["devices"].values()))]["host"]
        host = lab["hosts"][host_id]
        module = config["test_modules"][assignment["module_id"]]
        disabled = [
            item
            for item in setup["devices"].values()
            if not lab["devices"][item].get("enabled", True)
        ]
        if disabled:
            self.database.finish_assignment(
                assignment["id"],
                status="error",
                detail=f"setup contains disabled devices: {', '.join(disabled)}",
            )
            return
        state_dir = Path(config["controller"]["state_dir"])
        if not state_dir.is_absolute():
            state_dir = (snapshot.source.parent / state_dir).resolve()
        job_dir = state_dir / "jobs" / run["id"] / assignment["id"]
        job_dir.mkdir(parents=True, exist_ok=True)
        pointer = job_dir / "result-pointer.json"
        log_path = job_dir / "worker.log"
        profile = Path(module.get("runner_profile") or setup["runner_profile"])
        if host["transport"] == "local" and not profile.is_absolute():
            profile = (snapshot.source.parent / profile).resolve()

        try:
            deployment = self.firmware_deployer.ensure(
                run,
                assignment["setup_id"],
                config,
                state_dir,
            )
        except ConfigError as exc:
            log_path.write_text(
                f"{type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            self.database.finish_assignment(
                assignment["id"],
                status="error",
                detail=str(exc)[:2000],
            )
            return

        metadata = {
            "gate_id": run["gate_id"],
            "gate_run_id": run["id"],
            "gate_definition_digest": run["definition_digest"],
            "assignment_id": assignment["id"],
            "module_id": assignment["module_id"],
            "platform_key": assignment["platform_key"],
            "setup": assignment["setup_id"],
            "attempt": int(assignment["attempt"]) + 1,
            "trigger": {
                "type": run["trigger_type"],
                "branch": run.get("branch"),
                "pr_number": run.get("pr_number"),
            },
            "gate_result_id": run.get("qa_result_id"),
            "gate_result_url": run.get("qa_result_url"),
        }
        if deployment:
            metadata["firmware"] = deployment
        environment = self._safe_environment(config["controller"])
        result_pointer = pointer
        if host["transport"] == "ssh":
            remote_root = Path(str(host.get("work_root") or "/tmp/miner-testcode-orchestrator"))
            result_pointer = remote_root / "jobs" / run["id"] / assignment["id"] / "result-pointer.json"
        environment.update(
            {
                "MINER_TEST_ORCHESTRATION_METADATA": json.dumps(metadata, separators=(",", ":")),
                "MINER_TEST_EXTERNAL_RUN_ID": f"gate:{run['id']}:assignment:{assignment['id']}",
                "MINER_TEST_RESULT_POINTER": str(result_pointer),
                "GITHUB_REPOSITORY": repository["repository"],
                "GITHUB_SHA": run["commit_sha"],
                "GITHUB_REF_NAME": run.get("branch") or "",
            }
        )
        if run.get("pr_number"):
            environment["MINER_TEST_PR_NUMBER"] = str(run["pr_number"])
        executable = str(host.get("miner_test") or "miner-test")
        command = [executable, "--config", str(profile), "--pattern", module["pattern"]]
        runner_names = setup.get("runner_devices")
        if isinstance(runner_names, list):
            for name in runner_names:
                command.extend(["--device", str(name)])
        if run.get("pr_number") and module.get("validation_pr", True):
            command.extend(["--validation-pr", str(run["pr_number"])])

        cwd = setup.get("working_directory") or host.get("working_directory")
        timeout = float(module.get("timeout", gate.get("timeout", 3600)))
        try:
            if host["transport"] == "local":
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
                output = result.stdout
                returncode = result.returncode
            else:
                remote_env = [f"{key}={value}" for key, value in environment.items()]
                remote_command = shlex.join(["env", *remote_env, *command])
                if cwd:
                    remote_command = f"cd {shlex.quote(str(cwd))} && {remote_command}"
                result = subprocess.run(
                    ["ssh", "-o", "ForwardAgent=no", host["ssh_target"], remote_command],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
                output = result.stdout
                returncode = result.returncode
                pointer_result = subprocess.run(
                    ["ssh", "-o", "ForwardAgent=no", host["ssh_target"], "cat", str(result_pointer)],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                if pointer_result.returncode == 0:
                    pointer.write_text(pointer_result.stdout, encoding="utf-8")
            log_path.write_text(output, encoding="utf-8")
            pointer_payload = json.loads(pointer.read_text(encoding="utf-8")) if pointer.exists() else {}
            result_id, result_url = self._qa_result(pointer_payload)
            status = str(pointer_payload.get("status") or ("passed" if returncode == 0 else "error"))
            if status not in {"passed", "failed", "error", "skipped"}:
                status = "error"
            self.database.finish_assignment(
                assignment["id"],
                status=status,
                detail=f"miner-test exited {returncode}",
                result_pointer=str(pointer) if pointer.exists() else None,
                qa_result_id=result_id,
                qa_result_url=result_url,
            )
            updated_run = self.database.gate_run(run["id"], include_config=True)
            if result_id and updated_run.get("qa_result_id"):
                self.gate_publisher.link_result(
                    updated_run["qa_result_id"],
                    next(item for item in updated_run["assignments"] if item["id"] == assignment["id"]),
                    result_id,
                )
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, PublishError) as exc:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            self.database.finish_assignment(
                assignment["id"], status="error", detail=str(exc)[:2000]
            )


class OrchestratorEngine:
    def __init__(self, config_store: ConfigStore, database: OrchestratorDatabase) -> None:
        self.config_store = config_store
        self.database = database
        self.collector = EventCollector(database)
        self.planner = Planner(database)
        self.publisher = GatePublisher(config_store.snapshot.document["qa_status"])
        self.executor = AssignmentExecutor(database, config_store, self.publisher)

    def poll(self) -> int:
        config = self.config_store.snapshot.document
        created = 0
        for repository_id, repository in config["repositories"].items():
            created += self.collector.poll_repository(repository_id, repository)
        created += self.collector.collect_schedules(config)
        created += self.planner.plan(config)
        return created

    def _publish(self, run: Mapping[str, Any]) -> None:
        config = run.get("config_snapshot") or self.config_store.snapshot.document
        self.publisher.config = self.config_store.snapshot.document["qa_status"]
        gate = config["gates"][run["gate_id"]]
        repository = config["repositories"][run["repository_id"]]
        published = self.publisher.publish_run(
            run,
            gate=gate,
            repository=repository,
            assignments=run["assignments"],
        )
        if published:
            base_url = str(config["qa_status"]["base_url"]).rstrip("/")
            self.database.update_gate_run(
                run["id"],
                status=run["status"],
                qa_result_id=str(published["id"]),
                qa_result_url=f"{base_url}/gates/runs/{published['id']}",
            )
            for assignment in run["assignments"]:
                if assignment.get("qa_result_id"):
                    self.publisher.link_result(
                        str(published["id"]),
                        assignment,
                        str(assignment["qa_result_id"]),
                    )

    def tick(self) -> bool:
        self.planner.plan(self.config_store.snapshot.document)
        assignment = self.database.next_assignment()
        if assignment is not None:
            run = self.database.gate_run(assignment["gate_run_id"], include_config=True)
            if run["status"] == "queued":
                self.database.update_gate_run(run["id"], status="running")
                run = self.database.gate_run(run["id"], include_config=True)
                try:
                    self._publish(run)
                except PublishError as exc:
                    logger.error("could not publish running gate %s: %s", run["id"], exc)
            self.executor.execute(assignment)
            self._finish_if_complete(assignment["gate_run_id"])
            return True
        return False

    def _finish_if_complete(self, run_id: str) -> None:
        run = self.database.gate_run(run_id, include_config=True)
        assignments = run["assignments"]
        if any(item["status"] in {"queued", "running"} for item in assignments):
            return
        statuses = [item["status"] for item in assignments]
        if not statuses or all(item == "skipped" for item in statuses):
            status = "skipped"
        elif any(item == "cancelled" for item in statuses):
            status = "cancelled"
        elif any(item == "error" for item in statuses):
            status = "error"
        elif run["required_policy"] == "all":
            status = "passed" if all(item in {"passed", "skipped"} for item in statuses) else "failed"
        else:
            status = "passed" if any(item == "passed" for item in statuses) else "failed"
        summary = f"{sum(item == 'passed' for item in statuses)}/{len(statuses)} assignments passed"
        self.database.update_gate_run(run_id, status=status, summary=summary)
        try:
            self._publish(self.database.gate_run(run_id, include_config=True))
        except PublishError as exc:
            logger.error("could not publish completed gate %s: %s", run_id, exc)

    def manual_run(self, gate_id: str, commit_sha: str, branch: str | None = None) -> dict[str, Any]:
        config = self.config_store.snapshot.document
        if gate_id not in config["gates"]:
            raise ConfigError(f"unknown gate: {gate_id}")
        if len(commit_sha) < 7 or any(character not in "0123456789abcdefABCDEF" for character in commit_sha):
            raise ConfigError("commit_sha must be hexadecimal and at least seven characters")
        gate = config["gates"][gate_id]
        event = self.collector.manual(
            repository_id=gate["repository"],
            commit_sha=commit_sha.lower(),
            branch=branch,
            gate_id=gate_id,
        )
        self.planner.plan(config)
        runs = self.database.list_gate_runs(gate_id=gate_id)
        return next(item for item in runs if item["event_id"] == event["id"])
