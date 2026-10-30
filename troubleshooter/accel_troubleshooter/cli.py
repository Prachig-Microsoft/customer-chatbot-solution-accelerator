"""Command-line interface: ``python -m accel_troubleshooter <command> ...``"""

from __future__ import annotations

import argparse
import json
import sys
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
    result = actions_mod.execute(action, Path(diag.accelerator["repo_path"]), approved=True)
    if not result.get("executed"):
        print("Not executed:", result.get("reason"))
        return EXIT_FINDINGS
    print(f"\nAPPLIED  {action.id}")
    _print_action_result(args, result)
    if not result["success"]:
        print("  Stopped at the first failing command; nothing after it was run. Nothing else was changed.")
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
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "workspace"):
        args.workspace = None
    return args.func(args)
