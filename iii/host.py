"""Straightforward developer-host provisioning and removable-media helpers."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from .result import CommandResult, Finding, NextAction, Outcome


def _developer_first_boot_user_data() -> str:
    """Create the one-time cloud-init account used for every later online update."""

    key_paths = (
        Path.home() / ".ssh/id_ed25519.pub",
        Path.home() / ".ssh/id_rsa.pub",
    )
    public_key = next(
        (path.read_text(encoding="utf-8").strip() for path in key_paths if path.is_file()),
        None,
    )
    if not public_key:
        raise ValueError("no local SSH public key is available for first boot")
    return "\n".join(
        (
            "#cloud-config",
            "hostname: iii",
            "manage_etc_hosts: true",
            "ssh_pwauth: true",
            "users:",
            "  - name: iii",
            "    groups: [adm, dialout, render, sudo, video]",
            "    shell: /bin/bash",
            "    sudo: ALL=(ALL) NOPASSWD:ALL",
            "    lock_passwd: false",
            "    plain_text_passwd: iii",
            "    ssh_authorized_keys:",
            f"      - {public_key}",
            "runcmd:",
            "  - [systemctl, enable, ssh]",
            "",
        )
    )


def _developer_first_boot_network() -> str:
    return """version: 2
ethernets:
  workstation-usb-ethernet:
    match:
      driver: ax88179_178a
    addresses: [10.42.0.15/24]
    dhcp4: true
    link-local: []
    optional: true
