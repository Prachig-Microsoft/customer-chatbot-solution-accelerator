"""Single primary troubleshooting orchestrator."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import rca, telemetry
from .azcli import AzRunner, OfflineRunner
from .classify import classify, extract, identify_accelerator
from .diagnostics import CHECKS, DiagContext, run_check
from .knowledge import KnowledgeIndex, freshness, load_or_build
from .models import Action, CheckResult, DocPassage, Hypothesis, IssueContext
from .repo import DEFAULT_WORKSPACE, AzdEnv, git_info, load_azd_env, load_env_file, load_profiles, resolve_repo

ALWAYS = ["repo.azd_env", "repo.config_drift", "az.context"]
# Checks that need a resource group (the group is resolved once, up front, so a prompt happens at most once).
NEEDS_RG = {
    "rg.state",
    "deploy.operations",
    "resources.state",
    "activity.failures",
    "ai.deployments",
    "network.public_access",
    "webapp.state",
    "webapp.health",
    "rbac.app_identity",
    "rbac.deployer_data",
}
DEPENDS = {
    "rbac.app_identity": ["webapp.state"],
    "webapp.health": ["webapp.state"],
    "config.compare": ["webapp.state"],
    "deploy.operations": ["rg.state"],
    "resources.state": ["rg.state"],
    "webapp.state": ["rg.state", "resources.state"],
    "ai.deployments": ["rg.state", "resources.state"],
    "network.public_access": ["rg.state", "resources.state"],
    "activity.failures": ["rg.state"],
    "quota.models": ["repo.config_drift"],
    "rbac.deployer_data": ["rg.state", "resources.state"],
    "models.availability": ["repo.config_drift"],
}


@dataclass
class Diagnosis:
    run_id: str
    started_at: str
    mode: str
    issue: IssueContext
    accelerator: Dict[str, Any] = field(default_factory=dict)
    clarification: Optional[str] = None
    checks: List[CheckResult] = field(default_factory=list)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    actions: Dict[str, Action] = field(default_factory=dict)
    docs: List[DocPassage] = field(default_factory=list)
    sources: List[DocPassage] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    targets: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    verification: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "mode": self.mode,
            "issue": self.issue.to_dict(),
            "accelerator": self.accelerator,
            "clarification": self.clarification,
            "targets": self.targets,
            "checks": [c.to_dict() for c in self.checks],
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "actions": {k: a.to_dict() for k, a in self.actions.items()},
            "docs": [d.to_dict(include_text=False) | {"excerpt": d.text[:400]} for d in self.docs],
            "sources": [d.to_dict(include_text=False) for d in self.sources],
            "missing_evidence": self.missing_evidence,
            "limitations": self.limitations,
            "duration_ms": self.duration_ms,
            "verification": self.verification,
        }


class Troubleshooter:
    def __init__(
        self,
        workspace: Optional[Path] = None,
        runner: Optional[AzRunner] = None,
        repo_overrides: Optional[Dict[str, str]] = None,
    ):
        self.workspace = (workspace or DEFAULT_WORKSPACE).resolve()
        self.runner = runner or AzRunner()
        self.profiles = load_profiles()
        self.repo_overrides = repo_overrides or {}
        self._indexes: Dict[str, KnowledgeIndex] = {}

    # ------------------------------------------------------------------ helpers
    def repo_for(self, accel: str) -> Optional[Path]:
        prof = self.profiles[accel]
        return resolve_repo(prof, self.workspace, self.repo_overrides.get(accel))

    def index_for(self, accel: str, rebuild: bool = False) -> Optional[KnowledgeIndex]:
        if accel in self._indexes and not rebuild:
            return self._indexes[accel]
        repo = self.repo_for(accel)
        if repo is None:
            return None
        idx = load_or_build(self.profiles[accel], repo, rebuild=rebuild)
        self._indexes[accel] = idx
        return idx

    def identify(self, text: str, explicit: Optional[str] = None) -> Dict[str, Any]:
        prelim = extract(text)
        return identify_accelerator(text, self.profiles, self.workspace, explicit, prelim.resource_names)

    def plan_checks(self, issue: IssueContext, full: bool, only: Optional[List[str]] = None) -> List[str]:
        if only:
            selected = set(only)
        elif full or not issue.stages:
            selected = set(CHECKS)
        else:
            selected = set(ALWAYS)
            for cid, spec in CHECKS.items():
                if "*" in spec.stages or set(spec.stages) & set(issue.stages):
                    selected.add(cid)
        changed = True
        while changed:
            changed = False
            for cid in list(selected):
                for dep in DEPENDS.get(cid, []):
                    if dep not in selected:
                        selected.add(dep)
                        changed = True
        return sorted(selected, key=lambda c: CHECKS[c].order)

    def run_waves(self, planned: List[str], ctx: DiagContext, workers: int = 6) -> None:
        """Run checks level by level (a check waits for the checks it depends on); checks on one level run in parallel.

        Independent Azure CLI calls dominate the run time, so this cuts a full health check roughly in half.
        """
        deps = {c: [d for d in DEPENDS.get(c, []) if d in planned] for c in planned}
        if "models.availability" in planned and not ctx.ai_region and "quota.models" in planned:
            deps["models.availability"].append("quota.models")  # it falls back to the best region found by quota
        level: Dict[str, int] = {}

        def depth(cid: str, seen: tuple = ()) -> int:
            if cid not in level:
                level[cid] = 0 if not deps[cid] or cid in seen else 1 + max(depth(d, seen + (cid,)) for d in deps[cid])
            return level[cid]

        for cid in planned:
            depth(cid)
        for lvl in sorted(set(level.values())):
            wave = [c for c in planned if level[c] == lvl]
            if len(wave) == 1 or self.runner.mode == "replay":
                for cid in wave:  # replay stays sequential so recorded fixtures behave deterministically
                    run_check(CHECKS[cid], ctx)
                continue
            with ThreadPoolExecutor(max_workers=min(workers, len(wave))) as pool:
                list(pool.map(lambda c: run_check(CHECKS[c], ctx), wave))

    # ------------------------------------------------------------------ main entry
    def diagnose(
        self,
        text: str,
        accelerator: Optional[str] = None,
        env_name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        full: bool = False,
        only_checks: Optional[List[str]] = None,
        log: bool = True,
        env_file: Optional[str] = None,
        prompt_rg: Optional[Callable[[], str]] = None,
    ) -> Diagnosis:
        t0 = time.time()
        diag = Diagnosis(
            run_id=uuid.uuid4().hex[:12],
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            mode=self.runner.mode,
            issue=extract(text),
        )
        ident = self.identify(text, accelerator)
        diag.accelerator = {"identification": ident}
        if not ident["confident"]:
            diag.clarification = ident["question"]
            diag.duration_ms = int((time.time() - t0) * 1000)
            if log:
                telemetry.log_run(diag)
            return diag
        accel = ident["accelerator"]
        profile = self.profiles[accel]
        repo = self.repo_for(accel)
        diag.accelerator.update(
            id=accel,
            name=profile["name"],
            upstream=profile.get("upstream_repo"),
            profile=profile.get("_profile_file"),
            diagnostics_level=profile.get("diagnostics"),
        )
        if repo is None:
            diag.limitations.append(
                f"Repository for '{accel}' not found locally (looked in {profile.get('repo_candidates')}). "
                f"Clone {profile.get('upstream_repo')} next to this workspace or pass --repo. "
                "Documentation and IaC checks were not performed."
            )
            diag.duration_ms = int((time.time() - t0) * 1000)
            if log:
                telemetry.log_run(diag)
            return diag
        diag.accelerator.update(repo_path=str(repo), **{f"git_{k}": v for k, v in git_info(repo).items()})
        if profile.get("diagnostics") != "full":
            diag.limitations.append(profile.get("notes", "Partial diagnostic coverage for this accelerator."))
        idx = self.index_for(accel)
        fr = freshness(idx, repo)
        diag.accelerator["index"] = {
            "passages": len(idx.passages),
            "built_at": idx.meta.get("built_at"),
            "indexed_commit": fr["indexed_commit"],
            "stale_files": fr["stale"],
        }
        known = idx.known_error_codes
        issue = extract(text, known)
        code_cats = {}
        for p in idx.passages:
            if p.doc_type == "troubleshooting-row" and p.category != "summary":
                for c in p.error_codes:
                    for tok in c.replace("(", " ").split():
                        code_cats.setdefault(tok.lower(), p.category)
        classify(issue, code_cats)
        diag.issue = issue

        if env_file == "none":  # evaluation/tests: never read the developer's real .azure folder
            env = AzdEnv()
        else:
            env = load_env_file(Path(env_file)) if env_file else load_azd_env(repo, env_name)
        ctx = DiagContext(
            profile=profile, repo=repo, issue=issue, env=env, runner=self.runner, index=idx, overrides=overrides or {}
        )
        planned = self.plan_checks(issue, full, only_checks)
        if self.runner.mode != "offline" and any(c in NEEDS_RG for c in planned):
            ctx.resolve_resource_group(prompt_rg)
        self.run_waves(planned, ctx)
        diag.checks = [ctx.results[c] for c in planned if c in ctx.results]
        diag.targets = {
            "resource_group": ctx.resource_group or None,
            "resource_group_source": ctx.resource_group_source,
            "ai_region": ctx.ai_region or None,
            "azd_env": env.name or None,
            "subscription": ctx.facts.get("subscription_id"),
        }

        # Retrieval: user text + reported/observed error codes + classified stages.
        codes = list(dict.fromkeys(issue.error_codes + ctx.facts.get("deployment_error_codes", [])))
        if ctx.facts.get("rg_exists") is False:
            codes.append("ResourceGroupNotFound")
        query = " ".join([text, " ".join(codes), " ".join(issue.stages[:2])])
        diag.docs = idx.search(query, k=6, error_codes=codes, doc_types=["troubleshooting-row", "doc-section"])
        diag.sources = idx.search(query, k=3, doc_types=["source"])

        diag.hypotheses, diag.actions = rca.analyse(ctx)
        diag.missing_evidence = rca.missing_evidence(ctx, diag.hypotheses)
        if self.runner.mode == "replay":
            diag.limitations.append("Azure evidence served from recorded fixtures (replay mode), not live Azure.")
        diag.duration_ms = int((time.time() - t0) * 1000)
        self._ctx = ctx
        if log:
            telemetry.log_run(diag)
        return diag

    def verify(
        self,
        diag: Diagnosis,
        action: Action,
        env_name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        env_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Re-run an action's verification checks and compare with the pre-fix results."""
        rg = diag.targets.get("resource_group")
        if rg and not (overrides or {}).get("resource_group"):
            overrides = {**(overrides or {}), "resource_group": rg}  # keep a group the user typed at the prompt
        before = {c.check_id: c.status for c in diag.checks}
        after = self.diagnose(
            diag.issue.raw_text,
            accelerator=diag.accelerator.get("id"),
            env_name=env_name,
            overrides=overrides,
            only_checks=action.verify_checks,
            log=False,
            env_file=env_file,
        )
        rows = []
        for c in after.checks:
            if c.check_id in action.verify_checks:
                rows.append(
                    {"check": c.check_id, "before": before.get(c.check_id), "after": c.status, "summary": c.summary}
                )
        resolved = all(r["after"] in ("pass", "info") for r in rows) if rows else False
        return {
            "action": action.id,
            "checks": rows,
            "resolved": resolved,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


def offline_troubleshooter(workspace: Optional[Path] = None) -> Troubleshooter:
    return Troubleshooter(workspace, OfflineRunner())


def as_dict(obj: Any) -> Any:
    return asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj
