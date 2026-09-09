"""The ACiQ / Midea 0xB2 IR protocol, as a function rather than a table.

Single source of truth for the encoding. The repo's analysis tools import this module, and
`deploy.py` copies the whole component directory to the Home Assistant host, so there is
exactly one definition of the protocol in existence.

Everything here was derived from this unit's own remote across five capture rounds, not from
a published table — several mutually incompatible tables for the 0xB2 Midea variant are in
circulation. `midea.py` reproduces 26 of 27 captures bit-for-bit, whole transmission
included, and must stay there.

A press is THREE frames: the state frame twice, then a trailer. Each is 48 bits with a
4590/4590 header; the state frames are complement-paired, the trailer is not.

    STATE    B2 <fan band> <temp|mode>        x2
    TRAILER  D5 <fan %> <half> <unit> 00 <checksum>

    COMMAND  B9 F5 <code>                     x2, no trailer (turbo, swing)
    OFF      B2 7B E0                         x2, no trailer

**The trailer is not optional.** It carries three things the state frame cannot:

- **The exact fan percentage**, 1-100 as a literal byte. The state frame carries only a
  4-value band, so 1% and 20% are identical there, as are 37/40 and 80/100.
- **The half degree.** The wire is Celsius on a 0.5 grid; the nibble holds the whole degree
  and byte2 bit 0x20 holds the half. 84 F and 85 F share nibble 0xA and differ only here.
- **The display unit.** byte3 bit 0 set means Fahrenheit. Established by a controlled A/B/A
  on one unchanged setpoint: 0x01 with the handset showing F, 0x00 showing C, 0x01 again
  after toggling back. Sending only the state frames leaves this bit unsent, and the unit
  reverts to a Celsius display — which is exactly what happened here on 2026-09-06.

Deliberately NOT modelled:

- **ECO/GEAR.** Four presses of the cycling button produced two alternating codes
  (B9 F5 24/25), so no frame identifies the resulting mode. Anything claiming to set
  "GEAR 50%" would be lying about what it does.
- **Trailer byte3 bit 4.** cool_60 sent 0x11 rather than 0x01. 60 F clamps to 17 C, but so
  does 62 F, which sent 0x01 — so "clamped" is not the trigger. One sample, unexplained,
  left as the single known validation failure rather than guessed at.
"""

from __future__ import annotations

import base64

# --- wire timings (microseconds) ------------------------------------------------------

HEADER_MARK = 4590
HEADER_SPACE = 4590
BIT_MARK = 541
ZERO_SPACE = 541
ONE_SPACE = 1613
GAP = 4590
REPEATS = 2
CARRIER_HZ = 38000

# --- tables ---------------------------------------------------------------------------

MIN_TEMP_C = 17
MAX_TEMP_C = 30

# Standard Midea Gray code for 17-30 C. Confirmed on this unit at 20 C (68 F -> 0x2),
# 22 C (72 F -> 0x7), 23 C (73/74 F -> 0x5), 29 C (85 F -> 0xA) and 30 C (86 F -> 0xB).
TEMP_GRAY = (0x0, 0x1, 0x3, 0x2, 0x6, 0x7, 0x5, 0x4, 0xC, 0xD, 0x9, 0x8, 0xA, 0xB)

# byte1 = (fan << 5) | 0x1F. The state frame carries a BAND, not a speed: 1% and 20% both
# send 0b111, 37% and 40% both send 0b100, 80% and 100% both send 0b001. The trailer
# carries the exact percentage, which is the only place the two ends of a band differ.
FAN_AUTO_BITS = 0b101
FAN_BANDS = ((20, 0b111), (40, 0b100), (60, 0b010), (100, 0b001))
# What the entity offers. Any 1-100 encodes; these are the rungs the handset steps through.
FAN_MODES = ("auto", "20", "40", "60", "80", "100")

# AUTO and DRY lock the fan; the remote emits 000 in those modes whatever the fan button says.
FAN_LOCKED = 0b000
FAN_LOCKED_MODES = frozenset({"auto", "dry"})

MODE_NIBBLE = {"cool": 0x0, "dry": 0x4, "auto": 0x8, "heat": 0xC}

# FAN-only reuses DRY's mode nibble and is marked by a sentinel temperature nibble, which
# fits the manual: no temperature is settable in FAN mode.
FAN_ONLY_TEMP_NIBBLE = 0xE
FAN_ONLY_MODE_NIBBLE = 0x4

STATE_PREFIX = 0xB2
POWER_OFF = (0xB2, 0x7B, 0xE0)

COMMAND_PREFIX = (0xB9, 0xF5)
COMMANDS = {"turbo_on": 0x01, "turbo_off": 0x02, "swing_on": 0x04, "swing_off": 0x05}

