"""Operational observability: append-only JSONL run log + summary statistics."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

from .repo import TOOL_ROOT

RUNS_FILE = Path(os.environ.get("ACCEL_TS_RUNS_FILE", TOOL_ROOT / ".runs" / "runs.jsonl"))


def log_run(diag: Any) -> None:
    rec = {
        "run_id": diag.run_id,
        "started_at": diag.started_at,
        "mode": diag.mode,
        "duration_ms": diag.duration_ms,
        "accelerator": diag.accelerator.get("id"),
        "clarification_needed": bool(diag.clarification),
        "stages": diag.issue.stages[:3],
        "error_codes": diag.issue.error_codes[:5],
        "checks": [
            {"id": c.check_id, "status": c.status, "ms": c.duration_ms, "commands": len(c.commands)}
            for c in diag.checks
        ],
        "top_hypothesis": (diag.hypotheses[0].id if diag.hypotheses else None),
        "top_status": (diag.hypotheses[0].status if diag.hypotheses else None),
        "doc_refs": [d.ref for d in diag.docs[:3]],
    }
    try:
        RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RUNS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def load_runs() -> List[Dict[str, Any]]:
    if not RUNS_FILE.exists():
        return []
    out = []
    for line in RUNS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def stats() -> Dict[str, Any]:
    runs = load_runs()
    check_ms: Dict[str, List[int]] = defaultdict(list)
    check_status: Dict[str, Counter] = defaultdict(Counter)
    for r in runs:
        for c in r.get("checks", []):
            check_ms[c["id"]].append(c["ms"])
            check_status[c["id"]][c["status"]] += 1
    return {
        "runs": len(runs),
        "by_accelerator": Counter(r.get("accelerator") for r in runs),
        "by_mode": Counter(r.get("mode") for r in runs),
        "clarifications": sum(1 for r in runs if r.get("clarification_needed")),
        "top_hypotheses": Counter(r.get("top_hypothesis") for r in runs if r.get("top_hypothesis")).most_common(10),
        "avg_duration_ms": int(sum(r.get("duration_ms", 0) for r in runs) / len(runs)) if runs else 0,
        "checks": {
            cid: {
                "runs": len(ms),
                "avg_ms": int(sum(ms) / len(ms)),
                "p_max_ms": max(ms),
                "status": dict(check_status[cid]),
            }
            for cid, ms in sorted(check_ms.items())
        },
    }
