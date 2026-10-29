"""Azure CLI / HTTP execution layer.

* Only allow-listed **read-only** az commands can run through ``AzRunner.run``.
* State-changing commands are only executed by ``actions.execute`` after explicit approval.
* Every call is timed, recorded, and its output redacted.
* ``ReplayRunner`` serves recorded fixtures so tests/demos are reproducible without Azure.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .models import CommandRecord
from .redact import redact_obj, redact_text

READ_ONLY_PREFIXES = [
    "account show",
    "ad signed-in-user show",
    "group show",
    "group exists",
    "group list",
    "deployment group list",
    "deployment group show",
    "deployment operation group list",
    "deployment sub list",
    "deployment operation sub list",
    "cognitiveservices usage list",
    "cognitiveservices model list",
    "cognitiveservices account show",
    "cognitiveservices account list",
    "cognitiveservices account deployment list",
    "cognitiveservices account list-deleted",
    "webapp show",
    "webapp list",
    "webapp config appsettings list",
    "webapp config container show",
    "webapp log deployment show",
    "resource list",
    "provider show",
    "role assignment list",
    "cosmosdb show",
    "cosmosdb sql role assignment list",
    "search service show",
    "monitor activity-log list",
    "policy state list",
]

ALLOWED_HTTP_HOST_SUFFIXES = (".azurewebsites.net", "localhost", "127.0.0.1")

DEMO_SUBSCRIPTION = "00000000-0000-0000-0000-00000000aaaa"
DEMO_TENANT = "00000000-0000-0000-0000-00000000bbbb"


def anonymize(text: str, recorded: Dict[str, Any]) -> str:
    """Replace subscription/tenant ids, subscription name and user principal names in recorded fixtures."""
    acct = (recorded.get("account show") or {}).get("stdout") or {}
    if isinstance(acct, dict):
        for value, repl in (
            (acct.get("id"), DEMO_SUBSCRIPTION),
            (acct.get("tenantId"), DEMO_TENANT),
            (acct.get("homeTenantId"), DEMO_TENANT),
            (acct.get("name"), "demo-subscription"),
        ):
            if value:
                text = text.replace(value, repl)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "user@contoso.example", text)
    text = re.sub(r"[A-Za-z0-9-]+\.onmicrosoft\.com", "contoso.onmicrosoft.com", text)
    text = re.sub(r'"tenantDisplayName":\s*"[^"]*"', '"tenantDisplayName": "Contoso"', text)
    text = re.sub(r'"managedByTenants":\s*\[[^\]]*\]', '"managedByTenants": []', text)
    return re.sub(r"/subscriptions/[0-9a-fA-F-]{36}", f"/subscriptions/{DEMO_SUBSCRIPTION}", text)


class CommandNotAllowed(Exception):
    pass


# az on Windows is a .cmd shim interpreted by cmd.exe, so arguments must never carry shell metacharacters.
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_\-./:=@,()]+$")


def _validate_args(args: List[str]) -> None:
    bad = [a for a in args if not _SAFE_ARG.match(a)]
    if bad:
        raise CommandNotAllowed(f"unsafe characters in arguments: {bad}")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_read_only(args: List[str]) -> bool:
    joined = " ".join(a for a in args if not a.startswith("-"))
    return any(joined.startswith(p) for p in READ_ONLY_PREFIXES)


def _excerpt(stdout: str, stderr: str, limit: int = 1200) -> str:
    text = (stdout or "").strip()
    if stderr and stderr.strip():
        text = (text + "\n[stderr] " + stderr.strip()).strip()
    text = redact_text(text)
    return text if len(text) <= limit else text[:limit] + f"... <{len(text) - limit} more chars>"


class AzRunner:
    mode = "live"

    def __init__(self, timeout: int = 90, record_to: Optional[Path] = None):
        self.timeout = timeout
        self.az = shutil.which("az") or shutil.which("az.cmd")
        self.record_to = record_to
        self._recorded: Dict[str, Any] = {}
        self.history: List[CommandRecord] = []

    @property
    def available(self) -> bool:
        return self.az is not None

    def run(self, args: List[str], timeout: Optional[int] = None) -> Tuple[bool, Any, CommandRecord]:
        if not is_read_only(args):
            raise CommandNotAllowed(f"az {' '.join(args)} is not on the read-only allowlist")
        _validate_args(args)
        cmd_str = "az " + " ".join(args)
        if not self.available:
            rec = CommandRecord(cmd_str, None, _now(), 0, "Azure CLI not found on PATH", source="skipped")
            self.history.append(rec)
            return False, {"error": "az-not-installed"}, rec
        start = time.time()
        try:
            proc = subprocess.run(
                [self.az, *args, "-o", "json", "--only-show-errors"],
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,  # never inherit the MCP stdio pipe
            )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            code, out, err = None, "", f"timed out after {timeout or self.timeout}s"
        dur = int((time.time() - start) * 1000)
        data: Any
        try:
            data = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            data = out.strip()
        ok = code == 0
        if not ok:
            data = {"error": redact_text(err.strip())[:3000]}
        else:
            data = redact_obj(data)
        # The excerpt is built from redacted data, never from raw stdout.
        excerpt_src = (json.dumps(data) if not isinstance(data, str) else data) if ok else ""
        rec = CommandRecord(cmd_str, code, _now(), dur, _excerpt(excerpt_src, err if not ok else ""), source="live")
        self.history.append(rec)
        if self.record_to is not None:
            self._recorded[" ".join(args)] = {
                "exit_code": code,
                "stdout": data if ok else None,
                "stderr": data.get("error") if not ok else "",
            }
        return ok, data, rec

    def http_get(self, url: str, timeout: int = 20) -> Tuple[bool, Dict[str, Any], CommandRecord]:
        host = urlparse(url).hostname or ""
        if not host.endswith(ALLOWED_HTTP_HOST_SUFFIXES):
            raise CommandNotAllowed(f"HTTP probe to {host} not allowed")
        start = time.time()
        status, body, err = None, "", ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "accel-troubleshooter/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - host allowlisted
                status, body = resp.status, resp.read(2000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read(2000).decode("utf-8", "replace") if e.fp else ""
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
        dur = int((time.time() - start) * 1000)
        result = {"status": status, "body_excerpt": redact_text(body[:500]), "error": err, "latency_ms": dur}
        rec = CommandRecord(f"HTTP GET {url}", status, _now(), dur, _excerpt(body[:500], err), source="live")
        self.history.append(rec)
        if self.record_to is not None:
            self._recorded[f"HTTP GET {url}"] = {"exit_code": status, "stdout": result, "stderr": err}
        return status is not None and 200 <= status < 400, result, rec

    def save_recording(self) -> Optional[Path]:
        if self.record_to is None:
            return None
        self.record_to.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if self.record_to.exists():
            existing = json.loads(self.record_to.read_text(encoding="utf-8"))
        existing.setdefault("_meta", {"recorded_at": _now(), "note": "Sanitized + anonymized live az outputs"})
        existing.setdefault("commands", {}).update(self._recorded)
        self.record_to.write_text(anonymize(json.dumps(existing, indent=2), self._recorded), encoding="utf-8")
        return self.record_to


class ReplayRunner(AzRunner):
    """Serves fixtures: {"commands": {"<az args>" | glob: {exit_code, stdout, stderr}}}."""

    mode = "replay"

    def __init__(self, fixtures: Dict[str, Any]):
        super().__init__()
        self.az = "replay"
        self.fixtures = fixtures.get("commands", fixtures)

    @classmethod
    def from_file(cls, path: Path) -> "ReplayRunner":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _lookup(self, key: str) -> Optional[Dict[str, Any]]:
        if key in self.fixtures:
            return self.fixtures[key]
        for pattern, value in self.fixtures.items():
            if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(key, pattern):
                return value
        return None

    def run(self, args: List[str], timeout: Optional[int] = None) -> Tuple[bool, Any, CommandRecord]:
        if not is_read_only(args):
            raise CommandNotAllowed(f"az {' '.join(args)} is not on the read-only allowlist")
        _validate_args(args)
        key = " ".join(args)
        fx = self._lookup(key)
        if fx is None:
            rec = CommandRecord(
                "az " + key, None, _now(), 0, "no fixture recorded for this command", source="replay-missing"
            )
            self.history.append(rec)
            return False, {"error": "no-fixture"}, rec
        code = fx.get("exit_code", 0)
        ok = code == 0
        data = redact_obj(fx.get("stdout")) if ok else {"error": redact_text(fx.get("stderr", ""))}
        text = json.dumps(data) if not isinstance(data, str) else data
        rec = CommandRecord(
            "az " + key, code, _now(), 0, _excerpt(text, "" if ok else fx.get("stderr", "")), source="replay"
        )
        self.history.append(rec)
        return ok, data, rec

    def http_get(self, url: str, timeout: int = 20) -> Tuple[bool, Dict[str, Any], CommandRecord]:
        fx = self._lookup(f"HTTP GET {url}")
        if fx is None:
            rec = CommandRecord(f"HTTP GET {url}", None, _now(), 0, "no fixture", source="replay-missing")
            return False, {"status": None, "error": "no-fixture"}, rec
        result = fx.get("stdout") or {}
        status = result.get("status")
        rec = CommandRecord(f"HTTP GET {url}", status, _now(), 0, json.dumps(result)[:500], source="replay")
        self.history.append(rec)
        return status is not None and 200 <= status < 400, result, rec


class OfflineRunner(AzRunner):
    """No Azure access at all: every call is recorded as skipped."""

    mode = "offline"

    def __init__(self):
        super().__init__()
        self.az = None

    def run(self, args, timeout=None):
        if not is_read_only(args):
            raise CommandNotAllowed(" ".join(args))
        rec = CommandRecord("az " + " ".join(args), None, _now(), 0, "offline mode: not executed", source="skipped")
        self.history.append(rec)
        return False, {"error": "offline"}, rec

    def http_get(self, url, timeout=20):
        rec = CommandRecord(f"HTTP GET {url}", None, _now(), 0, "offline mode: not executed", source="skipped")
        return False, {"status": None, "error": "offline"}, rec
