"""Diagnostic check registry and shared context."""

from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..azcli import AzRunner, CommandNotAllowed
from ..knowledge import KnowledgeIndex
from ..models import ERROR, SKIP, CheckResult, IssueContext
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
    _rg: Optional[Tuple[str, str]] = None

    # ---- resolved targets (explicit override > azd env > convention > tag > prompt) ----
    def env_key(self, logical: str) -> Optional[str]:
        return self.profile.get("azd_env_keys", {}).get(logical)

    def _static_rg(self) -> Tuple[str, str]:
        """Resource group from the flag, the azd env or the issue text. No Azure calls, no prompting."""
        if self.overrides.get("resource_group"):
            return self.overrides["resource_group"], "--resource-group argument"
        for key in (self.env_key("resource_group"), "RESOURCE_GROUP_NAME"):
            val = self.env.get(key)
            if val:
                return val, f"azd env '{self.env.name}' ({key})"
        for n in self.issue.resource_names:
            if n.lower().startswith("rg-"):
                return n, "issue text"
        return "", "unknown"

    def resolve_resource_group(self, prompt: Optional[Callable[[], str]] = None) -> None:
        """Resolve once: flag, azd env, issue text, 'rg-<env>' if it exists, azd-env-name tag, then prompt."""
        if self._rg is not None:
            return
        name, source = self._static_rg()
        if not name and self.env.name and self.runner.mode != "offline":
            cand = f"rg-{self.env.name}"
            ok, exists, _ = self.az("group", "exists", "--name", cand)
            if ok and exists is True:
                name, source = cand, f"azd naming convention rg-<env> (exists in Azure, env '{self.env.name}')"
            else:
                ok, groups, _ = self.az("group", "list")
                tagged = [
                    g["name"]
                    for g in (groups if ok and isinstance(groups, list) else [])
                    if (g.get("tags") or {}).get("azd-env-name") == self.env.name
                ]
                if len(tagged) == 1:
                    name, source = tagged[0], f"resource group tag azd-env-name={self.env.name}"
        if not name and prompt is not None:
            answer = (prompt() or "").strip()
            if answer:
                name, source = answer, "entered by you"
        self._rg = (name, source)

    @property
    def resource_group(self) -> str:
        return (self._rg or self._static_rg())[0]

    @property
    def resource_group_source(self) -> str:
        return (self._rg or self._static_rg())[1]

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


def parallel_map(ctx: "DiagContext", fn: Callable[[Any], Any], items: List[Any], workers: int = 6) -> List[Any]:
    """Run fn over items on helper threads (results keep input order); their commands count for the calling check."""
    parent = ctx.runner.current_capture()

    def wrapped(item):
        ctx.runner.begin_capture(parent)
        try:
            return fn(item)
        finally:
            ctx.runner.end_capture()

    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(wrapped, items))


def run_check(spec: CheckSpec, ctx: DiagContext) -> CheckResult:
    start = time.time()
    captured = (
        ctx.runner.begin_capture()
    )  # this thread's commands belong to this check, even when checks run in parallel
    try:
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
    finally:
        ctx.runner.end_capture()
    if not res.commands:
        res.commands = list(captured)
    res.duration_ms = int((time.time() - start) * 1000)
    ctx.results[spec.id] = res
    return res
