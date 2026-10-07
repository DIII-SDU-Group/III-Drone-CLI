"""The dependency-free MAVLink client behind the USB fallback of `iii px4 param-baseline`."""

from __future__ import annotations

import struct

import pytest

from iii import px4_mavlink as mavlink

# Frames produced by pymavlink (common dialect, system 255, component 191, sequence 0).
GOLDEN = {
    "heartbeat": (
        mavlink.HEARTBEAT,
        struct.pack("<IBBBBB", 0, 6, 8, 0, 0, 3),
        "fd09000000ffbf000000000000000608000003b655",
    ),
    "param_request_read": (
        mavlink.PARAM_REQUEST_READ,
        struct.pack("<hBB16s", -1, 1, 1, b"EKF2_EV_CTRL"),
        "fd10000000ffbf140000ffff0101454b46325f45565f4354524c772f",
    ),
    "param_set": (
        mavlink.PARAM_SET,
        struct.pack("<4sBB16sB", struct.pack("<f", 1.5), 1, 1, b"EKF2_EV_CTRL", 9),
        "fd17000000ffbf1700000000c03f0101454b46325f45565f4354524c00000000093522",
    ),
    "reboot": (
        mavlink.COMMAND_LONG,
        struct.pack("<7fHBBB", 1, 0, 0, 0, 0, 0, 0, 246, 1, 1, 0),
        "fd20000000ffbf4c00000000803f000000000000000000000000000000000000000000000000f60001015172",
    ),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_frames_match_the_reference_encoding(name):
    message_id, payload, expected = GOLDEN[name]
    assert mavlink.encode(message_id, payload).hex() == expected


def test_the_decoder_resynchronizes_and_restores_truncated_payloads():
    message_id, payload, expected = GOLDEN["reboot"]
    stream = b"\x00\x11" + bytes.fromhex(expected) + bytes.fromhex(GOLDEN["heartbeat"][2])
    decoder = mavlink.Decoder()
    frames = decoder.feed(stream[:15]) + decoder.feed(stream[15:])
    assert [frame.message_id for frame in frames] == [mavlink.COMMAND_LONG, mavlink.HEARTBEAT]
    # MAVLink 2 truncated the trailing zero bytes on the wire.
    assert frames[0].payload == payload and (frames[0].system, frames[0].component) == (255, 191)

    corrupted = bytearray(bytes.fromhex(expected))
    corrupted[12] ^= 0xFF
    assert mavlink.Decoder().feed(bytes(corrupted)) == []


class _Px4:
    """The flight controller's end of the link, speaking through the same codec."""

    def __init__(self, parameters, *, armed=False, landed=True, silent=False):
        self.parameters = dict(parameters)
        self.armed, self.landed, self.silent = armed, landed, silent
        self.rebooted = False
        self._decoder = mavlink.Decoder()
        self._out = bytearray()

    def _send(self, message_id, payload):
        self._out += mavlink.encode(message_id, payload, system=1, component=1)

    def _value(self, name):
        value = self.parameters[name]
        raw, kind = (
            (struct.pack("<f", value), mavlink.MAV_PARAM_TYPE_REAL32)
            if isinstance(value, float)
            else (struct.pack("<i", value), mavlink.MAV_PARAM_TYPE_INT32)
        )
        self._send(mavlink.PARAM_VALUE, struct.pack("<4sHH16sB", raw, 900, 0, name.encode(), kind))

    def write(self, data):
        for frame in self._decoder.feed(data):
            if self.silent:
                continue
            if frame.message_id == mavlink.HEARTBEAT:
                base_mode = 128 if self.armed else 0
                self._send(mavlink.HEARTBEAT, struct.pack("<IBBBBB", 0, 2, 12, base_mode, 3, 3))
            elif frame.message_id == mavlink.PARAM_REQUEST_READ:
                name = frame.payload[4:20].rstrip(b"\x00").decode()
                if name in self.parameters:
                    self._value(name)
            elif frame.message_id == mavlink.PARAM_SET:
                raw, _, _, identifier, kind = struct.unpack("<4sBB16sB", frame.payload)
                name = identifier.rstrip(b"\x00").decode()
                self.parameters[name] = struct.unpack(
                    "<f" if kind == mavlink.MAV_PARAM_TYPE_REAL32 else "<i", raw
                )[0]
                self._value(name)
            elif frame.message_id == mavlink.COMMAND_LONG:
                command = struct.unpack("<7fHBBB", frame.payload)[7]
                if command == mavlink.MAV_CMD_REQUEST_MESSAGE:
                    self._send(
                        mavlink.EXTENDED_SYS_STATE, bytes([0, 1 if self.landed else 2])
                    )
                elif command == mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN:
                    self.rebooted = True
                    self._send(mavlink.COMMAND_ACK, struct.pack("<HB", command, 0))

    def read(self, timeout):
        data, self._out = bytes(self._out), bytearray()
        return data

    def close(self):
        pass


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.05
        return self.now


def _link(px4):
    link = mavlink.Px4Link(px4, clock=_Clock())
    link.connect()
    return link


def test_parameters_are_read_and_written_with_px4s_bytewise_types():
    px4 = _Px4({"EKF2_EV_CTRL": 0, "EKF2_EV_DELAY": 0.0})
    link = _link(px4)
    assert link.system == 1
    assert link.read_parameters(["EKF2_EV_CTRL", "EKF2_EV_DELAY", "NO_SUCH_PARAM"]) == {
        "EKF2_EV_CTRL": 0,
        "EKF2_EV_DELAY": 0.0,
        "NO_SUCH_PARAM": None,
    }
    assert isinstance(link.read_parameter("EKF2_EV_CTRL"), int)
    link.write_parameter("EKF2_EV_CTRL", 11)
    link.write_parameter("EKF2_EV_DELAY", 30.0)
    assert px4.parameters == {"EKF2_EV_CTRL": 11, "EKF2_EV_DELAY": 30.0}
    assert isinstance(px4.parameters["EKF2_EV_DELAY"], float)


@pytest.mark.parametrize(
    ("armed", "landed"), [(False, True), (True, True), (False, False)]
)
def test_the_vehicle_state_comes_from_the_heartbeat_and_the_landed_state(armed, landed):
    assert _link(_Px4({}, armed=armed, landed=landed)).vehicle_state() == (armed, landed)


def test_reboot_is_commanded_and_a_silent_port_fails_loudly():
    px4 = _Px4({})
    _link(px4).reboot()
    assert px4.rebooted
    with pytest.raises(mavlink.MavlinkError, match="no PX4 heartbeat"):
        _link(_Px4({}, silent=True))
