"""Turning pasted/captured terminal output into a compact issue description."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_NOISE = re.compile(r"^\s*(Update available:|To update, run)", re.I)
_SIGNAL = re.compile(
    r"(error|exception|failed|failure|fatal|denied|unauthori[sz]ed|forbidden|not found|quota|exceeded|invalid|"
    r"\bcode\b\s*[:=]|\bmessage\b\s*[:=]|deployment .*(failed|canceled)|status code|traceback|exit code|"
    r"InsufficientQuota|[A-Z][a-z]+[A-Z]\w{5,})",
    re.I,
)
MAX_SIGNAL_LINES = 40
MAX_CHARS = 6000


def clean_terminal_text(text: str) -> str:
    """Strip ANSI colours, carriage-return progress redraws and azd/az banner noise."""
    text = _ANSI.sub("", text or "").replace("\r\n", "\n")
    text = "\n".join(seg.split("\r")[-1] for seg in text.split("\n"))
    return "\n".join(ln.rstrip() for ln in text.split("\n") if not _NOISE.match(ln)).strip()


def condense_log(text: str, max_lines: int = MAX_SIGNAL_LINES) -> str:
    """Keep what matters from a long log: the error lines with one line of context, plus the last lines."""
    text = clean_terminal_text(text)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(text) <= MAX_CHARS and len(lines) <= max_lines:
        return text
    keep = set()
    for i, ln in enumerate(lines):
        if _SIGNAL.search(ln):
            keep.update({i - 1, i, i + 1})
    keep.update(range(max(0, len(lines) - 8), len(lines)))
    picked = [lines[i][:400] for i in sorted(k for k in keep if 0 <= k < len(lines))]
    if len(picked) > max_lines:
        picked = picked[: max_lines // 2] + ["..."] + picked[-(max_lines // 2) :]
    return "\n".join(picked)[:MAX_CHARS]


def read_multiline(prompt: str = "", reader: Optional[Callable[[], str]] = None, out=None) -> str:
    """Read lines until two consecutive empty lines (or EOF). Lets users paste multi-line azd errors."""
    out = out or sys.stderr
    if prompt:
        out.write(prompt + "\n")
        out.flush()
    read = reader or sys.stdin.readline
    lines: List[str] = []
    blank = 0
    while True:
        line = read()
        if line == "":  # EOF (Ctrl-Z/Ctrl-D)
            break
        line = line.rstrip("\n")
        if not line.strip():
            blank += 1
            if blank >= 2 or (blank == 1 and not lines):
                break
        else:
            blank = 0
        lines.append(line)
    return "\n".join(lines).strip()


def read_clipboard() -> str:
    """Clipboard text via the OS tool (PowerShell Get-Clipboard / pbpaste / xclip / wl-paste). '' if unavailable."""
    candidates = []
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell:
            candidates.append([shell, "-NoProfile", "-Command", "Get-Clipboard -Raw"])
    elif sys.platform == "darwin":
        candidates.append(["pbpaste"])
    else:
        for tool, extra in (("wl-paste", []), ("xclip", ["-selection", "clipboard", "-o"]), ("xsel", ["-b"])):
            if shutil.which(tool):
                candidates.append([tool, *extra])
    for cmd in candidates:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return ""


def tail_file(path: Path, max_bytes: int = 200_000) -> str:
    data = path.read_bytes()
    return data[-max_bytes:].decode("utf-8", errors="replace")


def build_issue(
    text: Optional[str] = None,
    log_file: Optional[str] = None,
    stdin_text: Optional[str] = None,
    clipboard: bool = False,
    last_log: Optional[Path] = None,
) -> str:
    """Combine every input source into one condensed issue string."""
    parts: List[str] = []
    if text and text != "-":
        parts.append(text)
    if text == "-" and stdin_text:
        parts.append(stdin_text)
    if log_file:
        parts.append(tail_file(Path(log_file)))
    if last_log is not None and last_log.exists():
        parts.append(tail_file(last_log))
    if clipboard:
        parts.append(read_clipboard())
    return condense_log("\n".join(p for p in parts if p and p.strip()))