# --- the trailer frame ------------------------------------------------------------------
#
# D5 <fan%> <half> <byte3> 00 <checksum>, six plain bytes with checksum = sum of the
# first five. Not complement-paired, which is why it fails the check the state frames pass.
TRAILER_PREFIX = 0xD5
TRAILER_FAN_AUTO = 0x66      # 102 - fan on auto
TRAILER_FAN_LOCKED = 0x65    # 101 - AUTO and DRY, where the fan cannot be set
HALF_DEGREE_FLAG = 0x20      # byte2: the setpoint is X.5 C, not X.0
# byte3 bit 0 is the DISPLAY UNIT, established by a controlled A/B/A: the same Cool 72
# fan-100 press sent 0x01 with the handset showing Fahrenheit, 0x00 showing Celsius, and
# 0x01 again after toggling back. The main frame was byte-identical across all three.
# Omitting this frame is therefore why the unit reverted to a Celsius display.
FAHRENHEIT_FLAG = 0x01
# cool_60 additionally set bit 4 (0x11). 60 F clamps to 17 C, but so does 62 F, which sent
# 0x01 - so "clamped" is not the trigger. One sample, still unexplained, not generated.
TRAILER_BYTE3_UNKNOWN = 0x10

Frame = tuple[int, int, int]


class ProtocolError(ValueError):
    """A state that this air conditioner cannot express."""


# --- temperature ------------------------------------------------------------------------


def fahrenheit_to_half_celsius(temp_f: float) -> float:
    """Fahrenheit -> Celsius on the half-degree grid the remote actually uses, clamped.

    The state frame's nibble carries whole degrees and the trailer's 0x20 bit carries the
    half, which is how 74 F (23.5 C) is distinguished from 73 F (23.0 C) despite both
    landing on nibble 5.

    Clamped because the remote clamps: its display reaches 60 F but a captured 60 F press
    transmits 17 C, byte-identical to 62 F. There is no 16 C code on the wire despite the
    manual quoting a 16 C minimum, so the bottom Fahrenheit steps are cosmetic.
    """
    exact = round((temp_f - 32) * 5 / 9 * 2) / 2
    return max(float(MIN_TEMP_C), min(float(MAX_TEMP_C), exact))


def fahrenheit_to_celsius(temp_f: float) -> int:
    """The whole-degree part, which is what the state frame's nibble encodes."""
    return int(fahrenheit_to_half_celsius(temp_f))


def has_half_degree(temp_f: float) -> bool:
    value = fahrenheit_to_half_celsius(temp_f)
    return value - int(value) >= 0.5


def celsius_to_fahrenheit(temp_c: int) -> int:
    """The Fahrenheit value the remote displays for a given code."""
    return round(temp_c * 9 / 5 + 32)


MIN_TEMP_F = celsius_to_fahrenheit(MIN_TEMP_C)
MAX_TEMP_F = celsius_to_fahrenheit(MAX_TEMP_C)


# --- frames -----------------------------------------------------------------------------


def fan_percent(fan: str) -> int:
    """Validate a fan setting expressed as a percentage string."""
    try:
        percent = int(fan)
    except (TypeError, ValueError):
        raise ProtocolError(f"fan {fan!r} is neither 'auto' nor a percentage") from None
    if not 1 <= percent <= 100:
        raise ProtocolError(f"fan {percent}% outside 1-100")
    return percent


def fan_state_bits(fan: str) -> int:
    """The coarse 3-bit band the state frame carries."""
    if fan == "auto":
        return FAN_AUTO_BITS
    percent = fan_percent(fan)
    for top, bits in FAN_BANDS:
        if percent <= top:
            return bits
    raise ProtocolError(f"fan {percent}% outside 1-100")


def state_frame(mode: str, temp_c: int, fan: str = "auto") -> Frame:
    """A full-state frame. `mode` is a key of MODE_NIBBLE; use fan_only_frame for FAN."""
    if mode not in MODE_NIBBLE:
        raise ProtocolError(f"unknown mode {mode!r}; known: {sorted(MODE_NIBBLE)}")
    if not MIN_TEMP_C <= temp_c <= MAX_TEMP_C:
        raise ProtocolError(f"{temp_c}C outside {MIN_TEMP_C}-{MAX_TEMP_C}")

    bits = FAN_LOCKED if mode in FAN_LOCKED_MODES else fan_state_bits(fan)
    byte1 = (bits << 5) | 0x1F
    byte2 = (TEMP_GRAY[temp_c - MIN_TEMP_C] << 4) | MODE_NIBBLE[mode]
    return (STATE_PREFIX, byte1, byte2)


def fan_only_frame(fan: str = "auto") -> Frame:
    byte1 = (fan_state_bits(fan) << 5) | 0x1F
    return (STATE_PREFIX, byte1, (FAN_ONLY_TEMP_NIBBLE << 4) | FAN_ONLY_MODE_NIBBLE)


def command_frame(name: str) -> Frame:
    if name not in COMMANDS:
        raise ProtocolError(f"unknown command {name!r}; known: {sorted(COMMANDS)}")
    return (*COMMAND_PREFIX, COMMANDS[name])


