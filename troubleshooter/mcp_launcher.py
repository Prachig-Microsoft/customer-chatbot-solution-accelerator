"""Start the MCP server from any working directory: ``python troubleshooter/mcp_launcher.py``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accel_troubleshooter.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
