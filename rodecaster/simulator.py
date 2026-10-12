"""In-memory RØDECaster Pro emulator speaking the same HID report protocol.

Used by the test-suite and by `--simulate` so the UI can be explored without
hardware. It implements the protocol *as reconstructed*; it is not evidence
that real firmware behaves identically.
"""

from __future__ import annotations

import collections
import datetime as _dt
import struct
import threading

from . import protocol as P
from .effects import ChannelFx
from .transport import Transport, TransportError


class _Pad:
    def __init__(self, idx: int):
        self.name = ""
        self.colour = [(255, 21, 21), (255, 169, 21), (95, 255, 21), (21, 239, 255),
                       (21, 107, 255), (232, 21, 255), (255, 255, 255), (255, 80, 140)][idx % 8]
        self.mode = 0
        self.loop = False
        self.channels = 2
        self.pcm = b""


class SimulatedDevice:
    MEMORY = 512 * 1024 * 1024

    def __init__(self):
        self.firmware = (2, 1, 2)
        self.pads = {a: _Pad(a & 7) for a in range(P.BANKS * P.PADS_PER_BANK)}
        self.bank_names = [f"Bank {i + 1}" for i in range(P.BANKS)]
        self.bank_count = 8
        self.fx = {c: ChannelFx.defaults().pack() for c in (0, 1, 2, 3, 8)}
        self.settings = bytes(range(256)) * 4
        self.clock: _dt.datetime | None = None
        self.transfer_mode = False
        self.effects_edit = False
        self.status_flags = 0
        self.log: list[str] = []

    def used(self) -> int:
        return sum(len(p.pcm) for p in self.pads.values())

    def free(self) -> int:
        return self.MEMORY - self.used()

    def preload_demo(self) -> None:
        import math
        names = ["Intro", "Laughter", "Applause", "Drum roll", "", "Sting", "", "Outro"]
        for i, n in enumerate(names):
            if not n:
                continue
            p = self.pads[i]
            p.name = n
            frames = P.SAMPLE_RATE // 2
            p.pcm = b"".join(struct.pack("<hh", int(8000 * math.sin(k * (i + 2) * 0.01)),
                                         int(8000 * math.sin(k * (i + 2) * 0.01))) for k in range(frames))
        self.pads[1].mode = P.PadMode.PLAY
        self.pads[7].loop = True
        self.bank_names[0] = "Show Open"