def trailer_frame(
    mode: str,
    temp_f: float | None,
    fan: str = "auto",
    fahrenheit: bool = True,
) -> tuple[int, ...]:
    """The D5 frame: exact fan percentage, the half-degree bit, and a checksum.

    `byte1` is a literal percentage — 20 encodes as 0x14, not as an index — which is where
    the manual's 1% fan resolution lives. It is also what separates 80% from 100%: the state
    frame's 3-bit fan field gives both the same value, so without this frame they are
    genuinely indistinguishable.
    """
    if mode in FAN_LOCKED_MODES:
        fan_byte = TRAILER_FAN_LOCKED
    elif fan == "auto":
        fan_byte = TRAILER_FAN_AUTO
    else:
        fan_byte = fan_percent(fan)

    half = HALF_DEGREE_FLAG if (temp_f is not None and has_half_degree(temp_f)) else 0x00
    unit = FAHRENHEIT_FLAG if fahrenheit else 0x00
    body = (TRAILER_PREFIX, fan_byte, half, unit, 0x00)
    return (*body, sum(body) & 0xFF)


def frame_for(mode: str, temp_f: float | None = None, fan: str = "auto") -> Frame:
    """The frame for a Home Assistant hvac mode, in Fahrenheit."""
    if mode == "off":
        return POWER_OFF
    if mode == "fan_only":
        return fan_only_frame(fan)
    if temp_f is None:
        raise ProtocolError(f"mode {mode!r} needs a temperature")
    return state_frame(mode, fahrenheit_to_celsius(temp_f), fan)


# --- wire encoding ------------------------------------------------------------------------


def _message_bytes(payload: tuple[int, ...]) -> list[int]:
    """The six bytes that actually go on the wire.

    A 3-byte payload is complement-paired; the 6-byte trailer is sent as-is with its own
    checksum instead, which is why it never satisfies the complement check.
    """
    if len(payload) == 6:
        return list(payload)
    expanded: list[int] = []
    for byte in payload:
        expanded.append(byte)
        expanded.append(~byte & 0xFF)
    return expanded


def _one_frame(payload: tuple[int, ...]) -> list[int]:
    out = [HEADER_MARK, HEADER_SPACE]
    for byte in _message_bytes(payload):
        for shift in range(7, -1, -1):
            out.append(BIT_MARK)
            out.append(ONE_SPACE if (byte >> shift) & 1 else ZERO_SPACE)
    out.append(BIT_MARK)
    return out


def frames_to_timings(payloads: list[tuple[int, ...]]) -> list[int]:
    """Concatenate whole frames, header and gap included, as the remote transmits them."""
    out: list[int] = []
    for index, payload in enumerate(payloads):
        if index:
            out.append(GAP)
        out += _one_frame(payload)
    return out


def frame_to_timings(payload: Frame, repeats: int = REPEATS) -> list[int]:
    """One payload, repeated. No trailer — see transmission() for a full remote press."""
    return frames_to_timings([payload] * repeats)


def transmission(
    payload: Frame, trailer: tuple[int, ...] | None = None
) -> list[int]:
    """A complete press: the state frame twice, then the trailer if the frame has one.

    Power-off and the B9 command frames carry no trailer — measured, and they demonstrably
    work without one.
    """
    payloads: list[tuple[int, ...]] = [payload] * REPEATS
    if trailer is not None:
        payloads.append(trailer)
    return frames_to_timings(payloads)


# FastLZ level 1 encodes a literal run as one control byte (len-1) for len in 1..32.
_MAX_LITERAL_RUN = 32


def fastlz_compress_literal(data: bytes) -> bytes:
    """Valid FastLZ level-1 output using literal runs only.

    Larger than the blaster's own encoding, which uses back-references, but it decompresses
    identically and needs no match finder. The transfer is chunked over cluster 0xED00, so
    length is not a practical constraint.
    """
    out = bytearray()
    for start in range(0, len(data), _MAX_LITERAL_RUN):
        chunk = data[start : start + _MAX_LITERAL_RUN]
        out.append(len(chunk) - 1)
        out += chunk
    return bytes(out)


def encode_timings(values: list[int]) -> str:
    raw = bytearray()
    for value in values:
        if not 0 <= value <= 0xFFFF:
            raise ProtocolError(f"timing {value} does not fit in uint16")
        raw += int(value).to_bytes(2, "little")
    return base64.b64encode(fastlz_compress_literal(bytes(raw))).decode()


def build(payload: Frame) -> str:
    """Payload bytes -> the Zosung base64 string an IRSend takes verbatim. No trailer."""
    return encode_timings(frame_to_timings(payload))


def code_for(
    mode: str,
    temp_f: float | None = None,
    fan: str = "auto",
    fahrenheit: bool = True,
) -> str:
    """A complete press, trailer included — what the remote itself sends.

    `fahrenheit` sets the unit's DISPLAY, not the encoding: the wire is always Celsius.
    Leaving it True is what keeps the unit showing Fahrenheit.
    """
    payload = frame_for(mode, temp_f, fan)
    trailer = (
        None if payload == POWER_OFF
        else trailer_frame(mode, temp_f, fan, fahrenheit)
    )
    return encode_timings(transmission(payload, trailer))


def command_code(name: str) -> str:
    return build(command_frame(name))
