from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from .errors import ConfigError
from .results import TestCodeRecord

_GITHUB_SCP_REMOTE = re.compile(
    r"^(?:[^@]+@)?github\.com:(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "git command failed"
        raise ConfigError(f"could not determine test-code provenance: {detail}")
    return process.stdout.strip()


def _github_repository(remote: str) -> str:
    match = _GITHUB_SCP_REMOTE.fullmatch(remote.strip())
    if match:
        return match.group("repository")
    parsed = urlsplit(remote.strip())
    if parsed.hostname != "github.com":
        raise ConfigError("test-code origin must be hosted on github.com")
    repository = parsed.path.strip("/")
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not _REPOSITORY.fullmatch(repository):
        raise ConfigError("test-code origin must have owner/repository form")
    return repository


@dataclass(frozen=True, slots=True)
class ResolvedTestCode:
    root: Path
    record: TestCodeRecord

    def file_url(self, path: Path, line: int | None = None) -> str:
        relative = path.resolve().relative_to(self.root).as_posix()
        url = f"{self.record.url}/blob/{self.record.commit_sha}/{quote(relative, safe='/')}"
        return f"{url}#L{line}" if line is not None else url


def resolve_test_code(project_dir: Path, *, require_published: bool) -> ResolvedTestCode:
    root = Path(_git(project_dir, "rev-parse", "--show-toplevel")).resolve()
    commit_sha = _git(root, "rev-parse", "HEAD")
    repository = _github_repository(_git(root, "remote", "get-url", "origin"))
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    remote_refs = _git(
        root,
        "for-each-ref",
        "--contains",
        commit_sha,
        "--format=%(refname)",
        "refs/remotes/origin",
    ).splitlines()
    published = any(ref.startswith("refs/remotes/origin/") for ref in remote_refs)
    if require_published and dirty:
        raise ConfigError(
            "refusing remote result publication because tracked test code has "
            "uncommitted changes"
        )
    if require_published and not published:
        raise ConfigError(
            "refusing remote result publication because the test-code commit is not "
            "present in a local origin/* ref"
        )
    return ResolvedTestCode(
        root=root,
        record=TestCodeRecord(
            repository=repository,
            commit_sha=commit_sha,
            url=f"https://github.com/{repository}",
            dirty=dirty,
            published=published,
        ),
    )
