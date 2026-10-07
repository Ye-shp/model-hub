"""Make the standalone automation package importable from repository-root pytest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
