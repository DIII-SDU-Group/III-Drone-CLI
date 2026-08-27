from pathlib import Path
from types import SimpleNamespace

from iii.__main__ import build_parser
from iii.runner import inventory_parser
from iii import verification


ROOT = Path(__file__).resolve().parents[3]


def test_parser_inventory_declares_deployment_verification_read_only() -> None:
    inventory = inventory_parser(build_parser())
    leaf = inventory[("verify", "deployment")]
    assert leaf.mutating is False
    assert leaf.interactive is False


def test_deployment_verification_audits_committed_matrix(tmp_path: Path) -> None:
    args = SimpleNamespace(
        root=ROOT,
        evidence=[],
        trusted_signers=None,
        junit=tmp_path / "matrix.xml",
        report=tmp_path / "matrix.json",
        require_level=[],
        require_complete=False,
    )
    result = verification.deployment(args)
    assert result.outcome.value == "warning"
    assert result.code == "III_VERIFY_PENDING"
    assert result.payload["counts"]["not_run"] == len(result.payload["rows"])
    assert args.junit.is_file()
    assert args.report.is_file()


def test_required_level_fails_closed_without_authenticated_evidence(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        root=ROOT,
        evidence=[],
        trusted_signers=None,
        junit=None,
        report=None,
        require_level=["target-equivalent"],
        require_complete=False,
    )
    result = verification.deployment(args)
    assert result.outcome.value == "rejected"
    assert result.code == "III_VERIFY_REQUIRED_INCOMPLETE"
    assert result.findings


def test_deployment_verification_requires_explicit_workspace_root(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(
        root=None,
        evidence=[],
        trusted_signers=None,
        junit=None,
        report=None,
        require_level=[],
        require_complete=False,
    )
    result = verification.deployment(args)
    assert result.code == "III_VERIFY_ROOT_REQUIRED"
    assert result.outcome.value == "rejected"
