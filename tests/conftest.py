import sys
from pathlib import Path

# Add processing/ to sys.path so test imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "processing"))
