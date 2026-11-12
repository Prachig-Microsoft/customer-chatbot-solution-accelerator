"""Azure CLI / HTTP execution layer.

* Only allow-listed **read-only** az commands can run through ``AzRunner.run``.
* State-changing commands are only executed by ``actions.execute`` after explicit approval.
* Every call is timed, recorded, and its output redacted.
* ``ReplayRunner`` serves recorded fixtures so tests/demos are reproducible without Azure.
"""

from __future__ import annotations

import fnmatch
import http.client
import json
import re
import shutil
import subprocess
import threading
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
    "policy definition show",
    "resource show",
    "lock list",
    "extension list",
]

# Local tools whose installed version the preflight may read: fixed argv lists (never built from input) and a pattern
# that pulls the version out of the output. Candidates are tried in order; the first that runs wins.
LOCAL_TOOLS: Dict[str, Tuple[List[List[str]], str]] = {
    "azd": ([["azd", "version"]], r"azd version (\d+(?:\.\d+)+)"),
    "az": ([["az", "--version"]], r"azure-cli\s+(\d+(?:\.\d+)+)"),
    "python": (
        [["python", "--version"], ["python3", "--version"], ["py", "-3", "--version"]],
        r"Python (\d+(?:\.\d+)+)",
    ),
    "node": ([["node", "--version"]], r"v?(\d+(?:\.\d+)+)"),
    "pwsh": ([["pwsh", "--version"]], r"PowerShell (\d+(?:\.\d+)+)"),
    "docker": ([["docker", "--version"]], r"Docker version (\d+(?:\.\d+)+)"),
    "git": ([["git", "--version"]], r"git version (\d+(?:\.\d+)+)"),
    "dotnet": ([["dotnet", "--version"]], r"(\d+(?:\.\d+)+)"),
    "terraform": ([["terraform", "--version"]], r"Terraform v(\d+(?:\.\d+)+)"),
    "kubectl": ([["kubectl", "version", "--client"]], r"Client Version: v(\d+(?:\.\d+)+)"),
}

ALLOWED_HTTP_HOST_SUFFIXES = (".azurewebsites.net", "localhost", "127.0.0.1")


def _host_allowed(host: str) -> bool:
    """Exact local hosts, or a subdomain of azurewebsites.net (no look-alike hosts such as 'evillocalhost')."""
    return host in ("localhost", "127.0.0.1") or host.endswith(".azurewebsites.net")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - returning None raises HTTPError
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)
_TOKEN_SCOPE = re.compile(r"^api://[0-9a-fA-F-]{36}/[A-Za-z_.]+$")
_ARM_COMPONENT_PATH = (
    r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[\w.()-]+/providers/microsoft\.insights/components/[\w.-]+"
)
# App Service plan quota per region and SKU (read-only GET; verified to return "B3 VMs" limits like the ARM error).
_WEB_QUOTA_PATH = (
    r"/subscriptions/[0-9a-fA-F-]{36}/providers/Microsoft\.Web/locations/[a-z0-9]+/providers/Microsoft\.Quota/"
    r"(?:quotas|usages)"
)

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


def _rest_allowed(args: List[str]) -> bool:
    """``az rest`` is generic, so allow only: --method get, to ARM Application Insights component reads, the App Service
    quota reads (Microsoft.Quota for Microsoft.Web) or the Application Insights query API, with no body, headers or
    output file."""
    if not args or args[0] != "rest":
        return False
    method = url = ""
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("--method", "-m") and i + 1 < len(args):
            method, i = args[i + 1].lower(), i + 2
        elif a in ("--url", "--uri", "-u") and i + 1 < len(args):
            url, i = args[i + 1], i + 2
        elif a == "--uri-parameters":
            i += 1
            while i < len(args) and not args[i].startswith("-"):
                i += 1
        elif a == "--resource" and i + 1 < len(args):
            if args[i + 1] != "https://api.applicationinsights.io":
                return False
            i += 2
        else:
            return False  # --body, --headers, --output-file, --skip-authorization-header ... never allowed
    u = urlparse(url)
    if method != "get" or u.scheme != "https" or u.query:
        return False
    if u.hostname == "management.azure.com":
        return bool(re.fullmatch(_ARM_COMPONENT_PATH, u.path, re.I) or re.fullmatch(_WEB_QUOTA_PATH, u.path, re.I))
    if u.hostname == "api.applicationinsights.io":
        return bool(re.fullmatch(r"/v1/apps/[0-9a-fA-F-]{36}/query", u.path))
    return False


