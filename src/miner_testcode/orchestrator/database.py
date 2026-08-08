from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA = """
pragma journal_mode = WAL;
pragma foreign_keys = ON;

create table if not exists source_cursors (
    source_key text primary key,
    value text not null,
    etag text,
    updated_at real not null
);

create table if not exists events (
    id text primary key,
    event_key text not null unique,
    repository_id text not null,
    trigger_type text not null,
    commit_sha text not null,
    branch text,
    pr_number integer,
    pr_url text,
    contributor text,
    changed_paths text not null default '[]',
    payload text not null default '{}',
    created_at real not null,
    planned_at real
);

create table if not exists gate_runs (
    id text primary key,
    gate_id text not null,
    event_id text not null references events(id),
    repository_id text not null,
    commit_sha text not null,
    branch text,
    pr_number integer,
    trigger_type text not null,
    definition_digest text not null,
    config_snapshot text not null,
    status text not null,
    required_policy text not null default 'all',
    qa_result_id text,
    qa_result_url text,
    summary text,
    created_at real not null,
    started_at real,
    finished_at real,
    unique(gate_id, event_id, definition_digest)
);

create table if not exists assignments (
    id text primary key,
    gate_run_id text not null references gate_runs(id) on delete cascade,
    setup_id text not null,
    module_id text not null,
    platform_key text not null,
    status text not null,
    attempt integer not null default 0,
    result_pointer text,
    qa_result_id text,
    qa_result_url text,
    detail text,
    created_at real not null,
    started_at real,
    finished_at real,
    unique(gate_run_id, setup_id, module_id)
);

create table if not exists resource_leases (
    resource_id text primary key,
    assignment_id text not null references assignments(id) on delete cascade,
    acquired_at real not null
);

create table if not exists publications (
    gate_run_id text primary key references gate_runs(id) on delete cascade,
    status text not null,
    attempts integer not null default 0,
    last_error text,
    updated_at real not null
);

create index if not exists events_unplanned_idx on events(planned_at, created_at);
create index if not exists gate_runs_status_idx on gate_runs(status, created_at);
create index if not exists assignments_status_idx on assignments(status, created_at);
"""


class OrchestratorDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("begin immediate")
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def cursor(self, source_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "select * from source_cursors where source_key = ?", (source_key,)
        ).fetchone()
        return dict(row) if row else None

    def set_cursor(self, source_key: str, value: str, etag: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                insert into source_cursors(source_key, value, etag, updated_at)
                values (?, ?, ?, ?)
                on conflict(source_key) do update set
                    value = excluded.value,
                    etag = excluded.etag,
                    updated_at = excluded.updated_at
                """,
                (source_key, value, etag, time.time()),
            )

    def create_event(
        self,
        *,
        event_key: str,
        repository_id: str,
        trigger_type: str,
        commit_sha: str,
        branch: str | None = None,
        pr_number: int | None = None,
        pr_url: str | None = None,
        contributor: str | None = None,
        changed_paths: list[str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        event_id = str(uuid.uuid4())
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                insert into events(
                    id, event_key, repository_id, trigger_type, commit_sha,
                    branch, pr_number, pr_url, contributor, changed_paths,
                    payload, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(event_key) do nothing
                """,
                (
                    event_id,
                    event_key,
                    repository_id,
                    trigger_type,
                    commit_sha.lower(),
                    branch,
                    pr_number,
                    pr_url,
                    contributor,
                    json.dumps(changed_paths or []),
                    json.dumps(dict(payload or {})),
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "select * from events where event_key = ?", (event_key,)
            ).fetchone()
        return self._decode(row), created

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise KeyError("database row not found")
        value = dict(row)
        for key in ("changed_paths", "payload"):
            if key in value and isinstance(value[key], str):
                value[key] = json.loads(value[key])
        return value

    def list_events(self, *, limit: int = 100, unplanned: bool = False) -> list[dict[str, Any]]:
        where = "where planned_at is null" if unplanned else ""
        rows = self._connection.execute(
            f"select * from events {where} order by created_at desc limit ?", (limit,)
        ).fetchall()
        return [self._decode(row) for row in rows]

    def mark_event_planned(self, event_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "update events set planned_at = ? where id = ?", (time.time(), event_id)
            )

    def create_gate_run(
        self,
        *,
        gate_id: str,
        event: Mapping[str, Any],
        definition_digest: str,
        required_policy: str,
        assignments: list[Mapping[str, str]],
        config_snapshot: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        run_id = str(uuid.uuid4())
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                insert into gate_runs(
                    id, gate_id, event_id, repository_id, commit_sha, branch,
                    pr_number, trigger_type, definition_digest, config_snapshot, status,
                    required_policy, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                on conflict(gate_id, event_id, definition_digest) do nothing
                """,
                (
                    run_id,
                    gate_id,
                    event["id"],
                    event["repository_id"],
                    event["commit_sha"],
                    event.get("branch"),
                    event.get("pr_number"),
                    event["trigger_type"],
                    definition_digest,
                    json.dumps(dict(config_snapshot), sort_keys=True),
                    required_policy,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                """select * from gate_runs
                   where gate_id = ? and event_id = ? and definition_digest = ?""",
                (gate_id, event["id"], definition_digest),
            ).fetchone()
            actual_run_id = row["id"]
            if created:
                for assignment in assignments:
                    connection.execute(
                        """
                        insert into assignments(
                            id, gate_run_id, setup_id, module_id, platform_key,
                            status, created_at
                        ) values (?, ?, ?, ?, ?, 'queued', ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            actual_run_id,
                            assignment["setup_id"],
                            assignment["module_id"],
                            assignment["platform_key"],
                            now,
                        ),
                    )
        return dict(row), created

    def list_gate_runs(
        self, *, limit: int = 100, gate_id: str | None = None
    ) -> list[dict[str, Any]]:
        if gate_id:
            rows = self._connection.execute(
                "select * from gate_runs where gate_id = ? order by created_at desc limit ?",
                (gate_id, limit),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "select * from gate_runs order by created_at desc limit ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("config_snapshot", None)
            result.append(item)
        return result

    def gate_run(self, run_id: str, *, include_config: bool = False) -> dict[str, Any]:
        row = self._connection.execute(
            "select * from gate_runs where id = ?", (run_id,)
        ).fetchone()
        value = dict(row) if row else None
        if value is None:
            raise KeyError(run_id)
        snapshot = value.pop("config_snapshot", None)
        if include_config and isinstance(snapshot, str):
            value["config_snapshot"] = json.loads(snapshot)
        value["assignments"] = self.assignments(run_id)
        return value

    def assignments(self, run_id: str | None = None, *, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("gate_run_id = ?")
            parameters.append(run_id)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"select * from assignments {where} order by created_at, setup_id, module_id",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def next_assignment(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            select a.* from assignments a
            join gate_runs g on g.id = a.gate_run_id
            where a.status = 'queued' and g.status in ('queued', 'running')
            order by a.created_at limit 1
            """
        ).fetchone()
        return dict(row) if row else None

    def acquire(self, assignment_id: str, resources: list[str]) -> bool:
        now = time.time()
        try:
            with self.transaction() as connection:
                for resource in sorted(set(resources)):
                    connection.execute(
                        "insert into resource_leases(resource_id, assignment_id, acquired_at) values (?, ?, ?)",
                        (resource, assignment_id, now),
                    )
                connection.execute(
                    """update assignments set status = 'running', attempt = attempt + 1,
                       started_at = coalesce(started_at, ?) where id = ? and status = 'queued'""",
                    (now, assignment_id),
                )
                if connection.execute("select changes()").fetchone()[0] != 1:
                    raise sqlite3.IntegrityError("assignment is not queued")
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_assignment(
        self,
        assignment_id: str,
        *,
        status: str,
        detail: str | None = None,
        result_pointer: str | None = None,
        qa_result_id: str | None = None,
        qa_result_url: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                update assignments set status = ?, detail = ?, result_pointer = ?,
                    qa_result_id = ?, qa_result_url = ?, finished_at = ?
                where id = ?
                """,
                (
                    status,
                    detail,
                    result_pointer,
                    qa_result_id,
                    qa_result_url,
                    time.time(),
                    assignment_id,
                ),
            )
            connection.execute(
                "delete from resource_leases where assignment_id = ?", (assignment_id,)
            )

    def update_gate_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: str | None = None,
        qa_result_id: str | None = None,
        qa_result_url: str | None = None,
    ) -> None:
        now = time.time()
        terminal = status in {"passed", "failed", "error", "skipped", "cancelled"}
        with self.transaction() as connection:
            connection.execute(
                """
                update gate_runs set status = ?, summary = coalesce(?, summary),
                    qa_result_id = coalesce(?, qa_result_id),
                    qa_result_url = coalesce(?, qa_result_url),
                    started_at = case when ? = 'running' then coalesce(started_at, ?) else started_at end,
                    finished_at = case when ? then ? else finished_at end
                where id = ?
                """,
                (
                    status,
                    summary,
                    qa_result_id,
                    qa_result_url,
                    status,
                    now,
                    terminal,
                    now,
                    run_id,
                ),
            )

    def recover_interrupted(self) -> int:
        """Fail closed after restart; devices need a fresh preflight before reuse."""
        with self.transaction() as connection:
            rows = connection.execute(
                "select id from assignments where status = 'running'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "update assignments set status = 'error', detail = ?, finished_at = ? where id = ?",
                    ("orchestrator restarted while assignment was running", time.time(), row["id"]),
                )
            connection.execute("delete from resource_leases")
        return len(rows)

    def supersede_queued_pull_request(
        self, repository_id: str, pr_number: int, new_sha: str
    ) -> int:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                select id from gate_runs
                where repository_id = ? and pr_number = ? and commit_sha != ?
                  and status = 'queued'
                """,
                (repository_id, pr_number, new_sha),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "update assignments set status = 'superseded', finished_at = ? where gate_run_id = ? and status = 'queued'",
                    (time.time(), row["id"]),
                )
                connection.execute(
                    "update gate_runs set status = 'superseded', finished_at = ? where id = ?",
                    (time.time(), row["id"]),
                )
        return len(rows)

    def cancel_gate_run(self, run_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            running = connection.execute(
                "select count(*) from assignments where gate_run_id = ? and status = 'running'",
                (run_id,),
            ).fetchone()[0]
            connection.execute(
                "update assignments set status = 'cancelled', finished_at = ? where gate_run_id = ? and status = 'queued'",
                (time.time(), run_id),
            )
            if not running:
                connection.execute(
                    "update gate_runs set status = 'cancelled', finished_at = ? where id = ?",
                    (time.time(), run_id),
                )
        return self.gate_run(run_id)

    def retry_gate_run(self, run_id: str) -> dict[str, Any]:
        with self.transaction() as connection:
            connection.execute(
                """
                update assignments set status = 'queued', detail = null,
                    result_pointer = null, qa_result_id = null, qa_result_url = null,
                    started_at = null, finished_at = null
                where gate_run_id = ? and status in ('failed', 'error', 'cancelled', 'superseded')
                """,
                (run_id,),
            )
            changed = connection.execute("select changes()").fetchone()[0]
            if changed:
                connection.execute(
                    "update gate_runs set status = 'queued', summary = null, started_at = null, finished_at = null where id = ?",
                    (run_id,),
                )
        return self.gate_run(run_id)
