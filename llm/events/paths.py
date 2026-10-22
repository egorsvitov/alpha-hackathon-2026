"""Repository paths, independent of the caller working directory."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
CONTRACTS = ROOT / "docs/contracts"
