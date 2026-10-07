"""A minimal MAVLink 2 client for a PX4 flight controller on a direct link.

`iii px4 param-baseline` falls back to the flight controller's USB port when
the Pi has no MAVLink link to it. That needs only a handful of messages
(heartbeat, parameter read and write, landed state, reboot), so they are
encoded here without a MAVLink library: the ground computer's CLI stays free
of binary dependencies.

PX4 transfers parameters "bytewise": an INT32 parameter travels as the four
bytes of the message's float field.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import select
import socket
import struct
import termios
import time
import tty
from typing import Callable, Protocol

MAGIC_V2 = 0xFD
MAGIC_V1 = 0xFE

HEARTBEAT = 0
PARAM_REQUEST_READ = 20
PARAM_VALUE = 22
PARAM_SET = 23
COMMAND_LONG = 76
COMMAND_ACK = 77
EXTENDED_SYS_STATE = 245

# Message ID -> (CRC seed, payload length without extensions).
_MESSAGES = {
    HEARTBEAT: (50, 9),
    PARAM_REQUEST_READ: (214, 20),
    PARAM_VALUE: (220, 25),
    PARAM_SET: (168, 23),
    COMMAND_LONG: (152, 33),
    COMMAND_ACK: (143, 3),
    EXTENDED_SYS_STATE: (130, 2),
}

MAV_PARAM_TYPE_INT32 = 6
MAV_PARAM_TYPE_REAL32 = 9
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_LANDED_STATE_ON_GROUND = 1
MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN = 246
MAV_CMD_REQUEST_MESSAGE = 512
MAV_COMP_ID_AUTOPILOT1 = 1

# This client's own MAVLink identity (a ground-station component).
_OWN_SYSTEM = 255
_OWN_COMPONENT = 191


class MavlinkError(RuntimeError):
    """The flight controller did not answer as expected."""


def crc16(data: bytes, seed: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX, the MAVLink frame checksum."""

    crc = seed
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def encode(message_id: int, payload: bytes, *, sequence: int = 0,
           system: int = _OWN_SYSTEM, component: int = _OWN_COMPONENT) -> bytes:
    """One MAVLink 2 frame; trailing zero payload bytes are truncated."""

    seed, length = _MESSAGES[message_id]
    if len(payload) != length:
        raise ValueError(f"message {message_id} payload must be {length} bytes")
    trimmed = payload.rstrip(b"\x00") or payload[:1]
    header = struct.pack(
        "<BBBBBB", len(trimmed), 0, 0, sequence & 0xFF, system, component
    ) + message_id.to_bytes(3, "little")
    checksum = crc16(header + trimmed + bytes([seed]))
    return bytes([MAGIC_V2]) + header + trimmed + struct.pack("<H", checksum)


@dataclass(frozen=True)
class Frame:
    message_id: int
    system: int
    component: int
    payload: bytes


class Decoder:
    """Extract checksum-verified frames of the known messages from a byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            start = next(
                (i for i, byte in enumerate(self._buffer) if byte in (MAGIC_V2, MAGIC_V1)), None
            )
            if start is None:
                self._buffer.clear()
                return frames
            del self._buffer[:start]
            v2 = self._buffer[0] == MAGIC_V2
            header = 10 if v2 else 6
            if len(self._buffer) < header:
                return frames
            length = self._buffer[1]
            signed = v2 and bool(self._buffer[2] & 0x01)
            total = header + length + 2 + (13 if signed else 0)
            if len(self._buffer) < total:
                return frames
            if v2:
                system, component = self._buffer[5], self._buffer[6]
                message_id = int.from_bytes(self._buffer[7:10], "little")
            else:
                system, component, message_id = self._buffer[3], self._buffer[4], self._buffer[5]
            body = bytes(self._buffer[1:header + length])
            received = int.from_bytes(self._buffer[header + length:header + length + 2], "little")
            known = _MESSAGES.get(message_id)
            if known is not None and crc16(body + bytes([known[0]])) == received:
                payload = bytes(self._buffer[header:header + length])
                frames.append(
                    Frame(message_id, system, component, payload.ljust(known[1], b"\x00"))
                )
                del self._buffer[:total]
            elif known is None:
                # An unknown message cannot be checksummed; skip its frame.
                del self._buffer[:total]
            else:
                # Not a frame start after all: resynchronize on the next byte.
                del self._buffer[:1]


class Transport(Protocol):
    def read(self, timeout: float) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class SerialTransport:
    """A raw serial device, for example PX4's USB port (/dev/ttyACM0)."""

    def __init__(self, device: str):
        self.device = device
        try:
            self._fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise MavlinkError(f"cannot open {device}: {exc.strerror}") from None
        tty.setraw(self._fd)
        attributes = termios.tcgetattr(self._fd)
        attributes[4] = attributes[5] = termios.B57600
        termios.tcsetattr(self._fd, termios.TCSANOW, attributes)

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self._fd], [], [], max(timeout, 0.0))
        if not ready:
            return b""
        try:
            return os.read(self._fd, 4096)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise MavlinkError(f"{self.device} was disconnected: {exc.strerror}") from None

    def write(self, data: bytes) -> None:
        try:
            os.write(self._fd, data)
        except OSError as exc:
            raise MavlinkError(f"cannot write to {self.device}: {exc.strerror}") from None

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