"""


def _seed_first_boot(device: Path, *, dry_run: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="iii-first-boot-") as directory:
        seed = Path(directory)
        user_data = seed / "user-data"
        network_config = seed / "network-config"
        user_data.write_text(_developer_first_boot_user_data(), encoding="utf-8")
        network_config.write_text(_developer_first_boot_network(), encoding="utf-8")
        user_data.chmod(0o644)
        network_config.chmod(0o644)
        mountpoint = seed / "system-boot"
        command = [
            "bash",
            "-lc",
            "set -euo pipefail; "
            f"sudo partprobe {shlex.quote(str(device))}; sudo udevadm settle; "
            f"boot_partition=$(lsblk -nrpo PATH,PARTLABEL,TYPE {shlex.quote(str(device))} "
            "| awk '$3 == \"part\" && $2 == \"system-boot\" {print $1; exit}'); "
            f"if [ -z \"$boot_partition\" ]; then boot_partition=$(lsblk -nrpo PATH,TYPE {shlex.quote(str(device))} "
            "| awk '$2 == \"part\" {print $1; exit}'); fi; "
            "test -n \"$boot_partition\"; "
            "existing_mount=$(findmnt -nr -S \"$boot_partition\" -o TARGET || true); "
            f"if [ -n \"$existing_mount\" ]; then mountpoint=\"$existing_mount\"; else mkdir -p {shlex.quote(str(mountpoint))}; mountpoint={shlex.quote(str(mountpoint))}; sudo mount \"$boot_partition\" \"$mountpoint\"; trap 'sudo umount \"$mountpoint\"' EXIT; fi; "
            f"sudo install -m 0644 {shlex.quote(str(user_data))} \"$mountpoint\"/user-data; "
            f"sudo install -m 0644 {shlex.quote(str(network_config))} \"$mountpoint\"/network-config; "
            "sync",
        ]
        return _run(command, dry_run=dry_run)


def _workspace() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deployment/ansible/playbooks/aircraft-converge.yml").is_file():
            return candidate
    raise ValueError("run this command from the III workspace")


def _run(
    command: Sequence[str], *, dry_run: bool, environment: Mapping[str, str] | None = None
) -> dict[str, Any]:
    if dry_run:
        return {"command": list(command), "returncode": None, "stdout": "", "stderr": ""}
    child_environment = os.environ.copy()
    if environment is not None:
        child_environment.update(environment)
    completed = subprocess.run(
        command, check=False, text=True, capture_output=True, env=child_environment
    )
    return {
        "command": list(command),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _direct_result(
    *,
    command: str,
    target: str | None,
    result: Mapping[str, Any],
    success: str,
    failure: str,
    dry_run: bool = False,
) -> CommandResult:
    failed = result["returncode"] not in {0, None}
    preview = dry_run and not failed
    host = target.rsplit("@", 1)[-1] if target else "iii.local"
    return CommandResult(
        command=command,
        outcome=Outcome.FAILED if failed else Outcome.SUCCESS,
        summary=(
            failure
            if failed
            else f"{command} preview completed; no host command was run."
            if preview
            else success
        ),
        code=(
            "III_DEVELOPER_HOST_FAILED"
            if failed
            else "III_DEVELOPER_HOST_PREVIEW"
            if preview
            else "III_DEVELOPER_HOST_COMPLETED"
        ),
        target=target,
        findings=(
            (Finding("III_DEVELOPER_HOST_FAILED", result["stderr"] or "remote command failed"),)
            if failed
            else ()
        ),
        payload_schema="iii.developer-host-command/v1",
        payload={"command": dict(result)},
        next_actions=(
            NextAction(("iii", "host", "inspect", "--host", host), "Inspect the developer host."),
        ),
    )


def inspect(args: argparse.Namespace) -> CommandResult:
    target = f"{args.user}@{args.host}"
    result = _run(
        [
            "ssh",
            target,
            "hostname; id; systemctl is-active iii-system-daemon.service iii-runtime-api.service; ip -brief address",
        ],
        dry_run=False,
    )
    return _direct_result(
        command="iii host inspect",
        target=target,
        result=result,
        success="Developer host inspection completed.",
        failure="Developer host inspection could not contact or query the Pi.",
    )


def provision(args: argparse.Namespace) -> CommandResult:
    root = _workspace()
    binary = shutil.which("ansible-playbook")
    dry_run = bool(getattr(args, "_iii_dry_run", False))
    if binary is None and not dry_run:
        return CommandResult(
            command="iii host provision",
            outcome=Outcome.REJECTED,
            summary="Developer host provisioning needs ansible-playbook on this computer.",
            code="III_DEVELOPER_HOST_ANSIBLE_MISSING",
            findings=(Finding("III_DEVELOPER_HOST_ANSIBLE_MISSING", "install ansible-core locally"),),
            next_actions=(
                NextAction(("python3", "-m", "pip", "install", "ansible-core"), "Install the lightweight provisioning controller."),
            ),
        )
    target = f"{args.user}@{args.host}"
    command = [
        binary or "ansible-playbook",
        "-i",
        f"{args.host},",
        "-u",
        args.user,
        "--become",
        str(root / "deployment/ansible/playbooks/aircraft-converge.yml"),
        "-e",
        f"iii_profile={args.profile}",
    ]
    result = _run(
        command,
        dry_run=dry_run,
        environment={"ANSIBLE_CONFIG": str(root / "deployment/ansible/ansible.cfg")},
    )
    return _direct_result(
        command="iii host provision",
        target=target,
        result=result,
        success="Developer host provisioning completed without receiver or trust inputs.",
        failure="Developer host provisioning stopped; inspect the plain Ansible output.",
        dry_run=dry_run,
    )


def image_write(args: argparse.Namespace) -> CommandResult:
    image = args.image.expanduser().resolve()
    device = Path(args.device).resolve()
    if not image.is_file() or not str(device).startswith("/dev/"):
        return CommandResult(
            command="iii host image write",
            outcome=Outcome.USAGE_ERROR,
            summary="Supply an existing image and an explicit block device.",
            code="III_DEVELOPER_HOST_IMAGE_USAGE_ERROR",
            findings=(Finding("III_DEVELOPER_HOST_IMAGE_USAGE_ERROR", "image or device is invalid"),),
            next_actions=(NextAction(("iii", "host", "image", "write", "--help"), "Review image-writing arguments."),),
        )
    if image.suffix == ".xz":
        command = [
            "bash",
            "-lc",
            f"xz --decompress --stdout {shlex.quote(str(image))} | sudo dd of={shlex.quote(str(device))} bs=16M status=progress conv=fsync",
        ]
    else:
        command = ["sudo", "dd", f"if={image}", f"of={device}", "bs=16M", "status=progress", "conv=fsync"]
    dry_run = bool(getattr(args, "_iii_dry_run", False))
    result = _run(command, dry_run=dry_run)
    if result["returncode"] in {0, None} and args.developer_first_boot:
        result = _seed_first_boot(device, dry_run=dry_run)
    return _direct_result(
        command="iii host image write",
        target=None,
        result=result,
        success="Developer image write completed.",
        failure="Developer image write failed.",
        dry_run=dry_run,
    )


def initialize(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="host_command")
    inspect_parser = subparsers.add_parser("inspect", help="inspect the Pi over ordinary SSH")
    inspect_parser.add_argument("--host", default="iii.local")
    inspect_parser.add_argument("--user", default="iii")
    inspect_parser.set_defaults(func=inspect, _iii_mutating=False)

    provision_parser = subparsers.add_parser(
        "provision", help="run the writable developer-host Ansible playbook"
    )
    provision_parser.add_argument("--host", default="iii.local")
    provision_parser.add_argument("--user", default="iii")
    provision_parser.add_argument("--profile", choices=("real", "opti_track", "hil"), default="real")
    provision_parser.set_defaults(func=provision, _iii_mutating=False, _iii_direct_mutation=True)

    image_parser = subparsers.add_parser("image", help="write a selected image directly to removable media")
    image_commands = image_parser.add_subparsers(dest="host_image_command")
    write_parser = image_commands.add_parser("write", help="write an image to an explicit block device")
    write_parser.add_argument("--image", type=Path, required=True)
    write_parser.add_argument("--device", required=True)
    write_parser.add_argument(
        "--developer-first-boot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="seed normal iii SSH, sudo, and workstation-link access after this one flash",
    )
    write_parser.set_defaults(func=image_write, _iii_mutating=False, _iii_direct_mutation=True)
