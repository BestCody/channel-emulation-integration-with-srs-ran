#!/usr/bin/env python3

import json
import math
import struct
from dataclasses import dataclass


PROTOCOL_VERSION = 3
# Dense CIR limits match the ring buffer.
MAX_CHANNEL_LEN = 1024
MAX_TAPS = MAX_CHANNEL_LEN
MAX_DELAY = MAX_CHANNEL_LEN - 1
MAX_MESSAGE_BYTES = 1024 * 1024
NOISE_SIGMA_MAX = 512.0
VALID_DIRECTIONS = {"both", "downlink", "uplink"}
# UE index 0 targets all UEs.
MAX_UES = 64
_FRAME_MAGIC = b"SCIR"
_FRAME_HEADER = struct.Struct("<4sBBBBQQdI")
_FRAME_TAP = struct.Struct("<Idd")
_MSG_CHANNEL_UPDATE = 1
_DIRECTION_CODES = {"both": 0, "downlink": 1, "uplink": 2}
_DIRECTION_NAMES = {code: name for name, code in _DIRECTION_CODES.items()}


@dataclass(frozen=True)
class Tap:
    delay: int
    coefficient: complex


@dataclass(frozen=True)
class ChannelUpdate:
    sequence: int
    direction: str
    taps: tuple
    client_send_ns: int
    noise_sigma: float
    ue_index: int


def strict_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def strict_noise_sigma(value):
    sigma = float(value)
    if not math.isfinite(sigma):
        raise ValueError("noise_sigma must be finite")
    if sigma < 0.0 or sigma > NOISE_SIGMA_MAX:
        raise ValueError(f"noise_sigma must be in 0..{NOISE_SIGMA_MAX}")
    return sigma


def validate_taps(taps):
    if not taps:
        raise ValueError("at least one tap is required")

    combined = {}
    for index, tap in enumerate(taps):
        if not isinstance(tap, Tap):
            raise ValueError(f"tap {index} is invalid")
        delay = strict_integer(tap.delay, f"tap {index} delay")
        if delay < 0 or delay > MAX_DELAY:
            raise ValueError(
                f"tap delay {delay} is outside the allowed range "
                f"0..{MAX_DELAY}"
            )
        coefficient = complex(tap.coefficient)
        if not (
            math.isfinite(coefficient.real)
            and math.isfinite(coefficient.imag)
        ):
            raise ValueError("tap coefficients must be finite")
        combined[delay] = combined.get(delay, 0.0j) + coefficient

    validated = tuple(
        Tap(delay=delay, coefficient=coefficient)
        for delay, coefficient in sorted(combined.items())
        if coefficient != 0.0j
    )
    if not validated:
        raise ValueError("combined taps cannot all be zero")
    if len(validated) > MAX_TAPS:
        raise ValueError(f"at most {MAX_TAPS} unique taps are supported")
    return validated


def tap_objects(raw_taps):
    if not isinstance(raw_taps, list) or not raw_taps:
        raise ValueError("taps must be a non-empty list")
    taps = []
    for index, raw in enumerate(raw_taps):
        if not isinstance(raw, dict):
            raise ValueError(f"tap {index} must be an object")
        if set(raw) != {"delay", "real", "imag"}:
            raise ValueError(
                f"tap {index} must contain delay, real, and imag"
            )
        delay = strict_integer(raw["delay"], f"tap {index} delay")
        real = float(raw["real"])
        imag = float(raw["imag"])
        taps.append(Tap(delay, complex(real, imag)))
    return validate_taps(taps)


def parse_update(message):
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    if message.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if message.get("msg_type") != "channel_update":
        raise ValueError("message type must be channel_update")

    sequence = strict_integer(message.get("sequence"), "sequence")
    if sequence < 0 or sequence > (1 << 64) - 1:
        raise ValueError("sequence is outside uint64 range")
    direction = message.get("direction")
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"invalid direction: {direction}")
    client_send_ns = strict_integer(
        message.get("client_send_ns"),
        "client_send_ns",
    )
    if client_send_ns < 0:
        raise ValueError("client_send_ns cannot be negative")
    noise_sigma = strict_noise_sigma(message.get("noise_sigma"))
    ue_index = strict_integer(message.get("ue_index"), "ue_index")
    if ue_index < 0 or ue_index > MAX_UES:
        raise ValueError(f"ue_index must be in 0..{MAX_UES}")
    allowed = {
        "version",
        "msg_type",
        "sequence",
        "direction",
        "taps",
        "client_send_ns",
        "noise_sigma",
        "ue_index",
    }
    unknown = set(message) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    return ChannelUpdate(
        sequence=sequence,
        direction=direction,
        taps=tap_objects(message.get("taps")),
        client_send_ns=client_send_ns,
        noise_sigma=noise_sigma,
        ue_index=ue_index,
    )


