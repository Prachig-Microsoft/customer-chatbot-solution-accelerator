"""Command-line interface: ``python -m accel_troubleshooter <command> ...``"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import actions as actions_mod
from . import runcmd, telemetry
from .azcli import AzRunner, CommandNotAllowed, OfflineRunner, ReplayRunner
from .compact import color_enabled, colorize, enable_windows_vt, render_compact
from .diagnostics import CHECKS
from .engine import Troubleshooter
from .knowledge import coverage, freshness
from .models import APPROVAL_REQUIRED, RESTRICTED
from .report import render_markdown
from .textutil import build_issue, condense_log, read_multiline

EXIT_OK, EXIT_FINDINGS, EXIT_CLARIFY, EXIT_ERROR = 0, 1, 2, 3


def _runner(args) -> AzRunner:
    if getattr(args, "offline", False):
        return OfflineRunner()
    if getattr(args, "replay", None):
        return ReplayRunner.from_file(Path(args.replay))
    return AzRunner(record_to=Path(args.record) if getattr(args, "record", None) else None)


def _ts(args) -> Troubleshooter:
    repos = {}
    for spec in getattr(args, "repo", None) or []:
        k, _, v = spec.partition("=")
        repos[k] = v
    return Troubleshooter(Path(args.workspace) if args.workspace else None, _runner(args), repos)


def _overrides(args) -> Dict[str, Any]:
    o: Dict[str, Any] = {}
    if getattr(args, "resource_group", None):
        o["resource_group"] = args.resource_group
    if getattr(args, "region", None):
        o["region"] = args.region
    if getattr(args, "subscription", None):
        o["subscription"] = args.subscription
    if getattr(args, "all_regions", False):
        o["all_regions"] = True
    for attr, key in (("window", "window"), ("client_id", "client_id"), ("token_env", "token_env")):
        if getattr(args, attr, None):
            o[key] = getattr(args, attr)
    caps = {}
    for spec in getattr(args, "capacity", None) or []:
        k, _, v = spec.partition("=")
        caps[k] = int(v)
    if caps:
        o["capacity_overrides"] = caps
    return o


def _prompt_rg() -> str:
    sys.stderr.write(
        "\nNo resource group found for this azd environment (.azure/<env>/.env has no AZURE_RESOURCE_GROUP).\n"
        "Resource group name (press Enter to skip the Azure resource checks): "
    )
    sys.stderr.flush()
    return sys.stdin.readline().strip()


def _diagnose(ts: Troubleshooter, args, text: str, **kw):
    """Single entry point so every command resolves the resource group the same way (azd env first, else ask)."""
    interactive = sys.stdin.isatty() and not getattr(args, "no_prompt", False)
    kw.setdefault("overrides", _overrides(args))
    return ts.diagnose(
        text,
        accelerator=args.accelerator,
        env_name=args.env,
        env_file=args.env_file,
        prompt_rg=_prompt_rg if interactive else None,
        **kw,
    )


def _emit(args, d: Dict[str, Any], kind: str = "diagnose", extra_md: str = "") -> None:
    """Compact summary on screen (default). The complete report goes to --out, or print it with --format full."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    fmt = getattr(args, "format", "compact")
    full = (extra_md + "\n\n" if extra_md else "") + render_markdown(d, verbose=getattr(args, "verbose", False))
    if fmt == "json":
        text = json.dumps(d, indent=2, default=str)
        full = text
    elif fmt == "full":
        text = full
    else:
        text = render_compact(d, kind, plain=getattr(args, "plain", False))
        if color_enabled(plain=getattr(args, "plain", False), force=getattr(args, "color_flag", None)):
            enable_windows_vt()
            text = colorize(text)
    print(text)
    if args.out:
        Path(args.out).write_text(full, encoding="utf-8")
        print(f"\nFull report saved to {args.out}")


def _finish(ts: Troubleshooter, diag) -> int:
    rec = ts.runner.save_recording() if hasattr(ts.runner, "save_recording") else None
    if rec:
        print(f"Recorded sanitized az outputs to {rec}", file=sys.stderr)
    if diag.clarification:
        return EXIT_CLARIFY
    return EXIT_FINDINGS if any(h.status in ("confirmed", "likely") for h in diag.hypotheses) else EXIT_OK


def _issue_text(args) -> str:
    """Issue from text, '-' (stdin), --log FILE, --from-clipboard, --last, or an interactive multi-line paste."""
    stdin_text = sys.stdin.read() if args.issue == "-" else None
    text = build_issue(
        args.issue,
        args.log,
        stdin_text,
        args.from_clipboard,
        runcmd.last_log_path() if args.last else None,
    )
    if not text and sys.stdin.isatty():
        text = condense_log(
            read_multiline(
                "Describe the problem, or paste the error output.\n"
                "(finish with two empty lines, or Ctrl+Z then Enter on Windows / Ctrl+D elsewhere)"
            )
        )
    return text


# --------------------------------------------------------------------------- commands


def _print_action_result(args, result: Dict[str, Any]) -> None:
    plain = getattr(args, "plain", False)
    ok, bad = ("OK", "FAIL") if plain else ("✅", "❌")
    for r in result["results"]:
        mark = ok if r["exit_code"] == 0 else bad
        cmd = r["command"] if len(r["command"]) <= 130 else r["command"][:127] + "..."
        print(f"  {mark} {cmd}  ({r['duration_ms'] / 1000:.1f}s)")
        if r["exit_code"] != 0 and r["output_tail"]:
            print("      " + r["output_tail"].replace("\n", "\n      ")[:600])


