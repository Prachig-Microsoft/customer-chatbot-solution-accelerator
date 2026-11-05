"""Importing this package registers every diagnostic check in ``CHECKS``."""

from . import (  # noqa: F401
    app_checks,
    azure_checks,
    chat_checks,
    fabric_checks,
    insights_checks,
    inventory_checks,
    repo_checks,
    tool_checks,
)
from .base import CHECKS, CheckSpec, DiagContext, run_check  # noqa: F401