class SimulatedTransport(Transport):
    def __init__(self, device: SimulatedDevice | None = None, latency: float = 0.0):
        self.dev = device or SimulatedDevice()
        self.path = "simulator"
        self.product_id = P.PID_NORMAL
        self.queue: collections.deque[bytes] = collections.deque()
        self.cv = threading.Condition()
        self.pending_write: tuple[int, int, bytes] | None = None
        self.upload: dict | None = None
        self.settings_restore = False
        self.latency = latency
        self.closed = False

    # -- helpers
    def _push(self, rid: int, body: bytes) -> None:
        with self.cv:
            self.queue.append(bytes([rid]) + body)
            self.cv.notify_all()

    def _resp(self, payload: bytes = b"") -> None:
        self._push(P.RID_CMD_IN, P.pad64(payload))

    # -- Transport API
    def write_report(self, report: bytes) -> None:
        if self.closed:
            raise TransportError("device disconnected")
        rid, body = report[0], report[1:]
        if rid == P.RID_SIMPLE_OUT:
            self._simple(body[0])
        elif rid == P.RID_CMD_OUT:
            if len(body) != P.CMD_LEN:
                raise TransportError("bad command length")
            self._command(body)
        elif rid == P.RID_BULK_OUT:
            if len(body) != P.BULK_LEN:
                raise TransportError("bad bulk length")
            self._bulk(body)
        else:
            raise TransportError(f"unexpected output report {rid}")

    def read_report(self, timeout: float) -> bytes | None:
        with self.cv:
            if not self.queue:
                self.cv.wait(timeout)
            return self.queue.popleft() if self.queue else None

    def close(self) -> None:
        self.closed = True

    # -- protocol emulation
    def _simple(self, cmd: int) -> None:
        d = self.dev
        maj, mi, pa = d.firmware
        if cmd in (P.Simple.VERSION, P.Simple.IDENTIFY):
            body = bytes([cmd, ord("A"), (mi << 4) | pa, maj]) + struct.pack("<I", 0x52435031) + b"\0"
        elif cmd == P.Simple.RESET:
            return  # the app never waits for a reply to R
        else:
            body = bytes([cmd, ord("E")]) + bytes(7)
        self._push(P.RID_SIMPLE_IN, body)

    def _command(self, body: bytes) -> None:
        if self.pending_write is not None:
            group, index, sel = self.pending_write
            self.pending_write = None
            self._apply_write(group, index, sel, body)
            return
        direction, group, index, sel = P.parse_header(body)
        self.dev.log.append(f"{'R' if direction else 'W'} g{group} i{index} {sel.hex()}")
        if direction == P.DIR_WRITE:
            self.pending_write = (group, index, sel)
            self._resp()  # header ack
        else:
            self._apply_read(group, index, sel)

    def _apply_read(self, group: int, index: int, sel: bytes) -> None:
        d = self.dev
        cmd, a1 = sel[0], sel[1]
        pad = d.pads.get(a1)
        C = P.Cmd
        if group != P.GROUP_DATA:
            return self._resp()
        if cmd == C.PAD_COLOUR:
            self._resp(bytes(pad.colour))
        elif cmd == C.PAD_MODE:
            self._resp(bytes([pad.mode]))
        elif cmd == C.PAD_NAME:
            self._resp(P.encode_name(pad.name, 16))
        elif cmd == C.PAD_SOUND_SIZE:
            self._resp(struct.pack("<I", len(pad.pcm)))
        elif cmd == C.FREE_MEMORY:
            self._resp(struct.pack("<I", d.free()))
        elif cmd == C.PAD_DATA_LENGTH:
            self._download_pad = a1
            self._resp(struct.pack("<I", len(pad.pcm)))
        elif cmd == C.BANK_COUNT:
            self._resp(bytes([d.bank_count - 1]))
        elif cmd == C.PAD_CRC:
            import zlib
            self._resp(struct.pack("<I", zlib.crc32(pad.pcm) if pad.pcm else 0))
        elif cmd == C.BEGIN_DOWNLOAD:
            self._resp()
            data = d.pads[getattr(self, "_download_pad", 0)].pcm
            for off in range(0, len(data), P.BULK_LEN):
                self._push(P.RID_BULK_IN, data[off:off + P.BULK_LEN].ljust(P.BULK_LEN, b"\0"))
        elif cmd == C.PAD_FORMAT:
            self._resp(bytes([pad.channels, 0]))
        elif cmd == C.BANK_NAME:
            self._resp(P.encode_name(d.bank_names[a1], 16))
        elif cmd == C.SETTINGS_SIZE:
            self._resp(struct.pack("<I", len(d.settings)))
        elif cmd == C.SETTINGS:
            self._resp()
            self._push(P.RID_BULK_IN, d.settings.ljust(P.BULK_LEN, b"\0"))
        elif cmd == C.CHANNEL_FX:
            self._resp(d.fx.get(a1, bytes(48)))
        elif cmd == C.STATUS:
            flags = d.status_flags | (P.STATUS_EFFECTS_EDIT if d.effects_edit else 0)
            d.status_flags &= ~P.STATUS_CHANGED
            self._resp(bytes([flags]))
        elif cmd == C.PAD_LOOP:
            self._resp(bytes([1 if pad.loop else 0]))
        else:
            self._resp()

    def _apply_write(self, group: int, index: int, sel: bytes, payload: bytes) -> None:
        d = self.dev
        cmd, a1, a2 = sel[0], sel[1], sel[2]
        C = P.Cmd
        if group == P.GROUP_SYS:
            if index == P.SysAction.ENTER_TRANSFER_MODE:
                d.transfer_mode = True
            elif index == P.SysAction.LEAVE_TRANSFER_MODE:
                d.transfer_mode = False
            elif index == P.SysAction.EFFECTS_EDIT_MODE:
                d.effects_edit = bool(sel[0])
            return self._resp()
        pad = d.pads.get(a1)
        if cmd == C.PAD_COLOUR:
            pad.colour = tuple(payload[:3])
        elif cmd == C.PAD_MODE:
            pad.mode = payload[0]
        elif cmd == C.PAD_NAME:
            pad.name = P.decode_name(payload)
        elif cmd == C.PAD_LOOP:
            pad.loop = bool(payload[0])
        elif cmd == C.BANK_NAME:
            d.bank_names[a1] = P.decode_name(payload)
        elif cmd == C.CLEAR_BANK:
            for p in range(8):
                old = d.pads[(a1 << 3) | p]
                old.pcm, old.name = b"", ""
        elif cmd == C.PAD_BEGIN_UPLOAD:
            frames = P.u32(payload)
            pad.pcm = b""
            pad.channels = a2 or 2
            if frames:
                pad.name = P.decode_name(payload[4:16], 12)
                self.upload = {"pad": pad, "remaining": frames * a2 * 2, "buf": bytearray()}
            else:
                pad.name = ""
        elif cmd == C.SET_TIME:
            year = struct.unpack_from("<H", payload, 5)[0]
            d.clock = _dt.datetime(year, payload[4], payload[3], payload[0], payload[1], payload[2])
        elif cmd == C.SETTINGS:
            self.settings_restore = True
        elif cmd == C.CHANNEL_FX:
            d.fx[a1] = bytes(payload[:48])
        self._resp(payload)

    def _bulk(self, body: bytes) -> None:
        if self.settings_restore:
            self.dev.settings = bytes(body[: len(self.dev.settings)])
            self.settings_restore = False
            return self._resp()
        if self.upload:
            take = min(len(body), self.upload["remaining"])
            self.upload["buf"] += body[:take]
            self.upload["remaining"] -= take
            if self.upload["remaining"] <= 0:
                self.upload["pad"].pcm = bytes(self.upload["buf"])
                self.upload = None
        self._resp()