def _apply_and_recheck(ts: Troubleshooter, args, diag, action, overrides: Dict[str, Any]) -> int:
    """Run an approved action, verify it, then repeat the full health check so the result is visible."""
    result = actions_mod.execute(
        action,
        Path(diag.accelerator["repo_path"]),
        approved=True,
        progress=lambda m: print(m, flush=True),
    )
    if not result.get("executed"):
        print("Not executed:", result.get("reason"))
        return EXIT_FINDINGS
    print(f"\nAPPLIED  {action.id}")
    _print_action_result(args, result)
    if not result["success"]:
        print("  Stopped at the first failing step; nothing after it was run.")
        if result.get("guidance"):
            print("\nWHAT TO DO NOW")
            for i, step in enumerate(result["guidance"], 1):
                print(f"  {i}. {step}")
        return EXIT_FINDINGS
    verification = ts.verify(diag, action, env_name=args.env, env_file=args.env_file, overrides=overrides)
    diag.verification = verification
    print("\nVERIFY")
    for row in verification["checks"]:
        print(f"  • {row['check']}: {row['before']} → {row['after']}  {row['summary'][:110]}")
    print("  " + ("✔ fix verified" if verification["resolved"] else "✖ not resolved yet (policy/replication can lag)"))
    print("\nRe-running the full health check ...")
    rg = diag.targets.get("resource_group")
    recheck_args = argparse.Namespace(**{**vars(args), "out": None})
    after = _diagnose(
        ts, recheck_args, "check my deployment health", full=True, overrides={**overrides, "resource_group": rg}
    )
    _emit(recheck_args, after.to_dict())
    permanent = diag.actions.get("add-tag-to-template")
    if permanent:
        print("\n💡 This fix is temporary (this deployment only). Permanent fix for new deployments:")
        print(f"   {permanent.description}")
        print(f"   → {permanent.commands[0]}")
    return EXIT_OK if verification["resolved"] else EXIT_FINDINGS


def _print_proposal(action) -> None:
    print(f"\n🟡 Proposed fix: {action.title}")
    print(f"   {action.description}")
    for c in action.commands:
        print(f"   $ {c if len(c) <= 130 else c[:127] + '...'}")
    print(f"   Risk: {action.risk}")


def _offer_fix(ts: Troubleshooter, args, diag) -> None:
    """After an interactive diagnosis, offer a small fix and apply it only if the user types 'yes'."""
    if not sys.stdin.isatty() or getattr(args, "no_prompt", False) or getattr(args, "format", "compact") == "json":
        return
    action = next(
        (
            a
            for a in diag.actions.values()
            if a.category == APPROVAL_REQUIRED
            and a.id.split("#")[0] in actions_mod.OFFER_INLINE
            and not a.params.get("manual")
        ),
        None,
    )
    if action is None:
        return
    _print_proposal(action)
    sys.stdout.write("\nApply this fix now? Type 'yes' to apply, anything else to skip: ")
    sys.stdout.flush()
    if sys.stdin.readline().strip().lower() not in ("yes", "y"):
        print("Skipped. Nothing was changed.")
        return
    _apply_and_recheck(ts, args, diag, action, _overrides(args))


def cmd_diagnose(args) -> int:
    text = _issue_text(args)
    if not text:
        print(
            "Nothing to diagnose. Pass the problem as text, or use --log FILE, --from-clipboard, --last, or '-' "
            "(stdin).",
            file=sys.stderr,
        )
        return EXIT_ERROR
    ts = _ts(args)
    only = args.only.split(",") if args.only else None
    diag = _diagnose(ts, args, text, full=args.full, only_checks=only)
    _emit(args, diag.to_dict())
    _offer_fix(ts, args, diag)
    return _finish(ts, diag)


def _quota_md(diag) -> str:
    qc = next((c for c in diag.checks if c.check_id == "quota.models"), None)
    if not qc or not qc.evidence.get("regions"):
        return f"## Quota workflow\n{qc.summary if qc else 'quota check not run'}"
    lines = [
        "## Quota workflow",
        "",
        "Required capacity (resolved from IaC / azd env):",
        "",
        "| Model | SKU | Required (K TPM) | Source | azd env var |",
        "|---|---|---|---|---|",
    ]
    for r in qc.evidence["requirements"]:
        lines.append(
            f"| {r['model']} | {r['sku']} | {r['capacity']} | {r['capacity_source']} | {r['capacity_env'] or '-'} |"
        )
    lines += ["", "| Region | Model | Used/Limit | Available | Scope | OK |", "|---|---|---|---|---|---|"]
    for region, v in qc.evidence["regions"].items():
        if "error" in v:
            lines.append(f"| {region} | - | - | - | - | error: {v['error']} |")
            continue
        for m in v["models"]:
            lines.append(
                f"| {region} | {m['model']} | {m['used']}/{m['limit']} | {m['available']} | {m['scope']} | "
                f"{'✅' if m['ok'] else '❌'} |"
            )
    cands = qc.evidence.get("regions_with_sufficient_quota") or []
    lines += ["", f"**Regions with sufficient quota for every model:** {cands or 'none'}"]
    if any(m.get("scope") == "Global" for v in qc.evidence["regions"].values() for m in v.get("models", [])):
        lines.append(
            "> Note: models with scope **Global** draw from one subscription-wide pool — switching region does not "
            "change their availability; reduce capacity or request more quota instead."
        )
    return "\n".join(lines)


def cmd_quota(args) -> int:
    ts = _ts(args)
    o = _overrides(args)
    o.setdefault("all_regions", not args.region)
    text = args.issue or "quota check: is there enough model quota to deploy?"
    diag = _diagnose(
        ts,
        args,
        text,
        overrides=o,
        only_checks=["repo.azd_env", "repo.config_drift", "az.context", "quota.models", "models.availability"],
    )
    _emit(args, diag.to_dict(), "quota", extra_md=_quota_md(diag))
    return _finish(ts, diag)


