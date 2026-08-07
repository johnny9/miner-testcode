from __future__ import annotations

import re
from pathlib import Path

_ADDRESS = re.compile(
    rb"(?i)\b(?:bc1|tb1|npub1)[a-z0-9]{20,}(?:\.[A-Za-z0-9_-]+)?"
)
_KEYED_SECRET = re.compile(
    rb"(?i)(stratum(?:user|password)|pool(?:user|password))"
    rb"([\"'=:, ]{1,8})([^\s\"',}]+)"
)


def redact_bytes(data: bytes) -> bytes:
    data = _ADDRESS.sub(b"<redacted-pool-identity>", data)
    return _KEYED_SECRET.sub(
        lambda match: match.group(1) + match.group(2) + b"<redacted>", data
    )


def redact_text(value: str) -> str:
    return redact_bytes(value.encode("utf-8", errors="replace")).decode(
        "utf-8", errors="replace"
    )


def redact_file(path: Path) -> None:
    if not path.is_file():
        return
    original = path.read_bytes()
    redacted = redact_bytes(original)
    if redacted != original:
        path.write_bytes(redacted)
