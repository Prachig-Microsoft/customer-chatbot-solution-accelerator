import subprocess
import sys

from accel_troubleshooter.classify import extract
from accel_troubleshooter.engine import Troubleshooter

from conftest import REPO_ROOT, TOOL_ROOT


def test_planned_check_order_is_identical_across_processes():
    code = (
        "from accel_troubleshooter.engine import Troubleshooter\n"
        "from accel_troubleshooter.azcli import OfflineRunner\n"
        "from accel_troubleshooter.classify import extract\n"
        "from pathlib import Path\n"
        f"ts = Troubleshooter(Path(r'{REPO_ROOT}'), OfflineRunner())\n"
        "print(','.join(ts.plan_checks(extract('x'), full=True)))"
    )
    runs = set()
    for seed in ("1", "2", "3", "99"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=TOOL_ROOT,
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, **__import__("os").environ},
            timeout=120,
        )
        assert out.returncode == 0, out.stderr
        runs.add(out.stdout.strip())
    assert len(runs) == 1 and "chat.e2e" not in runs.pop()


def test_full_plan_includes_the_new_log_check_but_never_the_opt_in_chat_message():
    ts = Troubleshooter(REPO_ROOT)
    planned = ts.plan_checks(extract("x"), full=True)
    assert "appinsights.errors" in planned and "chat.e2e" not in planned
