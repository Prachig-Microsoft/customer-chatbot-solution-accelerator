"""Issue parsing, stage/category classification and accelerator identification."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import IssueContext
from .repo import PACKAGE_DIR, fingerprint_score

AZURE_REGIONS = [
    "eastus",
    "eastus2",
    "westus",
    "westus2",
    "westus3",
    "centralus",
    "northcentralus",
    "southcentralus",
    "westcentralus",
    "canadacentral",
    "canadaeast",
    "brazilsouth",
    "northeurope",
    "westeurope",
    "uksouth",
    "ukwest",
    "francecentral",
    "germanywestcentral",
    "switzerlandnorth",
    "swedencentral",
    "norwayeast",
    "polandcentral",
    "italynorth",
    "spaincentral",
    "australiaeast",
    "australiasoutheast",
    "eastasia",
    "southeastasia",
    "japaneast",
    "japanwest",
    "koreacentral",
    "centralindia",
    "southindia",
    "westindia",
    "uaenorth",
    "southafricanorth",
    "qatarcentral",
    "israelcentral",
    "mexicocentral",
]


def _spaced(region: str) -> str:
    s = re.sub(r"(north|south|east|west|central)", r" \1 ", region)
    s = re.sub(r"(\d+)$", r" \1", s)
    return re.sub(r"\s+", " ", s).strip()


_REGION_SPACED = {_spaced(r): r for r in AZURE_REGIONS}

_ERR_SUFFIX = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:Error|Exception|NotFound|Failed|Failure|Exceeded|Invalid|Required|Disabled|Denied|"
    r"Unavailable|Conflict|Quota|Policy|Registration|NotSupported|NotAvailable|Deleted|Deleting|InUse|"
    r"Canceled|Active|Timeout|Deprecated|NotPermitted))\b"
)
_CODE_FIELD = re.compile(r"[\"']?code[\"']?\s*[:=]\s*[\"']?([A-Za-z][A-Za-z0-9_.]{3,})", re.I)
# azd / az CLI output forms: "ERROR: (InsufficientQuota) ...", and a line that starts with "InsufficientQuota: ..."
_PAREN_CODE = re.compile(r"\(([A-Z][A-Za-z]{7,})\)")
_LINE_CODE = re.compile(r"(?m)^\s*(?:ERROR:\s*)?([A-Z][A-Za-z]{7,}):\s")


def _codeish(token: str) -> bool:
    return sum(1 for ch in token if ch.isupper()) >= 2 and not token.startswith(("Azure", "Microsoft", "PowerShell"))


_HTTP = re.compile(
    r"(?:HTTP|status|error|returns?|returning|code|got|response)\D{0,15}\b([45]\d\d)\b|"
    r"\b([45]\d\d)\s*(?:error|errors|response|status|unauthori[sz]ed|forbidden|too many|internal|bad gateway|"
    r"service unavailable|not found|gateway)",
    re.I,
)
_RESOURCE = re.compile(
    r"\b((?:rg|app|api|aif|srch|cosmos|cr|proj|ca|st|kv|log|appi|aisa|func|asp|vnet)-[a-z0-9][a-z0-9-]{2,})\b", re.I
)
_RG_PATH = re.compile(r"/resourceGroups/([A-Za-z0-9._()-]+)", re.I)
_RG_PHRASE = re.compile(r"resource group\s+['\"`]?([A-Za-z0-9._()-]{3,})", re.I)
_MODEL = re.compile(r"\b(gpt-[a-z0-9.\-]+|text-embedding-[a-z0-9\-]+|o[134](?:-mini|-pro)?)\b", re.I)
_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?\b")
_DEPLOYMENT = re.compile(r"deployment(?:\s+name)?\s*[:=]?\s*['\"`]([A-Za-z0-9._-]{3,})['\"`]", re.I)

_TAXONOMY: Optional[Dict[str, Any]] = None


def taxonomy() -> Dict[str, Any]:
    global _TAXONOMY
    if _TAXONOMY is None:
        _TAXONOMY = json.loads((PACKAGE_DIR / "taxonomy.json").read_text(encoding="utf-8"))
    return _TAXONOMY


def _uniq(items: Iterable[Any]) -> List[Any]:
    seen, out = set(), []
    for i in items:
        key = i.lower() if isinstance(i, str) else i
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


def extract(text: str, known_error_codes: Iterable[str] = ()) -> IssueContext:
    ctx = IssueContext(raw_text=text)
    codes = []
    for code in known_error_codes:
        if re.search(rf"\b{re.escape(code)}\b", text, re.I):
            codes.append(code)
    codes += _ERR_SUFFIX.findall(text)
    codes += [c for c in _CODE_FIELD.findall(text) if not c.isdigit()]
    codes += [c for c in _PAREN_CODE.findall(text) + _LINE_CODE.findall(text) if _codeish(c)]
    ctx.error_codes = _uniq(codes)
    ctx.http_statuses = _uniq(int(a or b) for a, b in _HTTP.findall(text))
    low = text.lower()
    regions = [r for r in AZURE_REGIONS if re.search(rf"\b{r}\b", low)]
    regions += [
        r for spaced, r in _REGION_SPACED.items() if re.search(r"\b" + spaced.replace(" ", r"\s*") + r"\b", low)
    ]
    ctx.regions = _uniq(regions)
    names = _RESOURCE.findall(text) + _RG_PATH.findall(text) + _RG_PHRASE.findall(text)
    ctx.resource_names = _uniq(n for n in names if n.lower() not in ("is", "was", "that", "does", "not"))
    ctx.models = _uniq(m.lower() for m in _MODEL.findall(text))
    ctx.timestamps = _uniq(_TS.findall(text))
    ctx.deployment_names = _uniq(_DEPLOYMENT.findall(text))
    return ctx


def classify(ctx: IssueContext, error_code_categories: Optional[Dict[str, str]] = None) -> IssueContext:
    tax = taxonomy()
    text = ctx.raw_text
    stage_scores: Dict[str, float] = {}
    reasons: List[str] = []
    for stage, spec in tax["stages"].items():
        w = spec.get("weight", 1)
        for pat in spec["patterns"]:
            if re.search(pat, text, re.I):
                stage_scores[stage] = stage_scores.get(stage, 0) + w
                reasons.append(f"stage '{stage}' matched pattern /{pat}/")
    cat_scores: Dict[str, float] = {}
    for cat, spec in tax["categories"].items():
        for pat in spec["patterns"]:
            if re.search(pat, text, re.I):
                cat_scores[cat] = cat_scores.get(cat, 0) + 1
    for status in ctx.http_statuses:
        if status == 429:
            stage_scores["throttling"] = stage_scores.get("throttling", 0) + 3
            reasons.append("HTTP 429 indicates throttling")
        elif status in (401, 403):
            stage_scores["authentication"] = stage_scores.get("authentication", 0) + 3
            reasons.append(f"HTTP {status} indicates an authentication/authorization failure")
        elif status >= 500:
            stage_scores["runtime"] = stage_scores.get("runtime", 0) + 2
            reasons.append(f"HTTP {status} indicates a server-side runtime failure")
    for code in ctx.error_codes:
        cat = (error_code_categories or {}).get(code.lower())
        if cat:
            stage_scores["deployment"] = stage_scores.get("deployment", 0) + 2
            cat_scores[cat] = cat_scores.get(cat, 0) + 3
            reasons.append(f"error code {code} is documented in the troubleshooting guide under '{cat}'")
    ctx.stages = [s for s, _ in sorted(stage_scores.items(), key=lambda kv: -kv[1])]
    ctx.categories = [c for c, _ in sorted(cat_scores.items(), key=lambda kv: -kv[1])]
    ctx.classification_reasons = reasons
    return ctx


def identify_accelerator(
    text: str,
    profiles: Dict[str, Dict[str, Any]],
    workspace: Path,
    explicit: Optional[str] = None,
    resource_names: Iterable[str] = (),
) -> Dict[str, Any]:
    """Return {accelerator, confident, scores, reasons, question}. Never silently picks on ambiguity."""
    if explicit:
        if explicit in profiles:
            return {
                "accelerator": explicit,
                "confident": True,
                "scores": {explicit: 100},
                "reasons": {explicit: ["explicitly selected by user"]},
                "question": None,
            }
        return {
            "accelerator": None,
            "confident": False,
            "scores": {},
            "reasons": {},
            "question": f"Unknown accelerator '{explicit}'. Choose one of: {', '.join(profiles)}",
        }
    scores: Dict[str, float] = {}
    reasons: Dict[str, List[str]] = {}
    low = text.lower()
    for pid, prof in profiles.items():
        s, r = 0.0, []
        for alias in prof.get("aliases", []):
            if re.search(rf"(?<![a-z]){re.escape(alias.lower())}(?![a-z])", low):
                s += 5
                r.append(f"issue text mentions '{alias}'")
                break
        for name in resource_names:
            if any(re.search(p, name, re.I) for p in prof.get("resource_name_patterns", [])):
                s += 2
                r.append(f"resource name '{name}' matches naming convention")
                break
        fp, fp_reasons = fingerprint_score(prof, workspace)
        if fp >= 3:
            s += 3
            r.append("current workspace is this accelerator (" + "; ".join(fp_reasons) + ")")
        scores[pid], reasons[pid] = s, r
    ranked: List[Tuple[str, float]] = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else 0
    if top_s < 3 or top_s - second_s < 2:
        options = ", ".join(f"{p} ({profiles[p]['name']})" for p, _ in ranked)
        return {
            "accelerator": None,
            "confident": False,
            "scores": scores,
            "reasons": reasons,
            "question": f"Which accelerator is affected? I could not identify it confidently. Options: {options}. "
            "Re-run with --accelerator <id>.",
        }
    return {"accelerator": top, "confident": True, "scores": scores, "reasons": reasons, "question": None}
