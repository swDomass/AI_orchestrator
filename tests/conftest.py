"""Add project root to sys.path so test modules can import project packages."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