def cmd_preflight(args) -> int:
    ts = _ts(args)
    if args.format != "json":
        print(
            "Checking login, providers, quota in every allowed region and model availability (about 30 s)...",
            file=sys.stderr,
        )
    o = _overrides(args)
    o.setdefault("all_regions", True)
    diag = _diagnose(
        ts,
        args,
        "pre-deployment preflight check before azd up",
        overrides=o,
        only_checks=[
            "repo.azd_env",
            "repo.config_drift",
            "repo.git_changes",
            "repo.exemption_tag",
            "az.context",
            "providers.registration",
            "quota.models",
            "models.availability",
        ],
    )
    blocking = [c for c in diag.checks if c.status in ("fail", "error")]
    quota = next((c for c in diag.checks if c.check_id == "quota.models"), None)
    checked = quota is not None and quota.status in ("pass", "fail", "warn", "info")
    label = "🛑 NO-GO" if blocking else ("✅ GO" if checked else "❔ NOT CHECKED")
    verdict = "## Preflight verdict: " + label + "\n" + "\n".join(f"- `{c.check_id}`: {c.summary}" for c in blocking)
    _emit(args, diag.to_dict(), "preflight", extra_md=verdict + "\n\n" + _quota_md(diag))
    _finish(ts, diag)
    if blocking:
        return EXIT_FINDINGS
    return EXIT_OK if checked else EXIT_ERROR


def cmd_compare(args) -> int:
    ts = _ts(args)
    diag = _diagnose(
        ts,
        args,
        args.issue or "compare configuration with deployed state",
        only_checks=[
            "repo.azd_env",
            "repo.config_drift",
            "repo.git_changes",
            "webapp.state",
            "config.compare",
            "ai.deployments",
        ],
    )
    _emit(args, diag.to_dict())
    return _finish(ts, diag)


def cmd_health(args) -> int:
    """Full read-only health check of the current azd environment."""
    ts = _ts(args)
    diag = _diagnose(ts, args, "check my deployment health", full=True)
    _emit(args, diag.to_dict())
    _offer_fix(ts, args, diag)
    return _finish(ts, diag)


def cmd_chat_test(args) -> int:
    """Health check plus ONE real signed-in chat message (asks first); the test session is deleted afterwards."""
    ts = _ts(args)
    if not args.yes:
        if not sys.stdin.isatty() or args.no_prompt:
            print("This sends one real chat message as you. Re-run with --yes to approve it.", file=sys.stderr)
            return EXIT_ERROR
        print(
            "This sends ONE real chat message to your chat API using your own sign-in, uses a little model quota,\n"
            "and deletes the test chat session afterwards. Nothing else is changed."
        )
        sys.stdout.write("Type 'yes' to continue: ")
        sys.stdout.flush()
        if sys.stdin.readline().strip().lower() not in ("yes", "y"):
            print("Nothing was sent.")
            return EXIT_FINDINGS
    o = _overrides(args)
    o["chat_test_approved"] = True
    checks = [c for c, s in CHECKS.items() if not s.opt_in] + ["chat.e2e"]
    diag = _diagnose(ts, args, "check my deployment health and chat", overrides=o, only_checks=checks)
    _emit(args, diag.to_dict())
    return _finish(ts, diag)


def cmd_run(args) -> int:
    """Run a deployment/post-provision command, keep its output, and diagnose automatically when it fails."""
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("Usage: python -m accel_troubleshooter run -- <command>   e.g.  run -- azd up", file=sys.stderr)
        return EXIT_ERROR
    ts = _ts(args)
    ident = ts.identify(" ".join(cmd), args.accelerator)
    if not ident["confident"]:
        print(ident["question"])
        return EXIT_CLARIFY
    accel = ident["accelerator"]
    repo = ts.repo_for(accel)
    if repo is None:
        print(f"Repository for {accel} not found locally; pass --repo {accel}=<path>.", file=sys.stderr)
        return EXIT_ERROR
    capture = args.capture or not (args.passthrough or runcmd.wants_passthrough(cmd))
    name = " ".join(cmd)
    note = "output saved for diagnosis" if capture else "azd needs a real terminal, so output is not captured"
    print(f"$ {name}    [{repo}; {note}]", file=sys.stderr)
    try:
        code, out = runcmd.run_command(cmd, repo, capture, runcmd.last_log_path())
    except CommandNotAllowed as e:
        print(f"Not run: {e}", file=sys.stderr)
        return EXIT_ERROR
    args.out = args.out or str(runcmd.last_log_path().with_name("last-report.md"))
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if code == 0:
        print(f"\n✔ `{name}` finished (exit 0).")
        if args.no_check:
            return EXIT_OK
        print("\nQuick health check of what is deployed so far:\n")
        diag = _diagnose(ts, args, f"check my deployment health after `{name}` succeeded", full=True)
        _emit(args, diag.to_dict())
        steps = runcmd.next_steps(cmd, ts.profiles[accel])
        if steps:
            print("\nNEXT STEP (findings about images/agents above are expected until these have run):")
            print("\n".join(f"  $ {s}" for s in steps))
        return EXIT_OK
    print(f"\n✖ `{name}` failed (exit {code}). Diagnosing from its output and from Azure...\n")
    pasted = ""
    if not capture and sys.stdin.isatty():
        pasted = read_multiline(
            "Paste the error text from above if you want it used (optional; press Enter twice to skip):"
        )
    diag = _diagnose(ts, args, f"`{name}` failed with exit code {code}. " + condense_log(out or pasted))
    _emit(args, diag.to_dict())
    return EXIT_FINDINGS


def cmd_search(args) -> int:
    ts = _ts(args)
    accel = args.accelerator or ts.identify(args.query)["accelerator"]
    if not accel:
        print(ts.identify(args.query)["question"])
        return EXIT_CLARIFY
    idx = ts.index_for(accel)
    if idx is None:
        print(f"Repository for {accel} not found.")
        return EXIT_ERROR
    hits = idx.search(args.query, k=args.k, error_codes=[w for w in args.query.split() if w[:1].isupper()])
    if args.format == "json":
        print(json.dumps([h.to_dict() for h in hits], indent=2))
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        for h in hits:
            print(
                f"[{h.score}] {h.ref}  ({h.doc_type}) {h.title} › {h.section}\n    "
                f"{h.text[:300].replace(chr(10), ' ')}\n    repo={h.repo} branch={h.branch} commit={h.commit}\n"
            )
    return EXIT_OK


