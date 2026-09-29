import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = PACKAGE_ROOT.parents[1] / "src/III-Drone-Contracts"

for package_path in (PACKAGE_ROOT, CONTRACTS_ROOT):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))
