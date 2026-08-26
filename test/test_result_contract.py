import argparse
from io import StringIO
import json
from pathlib import Path
import shlex
import subprocess
from concurrent.futures import ProcessPoolExecutor

import jsonschema
import pytest

from iii.__main__ import build_parser, main
from iii.operation import OperationError, OperationStore, create_plan
from iii.result import CommandResult, Finding, NextAction, Outcome
from iii.runner import (
    CommandSpec,
    REQUIRED_COMMAND_FAMILIES,
    UniversalOptions,
    inventory_parser,
    invoke,
)


SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "iii"
    / "schemas"
    / "command-result-v1.schema.json"
)
PLAN_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "iii"
    / "schemas"
    / "operation-plan-v1.schema.json"
)
STATE_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "iii"
    / "schemas"
    / "operation-state-v1.schema.json"
)


def _concurrent_record_write(root: str, index: int) -> None:
    OperationStore(Path(root)).write_record(
        "iii-concurrent-records", f"record-{index}.json", {"index": index}
    )


def _result(outcome=Outcome.SUCCESS, **overrides):
    values = {
        "command": "iii test",
        "outcome": outcome,
        "summary": "test result",
        "code": "III_TEST_RESULT",
        "next_actions": (NextAction(("iii", "--help"), "Continue safely."),),
    }
    values.update(overrides)
    return CommandResult(**values)


@pytest.mark.parametrize("outcome", list(Outcome))
def test_result_schema_golden_and_exit_family(outcome):
    result = _result(outcome)
    value = json.loads(result.render_json())
    # The contract uses only Draft 7-compatible keywords; this keeps the suite
    # runnable in the ROS image's older validator as well as current CI.
    jsonschema.Draft7Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(
        value
    )
    assert value["exit_code"] == outcome.exit_code


def test_human_next_and_json_next_are_the_same_argv_and_reason():
    action = NextAction(
        ("iii", "deploy", "release", "target with spaces", "quote'arg"),
        "Deploy the selected release.",
        mutating=True,
        prerequisites=("Qualification accepted.",),
        confirmation_required=True,
        target="target with spaces",
        profile="real",
        operation_id="iii-test-operation",
        arguments={"target": "target with spaces", "profile": "real"},
    )
    result = _result(next_actions=(action,))
    structured = result.to_dict()["next_actions"][0]
    human = result.render_human()
    assert shlex.split(structured["shell_command"]) == list(action.command)
    assert structured["shell_command"] in human
    assert structured["reason"] in human
    assert structured["confirmation_required"] is True
    assert structured["arguments"] == {
        "target": "target with spaces",
        "profile": "real",
    }


def test_terminal_result_requires_explicit_reason():
    with pytest.raises(ValueError, match="next action"):
        _result(next_actions=())
    terminal = _result(
        next_actions=(), terminal_reason="The aircraft was decommissioned."
    )
    assert terminal.to_dict()["next_actions"] == []
    assert "decommissioned" in terminal.render_human()


