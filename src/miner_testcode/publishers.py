from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .artifacts import append_jsonl
from .errors import ConfigError, MinerTestError
from .results import PublisherRecord, RunSummary, iso_timestamp

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_MAX_QA_ARTIFACT_BYTES = 50 * 1024 * 1024


class PublishError(MinerTestError):
    """A configured result publisher failed."""


class HttpTransport(Protocol):
    def json_request(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any],
        *,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]: ...

    def put_file(
        self,
        url: str,
        path: Path,
        *,
        content_type: str,
        timeout: float = 120.0,
    ) -> None: ...


class UrlLibTransport:
    """Bounded stdlib HTTP transport that never includes tokens in errors."""

    def json_request(
        self,
        method: str,
        url: str,
        body: Mapping[str, Any],
        *,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **dict(headers or {}),
        }
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        request = Request(
            url,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise PublishError(f"{method} {url} response exceeded 4 MiB")
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise PublishError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise PublishError(f"{method} {url} failed: {exc}") from exc
        if not payload:
            return {}
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PublishError(f"{method} {url} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise PublishError(f"{method} {url} returned non-object JSON")
        return value

    def put_file(
        self,
        url: str,
        path: Path,
        *,
        content_type: str,
        timeout: float = 120.0,
    ) -> None:
        request = Request(
            url,
            data=path.read_bytes(),
            headers={
                "Content-Type": content_type,
                "Cache-Control": "max-age=3600",
                "x-upsert": "false",
            },
            method="PUT",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                response.read(1024)
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise PublishError(f"PUT signed artifact URL returned HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise PublishError(f"PUT signed artifact URL failed: {exc}") from exc


def _configured_value(
    config: Mapping[str, Any],
    key: str,
    *,
    env_key: str | None = None,
    default_env: str | None = None,
    required: bool = False,
) -> str | None:
    value = config.get(key)
    if value is not None:
        result = str(value).strip()
    else:
        variable = str(config.get(env_key, default_env or "")) if env_key else default_env
        result = os.environ.get(variable, "").strip() if variable else ""
    if required and not result:
        source = default_env or key
        raise PublishError(f"{key} is required; configure it or set {source}")
    return result or None


def _enabled(config: Mapping[str, Any]) -> bool:
    return bool(config.get("enabled", False))


def _required(config: Mapping[str, Any]) -> bool:
    return bool(config.get("required", True))


def _safe_report_path(root: Path, filename: Any, default: str) -> Path:
    name = str(filename or default)
    if Path(name).name != name or name in {".", ".."}:
        raise ConfigError("publisher output filename must be a plain filename")
    return root / name


class LocalHtmlPublisher:
    name = "local"

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = config

    def publish(self, summary: RunSummary) -> PublisherRecord:
        required = _required(self.config)
        html_path = _safe_report_path(
            summary.artifact_root, self.config.get("filename"), "report.html"
        )
        json_path = _safe_report_path(
            summary.artifact_root, self.config.get("json_filename"), "result.json"
        )
        summary.write_json(json_path)
        html_path.write_text(self._render(summary, html_path), encoding="utf-8")
        return PublisherRecord(
            name=self.name,
            success=True,
            required=required,
            url=html_path.resolve().as_uri(),
            detail=str(html_path),
        )

    @staticmethod
    def _artifact_links(summary: RunSummary, artifact_dir: str | None) -> str:
        if not artifact_dir:
            return "<span class=\"muted\">No artifacts</span>"
        directory = summary.artifact_root / artifact_dir
        if not directory.is_dir():
            return "<span class=\"muted\">No artifacts</span>"
        links: list[str] = []
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            relative = path.relative_to(summary.artifact_root).as_posix()
            href = quote(relative, safe="/")
            label = html.escape(path.relative_to(directory).as_posix())
            links.append(f'<a href="{href}">{label}</a>')
        return " ".join(links) or "<span class=\"muted\">No artifacts</span>"

    def _render(self, summary: RunSummary, html_path: Path) -> str:
        rows: list[str] = []
        for record in summary.tests:
            detail = ""
            if record.detail:
                detail = (
                    "<details><summary>Failure details</summary><pre>"
                    + html.escape(record.detail)
                    + "</pre></details>"
                )
            rows.append(
                "<tr>"
                f'<td><span class="status {html.escape(record.outcome)}">'
                f"{html.escape(record.outcome)}</span></td>"
                f"<td>{html.escape(record.device)}</td>"
                f"<td><code>{html.escape(record.test_id)}</code>{detail}</td>"
                f"<td>{record.elapsed_seconds:.3f}s</td>"
                f"<td>{self._artifact_links(summary, record.artifact_dir)}</td>"
                "</tr>"
            )
        publisher_rows = "".join(
            "<tr>"
            f"<td>{html.escape(record.name)}</td>"
            f"<td>{'published' if record.success else 'failed'}</td>"
            f"<td>{'yes' if record.required else 'no'}</td>"
            f"<td>{self._publisher_link(record)}</td>"
            "</tr>"
            for record in summary.publishers
        )
        root_links: list[str] = []
        for path in sorted(
            item for item in summary.artifact_root.iterdir() if item.is_file() and item != html_path
        ):
            root_links.append(
                f'<a href="{quote(path.name)}">{html.escape(path.name)}</a>'
            )
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>miner-testcode {html.escape(summary.run_id)}</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1440px; margin: 2rem auto; padding: 0 1rem; }}
h1 {{ margin-bottom: .25rem; }} .muted {{ opacity: .7; }}
.cards {{ display: flex; flex-wrap: wrap; gap: .75rem; margin: 1.5rem 0; }}
.card {{ border: 1px solid #8886; border-radius: .6rem; padding: .8rem 1rem; min-width: 8rem; }}
.card strong {{ display: block; font-size: 1.5rem; }}
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0 2rem; }}
th, td {{ border-bottom: 1px solid #8885; padding: .65rem; text-align: left; vertical-align: top; }}
.status {{ border-radius: 1rem; padding: .2rem .55rem; font-weight: 650; }}
.passed, .expected_failure {{ background: #16803b33; color: #2fbf64; }}
.failed, .error, .unexpected_success {{ background: #c6282833; color: #ff6b6b; }}
.skipped {{ background: #8a6d1d33; color: #d7ae35; }}
a {{ margin-right: .7rem; }} code, pre {{ font-family: ui-monospace, monospace; }}
pre {{ max-width: 75vw; overflow: auto; white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>miner-testcode result</h1>
<p class="muted">Run {html.escape(summary.run_id)} · {iso_timestamp(summary.started_at)} · {summary.duration_seconds:.3f}s</p>
<div class="cards">
  <div class="card"><span>Status</span><strong>{html.escape(summary.status)}</strong></div>
  <div class="card"><span>Tests</span><strong>{summary.tests_run}</strong></div>
  <div class="card"><span>Passed</span><strong>{summary.passed_count}</strong></div>
  <div class="card"><span>Failures</span><strong>{summary.failures}</strong></div>
  <div class="card"><span>Errors</span><strong>{summary.errors}</strong></div>
  <div class="card"><span>Skipped</span><strong>{summary.skipped}</strong></div>
</div>
<h2>Tests</h2>
<table><thead><tr><th>Result</th><th>Device</th><th>Test</th><th>Time</th><th>Artifacts</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Run artifacts</h2><p>{' '.join(root_links) or '<span class="muted">None</span>'}</p>
<h2>Publishers</h2>
<table><thead><tr><th>Publisher</th><th>Status</th><th>Required</th><th>Details</th></tr></thead>
<tbody>{publisher_rows or '<tr><td colspan="4" class="muted">No publisher results</td></tr>'}</tbody></table>
</body></html>
"""

    @staticmethod
    def _publisher_link(record: PublisherRecord) -> str:
        if record.url:
            return f'<a href="{html.escape(record.url, quote=True)}">Open result</a>'
        return html.escape(record.detail or "")


class GithubCheckPublisher:
    name = "github"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()

    def publish(self, summary: RunSummary, *, details_url: str | None = None) -> PublisherRecord:
        required = _required(self.config)
        token = _configured_value(
            self.config,
            "token",
            env_key="token_env",
            default_env="GITHUB_TOKEN",
            required=True,
        )
        repository = _configured_value(
            self.config,
            "repository",
            env_key="repository_env",
            default_env="GITHUB_REPOSITORY",
            required=True,
        )
        head_sha = _configured_value(
            self.config,
            "head_sha",
            env_key="sha_env",
            default_env="GITHUB_SHA",
            required=True,
        )
        if not _REPOSITORY.fullmatch(repository or ""):
            raise PublishError("GitHub repository must have owner/name form")
        if not _SHA.fullmatch(head_sha or ""):
            raise PublishError("GitHub head SHA must contain 7 to 64 hexadecimal characters")
        configured_url = _configured_value(
            self.config,
            "details_url",
            env_key="details_url_env",
            default_env="MINER_TEST_DETAILS_URL",
        )
        result_url = configured_url or details_url
        if result_url and not result_url.startswith(("http://", "https://")):
            result_url = None
        conclusion = {
            "passed": "success",
            "failed": "failure",
            "error": "failure",
            "skipped": "skipped",
        }[summary.status]
        check_name = str(self.config.get("name", "miner-testcode / hardware-e2e"))
        text = self._markdown(summary)
        payload: dict[str, Any] = {
            "name": check_name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": iso_timestamp(summary.finished_at),
            "external_id": summary.run_id,
            "output": {
                "title": f"{summary.passed_count}/{summary.tests_run} tests passed",
                "summary": text[:65_535],
            },
        }
        if result_url:
            payload["details_url"] = result_url
        api_url = str(self.config.get("api_url", "https://api.github.com")).rstrip("/")
        response = self.transport.json_request(
            "POST",
            f"{api_url}/repos/{repository}/check-runs",
            payload,
            token=token,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=float(self.config.get("timeout", 20.0)),
        )
        url = response.get("html_url")
        return PublisherRecord(
            name=self.name,
            success=True,
            required=required,
            url=str(url) if url else result_url,
            detail=f"check_run_id={response.get('id')}" if response.get("id") else None,
        )

    @staticmethod
    def _markdown(summary: RunSummary) -> str:
        lines = [
            f"**Status:** {summary.status}",
            "",
            f"{summary.passed_count}/{summary.tests_run} passed; "
            f"{summary.failures} failed; {summary.errors} errors; {summary.skipped} skipped.",
            "",
            "| Device | Test | Result | Duration |",
            "|---|---|---:|---:|",
        ]
        for record in summary.tests[:100]:
            test_id = record.test_id.replace("|", "\\|")
            device = record.device.replace("|", "\\|")
            lines.append(
                f"| {device} | `{test_id}` | {record.outcome} | {record.elapsed_seconds:.3f}s |"
            )
        if len(summary.tests) > 100:
            lines.append(f"\n{len(summary.tests) - 100} additional tests omitted.")
        return "\n".join(lines)


class MiningQaStatusPublisher:
    name = "mining_qa_status"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        transport: HttpTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibTransport()

    def publish(self, summary: RunSummary) -> PublisherRecord:
        required = _required(self.config)
        base_url = _configured_value(
            self.config,
            "base_url",
            env_key="base_url_env",
            default_env="MINING_QA_URL",
            required=True,
        )
        token = _configured_value(
            self.config,
            "token",
            env_key="token_env",
            default_env="MINING_QA_TOKEN",
            required=True,
        )
        repository = _configured_value(
            self.config,
            "repository",
            env_key="repository_env",
            default_env="GITHUB_REPOSITORY",
            required=True,
        )
        commit_sha = _configured_value(
            self.config,
            "commit_sha",
            env_key="commit_sha_env",
            default_env="GITHUB_SHA",
            required=True,
        )
        if not _REPOSITORY.fullmatch(repository or ""):
            raise PublishError("Mining QA repository must have owner/name form")
        if not _SHA.fullmatch(commit_sha or ""):
            raise PublishError("Mining QA commit SHA must contain 7 to 64 hexadecimal characters")
        base_url = (base_url or "").rstrip("/")
        result_input = self._result_input(summary, repository or "", commit_sha or "")
        response = self.transport.json_request(
            "POST",
            f"{base_url}/api/v1/results",
            result_input,
            token=token,
            timeout=float(self.config.get("timeout", 30.0)),
        )
        result = response.get("result")
        if not isinstance(result, dict) or not result.get("id"):
            raise PublishError("Mining QA result response did not include result.id")
        result_id = str(result["id"])
        if bool(self.config.get("upload_artifacts", True)):
            for path in self._artifact_paths(summary):
                self._upload_artifact(base_url, token or "", result_id, path)
        url = f"{base_url}/results/{result_id}"
        return PublisherRecord(
            name=self.name,
            success=True,
            required=required,
            url=url,
            detail=f"result_id={result_id}",
        )

    def _result_input(
        self, summary: RunSummary, repository: str, commit_sha: str
    ) -> dict[str, Any]:
        device_names = ", ".join(device["name"] for device in summary.devices)
        target_type = str(
            self.config.get("target_type")
            or (summary.devices[0]["type"] if len(summary.devices) == 1 else "mining-device")
        )
        target_name = str(self.config.get("target_name") or device_names or "configured miners")
        branch = _configured_value(
            self.config,
            "branch",
            env_key="branch_env",
            default_env="GITHUB_REF_NAME",
        )
        external_run_id = _configured_value(
            self.config,
            "external_run_id",
            env_key="external_run_id_env",
            default_env="GITHUB_RUN_ID",
        ) or summary.run_id
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        github_run_id = os.environ.get("GITHUB_RUN_ID")
        external_url = _configured_value(
            self.config,
            "external_run_url",
            env_key="external_run_url_env",
            default_env="MINER_TEST_EXTERNAL_RUN_URL",
        )
        if not external_url and github_run_id:
            external_url = f"{server}/{repository}/actions/runs/{github_run_id}"
        source = str(
            self.config.get("source")
            or ("github_actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "local")
        )
        checks = [
            {
                "name": record.test_id,
                "passed": record.passed,
                "detail": f"{record.device}: {record.outcome} in {record.elapsed_seconds:.3f}s",
            }
            for record in summary.tests
        ]
        payload: dict[str, Any] = {
            "target_type": target_type[:64],
            "target_name": target_name[:128],
            "hardware": {"devices": list(summary.devices)},
            "repository": repository,
            "commit_sha": commit_sha.lower(),
            "branch": branch,
            "status": summary.status,
            "source": source,
            "suite": str(self.config.get("suite", "miner-testcode"))[:128],
            "title": str(
                self.config.get("title", f"{target_name} end-to-end qualification")
            )[:240],
            "summary": (
                f"{summary.passed_count}/{summary.tests_run} tests passed; "
                f"{summary.failures} failures, {summary.errors} errors, "
                f"{summary.skipped} skipped."
            ),
            "external_run_id": external_run_id,
            "external_run_url": external_url,
            "started_at": iso_timestamp(summary.started_at),
            "finished_at": iso_timestamp(summary.finished_at),
            "duration_ms": round(summary.duration_seconds * 1000),
            "details": {
                "passed": summary.successful,
                "checks": checks,
                "result": summary.to_dict(detail_limit=2000),
            },
        }
        pr_number = self._pr_number()
        if pr_number:
            payload["pr_number"] = pr_number
            payload["pr_url"] = f"{server}/{repository}/pull/{pr_number}"
        return payload

    def _pr_number(self) -> int | None:
        value = self.config.get("pr_number")
        if value is not None:
            number = int(value)
            return number if number > 0 else None
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not event_path:
            return None
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
            number = int(event.get("pull_request", {}).get("number", 0))
            return number if number > 0 else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _artifact_paths(self, summary: RunSummary) -> tuple[Path, ...]:
        configured = self.config.get("artifact_globs")
        patterns = configured or [
            "result.json",
            "report.html",
            "runner.log",
            "events.jsonl",
            "**/test.log",
            "**/device-state.jsonl",
            "**/serial.log",
            "**/device-api.log",
            "**/stratum-probe.json",
        ]
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise ConfigError("publishers.mining_qa_status.artifact_globs must be strings")
        selected: dict[Path, None] = {}
        for pattern in patterns:
            for path in summary.artifact_root.glob(pattern):
                if path.is_file() and path.stat().st_size > 0:
                    selected[path.resolve()] = None
        return tuple(sorted(selected))

    def _upload_artifact(
        self, base_url: str, token: str, result_id: str, path: Path
    ) -> None:
        size = path.stat().st_size
        if size > _MAX_QA_ARTIFACT_BYTES:
            raise PublishError(f"artifact {path.name} exceeds the 50 MiB collector limit")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative_name = path.name
        reservation = self.transport.json_request(
            "POST",
            f"{base_url}/api/v1/artifacts/upload-url",
            {
                "result_id": result_id,
                "filename": relative_name[:180],
                "content_type": content_type,
                "size_bytes": size,
                "sha256": digest,
            },
            token=token,
            timeout=float(self.config.get("timeout", 30.0)),
        )
        artifact_id = reservation.get("artifact_id")
        signed_url = reservation.get("signed_url")
        if not artifact_id or not signed_url:
            raise PublishError("artifact reservation omitted artifact_id or signed_url")
        self.transport.put_file(
            str(signed_url),
            path,
            content_type=content_type,
            timeout=float(self.config.get("upload_timeout", 120.0)),
        )
        self.transport.json_request(
            "POST",
            f"{base_url}/api/v1/artifacts/complete",
            {"artifact_id": artifact_id},
            token=token,
            timeout=float(self.config.get("timeout", 30.0)),
        )


class PublisherManager:
    def __init__(
        self,
        configs: Mapping[str, Mapping[str, Any]],
        *,
        logger: logging.Logger,
        transport: HttpTransport | None = None,
    ) -> None:
        unknown = set(configs).difference({"local", "github", "mining_qa_status"})
        if unknown:
            raise ConfigError(f"unknown result publisher(s): {', '.join(sorted(unknown))}")
        self.configs = configs
        self.logger = logger
        self.transport = transport

    def publish(self, summary: RunSummary) -> bool:
        local_config = self.configs.get("local", {})
        local = LocalHtmlPublisher(local_config) if _enabled(local_config) else None
        if local:
            self._run(summary, "local", _required(local_config), local.publish)

        qa_url: str | None = None
        qa_config = self.configs.get("mining_qa_status", {})
        if _enabled(qa_config):
            publisher = MiningQaStatusPublisher(qa_config, transport=self.transport)
            record = self._run(
                summary,
                "mining_qa_status",
                _required(qa_config),
                publisher.publish,
            )
            if record and record.success:
                qa_url = record.url

        github_config = self.configs.get("github", {})
        if _enabled(github_config):
            publisher = GithubCheckPublisher(github_config, transport=self.transport)
            self._run(
                summary,
                "github",
                _required(github_config),
                lambda result: publisher.publish(result, details_url=qa_url),
            )

        # Refresh the local files so they contain remote publication outcomes.
        if local:
            try:
                local.publish(summary)
            except Exception as exc:
                self.logger.error("could not finalize local result report: %s", exc)
                summary.publishers.append(
                    PublisherRecord(
                        name="local_finalize",
                        success=False,
                        required=_required(local_config),
                        detail=str(exc),
                    )
                )
        return not any(record.required and not record.success for record in summary.publishers)

    def _run(self, summary: RunSummary, name: str, required: bool, operation):
        try:
            record = operation(summary)
            self.logger.info("published result through %s%s", name, f": {record.url}" if record.url else "")
        except Exception as exc:
            self.logger.error("result publisher %s failed: %s", name, exc)
            record = PublisherRecord(
                name=name,
                success=False,
                required=required,
                detail=f"{type(exc).__name__}: {exc}",
            )
        summary.publishers.append(record)
        append_jsonl(
            summary.artifact_root / "events.jsonl",
            {
                "at": time.time(),
                "event": "publisher_finished",
                "publisher": record.name,
                "success": record.success,
                "required": record.required,
                "url": record.url,
                "detail": record.detail,
            },
        )
        return record
