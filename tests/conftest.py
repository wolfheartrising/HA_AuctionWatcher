import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import aw_loader  # noqa: E402,F401  (registers package "aw" without Home Assistant)