def test_json_stdout_is_clean_when_legacy_handler_prints(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    import iii.system as system

    def fake_status(_args):
        print("\x1b[31mdecorative legacy status\x1b[0m")
        raise SystemExit(0)

    monkeypatch.setattr(system, "status", fake_status)
    stdout = StringIO()
    stderr = StringIO()
    status = main(["system", "status", "--json"], stdout=stdout, stderr=stderr)
    value = json.loads(stdout.getvalue())
    assert status == 0
    assert value["schema"] == "iii.command-result/v1"
    assert value["payload"]["display"] == "\x1b[31mdecorative legacy status\x1b[0m"
    assert stderr.getvalue() == ""


def test_json_stdout_is_clean_when_child_process_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    import iii.system as system

    def fake_status(_args):
        subprocess.run(["printf", "child-process-output"], check=True)

    monkeypatch.setattr(system, "status", fake_status)
    stdout = StringIO()
    status = main(["system", "status", "--json"], stdout=stdout, stderr=StringIO())
    value = json.loads(stdout.getvalue())
    assert status == 0
    assert value["payload"]["display"] == "child-process-output"


@pytest.mark.parametrize(
    ("argv", "expected_code", "expected_status"),
    [
        (["--help", "--json"], "III_HELP", 0),
        (["system", "--help", "--json"], "III_HELP", 0),
        (["does-not-exist", "--json"], "III_USAGE_ERROR", 64),
        (["system", "service", "start", "--json"], "III_USAGE_ERROR", 64),
    ],
)
def test_help_and_parser_errors_share_result_and_next_action(
    argv, expected_code, expected_status
):
    stdout = StringIO()
    status = main(argv, stdout=stdout, stderr=StringIO())
    value = json.loads(stdout.getvalue())
    assert status == expected_status
    assert value["code"] == expected_code
    assert value["next_actions"]
    assert value["next_actions"][0]["command"][-1] == "--help"
    if expected_code == "III_HELP":
        assert value["next_actions"][0]["command"] != [
            "iii",
            *[item for item in argv if item not in {"--json", "--help"}],
            "--help",
        ]


def test_noninteractive_confirmation_refusal_never_calls_mutation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    called = []
    args = argparse.Namespace(func=lambda _args: called.append(True))
    result, _ = invoke(
        args=args,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start"],
        options=UniversalOptions(output="json", non_interactive=True),
    )
    assert result.code == "III_REQUIRED_INPUT"
    assert result.outcome is Outcome.REJECTED
    assert called == []
    assert result.next_actions[0].confirmation_required is True


def test_noninteractive_nested_prompt_is_structured_and_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))

    def prompts(_args):
        input("Enter the target host: ")

    args = argparse.Namespace(func=prompts)
    result, _ = invoke(
        args=args,
        spec=CommandSpec(("deploy", "ssh"), mutating=True, interactive=True),
        argv=["deploy", "ssh"],
        options=UniversalOptions(
            non_interactive=True, confirm=True, operation_id="iii-prompt-test"
        ),
    )
    assert result.code == "III_REQUIRED_INPUT"
    assert result.findings[0].field == "input"
    state = OperationStore(tmp_path).load_state("iii-prompt-test")
    assert state["state"] == "rejected"
    assert state["attempt"] == 1


def test_every_existing_parser_leaf_is_inventory_covered():
    inventory = inventory_parser(build_parser())
    assert inventory
    assert all(path and spec.path == path for path, spec in inventory.items())
    assert all(spec.identity.startswith("iii ") for spec in inventory.values())
    assert {path[0] for path in inventory} == {
        "system",
        "build",
        "deploy",
        "config",
        "release",
        "mission",
        "field",
        "logs",
    }
    # Future providers must select from this declared universal contract surface.
    assert {
        "system",
        "build",
        "deploy",
        "release",
        "host",
        "gc",
        "qgc",
        "px4",
        "mission",
        "config",
        "capture",
        "logs",
        "records",
        "governance",
        "field",
        "documentation",
    } == REQUIRED_COMMAND_FAMILIES


def test_operation_registry_serializes_concurrent_atomic_record_writes(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as executor:
        list(executor.map(_concurrent_record_write, [str(tmp_path)] * 12, range(12)))
    store = OperationStore(tmp_path)
    assert [
        store.load_record("iii-concurrent-records", f"record-{index}.json")["index"]
        for index in range(12)
    ] == list(range(12))
    assert list(tmp_path.rglob("*.tmp")) == []


def test_ctrl_c_retains_exact_reattach_command(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "remote")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))

    def interrupted(_args):
        raise KeyboardInterrupt

    result, _ = invoke(
        args=argparse.Namespace(func=interrupted),
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start", "--select-node", "camera node"],
        options=UniversalOptions(
            non_interactive=True, confirm=True, operation_id="iii-remote-detach"
        ),
    )
    assert result.outcome is Outcome.INTERRUPTED
    assert result.exit_code == 130
    assert result.operation_id == "iii-remote-detach"
    action = result.next_actions[0]
    assert shlex.split(action.shell_command) == list(action.command)
    assert "--resume" in action.command
    assert "camera node" in action.command
    state = OperationStore(tmp_path).load_state("iii-remote-detach")
    assert state["state"] == "interrupted"


