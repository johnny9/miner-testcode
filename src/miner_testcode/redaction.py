from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Mapping

_ADDRESS = re.compile(
    rb"(?i)\b(?:bc1|tb1|npub1)[a-z0-9]{20,}(?:\.[A-Za-z0-9_-]+)?"
)
_KEYED_SECRET = re.compile(
    rb"(?i)(stratum(?:user|password)|pool(?:user|password)|"
    rb"wifi(?:ssid|password)|ssid|macAddr|ipv4|ipv6)"
    rb"([\"'=:, ]{1,8})([^\s\"',}]+)"
)
_MAC_ADDRESS = re.compile(rb"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b")
_PRIVATE_IPV4 = re.compile(
    rb"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    rb"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
_LOCAL_PATH = re.compile(
    rb"(?<![A-Za-z0-9])/(?:home|Users|tmp|private|var|dev|usr|opt|etc|run)/"
    rb"(?:[^\s\"'<>])+"
)
_TEXT_SUFFIXES = frozenset({".html", ".json", ".jsonl", ".log", ".txt", ".xml"})


def redact_bytes(
    data: bytes,
    *,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    replacements: Mapping[str, str] | None = None,
) -> bytes:
    path_replacements: list[tuple[Path | None, bytes]] = [
        (artifact_root, b"<artifacts>"),
        (project_root, b""),
    ]
    for root, replacement in path_replacements:
        if root is None:
            continue
        resolved = str(root.resolve()).encode()
        data = data.replace(resolved + b"/", replacement + (b"/" if replacement else b""))
        data = data.replace(resolved, replacement or b".")
    for private, public in sorted(
        (replacements or {}).items(), key=lambda item: len(item[0]), reverse=True
    ):
        if private and private != public:
            pattern = re.compile(
                rb"(?<![A-Za-z0-9])" + re.escape(private.encode()) + rb"(?![A-Za-z0-9])"
            )
            data = pattern.sub(public.encode(), data)
    data = _ADDRESS.sub(b"<redacted-pool-identity>", data)
    data = _KEYED_SECRET.sub(
        lambda match: match.group(1) + match.group(2) + b"<redacted>", data
    )
    data = _MAC_ADDRESS.sub(b"<redacted-mac>", data)
    data = _PRIVATE_IPV4.sub(b"<redacted-private-ip>", data)
    return _LOCAL_PATH.sub(b"<local-path>", data)


def redact_text(
    value: str,
    *,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    replacements: Mapping[str, str] | None = None,
) -> str:
    return redact_bytes(
        value.encode("utf-8", errors="replace"),
        project_root=project_root,
        artifact_root=artifact_root,
        replacements=replacements,
    ).decode(
        "utf-8", errors="replace"
    )


def redact_file(
    path: Path,
    *,
    project_root: Path | None = None,
    artifact_root: Path | None = None,
    replacements: Mapping[str, str] | None = None,
) -> None:
    if not path.is_file():
        return
    original = path.read_bytes()
    redacted = redact_bytes(
        original,
        project_root=project_root,
        artifact_root=artifact_root,
        replacements=replacements,
    )
    if redacted != original:
        path.write_bytes(redacted)


def sanitize_artifacts(
    root: Path,
    *,
    project_root: Path,
    extra_suffixes: Iterable[str] = (),
    replacements: Mapping[str, str] | None = None,
) -> None:
    suffixes = _TEXT_SUFFIXES.union(extra_suffixes)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            redact_file(
                path,
                project_root=project_root,
                artifact_root=root,
                replacements=replacements,
            )


class PrivacyFormatter(logging.Formatter):
    def __init__(
        self,
        fmt: str,
        *,
        project_root: Path,
        artifact_root: Path,
        replacements: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(fmt)
        self.project_root = project_root
        self.artifact_root = artifact_root
        self.replacements = replacements

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(
            super().format(record),
            project_root=self.project_root,
            artifact_root=self.artifact_root,
            replacements=self.replacements,
        )