class UdpTransport:
    """A MAVLink UDP endpoint that answers its sender (PX4 SITL, for tests)."""

    def __init__(self, host: str, port: int):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.connect((host, port))

    def read(self, timeout: float) -> bytes:
        self._socket.settimeout(max(timeout, 0.001))
        try:
            return self._socket.recv(4096)
        except (socket.timeout, ConnectionRefusedError):
            return b""

    def write(self, data: bytes) -> None:
        try:
            self._socket.send(data)
        except ConnectionRefusedError:
            pass

    def close(self) -> None:
        self._socket.close()


def open_transport(device: str) -> Transport:
    if device.startswith("udp:"):
        _, host, port = device.split(":")
        return UdpTransport(host, int(port))
    return SerialTransport(device)


def _parameter_id(name: str) -> bytes:
    encoded = name.encode("ascii")
    if not 1 <= len(encoded) <= 16:
        raise ValueError(f"invalid PX4 parameter name {name!r}")
    return encoded.ljust(16, b"\x00")


class Px4Link:
    """Parameter and state access to one PX4 autopilot over a direct link."""

    def __init__(self, transport: Transport, *, clock: Callable[[], float] = time.monotonic):
        self._transport = transport
        self._clock = clock
        self._decoder = Decoder()
        # Decoded frames not yet looked at: one read can hold several.
        self._pending: list[Frame] = []
        self._sequence = 0
        self._last_heartbeat_sent = float("-inf")
        self.system: int | None = None
        self.component = MAV_COMP_ID_AUTOPILOT1
        self.armed: bool | None = None

    def close(self) -> None:
        self._transport.close()

    def _send(self, message_id: int, payload: bytes) -> None:
        self._transport.write(encode(message_id, payload, sequence=self._sequence))
        self._sequence += 1

    def _keep_alive(self) -> None:
        # PX4's USB port starts MAVLink when it sees a heartbeat and keeps the
        # link only while the peer keeps sending them.
        if self._clock() - self._last_heartbeat_sent >= 1.0:
            self._last_heartbeat_sent = self._clock()
            self._send(
                HEARTBEAT, struct.pack("<IBBBBB", 0, MAV_TYPE_GCS, MAV_AUTOPILOT_INVALID, 0, 0, 3)
            )

    def _receive(self, timeout: float, accept: Callable[[Frame], bool]) -> Frame | None:
        deadline = self._clock() + timeout
        while True:
            self._keep_alive()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            if not self._pending:
                self._pending = self._decoder.feed(self._transport.read(min(remaining, 0.2)))
            while self._pending:
                frame = self._pending.pop(0)
                if frame.message_id == HEARTBEAT and frame.component == MAV_COMP_ID_AUTOPILOT1:
                    _, vehicle_type, autopilot, base_mode, _, _ = struct.unpack(
                        "<IBBBBB", frame.payload
                    )
                    if vehicle_type != MAV_TYPE_GCS and autopilot != MAV_AUTOPILOT_INVALID:
                        self.system = frame.system
                        self.armed = bool(base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
                if self.system is not None and frame.system == self.system and accept(frame):
                    return frame

    def connect(self, timeout: float = 8.0) -> None:
        """Wait for the autopilot's heartbeat."""

        self.system = None
        self.armed = None
        if self._receive(timeout, lambda frame: frame.message_id == HEARTBEAT) is None:
            raise MavlinkError("no PX4 heartbeat was received")

    def _require(self) -> int:
        if self.system is None:
            raise MavlinkError("the PX4 link is not connected")
        return self.system

    @staticmethod
    def _decode_value(frame: Frame) -> int | float:
        raw, _, _, _, parameter_type = struct.unpack("<4sHH16sB", frame.payload)
        if parameter_type == MAV_PARAM_TYPE_INT32:
            return struct.unpack("<i", raw)[0]
        if parameter_type == MAV_PARAM_TYPE_REAL32:
            return struct.unpack("<f", raw)[0]
        raise MavlinkError(f"unsupported PX4 parameter type {parameter_type}")

    def _await_value(self, name: str, timeout: float) -> int | float | None:
        identifier = _parameter_id(name)
        frame = self._receive(
            timeout,
            lambda frame: frame.message_id == PARAM_VALUE and frame.payload[8:24] == identifier,
        )
        return None if frame is None else self._decode_value(frame)

    def read_parameter(self, name: str, *, attempts: int = 3) -> int | float | None:
        """The parameter's value, typed as PX4 stores it; None when PX4 has no such parameter."""

        system = self._require()
        for _ in range(attempts):
            self._send(
                PARAM_REQUEST_READ,
                struct.pack("<hBB16s", -1, system, self.component, _parameter_id(name)),
            )
            value = self._await_value(name, 1.0)
            if value is not None:
                return value
        return None

    def read_parameters(self, names: list[str]) -> dict[str, int | float | None]:
        return {name: self.read_parameter(name) for name in names}

    def write_parameter(self, name: str, value: int | float, *, attempts: int = 3) -> None:
        """Set a parameter with the type of the given value and await PX4's echo."""

        system = self._require()
        if isinstance(value, float):
            raw, parameter_type = struct.pack("<f", value), MAV_PARAM_TYPE_REAL32
        else:
            raw, parameter_type = struct.pack("<i", int(value)), MAV_PARAM_TYPE_INT32
        for _ in range(attempts):
            self._send(
                PARAM_SET,
                struct.pack(
                    "<4sBB16sB", raw, system, self.component, _parameter_id(name), parameter_type
                ),
            )
            if self._await_value(name, 1.5) is not None:
                return
        raise MavlinkError(f"PX4 did not acknowledge setting {name}")

    def _command(self, command: int, *parameters: float) -> None:
        values = list(parameters) + [0.0] * (7 - len(parameters))
        self._send(
            COMMAND_LONG,
            struct.pack("<7fHBBB", *values, command, self._require(), self.component, 0),
        )

    def vehicle_state(self, timeout: float = 4.0) -> tuple[bool | None, bool | None]:
        """(armed, landed) from a fresh heartbeat and the extended system state."""

        # Only frames that arrive from now on count.
        self._pending.clear()
        self.armed = None
        landed: bool | None = None
        deadline = self._clock() + timeout
        while self._clock() < deadline and (self.armed is None or landed is None):
            self._command(MAV_CMD_REQUEST_MESSAGE, float(EXTENDED_SYS_STATE))
            frame = self._receive(
                min(1.0, max(deadline - self._clock(), 0.0)),
                lambda frame: frame.message_id == EXTENDED_SYS_STATE,
            )
            if frame is not None:
                landed = frame.payload[1] == MAV_LANDED_STATE_ON_GROUND
        return self.armed, landed

    def reboot(self) -> None:
        """Ask PX4 to reboot; the link drops, so no acknowledgement is required."""

        self._command(MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 1.0)
        self._receive(1.0, lambda frame: frame.message_id == COMMAND_ACK)
