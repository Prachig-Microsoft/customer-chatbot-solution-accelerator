"""Regression / evaluation suite runner (replay mode, no live Azure)."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .azcli import OfflineRunner, ReplayRunner
from .engine import Troubleshooter
from .repo import TOOL_ROOT

EVAL_DIR = TOOL_ROOT / "evals"
APP_SETTINGS_KEY = "webapp config appsettings list --resource-group rg-demo --name api-chat-demo"


def _apply_patch(fixture: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, Any]:
    fx = copy.deepcopy(fixture)
    cmds = fx["commands"]
    for key, value in (case.get("patch") or {}).items():
        if value is None:
            cmds.pop(key, None)
        else:
            cmds[key] = value
    settings = cmds.get(APP_SETTINGS_KEY, {}).get("stdout")
    if settings is not None:
        for name, value in (case.get("appsettings") or {}).items():
            row = next((s for s in settings if s["name"] == name), None)
            if row:
                row["value"] = value
            else:
                settings.append({"name": name, "value": value})
        for name in case.get("remove_settings") or []:
            settings[:] = [s for s in settings if s["name"] != name]
    for key, path_value in (case.get("set") or {}).items():
        cmd, _, dotted = key.partition("::")
        target = cmds[cmd]["stdout"]
        parts = dotted.split(".")
        for p in parts[:-1]:
            target = target[int(p)] if isinstance(target, list) else target[p]
        last = parts[-1]
        if isinstance(target, list):
            target[int(last)] = path_value
        else:
            target[last] = path_value
    return fx


def _grounded(diag) -> bool:
    """Every confirmed/likely hypothesis must cite a failing check, an observed/documented error code, or an
    explicit reported symptom (HTTP status / error code) together with a check that observed the configuration."""
    status = {c.check_id: c.status for c in diag.checks}
    for h in diag.hypotheses:
        if h.status not in ("confirmed", "likely"):
            continue
        if any(status.get(c) in ("fail", "warn") for c in h.supporting_checks):
            continue
        if h.doc_refs and any("documented" in r or "error code" in r for r in h.rationale):
            continue
        if (diag.issue.http_statuses or diag.issue.error_codes) and any(
            status.get(c) in ("pass", "info") for c in h.supporting_checks
        ):
            continue
        return False
    return True


def run_case(case: Dict[str, Any], fixtures: Dict[str, Dict[str, Any]], workspace: Optional[Path]) -> Dict[str, Any]:
    if case.get("fixture") == "offline":
        runner = OfflineRunner()
    else:
        runner = ReplayRunner(_apply_patch(fixtures[case["fixture"]], case))
    ws = (EVAL_DIR / case["workspace"]).resolve() if case.get("workspace") else workspace
    ts = Troubleshooter(ws, runner)
    env_file = str(EVAL_DIR / "fixtures" / case["env_file"]) if case.get("env_file") else "none"
    start = time.time()
    diag = ts.diagnose(
        case["issue"],
        accelerator=case.get("accelerator_arg"),
        overrides=case.get("overrides"),
        env_file=env_file,
        log=False,
    )
    exp = case["expect"]
    top = diag.hypotheses[0] if diag.hypotheses else None
    strong = [h for h in diag.hypotheses if h.status in ("confirmed", "likely")]
    res: Dict[str, Any] = {
        "id": case["id"],
        "ms": int((time.time() - start) * 1000),
        "accelerator": diag.accelerator.get("id"),
        "clarification": bool(diag.clarification),
        "stages": diag.issue.stages[:3],
        "top": top.id if top else None,
        "top_status": top.status if top else None,
        "checks": {},
    }
    ok = {}
    if "clarification" in exp:
        ok["clarification"] = bool(diag.clarification) == exp["clarification"]
    if "accelerator" in exp:
        ok["accelerator"] = diag.accelerator.get("id") == exp["accelerator"]
    if "stage" in exp:
        ok["stage"] = exp["stage"] in diag.issue.stages[:2]
    if "top" in exp:
        allowed = exp["top"] if isinstance(exp["top"], list) else [exp["top"]]
        if allowed == [None]:
            ok["top"] = not strong
        else:
            ok["top"] = bool(top) and top.id in allowed and top.status in ("confirmed", "likely")
    if "top_status" in exp:
        ok["top_status"] = bool(top) and top.status == exp["top_status"]
    if "doc" in exp:
        refs = [d.ref for d in diag.docs] + [r for h in diag.hypotheses for r in h.doc_refs + h.source_refs]
        ok["doc"] = any(exp["doc"] in r for r in refs)
    if "action" in exp:
        ok["action"] = any(k.split("#")[0] == exp["action"] for k in diag.actions)
    if not diag.clarification:
        ok["grounded"] = _grounded(diag)
    res["checks"] = ok
    res["passed"] = all(ok.values())
    return res


def run_eval(cases_path: Optional[Path] = None, workspace: Optional[Path] = None) -> Dict[str, Any]:
    cases_path = cases_path or EVAL_DIR / "cases.json"
    cases: List[Dict[str, Any]] = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    fixtures = {}
    for p in (EVAL_DIR / "fixtures").glob("*.json"):
        fixtures[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    results = [run_case(c, fixtures, workspace) for c in cases]
    dims: Dict[str, List[bool]] = {}
    for r in results:
        for k, v in r["checks"].items():
            dims.setdefault(k, []).append(v)
    return {
        "total": len(results),
        "passed": sum(r["passed"] for r in results),
        "metrics": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(dims.items())},
        "failed": [r for r in results if not r["passed"]],
        "cases": results,
    }
