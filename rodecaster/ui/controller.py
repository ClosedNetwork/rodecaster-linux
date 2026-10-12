"""Device controller: owns the connection and runs all HID I/O on one worker thread."""

from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
from typing import Any, Callable

from gi.repository import GLib, GObject

from .. import protocol as P
from ..device import DeviceError, RodeCaster, SoundboardState
from ..transport import DeviceNotFound, HidrawTransport, TransportError, enumerate_hidraw

log = logging.getLogger(__name__)


class Controller(GObject.Object):
    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "disconnected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "busy-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "soundboard-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "pad-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "status-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, config, simulate: bool = False):
        super().__init__()
        self.config = config
        self.simulate = simulate
        self.rc: RodeCaster | None = None
        self.sb = SoundboardState()
        self.loaded_banks: set[int] = set()
        self.status_flags = 0
        self._q: queue.Queue = queue.Queue()
        self._busy = 0
        self._inflight = 0
        self._stop = False
        self.cancel_event = threading.Event()
        self._worker = threading.Thread(target=self._run, name="rodecaster-io", daemon=True)
        self._worker.start()
        GLib.timeout_add(1500, self._tick)
        GLib.idle_add(lambda: (self._tick(), False)[1])  # one-shot; _tick returns True for the timer

    # ---------------------------------------------------------------- worker
    def _run(self):
        while not self._stop:
            job = self._q.get()
            if job is None:
                break
            fn, done, err, label = job
            if label:
                GLib.idle_add(self._set_busy, +1, label)
            self._inflight += 1
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 - surfaced to UI
                log.debug("job failed: %s", traceback.format_exc())
                lost = isinstance(exc, (TransportError, DeviceError)) and "disconnect" in str(exc).lower()
                GLib.idle_add(self._job_failed, exc, err, lost)
            else:
                if done:
                    GLib.idle_add(lambda r=res, d=done: (d(r), False)[1])
            finally:
                self._inflight -= 1
                if label:
                    GLib.idle_add(self._set_busy, -1, "")

    def _set_busy(self, delta: int, label: str):
        self._busy += delta
        self.emit("busy-changed", self._busy > 0, label)
        return False

    def _job_failed(self, exc, err, lost):
        if lost:
            self._drop(str(exc))
        if err:
            err(exc)
        else:
            self.emit("error", str(exc))
        return False

    def submit(self, fn: Callable[[], Any], done=None, err=None, label: str = ""):
        self._q.put((fn, done, err, label))

    def progress_cb(self, frac: float):
        GLib.idle_add(lambda: (self.emit("progress", frac), False)[1])

    @property
    def busy(self) -> bool:
        return self._busy > 0 or self._inflight > 0 or not self._q.empty()

    @property
    def connected(self) -> bool:
        return self.rc is not None

    # ------------------------------------------------------------ connection
    def _tick(self):
        if self.rc is None:
            self._try_connect()
        elif not self.simulate and not os.path.exists(self.rc.t.path):
            # Status isn't polled in transfer mode, so notice an unplug here.
            self._drop("device disconnected")
        elif not self.busy and not self.rc.in_transfer_mode:
            self.submit(self.rc.status, self._on_status, err=lambda e: None)
        return True

    def _try_connect(self):
        if self.busy:
            return
        if self.simulate:
            from ..simulator import SimulatedDevice, SimulatedTransport
            dev = SimulatedDevice()
            dev.preload_demo()
            self._open(lambda: RodeCaster(SimulatedTransport(dev)))
            return
        if not any(d.product_id in P.KNOWN_PIDS for d in enumerate_hidraw()):
            return

        def opener():
            return RodeCaster(HidrawTransport.open_first())
        self._open(opener)

    def _open(self, opener):
        def work():
            rc = opener()
            try:
                try:
                    rc.refresh_info()
                except (TransportError, DeviceError) as exc:
                    if not rc.in_transfer_mode:
                        raise
                    log.warning("no version info in transfer mode: %s", exc)
                if self.config.get("auto_sync_time") and not rc.in_transfer_mode:
                    try:
                        rc.sync_time()
                    except DeviceError as exc:
                        log.warning("time sync failed: %s", exc)
            except BaseException:
                rc.close()  # otherwise every failed attempt leaks a hidraw fd
                raise
            return rc

        def done(rc):
            self.rc = rc
            self.sb = SoundboardState()
            self.loaded_banks.clear()
            self.emit("connected")

        def err(exc):
            if not isinstance(exc, DeviceNotFound):
                self.emit("disconnected", str(exc))
        self.submit(work, done, err, "Connecting…")

    def _drop(self, reason: str):
        if self.rc:
            try:
                self.rc.close()
            except Exception:  # noqa: BLE001
                pass
        self.rc = None
        self.emit("disconnected", reason)

    def disconnect(self):
        self._drop("closed")

    def _on_status(self, flags: int):
        changed = flags != self.status_flags
        self.status_flags = flags
        if changed:
            self.emit("status-changed", flags)
        if flags & P.STATUS_CHANGED:
            for b in sorted(self.loaded_banks):
                self.load_bank(b)

    # ----------------------------------------------------------- soundboard
    def load_bank(self, bank: int, done=None):
        rc = self.rc
        if rc is None or rc.in_transfer_mode:
            return

        def work():
            name = rc.bank_name(bank)
            pads = [rc.read_pad(bank, p) for p in range(P.PADS_PER_BANK)]
            free = rc.free_memory()
            count = rc.bank_count()
            return name, pads, free, count

        def ok(res):
            name, pads, free, count = res
            self.sb.banks[bank] = name
            for st in pads:
                self.sb.pads[st.address] = st
            self.sb.free_memory = free
            self.sb.bank_count = count
            self.loaded_banks.add(bank)
            self.emit("soundboard-changed")
            if done:
                done()
        self.submit(work, ok, label=f"Reading bank {bank + 1}…")

    def refresh_pad(self, addr: int):
        rc = self.rc
        if rc is None:
            return
        b, p = P.split_pad_address(addr)

        def ok(res):
            st, free = res
            self.sb.pads[addr] = st
            self.sb.free_memory = free
            self.emit("pad-changed", addr)
        self.submit(lambda: (rc.read_pad(b, p), rc.free_memory()), ok)

    def device_call(self, method: str, *args, done=None, err=None, label="", refresh_pad: int | None = None):
        rc = self.rc
        if rc is None:
            if err:
                err(DeviceError("not connected"))
            return

        def ok(res):
            if refresh_pad is not None:
                self.refresh_pad(refresh_pad)
            if done:
                done(res)
        self.submit(lambda: getattr(rc, method)(*args), ok, err, label)

    def shutdown(self):
        self._stop = True
        self._q.put(None)
        if self.rc:
            self.rc.close()
