from pathlib import Path
from types import SimpleNamespace

from iii import docs
from iii.__main__ import build_parser
from iii.runner import inventory_parser

ROOT = Path(__file__).resolve().parents[3]


def test_docs_check_parser_is_read_only() -> None:
    inventory = inventory_parser(build_parser())
    assert inventory[("docs", "check")].mutating is False
    assert inventory[("docs", "check")].interactive is False


def test_docs_check_validates_current_workspace_offline() -> None:
    result = docs.check(SimpleNamespace(root=ROOT))
    assert result.outcome.value == "success"
    assert result.code == "III_DOCS_OK"
    assert result.payload["errors"] == []
    assert result.payload["generated"] == 2


def test_docs_check_requires_a_governed_workspace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = docs.check(SimpleNamespace(root=None))
    assert result.outcome.value == "failed"
    assert result.code == "III_DOCS_INVALID"