def cmd_identify(args) -> int:
    ts = _ts(args)
    r = ts.identify(args.issue, args.accelerator)
    print(json.dumps(r, indent=2))
    return EXIT_OK if r["confident"] else EXIT_CLARIFY


def _export_index(ts: Troubleshooter, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Troubleshooting knowledge index",
        "",
        "Generated by `python -m accel_troubleshooter index --export knowledge`. Every entry links to the exact "
        "source file/line in the accelerator repository at the recorded commit. Do not edit by hand.",
        "",
    ]
    for pid, prof in ts.profiles.items():
        repo = ts.repo_for(pid)
        lines.append(f"## {prof['name']} (`{pid}`)")
        if repo is None:
            lines += [f"_Repository not available locally — upstream {prof.get('upstream_repo')} was not indexed._", ""]
            continue
        idx = ts.index_for(pid)
        g = idx.meta.get("git", {})
        cov = coverage(prof, idx, repo)
        lines += [
            f"- Source repo: {g.get('remote') or prof.get('upstream_repo')} · branch `{g.get('branch')}` · "
            f"commit `{g.get('commit')}` · indexed {idx.meta.get('built_at')}",
            f"- Passages: {cov['passages']} · troubleshooting rows: {cov['troubleshooting_rows']} · "
            f"known error codes: {cov['known_error_codes']}",
            f"- Services: {', '.join(prof.get('services', []))}",
            f"- Docs not in profile (coverage gap): {', '.join(cov['unindexed_docs']) or 'none'}",
            f"- Broken local doc links: {len(cov['broken_local_links'])}",
            "",
            "### Documents",
            "",
            "| Category | Document | Path |",
            "|---|---|---|",
        ]
        for kind, paths in prof.get("docs", {}).items():
            for rel in paths:
                title = next((p.title for p in idx.passages if p.path == rel), "(missing)")
                lines.append(f"| {kind} | {title} | `{rel}` |")
        lines += [
            "",
            "### Documented errors",
            "",
            "| Error code(s) | Category | Services | Symptom / description | Commands in solution | Source |",
            "|---|---|---|---|---|---|",
        ]
        for p in idx.passages:
            if p.doc_type != "troubleshooting-row" or p.category == "summary":
                continue
            desc = p.text.split("|")[1].strip() if "|" in p.text else ""
            cmds = "; ".join(f"`{c[:60]}`" for c in p.commands[:3])
            lines.append(
                f"| {' / '.join(p.error_codes)[:90]} | {p.category} | {', '.join(p.services[:3])} | "
                f"{desc[:100]} | {cmds} | `{p.ref}` |"
            )
        lines.append("")
        (out_dir / f"index-{pid}.json").write_text(
            json.dumps({"meta": idx.meta, "passages": [p.to_dict(include_text=False) for p in idx.passages]}, indent=1),
            encoding="utf-8",
        )
    (out_dir / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")


def cmd_index(args) -> int:
    ts = _ts(args)
    if args.export:
        _export_index(ts, Path(args.export))
        print(f"Exported knowledge index to {args.export}/INDEX.md")
        return EXIT_OK
    report = {}
    for pid, prof in ts.profiles.items():
        if args.accelerator and pid != args.accelerator:
            continue
        repo = ts.repo_for(pid)
        if repo is None:
            report[pid] = {
                "status": "repository not found",
                "looked_in": prof.get("repo_candidates"),
                "upstream": prof.get("upstream_repo"),
            }
            continue
        idx = ts.index_for(pid, rebuild=args.rebuild)
        report[pid] = {
            "repo": str(repo),
            "git": idx.meta.get("git"),
            "built_at": idx.meta.get("built_at"),
            "freshness": freshness(idx, repo),
            "coverage": coverage(prof, idx, repo),
            "missing_profile_docs": idx.meta.get("missing_docs"),
        }
    print(json.dumps(report, indent=2, default=str))
    return EXIT_OK


def _docs_lines(cov: Dict[str, Any]) -> List[str]:
    """Human summary of what documentation was read, how the troubleshooting guides parsed, and what was skipped."""
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(cov["docs_by_kind"].items()))
    out = [f"  ▸ Documents read: {cov['docs_read']} ({kinds}); {cov['passages']} searchable passages"]
    if cov["troubleshooting_docs"]:
        for g in cov["troubleshooting_docs"]:
            out.append(f"  ▸ Troubleshooting guide: {g['path']} — {g['entries']} entries, {g['format']}")
    else:
        out.append("  ⚠️  No troubleshooting guide found: errors can only be matched to README sections and other docs")
    rd = cov.get("readme") or {}
    if rd:
        out.append(
            "  ▸ README: "
            + f"{len(rd.get('headings', []))} headings"
            + (f", prerequisites '{rd['prerequisites_section']}'" if rd.get("prerequisites_section") else "")
            + (f", regions {', '.join(rd['regions_mentioned'][:6])}" if rd.get("regions_mentioned") else "")
            + (", has a troubleshooting section" if rd.get("has_troubleshooting_section") else "")
        )
    if cov["docs_skipped"]:
        reasons: Dict[str, int] = {}
        for s in cov["docs_skipped"]:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        out.append("  ▸ Not read: " + "; ".join(f"{n} {r}" for r, n in sorted(reasons.items())))
    if cov["unindexed_docs"]:
        out.append(f"  ▸ Not in the index yet: {len(cov['unindexed_docs'])} (run `index --rebuild`)")
    if cov["broken_local_links"]:
        out.append(f"  ▸ Broken links inside the docs: {len(cov['broken_local_links'])} (see `index` for the list)")
    return out


