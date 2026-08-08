from __future__ import annotations

import fnmatch
import json
import os
import time
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import ConfigError
from .database import OrchestratorDatabase


class GithubError(ConfigError):
    pass


class GithubClient:
    def __init__(self, *, token_env: str = "GITHUB_TOKEN", timeout: float = 20.0) -> None:
        self.token_env = token_env
        self.timeout = timeout

    def get(self, path: str, *, etag: str | None = None) -> tuple[Any | None, str | None, bool]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "miner-testcode-orchestrator",
        }
        token = os.environ.get(self.token_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if etag:
            headers["If-None-Match"] = etag
        request = Request(f"https://api.github.com{path}", headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) > 8 * 1024 * 1024:
                    raise GithubError(f"GitHub response too large for {path}")
                return json.loads(payload), response.headers.get("ETag"), False
        except HTTPError as exc:
            if exc.code == 304:
                return None, etag, True
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise GithubError(f"GitHub GET {path} returned HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise GithubError(f"GitHub GET {path} failed: {exc}") from exc

    def branch_head(self, repository: str, branch: str) -> tuple[str, str | None]:
        payload, etag, _ = self.get(
            f"/repos/{repository}/commits/{quote(branch, safe='')}"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("sha"), str):
            raise GithubError(f"GitHub did not return a SHA for {repository}:{branch}")
        return payload["sha"].lower(), etag

    def open_pull_requests(self, repository: str) -> list[dict[str, Any]]:
        payload, _, _ = self.get(
            f"/repos/{repository}/pulls?state=open&sort=updated&direction=desc&per_page=100"
        )
        if not isinstance(payload, list):
            raise GithubError(f"GitHub did not return pull requests for {repository}")
        return [item for item in payload if isinstance(item, dict)]

    def merged_pull_request(
        self,
        repository: str,
        commit_sha: str,
        base_branch: str,
    ) -> dict[str, Any] | None:
        payload, _, _ = self.get(
            f"/repos/{repository}/commits/{quote(commit_sha, safe='')}/pulls"
        )
        if not isinstance(payload, list):
            return None
        candidates = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("merged_at"):
                continue
            base = item.get("base") or {}
            if isinstance(base, dict) and base.get("ref") == base_branch:
                candidates.append(item)
        candidates.sort(key=lambda item: int(item.get("number") or 0), reverse=True)
        return candidates[0] if candidates else None

    def changed_paths(self, repository: str, base: str, head: str) -> list[str]:
        payload, _, _ = self.get(f"/repos/{repository}/compare/{base}...{head}")
        if not isinstance(payload, dict):
            return []
        files = payload.get("files")
        if not isinstance(files, list):
            return []
        paths = [item.get("filename") for item in files if isinstance(item, dict)]
        return [item for item in paths if isinstance(item, str)]


def _cron_field_matches(field: str, value: int, minimum: int, maximum: int) -> bool:
    def expand(part: str) -> set[int]:
        step = 1
        if "/" in part:
            base, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) <= 0:
                raise ConfigError(f"invalid cron step: {part}")
            step = int(raw_step)
        else:
            base = part
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                raise ConfigError(f"invalid cron range: {part}")
            start, end = int(left), int(right)
        elif base.isdigit():
            start = end = int(base)
        else:
            raise ConfigError(f"invalid cron field: {part}")
        if start < minimum or end > maximum or start > end:
            raise ConfigError(f"cron field outside {minimum}-{maximum}: {part}")
        return set(range(start, end + 1, step))

    selected: set[int] = set()
    for part in field.split(","):
        selected.update(expand(part))
    return value in selected


def cron_matches(expression: str, when: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ConfigError("cron expressions must contain five fields")
    minute, hour, day, month, weekday = fields
    cron_weekday = (when.weekday() + 1) % 7
    return all(
        (
            _cron_field_matches(minute, when.minute, 0, 59),
            _cron_field_matches(hour, when.hour, 0, 23),
            _cron_field_matches(day, when.day, 1, 31),
            _cron_field_matches(month, when.month, 1, 12),
            _cron_field_matches(weekday, cron_weekday, 0, 6),
        )
    )


def paths_match(changed_paths: list[str], changes: Mapping[str, Any] | None) -> bool:
    if not changes or not changed_paths:
        return True
    includes = changes.get("include", ["**"])
    excludes = changes.get("exclude", [])
    for path in changed_paths:
        included = any(fnmatch.fnmatch(path, pattern) for pattern in includes)
        excluded = any(fnmatch.fnmatch(path, pattern) for pattern in excludes)
        if included and not excluded:
            return True
    return False


class EventCollector:
    def __init__(self, database: OrchestratorDatabase, github: GithubClient | None = None) -> None:
        self.database = database
        self.github = github or GithubClient()

    def poll_repository(self, repository_id: str, config: Mapping[str, Any]) -> int:
        repository = str(config["repository"])
        created = 0
        for branch in config["pushes"]["branches"]:
            source_key = f"github:{repository}:branch:{branch}"
            previous = self.database.cursor(source_key)
            head, etag = self.github.branch_head(repository, branch)
            if previous is None:
                self.database.set_cursor(source_key, head, etag)
                continue
            if previous["value"] == head:
                self.database.set_cursor(source_key, head, etag)
                continue
            paths = self.github.changed_paths(repository, previous["value"], head)
            merged_pull = self.github.merged_pull_request(repository, head, branch)
            pull_user = (merged_pull or {}).get("user") or {}
            pull_number = (merged_pull or {}).get("number")
            if not isinstance(pull_number, int):
                pull_number = None
            _, inserted = self.database.create_event(
                event_key=f"{source_key}:{head}",
                repository_id=repository_id,
                trigger_type="push",
                commit_sha=head,
                branch=branch,
                pr_number=pull_number,
                pr_url=str((merged_pull or {}).get("html_url") or "") or None,
                contributor=str(pull_user.get("login") or "") or None,
                changed_paths=paths,
                payload={
                    "previous_sha": previous["value"],
                    "merged_pull_request": pull_number,
                },
            )
            created += int(inserted)
            self.database.set_cursor(source_key, head, etag)

        pull_config = config["pull_requests"]
        trusted = {item.casefold() for item in pull_config["trusted_contributors"]}
        bases = set(pull_config["base_branches"])
        for pull in self.github.open_pull_requests(repository):
            user = pull.get("user") or {}
            head = pull.get("head") or {}
            base = pull.get("base") or {}
            login = str(user.get("login") or "")
            sha = str(head.get("sha") or "").lower()
            base_sha = str(base.get("sha") or "").lower()
            base_ref = str(base.get("ref") or "")
            number = pull.get("number")
            if (
                not sha
                or not isinstance(number, int)
                or base_ref not in bases
                or login.casefold() not in trusted
            ):
                continue
            source_key = f"github:{repository}:pr:{number}"
            previous = self.database.cursor(source_key)
            if previous and previous["value"] == sha:
                continue
            self.database.supersede_queued_pull_request(repository_id, number, sha)
            paths = self.github.changed_paths(repository, base_sha, sha) if base_sha else []
            _, inserted = self.database.create_event(
                event_key=f"{source_key}:{sha}",
                repository_id=repository_id,
                trigger_type="pull_request",
                commit_sha=sha,
                branch=str(head.get("ref") or ""),
                pr_number=number,
                pr_url=str(pull.get("html_url") or "") or None,
                contributor=login,
                changed_paths=paths,
                payload={"base_branch": base_ref, "base_sha": base_sha},
            )
            created += int(inserted)
            self.database.set_cursor(source_key, sha)
        return created

    def collect_schedules(self, config: Mapping[str, Any], *, now: datetime | None = None) -> int:
        now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
        created = 0
        for gate_id, gate in config["gates"].items():
            repository_id = gate["repository"]
            repository = config["repositories"][repository_id]
            branch = repository["pushes"]["branches"][0]
            cursor = self.database.cursor(
                f"github:{repository['repository']}:branch:{branch}"
            )
            if cursor is None:
                continue
            for schedule in gate["triggers"]["schedules"]:
                timezone = str(schedule.get("timezone") or "UTC")
                try:
                    local_now = now.astimezone(ZoneInfo(timezone))
                except ZoneInfoNotFoundError as exc:
                    raise ConfigError(f"unknown schedule timezone: {timezone}") from exc
                if not cron_matches(schedule["cron"], local_now):
                    continue
                minute = int(now.timestamp() // 60)
                _, inserted = self.database.create_event(
                    event_key=f"schedule:{gate_id}:{schedule['id']}:{minute}",
                    repository_id=repository_id,
                    trigger_type="schedule",
                    commit_sha=cursor["value"],
                    branch=branch,
                    payload={"gate_id": gate_id, "schedule_id": schedule["id"]},
                )
                created += int(inserted)
        return created

    def manual(
        self,
        *,
        repository_id: str,
        commit_sha: str,
        branch: str | None,
        gate_id: str,
    ) -> dict[str, Any]:
        event, _ = self.database.create_event(
            event_key=f"manual:{gate_id}:{commit_sha}:{time.time_ns()}",
            repository_id=repository_id,
            trigger_type="manual",
            commit_sha=commit_sha,
            branch=branch,
            payload={"gate_id": gate_id},
        )
        return event
