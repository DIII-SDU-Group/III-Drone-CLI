"""Installed mission-catalog inspection and maintenance-safe selection."""

from __future__ import annotations

import argparse
import json
from typing import Any, Mapping

from .result import CommandResult, Finding, Outcome
from .runtime_api_client import RuntimeApiClient, RuntimeApiError


STATUS_COMMAND = "mission.catalog.status"
LIST_COMMAND = "mission.catalog.list"
SHOW_COMMAND = "mission.catalog.show"
SELECT_COMMAND = "mission.catalog.select"


def _client() -> RuntimeApiClient:
    return RuntimeApiClient.from_env()


def _call(
    command: str,
    command_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, CommandResult | None]:
    try:
        response = _client().command(command_id, dict(parameters))
    except RuntimeApiError as exc:
        return None, _rejected(command, "III_MISSION_RUNTIME_UNAVAILABLE", str(exc))
    if not response.get("accepted", False):
        rejection = response.get("rejection") if isinstance(response.get("rejection"), dict) else {}
        message = str(rejection.get("message") or response.get("message") or "mission catalog command rejected")
        return None, _rejected(command, "III_MISSION_CATALOG_REJECTED", message)
    result = response.get("result")
    if not isinstance(result, dict):
        return None, _rejected(
            command,
            "III_MISSION_CATALOG_INVALID_RESPONSE",
            "runtime API omitted the mission catalog result",
        )
    violation = _absolute_path_violation(result)
    if violation:
        return None, _rejected(command, "III_MISSION_CATALOG_PATH_LEAK", violation)
    return result, None


def _rejected(command: str, code: str, message: str) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.REJECTED,
        summary="The mission catalog operation was refused.",
        code=code,
        findings=(Finding(code, message),),
        terminal_reason="No mission catalog state was changed.",
    )


def _absolute_path_violation(value: Any, *, location: str = "result") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            violation = _absolute_path_violation(child, location=f"{location}.{key}")
            if violation:
                return violation
    elif isinstance(value, list):
        for index, child in enumerate(value):
            violation = _absolute_path_violation(child, location=f"{location}[{index}]")
            if violation:
                return violation
    elif isinstance(value, str) and (value.startswith(("/", "file://")) or ":\\" in value):
        return f"runtime response exposed a forbidden filesystem path at {location}"
    return None


def status(args: argparse.Namespace) -> CommandResult:
    command = "iii mission status"
    result, failure = _call(command, STATUS_COMMAND, {})
    if failure:
        return failure
    assert result is not None
    state = result.get("status") if isinstance(result.get("status"), dict) else {}
    specification = state.get("specification") if isinstance(state.get("specification"), dict) else {}
    display = "\n".join(
        (
            f"Catalog: {specification.get('catalog_id') or 'unavailable'}",
            f"Catalog hash: {specification.get('catalog_hash') or 'unavailable'}",
            f"Entry hash: {specification.get('entry_hash') or 'unavailable'}",
            f"Default: {specification.get('default_catalog_id') or 'unavailable'}",
            f"Profile: {specification.get('active_profile') or 'unknown'}",
            f"Classification: {specification.get('classification') or 'unknown'}",
            f"Temporary override: {bool(specification.get('temporary_override'))}",
            f"Catalog ready: {bool(specification.get('catalog_ready'))}",
        )
    )
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS if specification.get("catalog_ready") else Outcome.WARNING,
        summary="Mission catalog runtime status returned.",
        code="III_MISSION_CATALOG_STATUS",
        terminal_reason="Installed catalog status was displayed without mutation.",
        payload_schema="iii.mission-catalog-status/v1",
        payload={"status": state, "display": display},
    )


