"""Importing this package registers every diagnostic check in ``CHECKS``."""

from . import app_checks, azure_checks, chat_checks, insights_checks, repo_checks  # noqa: F401
from .base import CHECKS, CheckSpec, DiagContext, run_check  # noqa: F401
