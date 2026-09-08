"""CLI entry point for the isolated execution-market worker."""
import sys
from pathlib import Path

# Keep the contract command runnable from a source checkout, while launchd
# still supplies its canonical checkout explicitly through PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kline.execution_market.worker import main

if __name__ == "__main__":
    raise SystemExit(main())
