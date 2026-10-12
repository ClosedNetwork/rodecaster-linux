"""HID transports: Linux hidraw (real hardware) and an abstract base.

hidraw semantics for devices with numbered reports:
  * write(): first byte is the report ID, the rest is the report body.
  * read():  first byte is the report ID, the rest is the report body.
"""

from __future__ import annotations

import errno
import glob
import logging
import os
import select
import time
from dataclasses import dataclass

from . import protocol as P

log = logging.getLogger(__name__)


class TransportError(Exception):
    pass


class DeviceNotFound(TransportError):
    pass


class Transport:
    """Report-level transport. Subclasses implement write_report/read_report."""

    product_id: int = P.PID_NORMAL
    path: str = "?"

    def write_report(self, report: bytes) -> None:  # report[0] is the report ID
        raise NotImplementedError

    def read_report(self, timeout: float) -> bytes | None:  # returns report incl. ID
        raise NotImplementedError

    def close(self) -> None:
        pass

    def await_report(self, rid: int, timeout: float = 5.0, accept=None) -> bytes:
        """Wait for an input report with `rid`; returns the body (ID stripped).

        Reports with other IDs are discarded, mirroring the macOS app's queue.
        """
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TransportError(f"timeout waiting for input report {rid}")
            rep = self.read_report(left)
            if not rep:
                continue
            if rep[0] != rid:
                log.debug("discarding report %d (%d bytes) while waiting for %d", rep[0], len(rep), rid)
                continue
            body = rep[1:]
            if accept is None or accept(body):
                return body

    def drain(self) -> int:
        n = 0
        while self.read_report(0.0):
            n += 1
        return n


@dataclass
class HidrawInfo:
    path: str
    vendor_id: int
    product_id: int
    name: str
    has_command_reports: bool


def _parse_hid_id(uevent: str) -> tuple[int, int] | None:
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            _bus, vid, pid = line.split("=", 1)[1].split(":")
            return int(vid, 16), int(pid, 16)
    return None


def _descriptor_report_ids(desc: bytes) -> set[int]:
    """Collect Report ID items (tag 0x85) from a HID report descriptor."""
    ids: set[int] = set()
    i = 0
    while i < len(desc):
        b = desc[i]
        if b == 0xFE:  # long item
            if i + 1 >= len(desc):
                break
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[b & 0x03]
        if (b & 0xFC) == 0x84 and size >= 1 and i + 1 < len(desc):
            ids.add(desc[i + 1])
        i += 1 + size
    return ids


def enumerate_hidraw(sysfs_root: str = "/sys/class/hidraw") -> list[HidrawInfo]:
    found = []
    for node in sorted(glob.glob(os.path.join(sysfs_root, "hidraw*"))):
        dev = os.path.join(node, "device")
        try:
            with open(os.path.join(dev, "uevent")) as fh:
                uevent = fh.read()
        except OSError:
            continue
        ids = _parse_hid_id(uevent)
        if not ids or ids[0] != P.VENDOR_ID:
            continue
        name = ""
        for line in uevent.splitlines():
            if line.startswith("HID_NAME="):
                name = line.split("=", 1)[1]
        try:
            with open(os.path.join(dev, "report_descriptor"), "rb") as fh:
                rids = _descriptor_report_ids(fh.read())
        except OSError:
            rids = set()
        has_cmd = {P.RID_CMD_OUT, P.RID_CMD_IN}.issubset(rids) if rids else True
        found.append(HidrawInfo("/dev/" + os.path.basename(node), ids[0], ids[1], name, has_cmd))
    return found


class HidrawTransport(Transport):
    def __init__(self, path: str, product_id: int = P.PID_NORMAL):
        self.path = path
        self.product_id = product_id
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as exc:
            raise TransportError(
                f"permission denied opening {path}. Install the udev rule "
                "(see README: 70-rodecaster-pro.rules) and replug the device."
            ) from exc
        except OSError as exc:
            raise TransportError(f"cannot open {path}: {exc}") from exc

    @classmethod
    def open_first(cls) -> "HidrawTransport":
        candidates = [d for d in enumerate_hidraw() if d.product_id in P.KNOWN_PIDS]
        candidates.sort(key=lambda d: (not d.has_command_reports, d.product_id != P.PID_NORMAL))
        if not candidates:
            raise DeviceNotFound("no RØDECaster Pro (USB 19f7:0011) HID interface found")
        info = candidates[0]
        log.info("opening %s (%04x:%04x %s)", info.path, info.vendor_id, info.product_id, info.name)
        return cls(info.path, info.product_id)

    def write_report(self, report: bytes) -> None:
        log.debug("OUT %s", report[:24].hex())
        deadline = time.monotonic() + 5.0
        while True:
            try:
                n = os.write(self.fd, report)
                if n != len(report):
                    raise TransportError(f"short write {n}/{len(report)}")
                return
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TransportError("write timed out")
                select.select([], [self.fd], [], 0.1)
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EIO, errno.EPIPE):
                    raise TransportError("device disconnected") from exc
                raise TransportError(f"write failed: {exc}") from exc

    def read_report(self, timeout: float) -> bytes | None:
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not r:
            return None
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.EIO):
                raise TransportError("device disconnected") from exc
            raise TransportError(f"read failed: {exc}") from exc
        log.debug("IN  %s (%d)", data[:24].hex(), len(data))
        return data

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