def is_read_only(args: List[str]) -> bool:
    if args and args[0] == "rest":
        return _rest_allowed(args)
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
        self._local = threading.local()

    def _log(self, rec: CommandRecord) -> None:
        """Keep every command in the global history and in the capture list of the check running on this thread."""
        self.history.append(rec)
        cap = getattr(self._local, "capture", None)
        if cap is not None:
            cap.append(rec)

    def begin_capture(self, into: Optional[List[CommandRecord]] = None) -> List[CommandRecord]:
        """Start attributing this thread's commands to a check (``into`` lets helper threads join their parent)."""
        self._local.capture = into if into is not None else []
        return self._local.capture

    def end_capture(self) -> None:
        self._local.capture = None

    def current_capture(self) -> Optional[List[CommandRecord]]:
        return getattr(self._local, "capture", None)

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
            self._log(rec)
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
        self._log(rec)
        if self.record_to is not None:
            self._recorded[" ".join(args)] = {
                "exit_code": code,
                "stdout": data if ok else None,
                "stderr": data.get("error") if not ok else "",
            }
        return ok, data, rec

    def local_version(self, tool: str, timeout: int = 20) -> Tuple[str, Optional[str], CommandRecord]:
        """Installed version of a known local tool: ("found", "1.2.3") / ("missing", None) / ("unknown", None)."""
        if tool not in LOCAL_TOOLS:
            raise CommandNotAllowed(f"local tool not allowed: {tool}")
        candidates, pattern = LOCAL_TOOLS[tool]
        start = time.time()
        last = ""
        for argv in candidates:
            exe = shutil.which(argv[0])
            if not exe:
                continue
            try:
                proc = subprocess.run(
                    [exe, *argv[1:]],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                )
            except (subprocess.TimeoutExpired, OSError):
                last = "did not run"
                continue
            m = re.search(pattern, (proc.stdout or "") + "\n" + (proc.stderr or ""))
            if proc.returncode == 0 and m:
                dur = int((time.time() - start) * 1000)
                rec = CommandRecord(" ".join(argv), 0, _now(), dur, f"version {m.group(1)}", source="live")
                self._log(rec)
                return "found", m.group(1), rec
            last = "no version in the output"
        dur = int((time.time() - start) * 1000)
        rec = CommandRecord(" ".join(candidates[0]), None, _now(), dur, last or "not installed", source="live")
        self._log(rec)
        return ("unknown" if last else "missing"), None, rec

    def http_get(self, url: str, timeout: int = 20) -> Tuple[bool, Dict[str, Any], CommandRecord]:
        host = urlparse(url).hostname or ""
        if not _host_allowed(host):
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
        self._log(rec)
        if self.record_to is not None:
            self._recorded[f"HTTP GET {url}"] = {"exit_code": status, "stdout": result, "stderr": err}
        return status is not None and 200 <= status < 400, result, rec

    def get_token(self, scope: str, timeout: int = 60) -> Tuple[bool, str, CommandRecord]:
        """Sign-in token for the user's own identity. Kept in memory only: never logged, recorded or put in a report."""
        if not _TOKEN_SCOPE.match(scope):
            raise CommandNotAllowed(f"token scope not allowed: {scope}")
        label = f"az account get-access-token --scope {scope} (token never recorded)"
        if not self.available:
            rec = CommandRecord(label, None, _now(), 0, "Azure CLI not found on PATH", source="skipped")
            self._log(rec)
            return False, "Azure CLI not found on PATH", rec
        start = time.time()
        try:
            proc = subprocess.run(
                [self.az, "account", "get-access-token", "--scope", scope, "--query", "accessToken", "-o", "tsv"]
                + ["--only-show-errors"],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
            )
            code, token, err = proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except subprocess.TimeoutExpired:
            code, token, err = None, "", f"timed out after {timeout}s"
        ok = code == 0 and token.count(".") == 2
        rec = CommandRecord(label, code, _now(), int((time.time() - start) * 1000), _excerpt("", err), source="live")
        self._log(rec)
        return ok, (token if ok else redact_text(err)[:600] or "no token returned"), rec

    def http_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 90,
    ) -> Tuple[bool, Dict[str, Any], CommandRecord]:
        """Send one request to an allow-listed host. Credentials in ``headers`` are never recorded or returned."""
        method = method.upper()
        if method not in ("GET", "POST", "DELETE"):
            raise CommandNotAllowed(f"HTTP {method} not allowed")
        host = urlparse(url).hostname or ""
        if not _host_allowed(host):
            raise CommandNotAllowed(f"HTTP request to {host} not allowed")
        bad = [
            k for k, v in (headers or {}).items() if re.search(r"[\x00-\x1f\x7f\s]", str(v).replace("Bearer ", "", 1))
        ]
        if bad:  # never let http.client echo a credential back in an error message
            raise CommandNotAllowed(f"header value for {bad[0]} contains whitespace or control characters")
        start = time.time()
        status, body, err = None, "", ""
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req_headers = {"User-Agent": "accel-troubleshooter/0.1", "Content-Type": "application/json", **(headers or {})}
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
            # Redirects are never followed: they would forward the Authorization header to whatever host answers.
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:  # nosec - host allowlisted
                status, body = resp.status, resp.read(20000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read(20000).decode("utf-8", "replace") if e.fp else ""
        except (urllib.error.URLError, OSError) as e:
            err = str(e)
        except (ValueError, http.client.HTTPException) as e:
            err = f"request could not be sent ({type(e).__name__})"  # never include e: it can echo header values
        dur = int((time.time() - start) * 1000)
        try:
            parsed = redact_obj(json.loads(body)) if body.strip() else None
        except ValueError:
            parsed = None
        result = {
            "status": status,
            "json": parsed,
            "body_excerpt": redact_text(body[:600]),
            "error": redact_text(err),
            "latency_ms": dur,
        }
        rec = CommandRecord(
            f"HTTP {method} {url} (credentials not recorded)", status, _now(), dur, _excerpt(body[:300], err), "live"
        )
        self._log(rec)
        return status is not None and 200 <= status < 300, result, rec

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
            self._log(rec)
            return False, {"error": "no-fixture"}, rec
        code = fx.get("exit_code", 0)
        ok = code == 0
        data = redact_obj(fx.get("stdout")) if ok else {"error": redact_text(fx.get("stderr", ""))}
        text = json.dumps(data) if not isinstance(data, str) else data
        rec = CommandRecord(
            "az " + key, code, _now(), 0, _excerpt(text, "" if ok else fx.get("stderr", "")), source="replay"
        )
        self._log(rec)
        return ok, data, rec

    def local_version(self, tool: str, timeout: int = 20):
        if tool not in LOCAL_TOOLS:
            raise CommandNotAllowed(f"local tool not allowed: {tool}")
        fx = self._lookup(f"LOCAL {tool}")
        label = " ".join(LOCAL_TOOLS[tool][0][0])
        if fx is None:
            rec = CommandRecord(label, None, _now(), 0, "no fixture", source="replay-missing")
            self._log(rec)
            return "unknown", None, rec
        if fx.get("exit_code", 0) != 0:
            rec = CommandRecord(label, fx.get("exit_code"), _now(), 0, "not installed", source="replay")
            self._log(rec)
            return "missing", None, rec
        version = str(fx.get("stdout") or "")
        rec = CommandRecord(label, 0, _now(), 0, f"version {version}", source="replay")
        self._log(rec)
        return "found", version, rec

    def http_get(self, url: str, timeout: int = 20) -> Tuple[bool, Dict[str, Any], CommandRecord]:
        fx = self._lookup(f"HTTP GET {url}")
        if fx is None:
            rec = CommandRecord(f"HTTP GET {url}", None, _now(), 0, "no fixture", source="replay-missing")
            return False, {"status": None, "error": "no-fixture"}, rec
        result = fx.get("stdout") or {}
        status = result.get("status")
        rec = CommandRecord(f"HTTP GET {url}", status, _now(), 0, json.dumps(result)[:500], source="replay")
        self._log(rec)
        return status is not None and 200 <= status < 400, result, rec

    def get_token(self, scope: str, timeout: int = 60) -> Tuple[bool, str, CommandRecord]:
        if not _TOKEN_SCOPE.match(scope):
            raise CommandNotAllowed(f"token scope not allowed: {scope}")
        fx = self._lookup(f"TOKEN {scope}")
        label = f"az account get-access-token --scope {scope} (token never recorded)"
        if fx is None:
            rec = CommandRecord(label, None, _now(), 0, "no fixture", source="replay-missing")
            self._log(rec)
            return False, "no fixture recorded for the token request", rec
        ok = fx.get("exit_code", 0) == 0
        rec = CommandRecord(label, fx.get("exit_code", 0), _now(), 0, "" if ok else fx.get("stderr", ""), "replay")
        self._log(rec)
        return ok, (fx.get("stdout") or "fake.token.value") if ok else fx.get("stderr", ""), rec

    def http_request(self, method, url, headers=None, json_body=None, timeout=90):
        method = method.upper()
        if method not in ("GET", "POST", "DELETE"):
            raise CommandNotAllowed(f"HTTP {method} not allowed")
        key = f"HTTP {method} {url}"
        fx = self._lookup(key)
        label = f"{key} (credentials not recorded)"
        if fx is None:
            rec = CommandRecord(label, None, _now(), 0, "no fixture", source="replay-missing")
            self._log(rec)
            return False, {"status": None, "error": "no-fixture", "json": None}, rec
        result = {"json": None, "body_excerpt": "", "error": "", "latency_ms": 0, **(fx.get("stdout") or {})}
        status = result.get("status")
        rec = CommandRecord(label, status, _now(), 0, json.dumps(result)[:300], source="replay")
        self._log(rec)
        return status is not None and 200 <= status < 300, result, rec


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
        self._log(rec)
        return False, {"error": "offline"}, rec

    def local_version(self, tool, timeout=20):
        rec = CommandRecord(tool, None, _now(), 0, "offline mode: not executed", source="skipped")
        return "unknown", None, rec

    def http_get(self, url, timeout=20):
        rec = CommandRecord(f"HTTP GET {url}", None, _now(), 0, "offline mode: not executed", source="skipped")
        return False, {"status": None, "error": "offline"}, rec

    def get_token(self, scope, timeout=60):
        rec = CommandRecord("token request", None, _now(), 0, "offline mode: not executed", source="skipped")
        return False, "offline mode", rec

    def http_request(self, method, url, headers=None, json_body=None, timeout=90):
        rec = CommandRecord(f"HTTP {method} {url}", None, _now(), 0, "offline mode: not executed", source="skipped")
        return False, {"status": None, "error": "offline", "json": None}, rec
