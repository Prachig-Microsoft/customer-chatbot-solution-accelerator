"""Traceable documentation/source knowledge index with lexical (BM25) retrieval.

Every passage keeps accelerator, repo, branch, commit, file path and line range so the
agent can cite exactly where a recommendation came from. No embeddings / vector DB.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import DocPassage
from .repo import TOOL_ROOT, git_info

CACHE_DIR = TOOL_ROOT / ".cache"
INDEX_VERSION = 2

SERVICE_KEYWORDS = {
    "Azure OpenAI": ["openai", "gpt", "embedding", "tpm", "model deployment", "deploymentmodelnotsupported"],
    "Azure AI Foundry": ["foundry", "ai project", "agent", "aiservices", "cognitive services"],
    "Azure AI Search": ["ai search", "search service", "search index", "srch"],
    "Azure Cosmos DB": ["cosmos", "databaseaccount"],
    "Azure App Service": ["app service", "webapp", "web app", "azurewebsites"],
    "Azure Container Apps": ["container app", "managedenvironment", "containerapp"],
    "Azure Container Registry": ["container registry", "acr"],
    "Azure Storage": ["storage account", "blob"],
    "Log Analytics": ["log analytics", "workspace"],
    "Application Insights": ["application insights", "app insights"],
    "Key Vault": ["key vault", "vaultname"],
    "Virtual Network": ["vnet", "subnet", "private endpoint", "bastion", "private dns"],
    "Microsoft Entra ID": ["entra", "app registration", "tenant", "principal"],
    "Azure Resource Manager": ["deployment", "resource group", "arm", "bicep"],
    "Azure Database for PostgreSQL": ["postgres"],
    "Azure AI Content Understanding": ["content understanding"],
}

STOPWORDS = set("""a an the and or of to in on for with is are be by this that it as at from your you
if not can will use using into when then than was were has have had do does did i my we our me
""".split())

_CODE_SPAN = re.compile(r"`([^`]+)`")
_ERROR_CODE = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*){1,})\b")


def tokenize(text: str) -> List[str]:
    text = text or ""
    tokens: List[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", text):
        raw = raw.strip(".-_")
        if not raw:
            continue
        low = raw.lower()
        if low not in STOPWORDS and len(low) > 1:
            tokens.append(low)
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", raw)
        if len(parts) > 1:
            tokens.extend(p.lower() for p in parts if p.lower() not in STOPWORDS and len(p) > 1)
        if "-" in raw or "." in raw:
            tokens.extend(p for p in re.split(r"[-.]", low) if p and p not in STOPWORDS and len(p) > 1)
    return tokens


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(ul|ol)>", "\n", text, flags=re.I)
    text = re.sub(r"<li>", "\n- ", text, flags=re.I)
    text = re.sub(r"</li>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _services_for(text: str) -> List[str]:
    low = text.lower()
    return [svc for svc, kws in SERVICE_KEYWORDS.items() if any(k in low for k in kws)]


def _commands(text: str) -> List[str]:
    return [c.strip() for c in _CODE_SPAN.findall(text) if re.match(r"^(az|azd|Get-Az|bash|\./|python)\b", c.strip())]


def _category_from_heading(heading: str) -> str:
    h = re.sub(r"[^a-z& ]", "", heading.lower()).strip()
    return h.replace("&", "and").replace("  ", " ").replace(" ", "-") or "general"


def _split_codes(cell: str) -> List[str]:
    cell = re.sub(r"\*\*", "", cell)
    codes = []
    for part in re.split(r"<br\s*/?>|/", cell):
        part = part.strip().strip("-").strip()
        if part:
            codes.append(part)
    return codes


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- parsing


def parse_markdown(path: Path, rel: str, accel: str, repo_meta: Dict[str, str], doc_kind: str) -> List[DocPassage]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    title = next((ln.lstrip("# ").strip() for ln in lines if ln.startswith("#")), path.stem)
    passages: List[DocPassage] = []
    heading = title
    sec_start, sec_lines = 1, []
    in_code = False

    def flush(end_line: int):
        body = "\n".join(sec_lines).strip()
        if body and len(body) > 30:
            for i in range(0, len(sec_lines), 60):
                chunk = sec_lines[i : i + 60]
                text = "\n".join(chunk).strip()
                if not text:
                    continue
                s = sec_start + i
                passages.append(
                    DocPassage(
                        id=f"{accel}:{rel}:{s}",
                        accelerator=accel,
                        title=title,
                        path=rel,
                        section=heading,
                        line_start=s,
                        line_end=min(s + len(chunk) - 1, end_line),
                        repo=repo_meta.get("remote", ""),
                        commit=repo_meta.get("commit", ""),
                        branch=repo_meta.get("branch", ""),
                        doc_type="doc-section",
                        category=doc_kind,
                        services=_services_for(heading + " " + text),
                        commands=_commands(text),
                        error_codes=sorted(set(c for c in _ERROR_CODE.findall(text) if _looks_like_error_code(c))),
                        text=_strip_html(text)[:4000],
                    )
                )

    for idx, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_code = not in_code
        is_row = doc_kind == "troubleshooting" and line.startswith("| **") and not in_code
        if not in_code and re.match(r"^#{1,4}\s", line):
            flush(idx - 1)
            heading = line.lstrip("#").strip()
            sec_start, sec_lines = idx, [line]
            continue
        if is_row:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3:
                codes = _split_codes(cells[0])
                text = _strip_html(" | ".join(cells))
                passages.append(
                    DocPassage(
                        id=f"{accel}:{rel}:{idx}",
                        accelerator=accel,
                        title=" / ".join(codes),
                        path=rel,
                        section=re.sub(r"[^\w &-]", "", heading).strip(),
                        line_start=idx,
                        line_end=idx,
                        repo=repo_meta.get("remote", ""),
                        commit=repo_meta.get("commit", ""),
                        branch=repo_meta.get("branch", ""),
                        doc_type="troubleshooting-row",
                        category="summary" if "frequent" in heading.lower() else _category_from_heading(heading),
                        error_codes=codes,
                        services=_services_for(line),
                        commands=_commands(line),
                        text=text[:6000],
                    )
                )
                continue
        sec_lines.append(line)
    flush(len(lines))
    return passages


def _looks_like_error_code(token: str) -> bool:
    return (
        len(token) >= 8
        and sum(1 for ch in token if ch.isupper()) >= 2
        and not token.startswith(
            ("Azure", "Microsoft", "PowerShell", "GitHub", "JavaScript", "TypeScript", "FastAPI", "YouTube")
        )
    )


def parse_source(path: Path, rel: str, accel: str, repo_meta: Dict[str, str]) -> List[DocPassage]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    for i in range(0, len(lines), 80):
        chunk = lines[i : i + 80]
        text = "\n".join(chunk)
        if not text.strip():
            continue
        out.append(
            DocPassage(
                id=f"{accel}:{rel}:{i + 1}",
                accelerator=accel,
                title=path.name,
                path=rel,
                section=f"lines {i + 1}-{i + len(chunk)}",
                line_start=i + 1,
                line_end=i + len(chunk),
                repo=repo_meta.get("remote", ""),
                commit=repo_meta.get("commit", ""),
                branch=repo_meta.get("branch", ""),
                doc_type="source",
                category="source",
                services=_services_for(text),
                text=text[:4000],
            )
        )
    return out


# --------------------------------------------------------------------------- index


class KnowledgeIndex:
    def __init__(self, accelerator: str, passages: List[DocPassage], meta: Dict[str, Any]):
        self.accelerator = accelerator
        self.passages = passages
        self.meta = meta
        self._tf = [Counter(tokenize(f"{p.title} {p.title} {p.section} {p.text}")) for p in passages]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(passages) or 1
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @property
    def known_error_codes(self) -> List[str]:
        codes = set()
        for p in self.passages:
            if p.doc_type == "troubleshooting-row":
                for c in p.error_codes:
                    for tok in re.split(r"[\s(]", c):
                        if _looks_like_error_code(tok):
                            codes.add(tok)
        return sorted(codes)

    def search(
        self, query: str, k: int = 5, error_codes: Optional[List[str]] = None, doc_types: Optional[List[str]] = None
    ) -> List[DocPassage]:
        q = tokenize(query)
        codes_low = [c.lower() for c in (error_codes or [])]
        scored: List[Tuple[float, int]] = []
        k1, b = 1.4, 0.75
        for i, p in enumerate(self.passages):
            if doc_types and p.doc_type not in doc_types:
                continue
            tf = self._tf[i]
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    score += self._idf.get(t, 0) * (f * (k1 + 1)) / (f + k1 * (1 - b + b * self._len[i] / self._avg))
            if codes_low:
                pcodes = " ".join(p.error_codes).lower()
                hits = sum(1 for c in codes_low if c in pcodes)
                if hits:
                    score += 25.0 * hits + (10 if p.doc_type == "troubleshooting-row" else 0)
            if p.doc_type == "source":
                score *= 0.6
            elif p.category == "summary":
                score *= 0.5
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        results = []
        for score, i in scored[:k]:
            p = self.passages[i]
            copy = DocPassage(**{**p.__dict__})
            copy.score = round(score, 2)
            results.append(copy)
        return results

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": INDEX_VERSION,
            "accelerator": self.accelerator,
            "meta": self.meta,
            "passages": [p.to_dict() for p in self.passages],
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "KnowledgeIndex":
        passages = []
        for d in data["passages"]:
            d = dict(d)
            d.pop("ref", None)
            passages.append(DocPassage(**d))
        return cls(data["accelerator"], passages, data["meta"])


def _doc_files(profile: Dict[str, Any]) -> List[Tuple[str, str]]:
    out = []
    for kind, paths in profile.get("docs", {}).items():
        for p in paths:
            out.append((kind, p))
    return out


def build_index(profile: Dict[str, Any], repo: Path) -> KnowledgeIndex:
    accel = profile["id"]
    meta_git = git_info(repo)
    passages: List[DocPassage] = []
    files: Dict[str, str] = {}
    missing: List[Dict[str, str]] = []
    for kind, rel in _doc_files(profile):
        path = repo / rel
        if not path.exists():
            missing.append({"kind": kind, "path": rel})
            continue
        files[rel] = _sha1(path)
        passages.extend(parse_markdown(path, rel, accel, meta_git, kind))
    seen = set(files)
    for pattern in profile.get("source_globs", []):
        for path in sorted(repo.glob(pattern)):
            rel = path.relative_to(repo).as_posix()
            if rel in seen or not path.is_file() or path.stat().st_size > 400_000:
                continue
            seen.add(rel)
            files[rel] = _sha1(path)
            passages.extend(parse_source(path, rel, accel, meta_git))
    meta = {
        "repo_path": str(repo),
        "git": meta_git,
        "files": files,
        "missing_docs": missing,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile_file": profile.get("_profile_file"),
    }
    return KnowledgeIndex(accel, passages, meta)


def cache_path(accel: str) -> Path:
    return CACHE_DIR / f"index-{accel}.json"


def load_or_build(profile: Dict[str, Any], repo: Path, rebuild: bool = False) -> KnowledgeIndex:
    path = cache_path(profile["id"])
    if not rebuild and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == INDEX_VERSION and data["meta"].get("repo_path") == str(repo):
                idx = KnowledgeIndex.from_json(data)
                if not freshness(idx, repo)["stale"]:
                    return idx
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    idx = build_index(profile, repo)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(idx.to_json()), encoding="utf-8")
    except OSError:
        pass
    return idx


def freshness(idx: KnowledgeIndex, repo: Path) -> Dict[str, Any]:
    stale, removed = [], []
    for rel, sha in idx.meta.get("files", {}).items():
        p = repo / rel
        if not p.exists():
            removed.append(rel)
        elif _sha1(p) != sha:
            stale.append(rel)
    current = git_info(repo)
    return {
        "stale": stale + removed,
        "changed_files": stale,
        "removed_files": removed,
        "indexed_commit": idx.meta.get("git", {}).get("commit"),
        "current_commit": current.get("commit"),
        "built_at": idx.meta.get("built_at"),
    }


def coverage(profile: Dict[str, Any], idx: KnowledgeIndex, repo: Path) -> Dict[str, Any]:
    """Documentation coverage: profile categories present, unindexed docs, broken local links."""
    kinds = {}
    for kind, rel in _doc_files(profile):
        kinds.setdefault(kind, {"present": [], "missing": []})
        kinds[kind]["present" if (repo / rel).exists() else "missing"].append(rel)
    indexed = set(idx.meta.get("files", {}))
    unindexed = []
    for d in ("documents", "docs"):
        if (repo / d).exists():
            unindexed += [
                p.relative_to(repo).as_posix()
                for p in (repo / d).glob("*.md")
                if p.relative_to(repo).as_posix() not in indexed
            ]
    broken = []
    for rel in indexed:
        if not rel.endswith(".md"):
            continue
        src = repo / rel
        for n, line in enumerate(src.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            for target in re.findall(r"\]\((?!https?:|#|mailto:)([^)\s#]+)", line):
                if not (src.parent / target).exists():
                    broken.append({"file": rel, "line": n, "target": target})
    ts_rows = [p for p in idx.passages if p.doc_type == "troubleshooting-row"]
    return {
        "categories": kinds,
        "unindexed_docs": sorted(unindexed),
        "broken_local_links": broken,
        "troubleshooting_rows": len(ts_rows),
        "known_error_codes": len(idx.known_error_codes),
        "passages": len(idx.passages),
    }
