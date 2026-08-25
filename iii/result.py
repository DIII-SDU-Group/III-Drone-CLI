"""Canonical, versioned result contract for every ``iii`` command.

The command runner is the sole renderer.  Command implementations return this
model (or are adapted into it), so human output and machine output cannot carry
different outcomes or recovery advice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import shlex
from typing import Any, Mapping


RESULT_SCHEMA = "iii.command-result/v1"


class Outcome(str, Enum):
    SUCCESS = "success"
    WARNING = "warning"
    REJECTED = "rejected"
    FAILED = "failed"
    PARTIAL = "partial"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    USAGE_ERROR = "usage_error"
    INTERNAL_ERROR = "internal_error"

    @property
    def exit_code(self) -> int:
        """Map detailed outcomes into stable process-exit families."""

        return {
            Outcome.SUCCESS: 0,
            Outcome.WARNING: 10,
            Outcome.REJECTED: 20,
            Outcome.FAILED: 30,
            Outcome.PARTIAL: 31,
            Outcome.INTERRUPTED: 130,
            Outcome.CANCELLED: 130,
            Outcome.USAGE_ERROR: 64,
            Outcome.INTERNAL_ERROR: 70,
        }[self]


@dataclass(frozen=True)
class Finding:
    """Stable diagnostic suitable for policy automation and operators."""

    code: str
    message: str
    severity: str = "error"
    field: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("finding code and message are required")
        if self.severity not in {"info", "warning", "error"}:
            raise ValueError("unsupported finding severity")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NextAction:
    """An argv-safe, context-bound command recommendation."""

    command: tuple[str, ...]
    reason: str
    mutating: bool = False
    prerequisites: tuple[str, ...] = ()
    confirmation_required: bool = False
    target: str | None = None
    profile: str | None = None
    operation_id: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command or any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("next action command must contain non-empty argv strings")
        if not self.reason.strip():
            raise ValueError("next action reason is required")

    @property
    def shell_command(self) -> str:
        return shlex.join(self.command)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "shell_command": self.shell_command,
            "reason": self.reason,
            "mutating": self.mutating,
            "prerequisites": list(self.prerequisites),
            "confirmation_required": self.confirmation_required,
            "context": {
                "target": self.target,
                "profile": self.profile,
                "operation_id": self.operation_id,
            },
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class CommandResult:
    """One authoritative outcome envelope for human and structured rendering."""

    command: str
    outcome: Outcome
    summary: str
    code: str
    next_actions: tuple[NextAction, ...] = ()
    terminal_reason: str | None = None
    findings: tuple[Finding, ...] = ()
    operation_id: str | None = None
    state: str | None = None
    target: str | None = None
    profile: str | None = None
    release_id: str | None = None
    evidence: tuple[str, ...] = ()
    payload_schema: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command.strip() or not self.summary.strip() or not self.code.strip():
            raise ValueError("command, summary, and stable code are required")
        if not self.next_actions and not self.terminal_reason:
            raise ValueError("a result needs a next action or terminal-state reason")
        if self.operation_id is None and self.state is not None:
            raise ValueError("operation state requires an operation ID")

    @property
    def exit_code(self) -> int:
        return self.outcome.exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "command": self.command,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "code": self.code,
            "findings": [finding.to_dict() for finding in self.findings],
            "operation": (
                {"id": self.operation_id, "state": self.state}
                if self.operation_id is not None
                else None
            ),
            "context": {
                "target": self.target,
                "profile": self.profile,
                "release_id": self.release_id,
            },
            "evidence": list(self.evidence),
            "payload_schema": self.payload_schema,
            "payload": dict(self.payload),
            "next_actions": [action.to_dict() for action in self.next_actions],
            "terminal_reason": self.terminal_reason,
        }

    def render_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def render_human(self) -> str:
        lines = [self.summary]
        display = self.payload.get("display") or self.payload.get("help")
        if isinstance(display, str) and display.strip():
            lines.extend(("", display.rstrip()))
        if self.findings:
            lines.extend(("", "Findings:"))
            for finding in self.findings:
                lines.append(f"  {finding.code}: {finding.message}")
        if self.operation_id is not None:
            lines.extend(("", f"Operation: {self.operation_id} ({self.state})"))
        if self.next_actions:
            lines.extend(("", "Next:"))
            for action in self.next_actions:
                mutation = " [mutating; confirmation required]" if action.confirmation_required else (
                    " [mutating]" if action.mutating else ""
                )
                lines.append(f"  {action.shell_command}{mutation} — {action.reason}")
                if action.prerequisites:
                    lines.append(f"    Requires: {', '.join(action.prerequisites)}")
        else:
            lines.extend(("", f"Terminal: {self.terminal_reason}"))
        return "\n".join(lines)


def internal_error_result(command: str, error: BaseException) -> CommandResult:
    return CommandResult(
        command=command,
        outcome=Outcome.INTERNAL_ERROR,
        summary=f"{command} failed because of an internal error.",
        code="III_INTERNAL_ERROR",
        findings=(Finding("III_INTERNAL_ERROR", type(error).__name__),),
        next_actions=(
            NextAction(
                ("iii", "--help"),
                "Review command requirements before retrying with retained diagnostics.",
            ),
        ),
    )


# Backwards-compatible name used by the deployment contract during migration.
result_from_exception = internal_error_result
