# Midea IR over ZHA

A Home Assistant `climate` entity for **ACiQ / Blueridge / Midea** mini-splits driven through
a **Zigbee IR blaster on ZHA** — a Tuya TS1201 (ZS06 / UFO-R11 family).

No code table. Every IR frame is computed from the protocol at send time, because the
device's entire state space is a function of (mode, temperature, fan). The tables are
validated against 27 codes captured from a physical handset; 26 of 27 reproduce the complete
transmission bit-for-bit.

**Sibling project:** [esphome_midea_b2](https://github.com/defl/esphome_midea_b2) is an ESPHome
external component for the same protocol, for an ESP32 with an IR LED and receiver. It also
decodes the handset, so its entity follows the unit when the remote is used. It is a separate,
independent implementation; nothing is shared between the two.

## Why this exists

SmartIR is the usual answer, and it has two problems here:

1. **It has no ZHA controller.** Zigbee blasters reached through ZHA cannot be used as
   SmartIR emitters at all. (A controller for it is offered upstream as
   [smartHomeHub/SmartIR#1595](https://github.com/smartHomeHub/SmartIR/pull/1595).)
2. **It is a lookup table.** A complete device file for this unit ran to 334 KB and 600
   entries — which were 174 distinct values of a three-argument function.

It also cannot express this unit's swing and turbo, which are separate command frames rather
than fields inside the state frame.

## Supported hardware

| | |
|---|---|
| Blaster | Tuya **TS1201** on ZHA, quirk `zhaquirks.tuya.ts1201:ZosungIRBlaster` |
| Air handler | Midea `0xB2` protocol — verified on ACiQ 9K/12K console, remote `RG10R(M2S)/BGEFU1` |

The same handset ships on Blueridge XS2A and several other Midea rebrands, so those are
likely to work. **This is not the protocol ESPHome's `midea_ir` speaks** — that is a 6-byte,
plain-sequential `0xA1` frame with a checksum; this is a 3-byte, complement-paired `0xB2`
frame with none. They share only the header timing, which makes them easy to confuse and
impossible to substitute.

## Installation

HACS → ⋮ → Custom repositories → add `https://github.com/defl/zha-midea-ir`, category
**Integration**. Install, then add to `configuration.yaml`:

```yaml
climate:
  - platform: zha_midea_ir
    name: Garage AC
    unique_id: garage_ac
    ieee: "xx:xx:xx:xx:xx:xx:xx:xx"
    temperature_sensor: sensor.garage_temperature   # optional
    humidity_sensor: sensor.garage_humidity         # optional
```

Restart Home Assistant. `endpoint_id` (1) and `cluster_id` (`0xE004`) default correctly for a
TS1201 and only need setting for other hardware.

Find the blaster's IEEE in Settings → Devices → your blaster → Device info.

## What you get

- **Modes** — off, cool, heat, dry, auto, fan_only
- **Temperature** — 63–86 °F / 17–30 °C, in whichever unit Home Assistant is set to
- **Fan** — auto, 20, 40, 60, 80, 100 (the protocol carries 1–100%, so any value encodes)
- **Swing** — as a real `swing_mode`
- **Turbo** — as a `preset_mode`

## Known limitations

- **IR is one-way.** The entity's state is what was last *sent*, not what the unit is doing.
  Using the handset desynchronises it silently. State is restored across restarts rather than
  assumed off, but a restored state has not been transmitted.
- **ECO/GEAR is not supported.** Four presses of that cycling button produce only two
  alternating codes, so no frame identifies which of its four states you land on. Rather than
  ship a control that misreports itself, it is absent.
- **The Fahrenheit range is wider than the wire.** The handset displays down to 60 °F but
  transmits 17 °C for 60, 61 and 62 alike; there is no 16 °C code despite the manual quoting
  a 16 °C minimum.

## Protocol notes

A press is **three** frames: the state frame twice, then a trailer.

```
STATE    B2 <fan band> <temp|mode>          x2, complement-paired
TRAILER  D5 <fan %> <half> <unit> 00 <cs>   once, plain bytes + checksum

COMMAND  B9 F5 <code>                       x2, no trailer (turbo, swing)
OFF      B2 7B E0                           x2, no trailer
```

The trailer is **not optional** and is easy to mistake for noise. It carries three things the
state frame cannot express:

- **Exact fan percentage**, 1–100 as a literal byte. The state frame holds only a 4-value
  band, so 1% and 20% are identical there, as are 37/40 and 80/100.
- **The half degree.** The wire is Celsius on a 0.5 grid: the nibble holds the whole degree,
  trailer byte2 bit `0x20` the half. 84 °F and 85 °F share nibble `0xA` and differ only here.
- **The display unit.** byte3 bit 0 set = Fahrenheit. Send only the state frames and the unit
  reverts to a Celsius display.

Carrier 38 kHz. Header 4590/4590 µs, bit mark 541, `0` space 541, `1` space 1613, gap 4590.

Codes go to `IRSend` (command 2 on cluster `0xE004`) as **bare base64**. The ZHA quirk builds
its own `{"key_num":1,…,"key_code":…}` envelope; pre-wrapping nests one inside the other and
the blaster then transmits nothing at all — no error, no LED, no IR.

See [`protocol.py`](custom_components/zha_midea_ir/protocol.py) for the tables.

## Adapting to another Midea unit

The `0xB2` family has several mutually incompatible published tables, so derive yours from
your own handset rather than trusting one. Capture codes with the blaster's `IRLearn`
(cluster `0xE004`, command 1) and read them back from the `last_learned_ir_code` attribute —
**the quirk fires no event**, so waiting on the event bus captures nothing however long you
wait. Then diff captures that differ in one variable at a time.

## License

MIT
