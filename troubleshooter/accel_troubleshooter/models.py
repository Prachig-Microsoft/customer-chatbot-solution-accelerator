"""Shared data structures. Everything is JSON-serialisable via ``to_dict``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Check statuses
PASS, FAIL, WARN, SKIP, ERROR, INFO = "pass", "fail", "warn", "skip", "error", "info"

# Hypothesis statuses
CONFIRMED, LIKELY, POSSIBLE, RULED_OUT = "confirmed", "likely", "possible", "ruled_out"

# Action categories
READ_ONLY = "read_only"
APPROVAL_REQUIRED = "approval_required"
RESTRICTED = "restricted"
MANUAL = "manual_review"  # documented steps shown for a human to review/run; never executed by the agent


@dataclass
class CommandRecord:
    command: str
    exit_code: Optional[int]
    timestamp: str
    duration_ms: int
    output_excerpt: str = ""
    source: str = "live"  # live | replay | skipped

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IssueContext:
    raw_text: str
    error_codes: List[str] = field(default_factory=list)
    http_statuses: List[int] = field(default_factory=list)
    regions: List[str] = field(default_factory=list)
    resource_names: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    timestamps: List[str] = field(default_factory=list)
    deployment_names: List[str] = field(default_factory=list)
    stages: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    classification_reasons: List[str] = field(default_factory=list)

    @property
    def primary_stage(self) -> str:
        return self.stages[0] if self.stages else "unknown"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["primary_stage"] = self.primary_stage
        return d


@dataclass
class DocPassage:
    id: str
    accelerator: str
    title: str
    path: str
    section: str
    line_start: int
    line_end: int
    repo: str
    commit: str
    branch: str
    doc_type: str  # troubleshooting-row | doc-section | source
    category: str = ""
    error_codes: List[str] = field(default_factory=list)
    services: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    text: str = ""
    score: float = 0.0

    @property
    def ref(self) -> str:
        return f"{self.path}:{self.line_start}-{self.line_end}"

    def to_dict(self, include_text: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        d["ref"] = self.ref
        if not include_text:
            d.pop("text", None)
        return d


@dataclass
class CheckResult:
    check_id: str
    title: str
    status: str
    summary: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    commands: List[CommandRecord] = field(default_factory=list)
    duration_ms: int = 0
    doc_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["commands"] = [c.to_dict() if isinstance(c, CommandRecord) else c for c in self.commands]
        return d


@dataclass
class Hypothesis:
    id: str
    title: str
    status: str
    confidence: str  # high | medium | low
    rationale: List[str]
    supporting_checks: List[str] = field(default_factory=list)
    contradicting: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    doc_refs: List[str] = field(default_factory=list)
    source_refs: List[str] = field(default_factory=list)
    remediation_ids: List[str] = field(default_factory=list)
    component: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Action:
    id: str
    title: str
    category: str
    description: str
    commands: List[str] = field(default_factory=list)
    risk: str = ""
    prerequisites: List[str] = field(default_factory=list)
    verify_checks: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    doc_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
