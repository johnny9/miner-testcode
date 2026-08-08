from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Mapping

from ..errors import ConfigError
from ..publishers import HttpTransport, PublishError, UrlLibTransport


def _timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class GatePublisher:
    """Publish only gate records and links; child results publish themselves."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def _connection(self) -> tuple[str, str]:
        base_url = str(self.config.get("base_url") or "").rstrip("/")
        token_env = str(self.config.get("token_env") or "MINING_QA_TOKEN")
        token = os.environ.get(token_env, "").strip()
        if not base_url or not token:
            raise PublishError(
                f"gate publication requires qa_status.base_url and environment {token_env}"
            )
        return base_url, token

    def publish_run(
        self,
        gate_run: Mapping[str, Any],
        *,
        gate: Mapping[str, Any],
        repository: Mapping[str, Any],
        assignments: list[Mapping[str, Any]],
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        base_url, token = self._connection()
        payload = {
            "gate_key": gate_run["gate_id"],
            "gate_name": str(gate.get("name") or gate_run["gate_id"]),
            "description": gate.get("description"),
            "repository": repository["repository"],
            "commit_sha": gate_run["commit_sha"],
            "branch": gate_run.get("branch"),
            "pr_number": gate_run.get("pr_number"),
            "trigger_type": gate_run["trigger_type"],
            "status": gate_run["status"],
            "definition_digest": gate_run["definition_digest"],
            "external_run_id": gate_run["id"],
            "summary": gate_run.get("summary"),
            "started_at": _timestamp(gate_run.get("started_at")),
            "finished_at": _timestamp(gate_run.get("finished_at")),
            "platforms": sorted({item["platform_key"] for item in assignments}),
            "details": {
                "required_policy": gate_run.get("required_policy", "all"),
                "assignments": [
                    {
                        "assignment_id": item["id"],
                        "setup": item["setup_id"],
                        "module": item["module_id"],
                        "platform_key": item["platform_key"],
                        "status": item["status"],
                        "result_id": item.get("qa_result_id"),
                        "result_url": item.get("qa_result_url"),
                    }
                    for item in assignments
                ],
            },
        }
        response = self.transport.json_request(
            "POST",
            f"{base_url}/api/v1/gates/runs",
            payload,
            token=token,
            timeout=float(self.config.get("timeout", 30)),
        )
        run = response.get("run")
        if not isinstance(run, dict) or not run.get("id"):
            raise PublishError("Mining QA gate response did not include run.id")
        return run

    def link_result(
        self,
        gate_qa_run_id: str,
        assignment: Mapping[str, Any],
        result_id: str,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        base_url, token = self._connection()
        return self.transport.json_request(
            "POST",
            f"{base_url}/api/v1/gates/runs/{gate_qa_run_id}/results",
            {
                "result_id": result_id,
                "assignment_id": assignment["id"],
                "platform_key": assignment["platform_key"],
                "setup_name": assignment["setup_id"],
                "module_id": assignment["module_id"],
                "required": True,
            },
            token=token,
            timeout=float(self.config.get("timeout", 30)),
        )
