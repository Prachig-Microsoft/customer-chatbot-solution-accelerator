"""``run -- <command>``: run a deployment/post-provision command, keep its output, and diagnose if it fails.

The wrapper only starts the command *you* typed (no shell, no extra arguments) and is not exposed through MCP.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .azcli import CommandNotAllowed, _validate_args
from . import telemetry

ALLOWED_EXE = frozenset({"azd", "az", "pwsh", "powershell", "bash", "sh"})
# azd needs a real terminal for its prompts and progress UI, so its output cannot be piped through us.
AZD_INTERACTIVE = {"up", "provision", "deploy", "init", "down", "auth", "env", "pipeline", "monitor"}


def last_log_path() -> Path:
    return telemetry.RUNS_FILE.parent / "last-run.log"


def resolve_command(argv: List[str], repo: Path, allowed=ALLOWED_EXE) -> List[str]:
    """Map the user's command to an argv list. Only known CLIs or scripts inside the accelerator repo."""
    if not argv:
        raise CommandNotAllowed("no command given. Example: run -- azd up")
    head = argv[0]
    base = Path(head).name.lower()
    stem = base.rsplit(".", 1)[0] if base.endswith((".exe", ".cmd", ".bat")) else base
    if stem in allowed:
        exe = shutil.which(head) or shutil.which(head + ".cmd") or shutil.which(head + ".exe")
        if not exe:
            raise CommandNotAllowed(f"{head} not found on PATH")
        resolved = [exe, *argv[1:]]
    elif base.endswith((".ps1", ".sh")):
        script = (repo / head).resolve() if not Path(head).is_absolute() else Path(head).resolve()
        try:
            script.relative_to(repo.resolve())
        except ValueError:
            raise CommandNotAllowed(f"scripts must live inside the accelerator repo ({repo})") from None
        if not script.is_file():
            raise CommandNotAllowed(f"script not found: {script}")
        if base.endswith(".ps1"):
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                raise CommandNotAllowed("PowerShell not found")
            resolved = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *argv[1:]]
        else:
            bash = shutil.which("bash")
            if not bash:
                raise CommandNotAllowed("bash not found")
            resolved = [bash, str(script), *argv[1:]]
    else:
        raise CommandNotAllowed(
            f"'{head}' is not allowed. Use azd/az/pwsh/bash or a .ps1/.sh script inside the accelerator repo."
        )
    if resolved[0].lower().endswith((".cmd", ".bat")):  # cmd.exe shim: reject shell metacharacters
        _validate_args(argv[1:])
    return resolved


def wants_passthrough(argv: List[str]) -> bool:
    """True for interactive azd commands that must keep the real terminal."""
    if not argv or Path(argv[0]).name.lower().rsplit(".", 1)[0] != "azd":
        return False
    return any(a in AZD_INTERACTIVE for a in argv[1:2]) and "--no-prompt" not in argv


def run_command(
    argv: List[str], repo: Path, capture: bool, log_path: Optional[Path] = None, allowed=ALLOWED_EXE
) -> Tuple[int, str]:
    """Run the command. Returns (exit code, captured output; '' when passthrough)."""
    resolved = resolve_command(argv, repo, allowed)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if not capture:
        return subprocess.call(resolved, cwd=str(repo), env=env), ""
    lines: List[str] = []
    proc = subprocess.Popen(
        resolved,
        cwd=str(repo),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.append(line)
    except KeyboardInterrupt:
        proc.terminate()
    proc.wait()
    text = "".join(lines)
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            header = f"# {time.strftime('%Y-%m-%dT%H:%M:%S')} $ {' '.join(argv)} (exit {proc.returncode})\n"
            log_path.write_text(header + text, encoding="utf-8")
        except OSError:
            pass
    return proc.returncode, text


def next_steps(argv: List[str], profile: dict) -> List[str]:
    """What to run after a successful command, derived from the accelerator profile's post-provision scripts."""
    pp = profile.get("post_provision") or {}
    chain = [pp.get("build_images", {}), pp.get("data_and_agents", {})]
    name = Path(argv[0]).name.lower() if argv else ""
    if name.rsplit(".", 1)[0] == "azd" and len(argv) > 1 and argv[1] in ("up", "provision"):
        idx = 0
    else:
        idx = next(
            (i + 1 for i, s in enumerate(chain) if name in {Path(s.get(k, "")).name.lower() for k in ("ps1", "sh")}),
            None,
        )
        if idx is None:
            return []
    if idx >= len(chain) or not chain[idx]:
        return ["Open the web app URL (azd env get-values | findstr WEB_APP_URL) and try a question."]
    ps1 = chain[idx].get("ps1")
    return [f"python -m accel_troubleshooter run -- {ps1}"] if ps1 else []