def list_entries(args: argparse.Namespace) -> CommandResult:
    command = "iii mission list" + (" --all" if args.all else "")
    result, failure = _call(command, LIST_COMMAND, {"all": bool(args.all)})
    if failure:
        return failure
    assert result is not None
    catalog = result.get("catalog") if isinstance(result.get("catalog"), dict) else {}
    entries = catalog.get("entries") if isinstance(catalog.get("entries"), list) else []
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        availability = (
            "available"
            if entry.get("available", True)
            else f"unavailable: {entry.get('unavailable_reason', 'incompatible')}"
        )
        rows.append(f"{entry.get('id', '<invalid>')}  {entry.get('classification', 'unknown'):<12}  {availability}")
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=f"Returned {len(entries)} installed mission catalog entr{'y' if len(entries) == 1 else 'ies'}.",
        code="III_MISSION_CATALOG_LIST",
        terminal_reason="Installed catalog metadata was displayed without mutation.",
        payload_schema="iii.mission-catalog-list/v1",
        payload={"catalog": catalog, "display": "\n".join(rows) or "No mission catalog entries are available."},
    )


def show(args: argparse.Namespace) -> CommandResult:
    command = f"iii mission show {args.catalog_id}"
    result, failure = _call(command, SHOW_COMMAND, {"catalog_id": args.catalog_id, "all": True})
    if failure:
        return failure
    assert result is not None
    return CommandResult(
        command=command,
        outcome=Outcome.SUCCESS,
        summary=f"Mission catalog entry {args.catalog_id} returned.",
        code="III_MISSION_CATALOG_SHOW",
        terminal_reason="Installed catalog metadata was displayed without mutation.",
        payload_schema="iii.mission-catalog-entry/v1",
        payload={**result, "display": json.dumps(result, indent=2, sort_keys=True)},
    )


def select(args: argparse.Namespace) -> CommandResult:
    command = "iii mission select --default" if args.default else f"iii mission select {args.catalog_id or ''}".rstrip()
    if bool(args.default) == bool(args.catalog_id):
        return _rejected(
            command,
            "III_MISSION_CATALOG_SELECTION_REQUIRED",
            "select exactly one catalog ID or --default",
        )
    result, failure = _call(
        command,
        SELECT_COMMAND,
        {"default": bool(args.default), **({"catalog_id": args.catalog_id} if args.catalog_id else {})},
    )
    if failure:
        return failure
    assert result is not None
    warning = result.get("warning")
    display = "\n".join(
        line for line in (
            f"Active catalog: {result.get('active_catalog_id')}",
            f"Entry hash: {result.get('active_entry_hash')}",
            f"Temporary override: {bool(result.get('temporary_override'))}",
            f"WARNING: {warning}" if warning else "",
        ) if line
    )
    return CommandResult(
        command=command,
        outcome=Outcome.WARNING if warning else Outcome.SUCCESS,
        summary=str(result.get("message") or "Mission catalog selection completed."),
        code="III_MISSION_CATALOG_SELECTED",
        terminal_reason="The runtime completed and reported the transactional catalog selection.",
        findings=(
            (Finding("III_MISSION_EXPERIMENTAL_WARNING", str(warning), severity="warning"),)
            if warning
            else ()
        ),
        payload_schema="iii.mission-catalog-selection/v1",
        payload={**result, "display": display},
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="mission_action")

    status_parser = actions.add_parser("status", help="Show active installed mission catalog identity")
    status_parser.set_defaults(func=status, _iii_mutating=False)

    list_parser = actions.add_parser("list", help="List installed mission catalog entries")
    list_parser.add_argument(
        "--all",
        action="store_true",
        help="Include incompatible classified local entries with reasons",
    )
    list_parser.set_defaults(func=list_entries, _iii_mutating=False)

    show_parser = actions.add_parser("show", help="Show one installed mission catalog entry")
    show_parser.add_argument("catalog_id")
    show_parser.set_defaults(func=show, _iii_mutating=False)

    select_parser = actions.add_parser("select", help="Select an installed mission catalog entry transactionally")
    select_parser.add_argument("catalog_id", nargs="?")
    select_parser.add_argument("--default", action="store_true", help="Restore the profile default")
    select_parser.set_defaults(func=select, _iii_mutating=True)