def test_completed_operation_is_idempotent_and_does_not_repeat(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    called = []

    def mutation(_args):
        called.append(True)

    arguments = argparse.Namespace(func=mutation)
    options = UniversalOptions(
        non_interactive=True, confirm=True, operation_id="iii-idempotent-test"
    )
    first, _ = invoke(
        args=arguments,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start"],
        options=options,
    )
    second, _ = invoke(
        args=arguments,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start"],
        options=options,
    )
    assert first.code == "III_SYSTEM_START_COMPLETED"
    assert second.code == "III_OPERATION_ALREADY_COMPLETE"
    assert called == [True]


def test_operation_id_rejects_changed_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    arguments = argparse.Namespace(func=lambda _args: None)
    options = UniversalOptions(dry_run=True, operation_id="iii-conflict-test")
    first, _ = invoke(
        args=arguments,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start"],
        options=options,
    )
    second, _ = invoke(
        args=arguments,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start", "--skip-activate"],
        options=options,
    )
    assert first.code == "III_OPERATION_PLAN_READY"
    assert second.code == "III_OPERATION_CONFLICT"
    assert second.outcome is Outcome.REJECTED


def test_read_only_command_rejects_operation_controls(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    result, _ = invoke(
        args=argparse.Namespace(func=lambda _args: None),
        spec=CommandSpec(("system", "status"), mutating=False),
        argv=["system", "status"],
        options=UniversalOptions(dry_run=True),
    )
    assert result.outcome is Outcome.USAGE_ERROR
    assert result.code == "III_OPERATION_NOT_MUTATING"


def test_operation_store_files_are_private_and_content_addressed(tmp_path):
    plan = create_plan(
        identifier="iii-storage-test",
        argv=["system", "start"],
        command="iii system start",
        mutating=True,
        target=None,
        profile="sim",
        release_id=None,
    )
    store = OperationStore(tmp_path)
    state = store.retain_plan(plan)
    jsonschema.Draft7Validator(
        json.loads(PLAN_SCHEMA.read_text(encoding="utf-8"))
    ).validate(plan)
    jsonschema.Draft7Validator(
        json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
    ).validate(state)
    assert state["plan_id"] == plan["plan_id"]
    assert len(plan["plan_id"]) == 64
    assert store.plan_path("iii-storage-test").stat().st_mode & 0o777 == 0o600
    assert store.state_path("iii-storage-test").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_tampered_retained_plan_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CLI_CONFIGURATION", "dev")
    monkeypatch.setenv("III_OPERATION_STATE_DIR", str(tmp_path))
    arguments = argparse.Namespace(func=lambda _args: None)
    options = UniversalOptions(dry_run=True, operation_id="iii-tampered-plan")
    result, _ = invoke(
        args=arguments,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start"],
        options=options,
    )
    assert result.code == "III_OPERATION_PLAN_READY"
    path = OperationStore(tmp_path).plan_path("iii-tampered-plan")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["argv"].append("--changed-after-review")
    path.write_text(json.dumps(value), encoding="utf-8")
    rejected, _ = invoke(
        args=arguments,
        spec=CommandSpec(("system", "start"), mutating=True),
        argv=["system", "start"],
        options=options,
    )
    assert rejected.code == "III_OPERATION_CONFLICT"
    assert rejected.outcome is Outcome.REJECTED


def test_symbolic_link_state_file_is_rejected(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    operation = tmp_path / "iii-linked-state"
    operation.mkdir()
    (operation / "state.json").symlink_to(target)
    with pytest.raises(OperationError, match="symbolic-link"):
        OperationStore(tmp_path).load_state("iii-linked-state")
