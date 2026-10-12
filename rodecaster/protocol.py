"""Wire protocol for the original RØDECaster Pro (gen 1) companion HID interface.

Everything in this module was reconstructed by static analysis of the macOS
"RODECaster Pro" companion app v2.1.2 (see PROTOCOL.md for the evidence trail).
It is pure: it only builds and parses byte strings, so it can be unit-tested
without hardware.

Report map (report IDs as seen by the host):

    OUT 2  ( 9 bytes)  simple command:   [cmd, 0, 0, 0, 0, 0, 0, 0, 0]
    IN  1  ( 9 bytes)  simple response:  [cmd, status('A'/'E'/'N'), v_lo, v_hi, u32 ...]
    OUT 5  (64 bytes)  command header or 64-byte command payload
    IN  4  (64 bytes)  command ack / command response payload
    OUT 3  (2048 bytes) bulk data host -> device (audio upload, settings restore)
    IN  6  (2048 bytes) bulk data device -> host (audio download, settings backup)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

VENDOR_ID = 0x19F7

PID_NORMAL = 0x0011        # RØDECaster Pro in normal operation
PID_ALT = 0x0012           # alternate mode accepted by the app (seen with transfer mode)
PID_TRANSFER = 0x0019      # USB transfer mode: microSD card exposed as mass storage
                           # (device reports "RODECaster Pro USB Transfer Mode")
KNOWN_PIDS = {PID_NORMAL: "normal", PID_ALT: "alternate", PID_TRANSFER: "USB transfer mode"}

# Report IDs
RID_SIMPLE_OUT = 2
RID_SIMPLE_IN = 1
RID_CMD_OUT = 5
RID_CMD_IN = 4
RID_BULK_OUT = 3
RID_BULK_IN = 6

SIMPLE_LEN = 9
CMD_LEN = 64
BULK_LEN = 2048

CMD_MAGIC = bytes.fromhex("520eb03e")

DIR_WRITE = 0
DIR_READ = 1

GROUP_SYS = 1   # system actions (transfer mode, effects edit mode)
GROUP_DATA = 3  # pads, banks, effects, settings


class Simple(IntEnum):
    """Single-letter commands on report 2 (answered on report 1)."""
    VERSION = ord("V")
    IDENTIFY = ord("I")
    RESET = ord("R")
    # Bootloader-only (documented, deliberately not exposed by the app):
    ERASE_PAGE = ord("E")
    PROGRAM = ord("P")


class Cmd(IntEnum):
    """Group-3 data commands. The 4-byte selector is [cmd, arg1, arg2, arg3]."""
    PAD_COLOUR = 0x00       # rw  arg1=pad   payload [R, G, B]
    PAD_MODE = 0x01         # rw  arg1=pad   payload [mode]
    PAD_NAME = 0x02         # rw  arg1=pad   payload char[16]
    PAD_BEGIN_UPLOAD = 0x03 # w   arg1=pad arg2=channels  payload u32 frames + char[12] name
    PAD_SOUND_SIZE = 0x07   # r   arg1=pad   payload u32 bytes of PCM stored on the pad
    FREE_MEMORY = 0x08      # r              payload u32 bytes free for sounds
    PAD_DATA_LENGTH = 0x09  # r   arg1=pad   payload u32 bytes to download
    BANK_COUNT = 0x0C       # r              payload [banks_in_use - 1]
    PAD_CRC = 0x0D          # r   arg1=pad   payload u32 checksum of the pad audio
    BEGIN_DOWNLOAD = 0x0E   # r              then N x report-6 bulk chunks
    PAD_FORMAT = 0x0F       # r   arg1=pad   payload [channels, flag]
    BANK_NAME = 0x10        # rw  arg1=bank  payload char[16]
    CLEAR_BANK = 0x12       # w   arg1=bank
    SETTINGS_SIZE = 0x13    # r              payload u32 (0x400)
    SETTINGS = 0x14         # rw             then bulk transfer of SETTINGS_SIZE bytes
    SET_TIME = 0x15         # w              payload [h, m, s, day, month, year_lo, year_hi]
    ABORT_DOWNLOAD = 0x16   # w              sent when the user cancels a pad download
    CHANNEL_FX = 0x17       # rw  arg1=chan  payload 48-byte processing block
    STATUS = 0x18           # r              payload [flags]
    PAD_LOOP = 0x19         # rw  arg1=pad   payload [0/1]
    ENTER_BOOTLOADER = 0x1B # w              (firmware update only - never sent by this app)


class SysAction(IntEnum):
    """Group-1 actions; the 16-bit 'index' field selects the action."""
    ENTER_TRANSFER_MODE = 5
    LEAVE_TRANSFER_MODE = 6
    EFFECTS_EDIT_MODE = 7   # payload u32 0/1 (inferred from the effects editor toggle)


class PadMode(IntEnum):
    """Playback styles in the order the app lists them."""
    LATCH = 0
    PAUSE = 1
    REPLAY = 2
    PLAY = 3
    CENSOR = 4      # shown as "!@#*" (swear button)
    TRASH_TALK = 5


PAD_MODE_LABELS = {
    PadMode.LATCH: "Latch",
    PadMode.PAUSE: "Pause",
    PadMode.REPLAY: "Replay",
    PadMode.PLAY: "Play",
    PadMode.CENSOR: "!@#* (Censor)",
    PadMode.TRASH_TALK: "Trash-talk",
}

BANKS = 8
PADS_PER_BANK = 8
SAMPLE_RATE = 48000
PAD_NAME_LEN = 16
UPLOAD_NAME_LEN = 12

# Status flag bits returned by Cmd.STATUS
STATUS_EFFECTS_EDIT = 0x01
STATUS_CHANGED = 0x02
STATUS_BIT2 = 0x04

FX_CHANNELS = {0: "Mic 1", 1: "Mic 2", 2: "Mic 3", 3: "Mic 4", 4: "Master"}


def fx_channel_wire(chan: int) -> int:
    """The app maps its 5th processing slot (master) to device channel 8."""
    return 8 if chan == 4 else chan


def pad_address(bank: int, pad: int) -> int:
    if not (0 <= bank < BANKS and 0 <= pad < PADS_PER_BANK):
        raise ValueError(f"bank/pad out of range: {bank}/{pad}")
    return (bank << 3) | pad


def split_pad_address(addr: int) -> tuple[int, int]:
    return (addr >> 3) & 7, addr & 7


class ProtocolError(Exception):
    pass


# --------------------------------------------------------------------------
# Simple commands (report 2 / report 1)
# --------------------------------------------------------------------------

def build_simple(cmd: int) -> bytes:
    """Full report including the report-ID byte (what hidraw expects)."""
    return bytes([RID_SIMPLE_OUT, cmd & 0xFF]) + bytes(SIMPLE_LEN - 1)


@dataclass
class SimpleReply:
    cmd: int
    status: str
    version: tuple[int, int, int] | None
    value: int

    @property
    def ok(self) -> bool:
        return self.status == "A"


def parse_simple_reply(data: bytes) -> SimpleReply:
    """`data` excludes the report ID."""
    if len(data) < 8:
        raise ProtocolError(f"short simple reply ({len(data)} bytes)")
    status = chr(data[1]) if 32 <= data[1] < 127 else f"0x{data[1]:02x}"
    version = None
    if status == "A":
        version = (data[3], data[2] >> 4, data[2] & 0x0F)
    value = struct.unpack_from("<I", data, 4)[0]
    return SimpleReply(cmd=data[0], status=status, version=version, value=value)


# --------------------------------------------------------------------------
# Command channel (report 5 / report 4)
# --------------------------------------------------------------------------

def selector(cmd: int, a1: int = 0, a2: int = 0, a3: int = 0) -> bytes:
    return bytes([cmd & 0xFF, a1 & 0xFF, a2 & 0xFF, a3 & 0xFF])


def build_header(direction: int, group: int, index: int, sel: bytes) -> bytes:
    """64-byte command header (without report ID)."""
    if len(sel) != 4:
        raise ValueError("selector must be 4 bytes")
    hdr = CMD_MAGIC + bytes([direction & 0xFF, group & 0xFF]) + struct.pack("<H", index) + sel
    return hdr.ljust(CMD_LEN, b"\0")


def parse_header(data: bytes) -> tuple[int, int, int, bytes]:
    if data[:4] != CMD_MAGIC:
        raise ProtocolError("bad command magic")
    direction, group = data[4], data[5]
    index = struct.unpack_from("<H", data, 6)[0]
    return direction, group, index, bytes(data[8:12])


def pad64(payload: bytes) -> bytes:
    if len(payload) > CMD_LEN:
        raise ValueError("payload longer than 64 bytes")
    return payload.ljust(CMD_LEN, b"\0")


def with_rid(rid: int, data: bytes) -> bytes:
    return bytes([rid]) + data


def encode_name(name: str, length: int) -> bytes:
    """Device names are NUL-terminated byte strings. Keep valid UTF-8."""
    raw = name.encode("utf-8")
    while len(raw) > length - 1:
        name = name[:-1]
        raw = name.encode("utf-8")
    return raw.ljust(length, b"\0")


def decode_name(data: bytes, length: int = PAD_NAME_LEN) -> str:
    raw = bytes(data[:length]).split(b"\0", 1)[0]
    return raw.decode("utf-8", errors="replace")


def build_begin_upload(frames: int, name: str) -> bytes:
    """u32 frames + up to 12 name bytes (strncpy semantics) + NUL at offset 16."""
    raw = name.encode("utf-8")
    while len(raw) > UPLOAD_NAME_LEN:
        name = name[:-1]
        raw = name.encode("utf-8")
    return pad64(struct.pack("<I", frames) + raw.ljust(UPLOAD_NAME_LEN, b"\0") + b"\0")


def build_time_payload(t) -> bytes:
    """`t` is a datetime/struct_time-like object with hour..year."""
    return pad64(bytes([t.hour, t.minute, t.second, t.day, t.month]) + struct.pack("<H", t.year))


def u32(data: bytes, off: int = 0) -> int:
    return struct.unpack_from("<I", data, off)[0]


# --------------------------------------------------------------------------
# Audio framing
# --------------------------------------------------------------------------

def frames_per_chunk(channels: int) -> int:
    return BULK_LEN // (channels * 2)


def chunk_pcm(pcm: bytes, channels: int):
    """Split interleaved s16le PCM into zero-padded 2048-byte bulk chunks."""
    step = frames_per_chunk(channels) * channels * 2
    for off in range(0, len(pcm), step):
        yield pcm[off:off + step].ljust(BULK_LEN, b"\0")


def wav_header(channels: int, data_len: int, rate: int = SAMPLE_RATE) -> bytes:
    block = channels * 2
    return (b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, 16)
            + b"data" + struct.pack("<I", data_len))


# --------------------------------------------------------------------------
# Settings backup file (.rcpsettings) — same layout the macOS app writes
# --------------------------------------------------------------------------

SETTINGS_MAGIC = b"RCP_SET[v1.0.0]\0"
SETTINGS_SIZE_EXPECTED = 0x400


def pack_settings_file(blob: bytes) -> bytes:
    return SETTINGS_MAGIC + blob


def unpack_settings_file(data: bytes) -> bytes:
    if not data.startswith(SETTINGS_MAGIC):
        raise ProtocolError("not a RØDECaster Pro settings file (bad header)")
    blob = data[len(SETTINGS_MAGIC):]
    if len(blob) < SETTINGS_SIZE_EXPECTED:
        raise ProtocolError("settings file is truncated")
    return blob[:SETTINGS_SIZE_EXPECTED]