def build_update(
    taps,
    sequence,
    direction="both",
    client_send_ns=0,
    noise_sigma=0.0,
    ue_index=0,
):
    taps = validate_taps(tuple(taps))
    message = {
        "version": PROTOCOL_VERSION,
        "msg_type": "channel_update",
        "sequence": strict_integer(sequence, "sequence"),
        "direction": direction,
        "noise_sigma": strict_noise_sigma(noise_sigma),
        "taps": [
            {
                "delay": tap.delay,
                "real": tap.coefficient.real,
                "imag": tap.coefficient.imag,
            }
            for tap in taps
        ],
        "client_send_ns": strict_integer(client_send_ns, "client_send_ns"),
        "ue_index": strict_integer(ue_index, "ue_index"),
    }
    parse_update(message)
    return message


def _encode_update_frame(message):
    direction = message.get("direction")
    if direction not in _DIRECTION_CODES:
        raise ValueError(f"invalid direction: {direction}")
    taps = message["taps"]
    try:
        header = _FRAME_HEADER.pack(
            _FRAME_MAGIC,
            PROTOCOL_VERSION,
            _MSG_CHANNEL_UPDATE,
            _DIRECTION_CODES[direction],
            int(message["ue_index"]),
            int(message["sequence"]),
            int(message["client_send_ns"]),
            float(message["noise_sigma"]),
            len(taps),
        )
        body = b"".join(
            _FRAME_TAP.pack(int(tap["delay"]), float(tap["real"]), float(tap["imag"]))
            for tap in taps
        )
    except (struct.error, KeyError, TypeError) as error:
        raise ValueError(f"could not encode channel_update: {error}") from error
    return header + body


def _decode_update_frame(payload):
    if len(payload) < _FRAME_HEADER.size:
        raise ValueError("binary frame is too short")
    (
        magic,
        version,
        msg_type,
        direction_code,
        ue_index,
        sequence,
        client_send_ns,
        noise_sigma,
        count,
    ) = _FRAME_HEADER.unpack_from(payload)
    if magic != _FRAME_MAGIC:
        raise ValueError("bad frame magic")
    if version != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if msg_type != _MSG_CHANNEL_UPDATE:
        raise ValueError("unsupported binary message type")
    if direction_code not in _DIRECTION_NAMES:
        raise ValueError(f"invalid direction code: {direction_code}")
    if len(payload) != _FRAME_HEADER.size + count * _FRAME_TAP.size:
        raise ValueError("binary frame length mismatch")
    taps = []
    offset = _FRAME_HEADER.size
    for _ in range(count):
        delay, real, imag = _FRAME_TAP.unpack_from(payload, offset)
        taps.append({"delay": delay, "real": real, "imag": imag})
        offset += _FRAME_TAP.size
    return {
        "version": version,
        "msg_type": "channel_update",
        "sequence": sequence,
        "direction": _DIRECTION_NAMES[direction_code],
        "noise_sigma": noise_sigma,
        "taps": taps,
        "client_send_ns": client_send_ns,
        "ue_index": ue_index,
    }


def decode_message(payload):
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("payload must be bytes")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds maximum size")
    if payload[:4] == _FRAME_MAGIC:
        return _decode_update_frame(bytes(payload))

    def reject_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    message = json.loads(
        payload.decode("utf-8"),
        parse_constant=reject_constant,
    )
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message


def encode_message(message):
    if message.get("msg_type") == "channel_update":
        payload = _encode_update_frame(message)
    else:
        payload = json.dumps(
            message,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds maximum size")
    return payload
