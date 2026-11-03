"""Opt-in end-to-end test: one real signed-in chat message, then delete the test session.

Health probes only prove the apps answer "alive". This sends a real message through the same path a user takes
(sign-in token -> chat API -> Cosmos DB -> Foundry agents -> model) and reports where it breaks. It writes one chat
session and uses a little model quota, so it only runs after the user explicitly approved it.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from ..models import FAIL, PASS, SKIP, WARN, CheckResult
from .base import DiagContext, check
from .insights_checks import classify_message

EXPLAIN = {
    "auth-token": "the API rejected the sign-in token (HTTP 401): it must be issued for this app's API (audience = "
    "the ENTRA_AUTH_CLIENT_ID app, scope user_impersonation, same tenant)",
    "config-missing": "the API reports missing configuration (HTTP 503): a Foundry setting or agent is not set",
    "cosmos-firewall": "Cosmos DB refused the API: its firewall blocks the request (public network access is off)",
    "rbac-denied": "an identity was denied access (403 / AuthorizationFailed): a role assignment is missing",
    "model-not-found": "the model deployment the app asks for does not exist in the AI account",
    "throttled": "the model is throttling requests (HTTP 429): the deployment capacity is too low or exhausted",
    "timeout-network": "the call timed out or could not connect",
    "other": "the API returned an error that matches no known cause",
}
_AADSTS = re.compile(r"AADSTS\d+[^\n]{0,160}")
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=\-]+")


def _chat_target(ctx: DiagContext, spec: Dict[str, Any]) -> Tuple[str, str]:
    """(base url, app name) of the API app that handles chat.

    The host comes from Azure (``az webapp show``), never from a URL in a local file: the sign-in token is sent there,
    so a wrong or tampered value in an env file must not be able to redirect it.
    """
    state = ctx.results.get("webapp.state")
    for app in ctx.profile.get("apps", []):
        if app["id"] != spec.get("app"):
            continue
        row = next((a for a in (state.evidence.get("apps", []) if state else []) if a.get("id") == app["id"]), {})
        return (f"https://{row['host']}" if row.get("host") else ""), str(row.get("name") or "")
    return "", ""


def failure_category(status: Optional[int], detail: str) -> str:
    if status is None:
        return "timeout-network"
    if status == 401:
        return "auth-token"
    if status == 429:
        return "throttled"
    category = classify_message(detail)
    if status == 503 and category == "other":
        return "other"  # a bare 503 is the platform (app starting or crashing), not proof of missing settings
    return category


def _detail(result: Dict[str, Any]) -> str:
    body = result.get("json")
    if isinstance(body, dict) and body.get("detail"):
        return str(body["detail"])
    return str(result.get("body_excerpt") or result.get("error") or "")


@check(
    "chat.e2e",
    "Real chat message (end to end)",
    stages=["runtime", "startup", "authentication"],
    needs_apps=True,
    order=48,
    opt_in=True,
    description="Sends one real signed-in chat message and deletes the test session. Only runs after approval.",
)
def chat_e2e(ctx: DiagContext) -> CheckResult:
    title = "Real chat message (end to end)"
    spec = ctx.profile.get("chat_test")
    if not spec:
        return CheckResult("chat.e2e", title, SKIP, "This accelerator's profile defines no chat test.")
    if not ctx.overrides.get("chat_test_approved"):
        return CheckResult(
            "chat.e2e", title, SKIP, "Not approved: this check sends a real message, so it only runs when asked."
        )
    if ctx.rg_known_missing():
        return CheckResult("chat.e2e", title, SKIP, "Resource group does not exist.")
    base, app_name = _chat_target(ctx, spec)
    if not base:
        return CheckResult("chat.e2e", title, SKIP, "Chat API host unknown (Azure did not report the API app).")
    if app_name and app_name in (ctx.facts.get("stopped_apps") or []):
        return CheckResult("chat.e2e", title, SKIP, f"The API app {app_name} is stopped: start it first.")

    # 1. a sign-in token for this API, held in memory only
    token_env = ctx.overrides.get("token_env")
    if token_env:
        token = os.environ.get(token_env, "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            return CheckResult("chat.e2e", title, SKIP, f"Environment variable {token_env} is empty.")
        if not _TOKEN_SHAPE.fullmatch(token):
            return CheckResult(
                "chat.e2e",
                title,
                SKIP,
                f"The value in {token_env} is not a single-line token (it has spaces or line breaks). Paste only the "
                "token, on one line.",
            )
    else:
        client_id = ctx.overrides.get("client_id") or ctx.env.get(spec.get("client_id_env", ""))
        if not client_id:
            return CheckResult(
                "chat.e2e",
                title,
                SKIP,
                f"Needs the API's app registration client id: pass --client-id, or set {spec.get('client_id_env')} "
                "(the post-provision auth script stores it in the azd environment).",
            )
        ok, token, _ = ctx.runner.get_token(f"api://{client_id}/{spec.get('scope', 'user_impersonation')}")
        if not ok:
            code = (_AADSTS.search(token) or [None])[0] if isinstance(token, str) else None
            return CheckResult(
                "chat.e2e",
                title,
                WARN,
                "Could not get a sign-in token for the API"
                + (f" ({code[:120]})" if code else ".")
                + " The test did not run.",
                evidence={
                    "how_to_fix": [
                        "Sign in with the account you use for the app: az login",
                        "Azure CLI must be allowed to request tokens for the API: in the Entra app registration open "
                        "Expose an API, Add a client application, enter 04b07795-8ddb-461a-bbee-02f9e1bf7b46 "
                        "(Azure CLI) and tick the user_impersonation scope (needs the app owner).",
                        "Or copy a token from the signed-in app in your browser, put it in an environment variable and "
                        "run again with --token-env <NAME>.",
                    ]
                },
                doc_refs=[ctx.ref("auth", "user_impersonation")],
            )

    # 2. one real message, then clean up the test session
    session_id = f"troubleshooter-probe-{int(time.time())}"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "content": spec.get("message", "Hello! Please reply with one short sentence."),
        "message_type": "user",
        "session_id": session_id,
    }
    ok, res, _ = ctx.runner.http_request(
        "POST", base + spec.get("path", "/api/chat/message"), headers, body, timeout=150
    )
    status = res.get("status")
    cleaned: Optional[bool] = None
    if status not in (401, 403):  # rejected before the handler: no session. Anything else (even a timeout) may have one
        delete_path = spec.get("delete_path", "/api/chat/sessions/{session_id}").format(session_id=session_id)
        deleted, del_res, _ = ctx.runner.http_request("DELETE", base + delete_path, headers, None, timeout=60)
        cleaned = deleted or del_res.get("status") == 404  # 404: there was no session to delete
    secs = (res.get("latency_ms") or 0) / 1000
    ev: Dict[str, Any] = {"http_status": status, "seconds": round(secs, 1), "test_session_deleted": cleaned}
    if cleaned is False:
        ev["cleanup_note"] = (
            f"The test chat session '{session_id}' may still exist in Cosmos DB (chat container): delete it by hand."
        )
    if ok:
        reply = ""
        if isinstance(res.get("json"), dict):
            reply = str(res["json"].get("content") or "")
        if not reply.strip():
            ctx.facts["chat_e2e"] = {"ok": False, "status": status, "category": "other", "detail": "empty reply"}
            return CheckResult(
                "chat.e2e", title, FAIL, "The API answered HTTP 200 but the reply was empty.", evidence=ev
            )
        ctx.facts["chat_e2e"] = {"ok": True, "status": status, "seconds": round(secs, 1)}
        ev["reply_preview"] = " ".join(reply.split())[:120]
        return CheckResult(
            "chat.e2e", title, PASS, f"A real chat message got a model reply in {secs:.1f} s.", evidence=ev
        )
    detail = _detail(res)
    category = failure_category(status, detail)
    ctx.facts["chat_e2e"] = {"ok": False, "status": status, "category": category, "detail": detail[:300]}
    ev.update(category=category, error=detail[:300])
    where = f"HTTP {status}" if status else "no response"
    return CheckResult(
        "chat.e2e",
        title,
        WARN if category == "throttled" else FAIL,
        f"A real chat message failed ({where}): {EXPLAIN[category]}.",
        evidence=ev,
    )
