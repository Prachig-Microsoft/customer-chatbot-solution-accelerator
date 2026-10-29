"""Secret redaction applied to every piece of output that leaves a check."""

from __future__ import annotations

import re
from typing import Any

SECRET_NAME = re.compile(
    r"(KEY|SECRET|PASSWORD|PASSWD|TOKEN|CONNECTION_?STRING|CONN_?STR|SAS|CREDENTIAL)",
    re.IGNORECASE,
)

_PATTERNS = [
    (
        re.compile(r"(AccountKey|SharedAccessKey|InstrumentationKey|sig|password|pwd|ApiKey)=([^;&\s\"']+)", re.I),
        r"\1=<redacted>",
    ),
    (
        re.compile(
            r"(\"(?:[A-Za-z_]*(?:key|secret|password|token|connectionString)[A-Za-z_]*)\"\s*:\s*\")[^\"]+(\")", re.I
        ),
        r"\1<redacted>\2",
    ),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "<redacted-jwt>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-\._~\+/]+=*", re.I), r"\1<redacted>"),
]


def redact_text(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name or ""))


def redact_value(name: str, value: Any) -> Any:
    if is_secret_name(name):
        return "<redacted>"
    if isinstance(value, str):
        return redact_text(value)
    return value


_NON_SECRET_PLACEHOLDER = re.compile(r"populate manually", re.I)


def redact_obj(obj: Any) -> Any:
    """Recursively redact dict/list structures by key name and value pattern.

    ``{"name": X, "value": Y}`` pairs (App Service settings, container settings, env lists) always have their value
    redacted, except empty values and the template's non-secret "populate manually" placeholders, so callers can
    still tell set / empty / placeholder apart without ever seeing the value.
    """
    if isinstance(obj, dict):
        if "name" in obj and "value" in obj:
            v = obj["value"]
            keep = v in ("", None) or (isinstance(v, str) and bool(_NON_SECRET_PLACEHOLDER.search(v)))
            return {k: ((v if keep else "<redacted>") if k == "value" else redact_obj(val)) for k, val in obj.items()}
        return {
            k: ("<redacted>" if is_secret_name(str(k)) and obj[k] not in ("", None) else redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
