import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = PACKAGE_ROOT.parents[1] / "src/III-Drone-Contracts"

for package_path in (PACKAGE_ROOT, CONTRACTS_ROOT):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _vehicle_gate_allows(monkeypatch):
    """Unit tests have no Pi; tests of the gate itself replace this."""

    from iii import vehicle_gate

    monkeypatch.setattr(
        vehicle_gate,
        "evaluate",
        lambda *args, **kwargs: vehicle_gate.RestartGate(True, "hil", "test gate"),
    )
