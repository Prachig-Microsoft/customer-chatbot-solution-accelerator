"""Application Insights: what the running apps themselves logged as errors (read-only, last hour by default)."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import ERROR, FAIL, INFO, PASS, SKIP, WARN, CheckResult
from .azure_checks import _need_rg
from .base import DiagContext, check, parallel_map

WINDOW_OK = re.compile(r"^P(T\d{1,3}[HM]|\d{1,2}D)$")
DEFAULT_WINDOW = "PT1H"
TABLES = ("exceptions", "traces", "requests", "customEvents")

# Order matters: the first matching category wins, so specific causes come before generic ones.
CATEGORIES = [
    (
        "cosmos-firewall",
        r"blocked by your cosmos db account firewall|request originated from ip|public network access.{0,30}disabled"
        r"|publicnetworkaccess",
    ),
    (
        "model-not-found",
        r"deploymentnotfound|api deployment for this resource does not exist|deployment .{0,60}does not exist"
        r"|model .{0,60}(not found|does not exist)",
    ),
    ("throttled", r"\b429\b|rate limit|too many requests|toomanyrequests|retry after|error_agent_rate_limit"),
    (
        "rbac-denied",
        r"permissiondenied|authorizationfailed|authorizationpermissionmismatch|required rbac permissions?"
        r"|does not have (the )?(required )?(rbac )?permissions?|principal does not have|\(forbidden\)|\b403\b",
    ),
    (
        "auth-token",
        r"\b401\b|unauthori[sz]ed|invalid (bearer )?token|authentication (is )?(required|not configured)|aadsts",
    ),
    (
        "config-missing",
        r"is not configured|missing or empty|not fully configured|environment variable|keyerror|must be set"
        r"|error_config_missing",
    ),
    (
        "timeout-network",
        r"timed? ?out|timeout|connection ?(error|refused|reset)|name or service not known|getaddrinfo|econnreset",
    ),
]
_CATEGORY_RX = [(name, re.compile(rx, re.I)) for name, rx in CATEGORIES]
# The Application Insights SDK reporting that it could not upload its own telemetry: not a fault of the app.
_TELEMETRY_NOISE = re.compile(
    r"envelopes could not be exported|pinging live metrics|live ?metrics|quickpulse|exporting telemetry|"
    r"azure\.monitor\.opentelemetry|transmission (of telemetry )?failed",
    re.I,
)

_NORMALISE = [
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<id>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b\d{3,}\b"), "<n>"),
]


def classify_message(text: str) -> str:
    for name, rx in _CATEGORY_RX:
        if rx.search(text or ""):
            return name
    return "other"


# A request turned away for having no sign-in is what an API with Entra sign-in is supposed to do. Token faults are not.
_NO_SIGN_IN = re.compile(
    r"authentication (is )?required|not authenticated|missing (bearer |access )?token|no (bearer |access )?token"
    r"|\b401\b|unauthori[sz]ed",
    re.I,
)
_TOKEN_FAULT = re.compile(
    r"invalid|expired|audience|issuer|tenant|signature|scope|aadsts|malformed|decode|not configured|mismatch", re.I
)
MOSTLY_REJECTED = 0.5  # if more than half of all requests are rejected, signed-in users are probably affected too
MOSTLY_REJECTED_MIN = 10


def split_expected_rejections(result: Dict[str, Any]) -> Dict[str, Any]:
    """Move "no sign-in" 401 groups out of the errors (only call when sign-in is configured on the APIs).

    Returns {count, last, examples, mostly_rejected}; ``result`` is updated in place. When most requests are rejected,
    nothing is moved: that pattern means signed-in users are turned away too (for example the front end sends no token).
    """
    expected = [
        g
        for g in result["groups"]
        if g["category"] == "auth-token"
        and g["source"] != "request"
        and _NO_SIGN_IN.search(g["what"])
        and not _TOKEN_FAULT.search(g["what"])
    ]
    count = sum(g["count"] for g in expected)
    total = result.get("requests_total") or 0
    mostly = bool(count >= MOSTLY_REJECTED_MIN and total and count > total * MOSTLY_REJECTED)
    info = {
        "count": count,
        "last": max((g["last"] for g in expected), default=""),
        "examples": [f"{g['role'] or 'app'}: {g['what']} (x{g['count']})" for g in expected[:3]],
        "mostly_rejected": mostly,
    }
    if not expected or mostly:
        return info
    ids = {id(g) for g in expected}
    result["groups"] = [g for g in result["groups"] if id(g) not in ids]
    result["by_category"]["auth-token"] = result["by_category"].get("auth-token", 0) - count
    if result["by_category"]["auth-token"] <= 0:
        del result["by_category"]["auth-token"]
    result["error_total"] -= count
    return info


def normalise(text: str, limit: int = 180) -> str:
    out = " ".join(str(text or "").split())
    for rx, repl in _NORMALISE:
        out = rx.sub(repl, out)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _rows(data: Any) -> List[Dict[str, Any]]:
    tables = (data or {}).get("tables") if isinstance(data, dict) else None
    if not tables:
        return []
    cols = [c.get("name") for c in tables[0].get("columns", [])]
    return [dict(zip(cols, row)) for row in tables[0].get("rows", [])]


def _minutes_ago(ts: Optional[str]) -> Optional[int]:
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - when).total_seconds() // 60))
    except (TypeError, ValueError):
        return None


def summarise(tables: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Group the logged errors; every group keeps its count, latest time and category."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    noise = [0]

    def add(source: str, role: str, what: str, when: Any, count: int = 1) -> None:
        if _TELEMETRY_NOISE.search(what or ""):
            noise[0] += count
            return
        key = (source, role, normalise(what))
        g = groups.setdefault(
            key, {"source": source, "role": role, "what": key[2], "count": 0, "last": "", "category": ""}
        )
        g["count"] += count
        g["last"] = max(g["last"], str(when or ""))
        g["category"] = classify_message(what)

    for r in tables.get("exceptions", []):
        text = r.get("outerMessage") or r.get("message") or r.get("innermostMessage") or ""
        add(
            "exception",
            str(r.get("cloud_RoleName") or ""),
            f"{r.get('type') or 'Exception'}: {text}",
            r.get("timestamp"),
            int(r.get("itemCount") or 1),
        )
    for r in tables.get("traces", []):
        try:
            level = int(r.get("severityLevel") or 0)
        except (TypeError, ValueError):
            level = 0
        if level >= 3:  # error and critical only
            add("log", str(r.get("cloud_RoleName") or ""), str(r.get("message") or ""), r.get("timestamp"))
    for r in tables.get("customEvents", []):
        name = str(r.get("name") or "")
        if name.startswith("Error_"):  # the accelerator's own error events (Error_Agent_Execution, ...)
            dims = r.get("customDimensions")
            add("event", str(r.get("cloud_RoleName") or ""), f"{name} {dims or ''}", r.get("timestamp"))
    requests_total = requests_failed = 0
    for r in tables.get("requests", []):
        n = int(r.get("itemCount") or 1)
        requests_total += n
        code = str(r.get("resultCode") or "")
        if code.startswith("5"):
            requests_failed += n
            add(
                "request",
                str(r.get("cloud_RoleName") or ""),
                f"HTTP {code} {r.get('name') or ''}",
                r.get("timestamp"),
                n,
            )
    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["last"]))
    by_category: Dict[str, int] = defaultdict(int)
    for g in ordered:
        if g["source"] != "request":  # a failed request is the symptom, the log line behind it is the cause
            by_category[g["category"]] += g["count"]
    return {
        "groups": ordered,
        "by_category": dict(by_category),
        "requests_total": requests_total,
        "requests_failed": requests_failed,
        "error_total": sum(g["count"] for g in ordered if g["source"] != "request"),
        "telemetry_noise": noise[0],
    }


@check(
    "appinsights.errors",
    "Errors logged by the apps (Application Insights)",
    stages=[
        "runtime",
        "startup",
        "ingestion",
        "authentication",
        "throttling",
        "networking",
        "local-vs-cloud",
        "configuration",
    ],
    order=47,
)
def appinsights_errors(ctx: DiagContext) -> CheckResult:
    title = "Errors logged by the apps (Application Insights)"
    if skip := _need_rg(ctx, "appinsights.errors", title):
        return skip
    window = str(ctx.overrides.get("window") or DEFAULT_WINDOW).upper()
    if not WINDOW_OK.match(window):
        return CheckResult(
            "appinsights.errors", title, ERROR, f"Unsupported window '{window}' (use e.g. PT30M, PT6H, P1D)."
        )
    comps = [
        r for r in ctx.facts.get("resources") or [] if (r.get("type") or "").lower() == "microsoft.insights/components"
    ]
    if not comps:
        return CheckResult(
            "appinsights.errors",
            title,
            INFO,
            "No Application Insights resource in this resource group (monitoring is off, or it uses a resource in "
            "another group), so the apps' own error logs cannot be read here.",
        )
    sub = ctx.facts.get("subscription_id")
    if not sub:
        return CheckResult("appinsights.errors", title, SKIP, "Subscription unknown (az login first).")
    name, rg = comps[0]["name"], ctx.resource_group
    base = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/microsoft.insights/components"
    )
    ok, comp, _ = ctx.az(
        "rest", "--method", "get", "--url", f"{base}/{name}", "--uri-parameters", "api-version=2020-02-02"
    )
    app_id = ((comp or {}).get("properties") or comp or {}).get("AppId") if ok and isinstance(comp, dict) else None
    if not app_id:
        return CheckResult(
            "appinsights.errors",
            title,
            WARN,
            f"Could not read Application Insights '{name}' (needs at least Reader on it): "
            f"{(comp or {}).get('error', 'no AppId returned') if isinstance(comp, dict) else comp}"[:240],
        )

    def run(table: str):
        query_url = f"https://api.applicationinsights.io/v1/apps/{app_id}/query"
        res = ctx.az(
            "rest",
            "--method",
            "get",
            "--url",
            query_url,
            "--uri-parameters",
            f"query={table}",
            f"timespan={window}",
            "--resource",
            "https://api.applicationinsights.io",
        )
        return table, res[0], res[1]

    tables: Dict[str, List[Dict[str, Any]]] = {}
    failed: List[str] = []
    for table, ok, data in parallel_map(ctx, run, list(TABLES)):
        if ok:
            tables[table] = _rows(data)
        else:
            failed.append(f"{table}: {str((data or {}).get('error', data))[:120]}")
    if len(failed) == len(TABLES):
        return CheckResult(
            "appinsights.errors",
            title,
            WARN,
            "Could not query Application Insights (needs Reader/Monitoring Reader on it): " + failed[0],
        )
    result = summarise(tables)
    sign_in = ctx.facts.get("auth_setup_state") == "configured"
    expected = split_expected_rejections(result) if sign_in else {"count": 0, "mostly_rejected": False}
    moved = expected["count"] if sign_in and not expected["mostly_rejected"] else 0
    ctx.facts["appinsights_errors"] = {**result, "window": window, "component": name}
    docs = _closest_docs(ctx, result["groups"][:3])
    ev = {
        "component": name,
        "window": window,
        "requests": {"total": result["requests_total"], "failed_5xx": result["requests_failed"]},
        "by_category": result["by_category"],
        "top_errors": [{**g, "minutes_ago": _minutes_ago(g["last"])} for g in result["groups"][:10]],
        "closest_docs": docs,
        "query_failures": failed,
        "telemetry_noise": result.get("telemetry_noise", 0),
        "expected_rejections": {**expected, "minutes_ago": _minutes_ago(expected.get("last"))} if sign_in else None,
    }
    note = (
        f" Sign-in is enforced: {moved} request(s) without a sign-in were rejected (HTTP 401), as expected."
        if moved
        else ""
    )
    if sign_in and expected["mostly_rejected"]:
        note = (
            f" {expected['count']} of {result['requests_total']} request(s) were rejected for missing sign-in: "
            "more than half, so signed-in users are probably turned away too (is the front end sending the token?)."
        )
    if not result["groups"]:
        if not any(tables.get(t) for t in TABLES):
            return CheckResult(
                "appinsights.errors",
                title,
                INFO,
                f"Nothing logged in the last {window}: no traffic yet, or telemetry is delayed. Send a request, "
                "wait a few minutes and run it again.",
                evidence=ev,
            )
        return CheckResult(
            "appinsights.errors",
            title,
            PASS,
            f"No errors logged in the last {window} ({result['requests_total']} request(s) seen).{note}",
            evidence=ev,
        )
    known = {c: n for c, n in result["by_category"].items() if c != "other"}
    top = result["groups"][0]
    parts = ", ".join(f"{c} x{n}" for c, n in sorted(known.items(), key=lambda kv: -kv[1])[:3])
    summary = (
        f"{result['error_total']} error(s) in the last {window}"
        + (f" ({parts})" if parts else "")
        + f"; most frequent: {top['what'][:90]}"
        + note
    )
    return CheckResult("appinsights.errors", title, FAIL if known else WARN, summary, evidence=ev)


def _closest_docs(ctx: DiagContext, groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if ctx.index is None:
        return out
    for g in groups:
        for hit in ctx.index.search(g["what"], k=1, doc_types=["troubleshooting-row"]):
            if hit.score >= 15 and all(o["ref"] != hit.ref for o in out):
                out.append({"title": hit.title, "ref": hit.ref, "for": g["what"][:60], "match": "keyword"})
    return out
