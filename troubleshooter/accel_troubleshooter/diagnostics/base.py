"""Diagnostic check registry and shared context."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..azcli import AzRunner, CommandNotAllowed
from ..knowledge import KnowledgeIndex
from ..models import ERROR, SKIP, CheckResult, CommandRecord, IssueContext
from ..repo import AzdEnv, find_line


@dataclass
class CheckSpec:
    id: str
    title: str
    fn: Callable[["DiagContext"], CheckResult]
    stages: List[str]
    needs_azure: bool
    needs_apps: bool
    order: int
    description: str = ""


CHECKS: Dict[str, CheckSpec] = {}


def check(
    check_id: str,
    title: str,
    stages: List[str],
    needs_azure: bool = True,
    needs_apps: bool = False,
    order: int = 50,
    description: str = "",
):
    def deco(fn):
        CHECKS[check_id] = CheckSpec(check_id, title, fn, stages, needs_azure, needs_apps, order, description)
        return fn

    return deco


@dataclass
class DiagContext:
    profile: Dict[str, Any]
    repo: Path
    issue: IssueContext
    env: AzdEnv
    runner: AzRunner
    index: Optional[KnowledgeIndex] = None
    overrides: Dict[str, Any] = field(default_factory=dict)
    facts: Dict[str, Any] = field(default_factory=dict)
    results: Dict[str, CheckResult] = field(default_factory=dict)

    # ---- resolved targets (explicit override > azd env > issue text) ----
    def env_key(self, logical: str) -> Optional[str]:
        return self.profile.get("azd_env_keys", {}).get(logical)

    @property
    def resource_group(self) -> str:
        if self.overrides.get("resource_group"):
            return self.overrides["resource_group"]
        val = self.env.get(self.env_key("resource_group"))
        if val:
            return val
        for n in self.issue.resource_names:
            if n.lower().startswith("rg-"):
                return n
        return ""

    @property
    def resource_group_source(self) -> str:
        if self.overrides.get("resource_group"):
            return "--resource-group argument"
        if self.env.get(self.env_key("resource_group")):
            return f"azd env '{self.env.name}'"
        return "issue text" if self.resource_group else "unknown"

    @property
    def ai_region(self) -> str:
        if self.overrides.get("region"):
            return self.overrides["region"]
        val = self.env.get(self.env_key("ai_location"))
        if val:
            return val
        param = self.profile.get("iac", {}).get("ai_location_param")
        if param and self.env.params.get(param):
            return str(self.env.params[param])
        allowed = self.facts.get("allowed_ai_regions") or []
        for r in self.issue.regions:
            if not allowed or r in allowed:
                return r
        return ""

    def doc_ref(self, rel: str, pattern: str) -> str:
        line = find_line(self.repo / rel, pattern)
        return f"{rel}:{line}" if line else rel

    def ts_ref(self, code: str) -> str:
        """Cite the detailed (non-summary) troubleshooting row documenting an error code."""
        if self.index is not None:
            for p in self.index.passages:
                if (
                    p.doc_type == "troubleshooting-row"
                    and p.category != "summary"
                    and any(code.lower() in c.lower() for c in p.error_codes)
                ):
                    return p.ref
        return self.ref("troubleshooting", code)

    def ref(self, kind: str, pattern: str) -> str:
        """Cite the first doc of a profile doc-kind (e.g. 'troubleshooting') containing pattern."""
        paths = self.profile.get("docs", {}).get(kind, [])
        for rel in paths:
            line = find_line(self.repo / rel, pattern)
            if line:
                return f"{rel}:{line}"
        return paths[0] if paths else ""

    def az(self, *args: str, timeout: Optional[int] = None):
        return self.runner.run(list(args), timeout=timeout)

    def rg_known_missing(self) -> bool:
        return self.facts.get("rg_exists") is False


def run_check(spec: CheckSpec, ctx: DiagContext) -> CheckResult:
    start = time.time()
    before = len(ctx.runner.history)
    if spec.needs_apps and not ctx.profile.get("apps"):
        res = CheckResult(
            spec.id,
            spec.title,
            SKIP,
            f"Not implemented for profile '{ctx.profile['id']}' (no app definitions in profile).",
        )
    elif spec.needs_azure and ctx.runner.mode == "offline":
        res = CheckResult(spec.id, spec.title, SKIP, "Offline mode: live Azure checks not executed.")
    else:
        try:
            res = spec.fn(ctx)
        except CommandNotAllowed as e:
            res = CheckResult(spec.id, spec.title, ERROR, f"Blocked by safety policy: {e}")
        except Exception as e:  # noqa: BLE001 - a failing check must never crash the diagnosis
            res = CheckResult(
                spec.id,
                spec.title,
                ERROR,
                f"Check raised {type(e).__name__}: {e}",
                evidence={"traceback": traceback.format_exc(limit=3)},
            )
    new_cmds: List[CommandRecord] = ctx.runner.history[before:]
    if not res.commands:
        res.commands = list(new_cmds)
    res.duration_ms = int((time.time() - start) * 1000)
    ctx.results[spec.id] = res
    return res
