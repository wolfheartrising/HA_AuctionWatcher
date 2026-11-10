"""Import the integration's site code without importing Home Assistant.

custom_components/auction_watcher/__init__.py needs Home Assistant, but the
site handlers and cookie parser don't. This registers the folder as package
"aw" without running its __init__.py, so `import aw.sites.govauctions` works
in plain Python (tests, tools/live_check.py).
"""

import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "auction_watcher"

if "aw" not in sys.modules:
    pkg = types.ModuleType("aw")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["aw"] = pkg
