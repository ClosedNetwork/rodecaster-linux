"""Per-channel processing block (Cmd.CHANNEL_FX, 48 bytes).

Field order and sizes come from the macOS app's "reset to default" routine,
which fills the block field-by-field; names come from the app's effects-preset
key table (same order, 36 entries). Units/scaling are NOT confirmed — the GUI
shows raw values and marks them as such. See PROTOCOL.md.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

# (name, struct format char, default) in wire order. 'b'=u8, 'h'=i16
FIELDS: list[tuple[str, str, int]] = [
    ("EqCutoff", "B", 60),
    ("GateThreshold", "h", -522),
    ("GateAttack", "h", 1585),
    ("GateRelease", "h", 70),
    ("GateDecay", "h", 160),
    ("GateRange", "h", 137),
    ("GateHysteresis", "B", 0),
    ("DeesserThreshold", "h", -390),
    ("DeesserRatio", "B", 24),
    ("DeesserAttack", "B", 16),
    ("DeesserRelease", "h", 50),
    ("DeesserGain", "B", 0),
    ("DeesserFrequency", "h", 5856),
    ("CompThreshold", "h", -192),
    ("CompRatio", "B", 20),
    ("CompAttack", "B", 2),
    ("CompRelease", "h", 220),
    ("CompGain", "B", 45),
    ("CompKnee", "B", 0),
    ("ExciterTune", "h", 3631),
    ("ExciterHarmonics", "B", 60),
    ("ExciterMix", "B", 50),
    ("BBDrive", "B", 60),
    ("BBTune", "B", 100),
    ("BBMix", "B", 30),
    ("DynamicsEnable", "B", 1),
    ("HPFilterEnable", "B", 1),
    ("NoiseGateEnable", "B", 1),
    ("DeesserEnable", "B", 1),
    ("AuralExciterEnable", "B", 1),
    ("BigBottomEnable", "B", 1),
    ("AllProcessingEnable", "B", 1),
    ("ReverbSendLevel", "B", 0),
    ("ReverbModel", "B", 0),
    ("ReverbDecay", "B", 0),
    ("ReverbCutoff", "B", 0),
]

FMT = "<" + "".join(f for _, f, _ in FIELDS)
SIZE = struct.calcsize(FMT)  # 47; the wire block is 48 with one trailing byte
BLOCK_LEN = 48
assert SIZE == 47, SIZE

ENABLE_FIELDS = [n for n, _, _ in FIELDS if n.endswith("Enable")]

GROUPS = {
    "High-pass filter": ["HPFilterEnable", "EqCutoff"],
    "Noise gate": ["NoiseGateEnable", "GateThreshold", "GateAttack", "GateRelease",
                   "GateDecay", "GateRange", "GateHysteresis"],
    "De-esser": ["DeesserEnable", "DeesserThreshold", "DeesserRatio", "DeesserAttack",
                 "DeesserRelease", "DeesserGain", "DeesserFrequency"],
    "Compressor": ["DynamicsEnable", "CompThreshold", "CompRatio", "CompAttack",
                   "CompRelease", "CompGain", "CompKnee"],
    "APHEX Aural Exciter": ["AuralExciterEnable", "ExciterTune", "ExciterHarmonics", "ExciterMix"],
    "APHEX Big Bottom": ["BigBottomEnable", "BBDrive", "BBTune", "BBMix"],
    "Reverb": ["ReverbSendLevel", "ReverbModel", "ReverbDecay", "ReverbCutoff"],
    "Master": ["AllProcessingEnable"],
}


def field_range(name: str) -> tuple[int, int]:
    fmt = next(f for n, f, _ in FIELDS if n == name)
    if name.endswith("Enable"):
        return 0, 1
    return (0, 255) if fmt == "B" else (-32768, 32767)


@dataclass
class ChannelFx:
    values: dict[str, int]
    trailer: int = 0

    @classmethod
    def defaults(cls) -> "ChannelFx":
        return cls({n: d for n, _, d in FIELDS})

    @classmethod
    def unpack(cls, block: bytes) -> "ChannelFx":
        if len(block) < BLOCK_LEN:
            raise ValueError("effects block too short")
        vals = struct.unpack_from(FMT, block, 0)
        return cls(dict(zip((n for n, _, _ in FIELDS), vals)), trailer=block[47])

    def pack(self) -> bytes:
        vals = []
        for n, _, _ in FIELDS:
            lo, hi = field_range(n)
            vals.append(max(lo, min(hi, int(self.values[n]))))
        return struct.pack(FMT, *vals) + bytes([self.trailer & 0xFF])

    def to_json(self) -> str:
        return json.dumps({"format": "rodecaster-linux-fx/1", "values": self.values}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ChannelFx":
        obj = json.loads(text)
        vals = obj.get("values", obj)
        base = cls.defaults()
        for n in base.values:
            if n in vals:
                base.values[n] = int(vals[n])
        return base
