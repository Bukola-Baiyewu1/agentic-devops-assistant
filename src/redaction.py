"""Remove secrets and personal identifiers before data leaves the process.

Applied to: text sent to the model, structured logs, and Langfuse traces.
The redactor reports how many values it removed, never the values themselves.
It is a conservative pattern-based control, not a full DLP system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/]+=*")),
    ("conn_string_password", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]+)(@)")),
    (
        "password_assignment",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[=:]\s*)([^\s,;\"']+)"),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
]

SENSITIVE_KEYS = re.compile(
    # "*_tokens" keys hold usage counts (input_tokens), not credentials.
    r"(?i)^(?!.*tokens$).*(token|secret|password|passwd|authorization|api[_-]?key|nonce|challenge|capability|cookie)"
)


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact_text(text: str) -> RedactionResult:
    counts: dict[str, int] = {}
    out = text
    for name, pattern in _PATTERNS:
        if name == "conn_string_password":
            out, n = pattern.subn(r"\1[REDACTED]\3", out)
        elif name == "password_assignment":
            out, n = pattern.subn(r"\1\2[REDACTED]", out)
        else:
            out, n = pattern.subn(f"[REDACTED:{name}]", out)
        if n:
            counts[name] = counts.get(name, 0) + n
    return RedactionResult(out, counts)


def redact(value: Any) -> Any:
    """Recursively redact strings and drop values stored under sensitive keys."""
    if isinstance(value, str):
        return redact_text(value).text
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if isinstance(k, str) and SENSITIVE_KEYS.search(k) and v not in (None, "") else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value
