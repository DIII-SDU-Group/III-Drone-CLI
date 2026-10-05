"""Straightforward developer-host provisioning and removable-media helpers."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from .result import CommandResult, Finding, NextAction, Outcome


# Provisioning defaults mirrored from deployment/ansible/vars. The flight
# controller's UXRCE_DDS_DOM_ID must equal the provisioned stack domain.
STACK_ROS_DOMAIN_DEFAULT = 42
# The OptiTrack lab gateway publishes motion capture in ROS domain 0
# (docs/opti-track-lab-readiness.md). The opti_track pose relay bridges it into
# the stack domain, so the two must differ.
OPTI_TRACK_LAB_ROS_DOMAIN = 0
# A preview never reads or writes the Wi-Fi secret; it shows this instead.
WIFI_VARIABLES_PREVIEW = "@<private 0600 Wi-Fi variables file>"
WIFI_INPUT_INVALID = "III_DEVELOPER_HOST_WIFI_INPUT_INVALID"
ROS_DOMAIN_INVALID = "III_DEVELOPER_HOST_ROS_DOMAIN_INVALID"


class WifiInputError(ValueError):
    """A Wi-Fi provisioning input is unusable. Messages never contain secrets."""


@dataclass(frozen=True)
class WifiRequest:
    ssid: str
    # None only for a preview, which never asks for or reads the secret.
    passphrase: str | None
    regulatory_domain: str


def _ros_domain_id(value: str) -> int:
    try:
        domain = int(value, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid ROS domain ID: {value!r}") from None
    # 0..101 is the portable ROS 2 domain range on Linux.
    if not 0 <= domain <= 101:
        raise argparse.ArgumentTypeError("the ROS domain ID must be within 0..101")
    return domain


def _validate_wifi_ssid(ssid: str) -> str:
    if not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise WifiInputError("the Wi-Fi SSID must be 1 to 32 bytes long")
    if any(ord(character) < 32 or ord(character) == 127 for character in ssid):
        raise WifiInputError("the Wi-Fi SSID must not contain control characters")
    return ssid


def _validate_wifi_passphrase(passphrase: str) -> str:
    """Accept a WPA2 passphrase or a raw 64-digit hexadecimal PSK."""

    if re.fullmatch(r"[0-9A-Fa-f]{64}", passphrase):
        return passphrase
    if 8 <= len(passphrase) <= 63 and all(32 <= ord(c) <= 126 for c in passphrase):
        return passphrase
    raise WifiInputError(
        "the Wi-Fi passphrase must be 8 to 63 printable ASCII characters "
        "or a 64-digit hexadecimal PSK"
    )


def _read_wifi_passphrase_file(path: Path, workspace: Path) -> str:
    resolved = path.expanduser().resolve()
    if resolved == workspace or workspace in resolved.parents:
        raise WifiInputError(
            "keep the Wi-Fi passphrase file outside the III checkout so it can never be committed"
        )
    try:
        status = resolved.stat()
    except OSError as exc:
        raise WifiInputError(
            f"cannot read the Wi-Fi passphrase file {resolved}: {exc.strerror}"
        ) from None
    if not stat.S_ISREG(status.st_mode):
        raise WifiInputError(f"the Wi-Fi passphrase file {resolved} is not a regular file")
    if status.st_mode & 0o077:
        raise WifiInputError(
            f"restrict the Wi-Fi passphrase file to its owner first: chmod 600 {resolved}"
        )
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise WifiInputError(
            f"cannot read the Wi-Fi passphrase file {resolved} as UTF-8 text"
        ) from None
    # Only the first line counts; strip its line terminator, nothing else.
    first_line = text.split("\n", 1)[0]
    return first_line[:-1] if first_line.endswith("\r") else first_line


def _wifi_request(
    args: argparse.Namespace, workspace: Path, *, dry_run: bool
) -> WifiRequest | None:
    ssid = getattr(args, "wifi_ssid", None)
    psk_file = getattr(args, "wifi_psk_file", None)
    country = getattr(args, "wifi_country", None)
    if ssid is None:
        if psk_file is not None or country is not None:
            raise WifiInputError("--wifi-psk-file and --wifi-country need --wifi-ssid")
        return None
    ssid = _validate_wifi_ssid(ssid)
    regulatory_domain = ""
    if country is not None:
        if not re.fullmatch(r"[A-Za-z]{2}", country):
            raise WifiInputError("--wifi-country must be a two-letter ISO 3166 country code")
        regulatory_domain = country.upper()
    if psk_file is not None:
        passphrase = _read_wifi_passphrase_file(psk_file, workspace)
    elif dry_run:
        return WifiRequest(ssid, None, regulatory_domain)
    else:
        # Under --non-interactive the runner turns this prompt into a typed
        # required-input result instead of blocking.
        passphrase = getpass.getpass(f"Wi-Fi passphrase for {ssid!r}: ")
    return WifiRequest(ssid, _validate_wifi_passphrase(passphrase), regulatory_domain)


def _write_wifi_variables(directory: Path, request: WifiRequest) -> Path:
    """Write the Wi-Fi extra-vars owner-only, so the secret is never in argv."""

    path = directory / "wifi.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "iii_wifi_ssid": request.ssid,
                "iii_wifi_psk": request.passphrase,
                "iii_wifi_regulatory_domain": request.regulatory_domain,
            },
            stream,
        )
    return path


def _provision_input_error(code: str, message: str, field: str) -> CommandResult:
    return CommandResult(
        command="iii host provision",
        outcome=Outcome.USAGE_ERROR,
        summary="Developer host provisioning input is invalid; nothing was run.",
        code=code,
        findings=(Finding(code, message, field=field),),
        next_actions=(
            NextAction(("iii", "host", "provision", "--help"), "Review the provisioning options."),
        ),
    )


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
    ros_domain_id = getattr(args, "ros_domain_id", STACK_ROS_DOMAIN_DEFAULT)
    if args.profile == "opti_track" and ros_domain_id == OPTI_TRACK_LAB_ROS_DOMAIN:
        return _provision_input_error(
            ROS_DOMAIN_INVALID,
            "opti_track needs a stack ROS domain other than the OptiTrack lab domain "
            f"{OPTI_TRACK_LAB_ROS_DOMAIN}; the pose relay bridges between the two.",
            "ros_domain_id",
        )
    try:
        wifi = _wifi_request(args, root, dry_run=dry_run)
    except WifiInputError as exc:
        return _provision_input_error(WIFI_INPUT_INVALID, str(exc), "wifi")
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
        "-e",
        f"iii_ros_domain_id={ros_domain_id}",
    ]
    if getattr(args, "remove_wifi", False):
        command.extend(("-e", "iii_wifi_remove=true"))
    environment = {"ANSIBLE_CONFIG": str(root / "deployment/ansible/ansible.cfg")}
    if wifi is None:
        result = _run(command, dry_run=dry_run, environment=environment)
    elif dry_run or wifi.passphrase is None:
        result = _run([*command, "-e", WIFI_VARIABLES_PREVIEW], dry_run=True)
    else:
        # The secret travels only in an owner-only file that is deleted after
        # the run; the retained command shows its path, never its content.
        with tempfile.TemporaryDirectory(prefix="iii-wifi-") as private:
            variables = _write_wifi_variables(Path(private), wifi)
            result = _run(
                [*command, "-e", f"@{variables}"],
                dry_run=dry_run,
                environment=environment,
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
    provision_parser.add_argument(
        "--ros-domain-id",
        type=_ros_domain_id,
        default=STACK_ROS_DOMAIN_DEFAULT,
        metavar="N",
        help=(
            "ROS 2 domain of the aircraft stack (default: %(default)s); the flight "
            "controller's UXRCE_DDS_DOM_ID must equal it"
        ),
    )
    wifi_group = provision_parser.add_argument_group(
        "optional Wi-Fi client",
        "Join a Wi-Fi network (for example the OptiTrack lab) in addition to the PX4 "
        "and workstation links; Wi-Fi then owns the default route while associated. "
        "The passphrase is read from --wifi-psk-file or prompted for, kept only on "
        "the Pi, and never passed on a command line. Without these options an "
        "existing Wi-Fi client is left unchanged.",
    )
    wifi_choice = wifi_group.add_mutually_exclusive_group()
    wifi_choice.add_argument("--wifi-ssid", metavar="SSID", help="Wi-Fi network name to join")
    wifi_choice.add_argument(
        "--remove-wifi",
        action="store_true",
        help="remove the Wi-Fi client configuration from the Pi",
    )
    wifi_group.add_argument(
        "--wifi-psk-file",
        type=Path,
        metavar="PATH",
        help=(
            "owner-only (chmod 600) file outside the checkout whose first line is the "
            "WPA2 passphrase or 64-digit hexadecimal PSK"
        ),
    )
    wifi_group.add_argument(
        "--wifi-country",
        metavar="CC",
        help="two-letter Wi-Fi regulatory domain, for example DK",
    )
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