def cmd_docs(args) -> int:
    """What documentation of the accelerator in this workspace was read, and how."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ts = _ts(args)
    ident = ts.identify("azd up failed", args.accelerator)
    if not ident.get("confident"):
        print(ident.get("question") or "Could not identify the accelerator.", file=sys.stderr)
        return EXIT_CLARIFY
    pid = ident["accelerator"]
    repo = ts.repo_for(pid)
    if repo is None:
        print(f"Repository for '{pid}' not found locally.", file=sys.stderr)
        return EXIT_ERROR
    idx = ts.index_for(pid, rebuild=args.rebuild)
    print(f"DOCUMENTATION · {ts.profiles[pid]['name']} @ {(idx.meta.get('git') or {}).get('commit', '')[:7]}")
    print("\n".join(_docs_lines(coverage(ts.profiles[pid], idx, repo))))
    return EXIT_OK


def cmd_checks(args) -> int:
    for cid, spec in sorted(CHECKS.items(), key=lambda kv: kv[1].order):
        print(f"{cid:28} azure={'y' if spec.needs_azure else 'n'} stages={','.join(spec.stages)}\n    {spec.title}")
    return EXIT_OK


def cmd_fix(args) -> int:
    ts = _ts(args)
    o = _overrides(args)
    diag = _diagnose(ts, args, args.issue, overrides=o, full=args.full)
    action = diag.actions.get(args.action_id)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if action is None:
        print(f"Action '{args.action_id}' is not proposed by the current diagnosis. Proposed: {list(diag.actions)}")
        return EXIT_ERROR
    if args.format == "json":
        print(json.dumps(action.to_dict(), indent=2))
    else:
        _print_proposal(action)
    if action.category == RESTRICTED:
        print("\n🔴 Restricted action: the agent will not execute it. Follow the documented manual steps above.")
        return EXIT_FINDINGS
    executable = (
        action.category == APPROVAL_REQUIRED
        and action.id.split("#")[0] in actions_mod.EXECUTABLE_ACTIONS
        and not action.params.get("manual")
    )
    if not executable:
        print("\n📘 Not agent-executable: review the commands above and run them yourself if appropriate.")
        return EXIT_FINDINGS
    if not args.approve:
        print(f"\n🟡 Approval required. Re-run with --approve to execute: {action.commands}")
        return EXIT_FINDINGS
    if not args.yes:
        answer = input(f"\nType the action id '{action.id}' to confirm execution: ").strip()
        if answer != action.id:
            print("Not confirmed — nothing executed.")
            return EXIT_FINDINGS
    rc = _apply_and_recheck(ts, args, diag, action, o)
    if args.out and diag.verification:
        Path(args.out).write_text(render_markdown(diag.to_dict()), encoding="utf-8")
    return rc


def cmd_eval(args) -> int:
    from .evaluation import run_eval

    result = run_eval(
        Path(args.cases) if args.cases else None, workspace=Path(args.workspace) if args.workspace else None
    )
    print(json.dumps(result if args.verbose else {k: v for k, v in result.items() if k != "cases"}, indent=2))
    return EXIT_OK if result["passed"] == result["total"] else EXIT_FINDINGS


def _minutes_ago(iso: str):
    from datetime import datetime, timezone

    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60
    except (AttributeError, ValueError):
        return None


def cmd_watch(args) -> int:
    """Follow a deployment started in another terminal: one line per change, a full diagnosis at the first failure."""
    ts = _ts(args)
    only = ["rg.state", "deploy.operations", "resources.state"]
    started, last, tick, seen_running = time.time(), "", 0, False
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Watching the deployment (read-only, Ctrl+C to stop). Start `azd up` in the other terminal.")
    try:
        while time.time() - started < args.timeout * 60:
            diag = _diagnose(ts, args, "azd up in progress", only_checks=only)
            if tick == 0:  # keep the group the user typed at the prompt and never prompt again
                args.resource_group = diag.targets.get("resource_group") or getattr(args, "resource_group", None)
                args.no_prompt = True
            tick += 1
            by = {c.check_id: c for c in diag.checks}
            ops, res, rg = by.get("deploy.operations"), by.get("resources.state"), by.get("rg.state")
            clock = time.strftime("%H:%M:%S")
            if ops is not None and ops.status == "fail":
                print(f"[{clock}] ❌ {ops.summary}\n")
                _emit(args, diag.to_dict())
                print("\nazd stops at the first failed step. Fix the cause above, then run `azd up` again.")
                return EXIT_FINDINGS
            if rg is not None and rg.status == "fail":
                line = f"⏳ waiting for azd to create the resource group {diag.targets.get('resource_group')}"
            elif ops is None or ops.status in ("skip", "error"):
                line = f"⚠️  cannot read deployments: {ops.summary if ops else 'no data'}"
            elif ops.status == "warn":
                seen_running = True
                line = f"⏳ {ops.summary} {res.summary if res else ''}".strip()
            else:
                recent = (ops.evidence.get("recent_deployments") or [{}])[0].get("timestamp")
                age = _minutes_ago(recent) if recent else None
                if ops.status == "pass" and (seen_running or (age is not None and age < 5)):
                    print(f"[{clock}] ✅ Deployment finished: {ops.summary}")
                    print(
                        "Next: run the post-provision steps from the deployment guide (build images, data and "
                        "agents), then `python -m accel_troubleshooter health`."
                    )
                    return EXIT_OK
                line = "⏳ waiting for `azd up` to start" + (
                    f" (last deployment finished {age:.0f} min ago)" if age else ""
                )
            shown = re.sub(r"\d+", "#", line)
            if shown != last or tick % 8 == 0:
                print(f"[{clock}] {line}", flush=True)
                last = shown
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")
        return EXIT_OK
    print(f"Stopped after {args.timeout} minutes without a result.")
    return EXIT_FINDINGS


def cmd_draft_guide(args) -> int:
    """Draft one new troubleshooting-guide row from a diagnosed incident (prints it; never edits the guide)."""
    from .guide_draft import draft_entry, render_draft

    text = _issue_text(args)
    if not text:
        print("Nothing to draft from. Pass the problem as text, --log FILE, --from-clipboard or '-'.", file=sys.stderr)
        return EXIT_ERROR
    ts = _ts(args)
    diag = _diagnose(ts, args, text, full=getattr(args, "full", False))
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if diag.clarification:
        print(diag.clarification)
        return EXIT_CLARIFY
    accel = diag.accelerator["id"]
    profile = ts.profiles[accel]
    guide_rel = (profile.get("docs", {}).get("troubleshooting") or [""])[0]
    draft = draft_entry(diag, Path(diag.accelerator["repo_path"]) / guide_rel, args.note or "", args.force, profile)
    print(json.dumps(draft, indent=2) if args.format == "json" else render_draft(draft, guide_rel, args.plain))
    if args.out and draft.get("drafted"):
        Path(args.out).write_text(render_draft(draft, guide_rel, True), encoding="utf-8")
        print(f"\nDraft saved to {args.out}")
    return EXIT_OK if draft.get("drafted") else EXIT_FINDINGS


def cmd_drift(args) -> int:
    """Where the docs and scripts disagree with the Bicep and parameter files (offline; nothing is changed)."""
    from .drift import build_drift_report, render_drift_compact, render_drift_markdown

    ts = _ts(args)
    diag = ts.diagnose(
        "documentation drift",
        accelerator=args.accelerator,
        only_checks=["repo.config_drift"],
        env_file="none",
        log=False,
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if diag.clarification:
        print(diag.clarification)
        return EXIT_CLARIFY
    report = build_drift_report(diag.to_dict())
    if args.format == "json":
        text = json.dumps(report, indent=2)
    elif args.format == "full":
        text = render_drift_markdown(report)
    else:
        text = render_drift_compact(report, plain=args.plain)
    print(text)
    if args.out:
        Path(args.out).write_text(render_drift_markdown(report), encoding="utf-8")
        print(f"\nReport saved to {args.out}")
    return EXIT_FINDINGS if report["findings"] else EXIT_OK


def cmd_about(args) -> int:
    """What is this accelerator? Name, short description from its README, services, docs and repo."""
    from .about import build_about, render_about

    ts = _ts(args)
    ident = ts.identify("", args.accelerator)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not ident["confident"]:
        print(ident["question"])
        return EXIT_CLARIFY
    info = build_about(ts, ident["accelerator"], ts.repo_for(ident["accelerator"]))
    print(json.dumps(info, indent=2) if args.format == "json" else render_about(info, plain=args.plain))
    return EXIT_OK


def cmd_install(args) -> int:
    """Copy the troubleshooter, agent, skills and MCP entry into another accelerator repository."""
    from . import install as install_mod

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not args.target:
        print(
            "install needs the path of the repository to copy the tool INTO, and is run from the repository that\n"
            "has the tool. If this repository already has a troubleshooter/ folder, it is installed: skip this\n"
            "command and run  python -m accel_troubleshooter preflight  (or health).\n\n"
            "To install into another repository, run from the source repository's troubleshooter folder:\n"
            "  python -m accel_troubleshooter install <path-of-other-repo> --dry-run",
            file=sys.stderr,
        )
        return EXIT_CLARIFY
    try:
        res = install_mod.install(Path(args.target), force=args.force, full=args.full, dry_run=args.dry_run)
    except ValueError as e:
        print(f"Cannot install: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(("DRY RUN - nothing was written\n" if args.dry_run else "") + f"INSTALL into {res['target']}")
    for step in res["steps"]:
        print(f"  • {step}")
    if res["manual_mcp"]:
        print("\n  Add this to .vscode/mcp.json:\n" + json.dumps(res["snippet"], indent=2))
    found = install_mod.detect(Path(res["target"]))
    print("\nDETECTED")
    if found.get("confident") and found.get("auto_derived"):
        print(f"  ✅ No shipped profile matches, so one was derived from the repository: {found['accelerator']}")
        print("     Docs search, quota/model checks and generic Azure checks work now. To save and review it:")
        print("     cd <that repo>/troubleshooter; python -m accel_troubleshooter init-profile")
    elif found.get("confident"):
        print(f"  ✅ This repository is recognised as: {found['accelerator']}")
    else:
        print("  ⚠️  This folder does not look like an azd accelerator (no azure.yaml or infra/main.bicep).")
        print("     Create troubleshooter/accel_troubleshooter/profiles/<id>.json (see README 'Extending').")
    print(
        "\nNEXT STEPS\n"
        "  1. cd <that repo>; pip install -r troubleshooter/requirements-mcp.txt   (once, for chat)\n"
        "  2. cd troubleshooter; python -m accel_troubleshooter preflight          (terminal check)\n"
        "  3. In VS Code: Developer: Reload Window, open a new chat, pick 'accelerator-troubleshooter',\n"
        "     send any message once (the MCP server starts with the first message), then ask 'hi'.\n"
        "  4. Commit .github/agents, .github/skills, .vscode/mcp.json and troubleshooter/ (not .runs/ or .cache/)."
    )
    return EXIT_OK


def cmd_init_profile(args) -> int:
    """Save the profile derived from this repository so it can be reviewed, edited and committed."""
    from .autoprofile import derive_profile
    from .repo import PROFILES_DIR

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ts = _ts(args)
    prof = derive_profile(ts.workspace)
    if prof is None:
        print(
            f"No azure.yaml or infra/main.bicep in {ts.workspace}: nothing to derive a profile from.", file=sys.stderr
        )
        return EXIT_ERROR
    for key in ("auto_derived", "_profile_file"):
        prof.pop(key, None)
    prof["notes"] = (
        "Derived from the repository by `init-profile`; review it. Docs search, quota/model checks and the generic "
        "Azure checks apply; app-level checks need an `apps` section."
    )
    out = Path(args.out) if args.out else PROFILES_DIR / f"{prof['id']}.json"
    kinds = {k: len(v) for k, v in prof["docs"].items()}
    models = [f"{m['name_fixed']} {m['sku_fixed']} {m['capacity_fixed']}K" for m in prof["iac"]["models"]]
    print(f"PROFILE · {prof['name']} (id {prof['id']})")
    print(f"  ▸ Docs found: {sum(kinds.values())} ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))})")
    print(f"  ▸ Models from the Bicep usageName metadata: {', '.join(models) or 'none found'}")
    print(f"  ▸ Providers from the Bicep: {', '.join(prof['required_providers']) or 'none found'}")
    from .knowledge import build_index

    print("\n".join(_docs_lines(coverage(prof, build_index(prof, ts.workspace), ts.workspace))))
    todo = []
    if "troubleshooting" not in prof["docs"]:
        todo.append("no troubleshooting guide found: add one to docs/ so errors can be matched to it")
    if not prof["iac"]["models"]:
        todo.append("no model quota found in the Bicep: add `iac.models` if the accelerator deploys OpenAI models")
    todo.append("optional: add `apps` (names, required settings, expected roles) to unlock app-level checks")
    for t in todo:
        print(f"  • Review: {t}")
    if args.print:
        print(json.dumps(prof, indent=2))
        return EXIT_OK
    if out.exists() and not args.force:
        print(f"{out} already exists. Use --force to overwrite or --out <file> to write elsewhere.", file=sys.stderr)
        return EXIT_ERROR
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(prof, indent=2) + "\n", encoding="utf-8")
    print(f"  ▸ Saved {out}. Commit it with the troubleshooter folder.")
    return EXIT_OK


def cmd_stats(args) -> int:
    print(json.dumps(telemetry.stats(), indent=2, default=str))
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="accel_troubleshooter", description="Accelerator-aware Azure troubleshooting agent"
    )
    p.add_argument("--workspace", help="Workspace root (default: repository containing this tool)")
    p.add_argument("--repo", action="append", metavar="ACCEL=PATH", help="Override repository path for an accelerator")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, issue_positional: bool = False, live: bool = True):
        if issue_positional:
            sp.add_argument(
                "issue",
                nargs="?",
                help="Problem in your own words (quote it), or '-' to read stdin. Omit it to be prompted.",
            )
            sp.add_argument("--log", metavar="FILE", help="Read the error from a log/output file (last 200 KB)")
            sp.add_argument("--from-clipboard", action="store_true", help="Use the text currently on the clipboard")
            sp.add_argument("--last", action="store_true", help="Use the output saved by the last 'run' command")
        sp.add_argument("--accelerator", help="Accelerator id (skip auto-identification)")
        sp.add_argument(
            "--format",
            choices=["compact", "full", "json"],
            default="compact",
            help="compact (default, short summary), full (complete Markdown report), json",
        )
        sp.add_argument("--out", help="Save the FULL report to this file (summary is still printed)")
        sp.add_argument("--verbose", action="store_true", help="Include raw check evidence in the full report")
        sp.add_argument("--plain", action="store_true", help="ASCII markers, no emoji and no colour")
        sp.add_argument("--color", dest="color_flag", action="store_true", default=None, help="Force colour on")
        sp.add_argument("--no-color", dest="color_flag", action="store_false", help="Never use colour")
        if live:
            sp.add_argument("--env", help="azd environment name (default: .azure/config.json defaultEnvironment)")
            sp.add_argument("--env-file", help="Read azd-style KEY=VALUE values from this file instead of .azure/")
            sp.add_argument(
                "--resource-group",
                help="Resource group (default: from the azd env in .azure/, else rg-<env>, else you are asked)",
            )
            sp.add_argument("--no-prompt", action="store_true", help="Never ask questions (for scripts/CI)")
            sp.add_argument("--region", help="AI services region (overrides azd env)")
            sp.add_argument("--subscription", help="Expected subscription id")
            sp.add_argument(
                "--window", help="How far back to read the apps' error logs, e.g. PT30M, PT6H, P1D (default PT1H)"
            )
            g = sp.add_mutually_exclusive_group()
            g.add_argument("--offline", action="store_true", help="No Azure calls (docs + repo only)")
            g.add_argument("--replay", help="Serve az outputs from a fixture JSON file")
            sp.add_argument("--record", help="Record sanitized live az outputs to a fixture JSON file")

    d = sub.add_parser("diagnose", help="One-command diagnosis of a natural-language issue")
    common(d, issue_positional=True)
    d.add_argument("--full", action="store_true", help="Run every check regardless of classification")
    d.add_argument("--only", help="Comma-separated check ids")
    d.add_argument("--all-regions", action="store_true", help="Quota: also evaluate all allowed AI regions")
    d.add_argument(
        "--capacity", action="append", metavar="PARAM=N", help="What-if capacity override (Bicep param name)"
    )
    d.set_defaults(func=cmd_diagnose)

    h = sub.add_parser("health", help="Full read-only health check of the current azd environment")
    common(h)
    h.set_defaults(func=cmd_health)

    r = sub.add_parser(
        "run",
        help="Run a command (azd up, post-provision scripts); on failure diagnose it automatically",
        description="Example: run -- azd up    |    run -- infra\\scripts\\post-provision\\build_push_images.ps1",
    )
    common(r)
    r.add_argument("--capture", action="store_true", help="Force output capture (may disable azd prompts)")
    r.add_argument("--passthrough", action="store_true", help="Never capture output (keeps the real terminal)")
    r.add_argument("--no-check", action="store_true", help="Skip the health check after a successful run")
    r.add_argument("cmd", nargs=argparse.REMAINDER, help="-- <command and arguments>")
    r.set_defaults(func=cmd_run)

    q = sub.add_parser("quota", help="End-to-end quota workflow (requirements → live quota → recommendation)")
    common(q)
    q.add_argument("--issue", help="Optional issue text / error message")
    q.add_argument("--capacity", action="append", metavar="PARAM=N", help="What-if capacity override")
    q.add_argument("--all-regions", action="store_true")
    q.set_defaults(func=cmd_quota)

    pf = sub.add_parser("preflight", help="Pre-deployment health check (GO / NO-GO)")
    common(pf)
    pf.add_argument("--capacity", action="append", metavar="PARAM=N")
    pf.set_defaults(func=cmd_preflight)

    c = sub.add_parser("compare", help="Compare IaC/docs/local config with deployed state")
    common(c)
    c.add_argument("--issue")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("search", help="Search the accelerator documentation index")
    s.add_argument("query")
    s.add_argument("--accelerator")
    s.add_argument("-k", type=int, default=5)
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.set_defaults(func=cmd_search)

    i = sub.add_parser("identify", help="Identify the accelerator for an issue")
    i.add_argument("issue")
    i.add_argument("--accelerator")
    i.set_defaults(func=cmd_identify)

    ix = sub.add_parser("index", help="Build index; report freshness and documentation coverage")
    ix.add_argument("--accelerator")
    ix.add_argument("--rebuild", action="store_true")
    ix.add_argument("--export", metavar="DIR", help="Write INDEX.md + per-accelerator JSON catalog to DIR")
    ix.set_defaults(func=cmd_index)

    dc = sub.add_parser("docs", help="Which documents of this accelerator were read, how, and what was skipped")
    dc.add_argument("--accelerator")
    dc.add_argument("--rebuild", action="store_true", help="Re-read every document first")
    dc.set_defaults(func=cmd_docs)

    ck = sub.add_parser("checks", help="List diagnostic checks")
    ck.set_defaults(func=cmd_checks)

    f = sub.add_parser("fix", help="Execute an approved remediation action, then verify")
    f.add_argument("action_id")
    common(f)
    f.add_argument("--issue", required=True, help="Same issue text used for diagnose")
    f.add_argument("--approve", action="store_true", help="Approve executing an approval_required action")
    f.add_argument("--yes", action="store_true", help="Skip interactive confirmation (CI only)")
    f.add_argument("--full", action="store_true")
    f.add_argument("--capacity", action="append", metavar="PARAM=N")
    f.add_argument("--all-regions", action="store_true")
    f.set_defaults(func=cmd_fix)

    e = sub.add_parser("eval", help="Run the regression/evaluation suite (replay mode)")
    e.add_argument("--cases")
    e.add_argument("--verbose", action="store_true")
    e.set_defaults(func=cmd_eval)

    st = sub.add_parser("stats", help="Operational statistics from the run log")
    st.set_defaults(func=cmd_stats)

    dg = sub.add_parser(
        "draft-guide", help="Draft a new troubleshooting-guide row from a diagnosed incident (never edits the guide)"
    )
    common(dg, issue_positional=True)
    dg.add_argument("--note", help="What fixed it, in your own words (added as 'Verified fix')")
    dg.add_argument("--force", action="store_true", help="Draft even if the guide already documents the cause")
    dg.add_argument("--full", action="store_true", help="Run every check regardless of classification")
    dg.set_defaults(func=cmd_draft_guide)

    dr = sub.add_parser("drift", help="Where docs and scripts disagree with the Bicep/parameter files (offline)")
    common(dr, live=False)
    dr.set_defaults(func=cmd_drift)

    ct = sub.add_parser(
        "chat-test", help="Health check plus one real signed-in chat message (asks first; deletes the test session)"
    )
    common(ct)
    ct.add_argument("--yes", action="store_true", help="Approve sending the test message (no question asked)")
    ct.add_argument("--client-id", help="Client id of the API's Entra app registration (default: azd env)")
    ct.add_argument("--token-env", metavar="NAME", help="Read the bearer token from this environment variable")
    ct.set_defaults(func=cmd_chat_test)

    wt = sub.add_parser("watch", help="Follow a deployment started in another terminal; diagnose at the first failure")
    common(wt)
    wt.add_argument("--interval", type=int, default=30, help="Seconds between checks (default 30)")
    wt.add_argument("--timeout", type=int, default=90, help="Stop after this many minutes (default 90)")
    wt.set_defaults(func=cmd_watch)

    ab = sub.add_parser("about", help="What is this accelerator? Short description from its README, services, docs")
    common(ab, live=False)
    ab.set_defaults(func=cmd_about)

    ins = sub.add_parser(
        "install", help="Copy the troubleshooter, agent, skills and MCP entry into another accelerator repository"
    )
    ins.add_argument(
        "target", nargs="?", help="Path of the other accelerator repository (run this from the repo that has the tool)"
    )
    ins.add_argument("--force", action="store_true", help="Overwrite files that already exist in the target")
    ins.add_argument("--full", action="store_true", help="Also copy tests, evals and the demo script")
    ins.add_argument("--dry-run", action="store_true", help="Show what would happen without writing anything")
    ins.set_defaults(func=cmd_install)

    ip = sub.add_parser(
        "init-profile", help="Save the profile derived from this repository (azure.yaml, Bicep, docs) for review"
    )
    ip.add_argument("--out", help="Write the profile here instead of accel_troubleshooter/profiles/<id>.json")
    ip.add_argument("--print", action="store_true", help="Show the profile as JSON without saving it")
    ip.add_argument("--force", action="store_true", help="Overwrite an existing profile file")
    ip.set_defaults(func=cmd_init_profile)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "workspace"):
        args.workspace = None
    return args.func(args)
