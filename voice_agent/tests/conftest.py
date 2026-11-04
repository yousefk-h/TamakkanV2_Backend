import sys
from pathlib import Path

# The agent runs as `uv run src/agent.py`, so `transcript` is a top-level module there too.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
