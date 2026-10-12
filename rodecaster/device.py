"""High-level API for the RØDECaster Pro (gen 1).

All public methods are thread-safe (one transaction at a time), mirroring the
mutex the macOS app holds around every HID exchange.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as P
from .effects import BLOCK_LEN, ChannelFx
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

Progress = Callable[[float], None]


class Cancelled(Exception):
    pass


class DeviceError(Exception):
    pass


@dataclass
class PadState:
    bank: int
    pad: int
    name: str = ""
    colour: tuple[int, int, int] = (0, 0, 0)
    mode: int = 0
    loop: bool = False
    size: int = 0
    crc: int = 0

    @property
    def address(self) -> int:
        return P.pad_address(self.bank, self.pad)

    @property
    def has_sound(self) -> bool:
        return self.size > 0

    @property
    def channels_guess(self) -> int:
        return 2

    def duration_seconds(self, channels: int = 2) -> float:
        return self.size / (channels * 2 * P.SAMPLE_RATE) if self.size else 0.0


@dataclass
class DeviceInfo:
    path: str
    product_id: int
    firmware: str = "?"
    identify_version: str = "?"
    identify_value: int = 0

    @property
    def mode(self) -> str:
        return P.KNOWN_PIDS.get(self.product_id, f"unknown (0x{self.product_id:04x})")


@dataclass
class SoundboardState:
    banks: list[str] = field(default_factory=lambda: [""] * P.BANKS)
    pads: dict[int, PadState] = field(default_factory=dict)
    bank_count: int = P.BANKS
    free_memory: int = 0

    def pad(self, bank: int, pad: int) -> PadState:
        return self.pads.setdefault(P.pad_address(bank, pad), PadState(bank, pad))


class RodeCaster:
    RETRIES = 3

    def __init__(self, transport: Transport):
        self.t = transport
        self.lock = threading.RLock()
        self.info = DeviceInfo(path=transport.path, product_id=transport.product_id)

    # ------------------------------------------------------------------ basics
    def close(self) -> None:
        with self.lock:
            self.t.close()

    @property
    def in_transfer_mode(self) -> bool:
        return self.t.product_id == P.PID_TRANSFER

    def simple(self, cmd: int, timeout: float = 5.0) -> P.SimpleReply:
        with self.lock:
            self.t.write_report(P.build_simple(cmd))
            body = self.t.await_report(P.RID_SIMPLE_IN, timeout, accept=lambda b: b[0] == (cmd & 0xFF))
            return P.parse_simple_reply(body)

    def command(self, direction: int, group: int, index: int, sel: bytes,
                payload: bytes | None = None, timeout: float = 5.0) -> bytes:
        """One header/payload transaction. Returns the 64-byte response body."""
        header = P.with_rid(P.RID_CMD_OUT, P.build_header(direction, group, index, sel))
        last: Exception | None = None
        with self.lock:
            for attempt in range(self.RETRIES):
                try:
                    self.t.write_report(header)
                    if direction == P.DIR_READ:
                        return self.t.await_report(P.RID_CMD_IN, timeout)
                    self.t.await_report(P.RID_CMD_IN, timeout)  # header ack
                    self.t.write_report(P.with_rid(P.RID_CMD_OUT, P.pad64(payload or b"")))
                    return self.t.await_report(P.RID_CMD_IN, timeout)
                except TransportError as exc:
                    last = exc
                    if "disconnected" in str(exc):
                        break
                    log.warning("command %s attempt %d failed: %s", sel.hex(), attempt + 1, exc)
            raise DeviceError(f"command {sel.hex()} failed: {last}")

    def read(self, cmd: int, a1: int = 0, a2: int = 0, a3: int = 0, group: int = P.GROUP_DATA) -> bytes:
        return self.command(P.DIR_READ, group, 1, P.selector(cmd, a1, a2, a3))

    def write(self, cmd: int, payload: bytes = b"", a1: int = 0, a2: int = 0, a3: int = 0,
              group: int = P.GROUP_DATA) -> bytes:
        return self.command(P.DIR_WRITE, group, 1, P.selector(cmd, a1, a2, a3), payload)

    def sys_action(self, action: int, value: int = 0) -> bytes:
        sel = value.to_bytes(4, "little")
        return self.command(P.DIR_WRITE, P.GROUP_SYS, int(action), sel, b"")

    # ------------------------------------------------------------- device info
    def refresh_info(self) -> DeviceInfo:
        """Version/identify via the simple channel, then a status read to prove the link.

        A real unit (fw 2.x, normal mode) never answers the simple 'V'/'I' queries, so they
        are best-effort; the command-channel status read is what must succeed.
        """
        try:
            v = self.simple(P.Simple.VERSION, timeout=1.0)
            if v.ok and v.version:
                self.info.firmware = "%d.%d.%d" % v.version
            elif v.status == "E":
                self.info.firmware = "error"
            i = self.simple(P.Simple.IDENTIFY, timeout=1.0)
            if i.ok and i.version:
                self.info.identify_version = "%d.%d.%d" % i.version
            self.info.identify_value = i.value
        except TransportError as exc:
            if "disconnected" in str(exc):
                raise
            log.info("no reply to version query (%s); continuing", exc)
            self.info.firmware = "unknown"
        if not self.in_transfer_mode:
            self.status()
        return self.info

    def status(self) -> int:
        return self.read(P.Cmd.STATUS)[0]

    def sync_time(self, when: _dt.datetime | None = None) -> None:
        when = when or _dt.datetime.now()
        self.write(P.Cmd.SET_TIME, P.build_time_payload(when))

    def set_transfer_mode(self, enabled: bool) -> None:
        self.sys_action(P.SysAction.ENTER_TRANSFER_MODE if enabled else P.SysAction.LEAVE_TRANSFER_MODE)

    def set_effects_edit_mode(self, enabled: bool) -> None:
        self.sys_action(P.SysAction.EFFECTS_EDIT_MODE, 1 if enabled else 0)

    # -------------------------------------------------------------- pads/banks
    def free_memory(self) -> int:
        return P.u32(self.read(P.Cmd.FREE_MEMORY))

    def bank_count(self) -> int:
        return min(self.read(P.Cmd.BANK_COUNT)[0], P.BANKS - 1) + 1

    def bank_name(self, bank: int) -> str:
        return P.decode_name(self.read(P.Cmd.BANK_NAME, bank))

    def set_bank_name(self, bank: int, name: str) -> None:
        self.write(P.Cmd.BANK_NAME, P.encode_name(name, P.PAD_NAME_LEN), bank)

    def clear_bank(self, bank: int) -> None:
        self.write(P.Cmd.CLEAR_BANK, b"", bank)

    def pad_name(self, addr: int) -> str:
        return P.decode_name(self.read(P.Cmd.PAD_NAME, addr))

    def set_pad_name(self, addr: int, name: str) -> None:
        self.write(P.Cmd.PAD_NAME, P.encode_name(name, P.PAD_NAME_LEN), addr)

    def pad_colour(self, addr: int) -> tuple[int, int, int]:
        r = self.read(P.Cmd.PAD_COLOUR, addr)
        return r[0], r[1], r[2]

    def set_pad_colour(self, addr: int, rgb: tuple[int, int, int]) -> None:
        self.write(P.Cmd.PAD_COLOUR, bytes(int(c) & 0xFF for c in rgb), addr)

    def pad_mode(self, addr: int) -> int:
        return self.read(P.Cmd.PAD_MODE, addr)[0]

    def set_pad_mode(self, addr: int, mode: int) -> None:
        self.write(P.Cmd.PAD_MODE, bytes([int(mode)]), addr)

    def pad_loop(self, addr: int) -> bool:
        return self.read(P.Cmd.PAD_LOOP, addr)[0] != 0

    def set_pad_loop(self, addr: int, loop: bool) -> None:
        self.write(P.Cmd.PAD_LOOP, bytes([1 if loop else 0]), addr)

    def pad_sound_size(self, addr: int) -> int:
        return P.u32(self.read(P.Cmd.PAD_SOUND_SIZE, addr))

    def pad_crc(self, addr: int) -> int:
        return P.u32(self.read(P.Cmd.PAD_CRC, addr))

    def pad_format(self, addr: int) -> tuple[int, bool]:
        r = self.read(P.Cmd.PAD_FORMAT, addr)
        return r[0], r[1] != 0

    def clear_pad(self, addr: int) -> None:
        self.write(P.Cmd.PAD_BEGIN_UPLOAD, P.build_begin_upload(0, ""), addr, 0)

    def read_pad(self, bank: int, pad: int) -> PadState:
        addr = P.pad_address(bank, pad)
        st = PadState(bank, pad)
        st.colour = self.pad_colour(addr)
        st.mode = self.pad_mode(addr)
        st.name = self.pad_name(addr)
        st.loop = self.pad_loop(addr)
        st.size = self.pad_sound_size(addr)
        if st.size:
            st.crc = self.pad_crc(addr)
        return st

    def read_soundboard(self, progress: Progress | None = None, banks: list[int] | None = None) -> SoundboardState:
        sb = SoundboardState()
        sb.free_memory = self.free_memory()
        sb.bank_count = self.bank_count()
        banks = list(range(P.BANKS)) if banks is None else banks
        total = len(banks) * (P.PADS_PER_BANK + 1)
        done = 0
        for b in banks:
            sb.banks[b] = self.bank_name(b)
            done += 1
            for p in range(P.PADS_PER_BANK):
                sb.pads[P.pad_address(b, p)] = self.read_pad(b, p)
                done += 1
                if progress:
                    progress(done / total)
        return sb

    # ------------------------------------------------------------ audio xfer
    def upload_pcm(self, addr: int, pcm: bytes, channels: int, name: str,
                   progress: Progress | None = None, cancel: threading.Event | None = None) -> int:
        """Upload interleaved s16le 48 kHz PCM to a pad. Returns the new CRC."""
        if channels not in (1, 2):
            raise ValueError("channels must be 1 or 2")
        frames = len(pcm) // (channels * 2)
        pcm = pcm[: frames * channels * 2]
        with self.lock:
            free = self.free_memory() + self.pad_sound_size(addr)
            if len(pcm) > free:
                raise DeviceError(
                    f"Not enough memory to transfer the file ({len(pcm)/1e6:.1f} MB needed, "
                    f"{free/1e6:.1f} MB available)")
            self.write(P.Cmd.PAD_BEGIN_UPLOAD, P.build_begin_upload(frames, name), addr, channels)
            chunks = list(P.chunk_pcm(pcm, channels))
            for i, chunk in enumerate(chunks):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                try:
                    self.t.write_report(P.with_rid(P.RID_BULK_OUT, chunk))
                    self.t.await_report(P.RID_CMD_IN, 5.0)
                except TransportError as exc:
                    raise DeviceError(f"Error transferring audio (chunk {i + 1}/{len(chunks)}): {exc}") from exc
                if progress:
                    progress((i + 1) / len(chunks))
            crc = self.pad_crc(addr)
            self.pad_sound_size(addr)
            return crc

    def download_pcm(self, addr: int, progress: Progress | None = None,
                     cancel: threading.Event | None = None) -> tuple[int, bytes]:
        """Download a pad's audio. Returns (channels, s16le PCM)."""
        with self.lock:
            if not self.pad_sound_size(addr):
                raise DeviceError("pad is empty")
            channels, _flag = self.pad_format(addr)
            if channels == 0:
                raise DeviceError("pad reports no audio channels")  # the app stops here too
            length = P.u32(self.read(P.Cmd.PAD_DATA_LENGTH, addr))
            if length == 0:
                raise DeviceError("pad reports zero bytes to download")
            self.read(P.Cmd.BEGIN_DOWNLOAD)
            out = bytearray()
            n_chunks = (length + P.BULK_LEN - 1) // P.BULK_LEN
            try:
                for i in range(n_chunks):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    body = self.t.await_report(P.RID_BULK_IN, 5.0)
                    out += body[: min(P.BULK_LEN, length - len(out))]
                    if progress:
                        progress((i + 1) / max(1, n_chunks))
            except Cancelled:
                # Mirrors the app: when the user cancels, it posts command 0x16.
                try:
                    self.t.drain()
                    self.write(P.Cmd.ABORT_DOWNLOAD)
                except (DeviceError, TransportError) as exc:
                    log.warning("abort-download notification failed: %s", exc)
                raise
            except TransportError as exc:
                raise DeviceError(f"Error downloading sound: {exc}") from exc
            return channels, bytes(out)

    # ---------------------------------------------------------------- effects
    def read_fx(self, chan: int) -> ChannelFx:
        r = self.read(P.Cmd.CHANNEL_FX, P.fx_channel_wire(chan))
        return ChannelFx.unpack(r[:BLOCK_LEN])

    def write_fx(self, chan: int, fx: ChannelFx) -> None:
        self.write(P.Cmd.CHANNEL_FX, fx.pack(), P.fx_channel_wire(chan))

    # --------------------------------------------------------------- settings
    def settings_size(self) -> int:
        return P.u32(self.read(P.Cmd.SETTINGS_SIZE))

    def backup_settings(self) -> bytes:
        with self.lock:
            size = self.settings_size()
            if size != P.SETTINGS_SIZE_EXPECTED:
                raise DeviceError(f"unexpected settings size {size:#x} (expected 0x400)")
            self.read(P.Cmd.SETTINGS)
            body = self.t.await_report(P.RID_BULK_IN, 5.0)
            return bytes(body[:size])

    def restore_settings(self, blob: bytes) -> None:
        with self.lock:
            size = self.settings_size()
            if size != P.SETTINGS_SIZE_EXPECTED or len(blob) < size:
                raise DeviceError("settings size mismatch; refusing to restore")
            self.write(P.Cmd.SETTINGS, b"")
            self.t.write_report(P.with_rid(P.RID_BULK_OUT, blob[:size].ljust(P.BULK_LEN, b"\0")))
            self.t.await_report(P.RID_CMD_IN, 5.0)
