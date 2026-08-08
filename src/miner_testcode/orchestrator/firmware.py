from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from ..errors import ConfigError


class FirmwareDeploymentError(ConfigError):
    pass


def _read_bounded(response: Any, limit: int) -> bytes:
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise FirmwareDeploymentError(f"response exceeded {limit} bytes")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class GithubActionsArtifactFetcher:
    """Resolve and verify one artifact built for an exact Git commit."""

    api_base = "https://api.github.com"

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "miner-testcode-orchestrator",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _json(self, path: str, *, token: str | None, timeout: float) -> Any:
        request = Request(
            f"{self.api_base}{path}",
            headers=self._headers(token),
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(_read_bounded(response, 8 * 1024 * 1024))
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise FirmwareDeploymentError(
                f"GitHub GET {path} returned HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise FirmwareDeploymentError(f"GitHub GET {path} failed: {exc}") from exc

    def _download(
        self,
        url: str,
        *,
        token: str,
        timeout: float,
        max_bytes: int,
    ) -> bytes:
        request = Request(url, headers=self._headers(token))
        opener = build_opener(_NoRedirect())
        try:
            opener.open(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code != 302 or not exc.headers.get("Location"):
                detail = exc.read(1000).decode("utf-8", errors="replace")
                raise FirmwareDeploymentError(
                    f"GitHub artifact download returned HTTP {exc.code}: {detail}"
                ) from exc
            signed_url = urljoin(url, exc.headers["Location"])
        else:
            raise FirmwareDeploymentError("GitHub artifact download did not redirect")

        # The signed storage URL is already authorized. Do not forward the GitHub token.
        try:
            with urlopen(
                Request(signed_url, headers={"User-Agent": "miner-testcode-orchestrator"}),
                timeout=timeout,
            ) as response:
                return _read_bounded(response, max_bytes)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise FirmwareDeploymentError(f"artifact archive download failed: {exc}") from exc

    def fetch(
        self,
        repository: str,
        commit_sha: str,
        config: Mapping[str, Any],
        cache_root: Path,
    ) -> dict[str, Any]:
        token_env = str(config.get("token_env") or "GITHUB_TOKEN")
        token = os.environ.get(token_env, "").strip()
        wait_timeout = float(config.get("wait_timeout", 1800))
        poll_seconds = float(config.get("poll_seconds", 15))
        request_timeout = min(60.0, max(5.0, poll_seconds))
        deadline = time.monotonic() + wait_timeout
        workflow = quote(str(config["workflow"]), safe="")
        query = urlencode({"head_sha": commit_sha, "per_page": 20})
        run: dict[str, Any] | None = None
        artifact: dict[str, Any] | None = None

        while time.monotonic() < deadline:
            payload = self._json(
                f"/repos/{repository}/actions/workflows/{workflow}/runs?{query}",
                token=token or None,
                timeout=request_timeout,
            )
            runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
            candidates = [
                item
                for item in runs or []
                if isinstance(item, dict)
                and str(item.get("head_sha") or "").lower() == commit_sha.lower()
            ]
            candidates.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
            run = candidates[0] if candidates else None
            if run and run.get("status") == "completed":
                if run.get("conclusion") != "success":
                    raise FirmwareDeploymentError(
                        f"workflow {config['workflow']} failed for {commit_sha[:12]}: "
                        f"{run.get('conclusion') or 'unknown'}"
                    )
                artifacts_payload = self._json(
                    f"/repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100",
                    token=token or None,
                    timeout=request_timeout,
                )
                values = (
                    artifacts_payload.get("artifacts")
                    if isinstance(artifacts_payload, dict)
                    else None
                )
                artifact = next(
                    (
                        item
                        for item in values or []
                        if isinstance(item, dict)
                        and item.get("name") == config["artifact_name"]
                        and not item.get("expired")
                    ),
                    None,
                )
                if artifact:
                    break
            time.sleep(poll_seconds)

        if not run or not artifact:
            raise FirmwareDeploymentError(
                f"timed out waiting for {config['artifact_name']} from "
                f"{config['workflow']} at {commit_sha[:12]}"
            )
        if not token:
            raise FirmwareDeploymentError(
                f"downloading GitHub Actions artifacts requires environment {token_env}"
            )

        artifact_id = str(artifact["id"])
        target_dir = cache_root / commit_sha.lower() / artifact_id
        firmware_path = target_dir / str(config["filename"])
        metadata_path = target_dir / "artifact.json"
        if firmware_path.is_file() and metadata_path.is_file():
            try:
                cached = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = {}
            digest = hashlib.sha256(firmware_path.read_bytes()).hexdigest()
            if cached.get("firmware_sha256") == digest:
                return cached

        archive = self._download(
            str(artifact["archive_download_url"]),
            token=token,
            timeout=request_timeout,
            max_bytes=int(config.get("max_bytes", 64 * 1024 * 1024)),
        )
        archive_digest = hashlib.sha256(archive).hexdigest()
        expected_archive_digest = str(artifact.get("digest") or "")
        if not expected_archive_digest.startswith("sha256:"):
            raise FirmwareDeploymentError("GitHub artifact did not include a SHA-256 digest")
        if not hmac.compare_digest(
            expected_archive_digest.removeprefix("sha256:"),
            archive_digest,
        ):
            raise FirmwareDeploymentError("GitHub artifact archive digest did not match")

        filename = str(config["filename"])
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                members = [
                    item
                    for item in bundle.infolist()
                    if not item.is_dir()
                    and PurePosixPath(item.filename).name == filename
                    and ".." not in PurePosixPath(item.filename).parts
                ]
                if len(members) != 1:
                    raise FirmwareDeploymentError(
                        f"artifact must contain exactly one {filename}"
                    )
                if members[0].file_size > int(config.get("max_bytes", 64 * 1024 * 1024)):
                    raise FirmwareDeploymentError("firmware artifact is too large")
                firmware = bundle.read(members[0])
        except zipfile.BadZipFile as exc:
            raise FirmwareDeploymentError("GitHub artifact was not a valid ZIP archive") from exc
        if len(firmware) < 64 * 1024:
            raise FirmwareDeploymentError("firmware artifact is unexpectedly small")

        target_dir.mkdir(parents=True, exist_ok=True)
        temporary = firmware_path.with_suffix(firmware_path.suffix + ".tmp")
        temporary.write_bytes(firmware)
        os.chmod(temporary, 0o600)
        os.replace(temporary, firmware_path)
        result = {
            "source_repository": repository,
            "commit_sha": commit_sha.lower(),
            "workflow_run_id": int(run["id"]),
            "workflow_run_url": str(run.get("html_url") or ""),
            "artifact_id": int(artifact["id"]),
            "artifact_name": str(artifact["name"]),
            "archive_sha256": archive_digest,
            "firmware_sha256": hashlib.sha256(firmware).hexdigest(),
            "firmware_size": len(firmware),
            "firmware_path": str(firmware_path),
        }
        _write_json(metadata_path, result)
        return result


class FirmwareDeployer:
    def __init__(self, fetcher: GithubActionsArtifactFetcher | None = None) -> None:
        self.fetcher = fetcher or GithubActionsArtifactFetcher()

    def _device_info(self, api: str, *, timeout: float = 10) -> dict[str, Any]:
        request = Request(
            f"{api.rstrip('/')}/api/system/info",
            headers={"Accept": "application/json", "User-Agent": "miner-testcode-orchestrator"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(_read_bounded(response, 2 * 1024 * 1024))
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise FirmwareDeploymentError(f"device preflight failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise FirmwareDeploymentError("device info response was not an object")
        return payload

    def _ota(self, api: str, firmware: bytes, *, timeout: float) -> None:
        request = Request(
            f"{api.rstrip('/')}/api/system/OTA",
            data=firmware,
            headers={
                "Content-Type": "application/octet-stream",
                "User-Agent": "miner-testcode-orchestrator",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                _read_bounded(response, 64 * 1024)
                if response.status != 200:
                    raise FirmwareDeploymentError(
                        f"firmware OTA returned HTTP {response.status}"
                    )
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise FirmwareDeploymentError(
                f"firmware OTA returned HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise FirmwareDeploymentError(f"firmware OTA failed: {exc}") from exc

    def _wait_for_reboot(
        self,
        api: str,
        expected_board: str,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        time.sleep(5)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                info = self._device_info(api)
                if str(info.get("boardVersion") or "") != expected_board:
                    raise FirmwareDeploymentError(
                        "device board identity changed after firmware deployment"
                    )
                return info
            except FirmwareDeploymentError as exc:
                last_error = exc
                time.sleep(2)
        raise FirmwareDeploymentError(
            f"device did not return after OTA: {last_error or 'timeout'}"
        )

    def ensure(
        self,
        run: Mapping[str, Any],
        setup_id: str,
        config: Mapping[str, Any],
        state_dir: Path,
    ) -> dict[str, Any] | None:
        gate = config["gates"][run["gate_id"]]
        deployment = gate.get("deployment")
        if not deployment:
            return None
        marker = state_dir / "jobs" / run["id"] / "deployments" / f"{setup_id}.json"
        if marker.exists():
            try:
                existing = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FirmwareDeploymentError(
                    f"deployment marker is unreadable: {marker}"
                ) from exc
            if existing.get("status") == "passed":
                return existing
            raise FirmwareDeploymentError(
                f"previous deployment for this gate run is {existing.get('status', 'unknown')}; "
                "manual review is required before retrying"
            )

        _write_json(
            marker,
            {
                "status": "deploying",
                "commit_sha": run["commit_sha"],
                "setup": setup_id,
            },
        )
        try:
            repository = config["repositories"][run["repository_id"]]
            artifact_config = repository["artifacts"][deployment["artifact"]]
            artifact = self.fetcher.fetch(
                repository["repository"],
                run["commit_sha"],
                artifact_config,
                state_dir / "artifacts" / run["repository_id"],
            )
            firmware = Path(artifact["firmware_path"]).read_bytes()
            setup = config["lab"]["setups"][setup_id]
            deployed_devices: list[dict[str, Any]] = []
            for role in deployment["device_roles"]:
                device = config["lab"]["devices"][setup["devices"][role]]
                api = str(device["addresses"]["api"])
                expected_board = str(device["expected"]["board_version"])
                before = self._device_info(api)
                actual_board = str(before.get("boardVersion") or "")
                if actual_board != expected_board:
                    raise FirmwareDeploymentError(
                        f"device {device['name']} is board {actual_board or 'unknown'}, "
                        f"expected {expected_board}; refusing OTA"
                    )
                self._ota(api, firmware, timeout=float(deployment["reboot_timeout"]))
                after = self._wait_for_reboot(
                    api,
                    expected_board,
                    timeout=float(deployment["reboot_timeout"]),
                )
                deployed_devices.append(
                    {
                        "name": str(device["name"]),
                        "role": role,
                        "board_version": expected_board,
                        "version_before": str(before.get("version") or "unknown"),
                        "version_after": str(after.get("version") or "unknown"),
                    }
                )
            public_artifact = {
                key: value
                for key, value in artifact.items()
                if key != "firmware_path"
            }
            result = {
                "status": "passed",
                "commit_sha": run["commit_sha"],
                "setup": setup_id,
                "artifact": public_artifact,
                "devices": deployed_devices,
            }
            _write_json(marker, result)
            return result
        except Exception as exc:
            _write_json(
                marker,
                {
                    "status": "error",
                    "commit_sha": run["commit_sha"],
                    "setup": setup_id,
                    "detail": f"{type(exc).__name__}: {exc}"[:2000],
                },
            )
            if isinstance(exc, FirmwareDeploymentError):
                raise
            raise FirmwareDeploymentError(str(exc)) from exc
