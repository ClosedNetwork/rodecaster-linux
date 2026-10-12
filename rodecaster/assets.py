"""Factory pad sounds.

RØDE's eight factory sounds are embedded (as MP3) in the macOS companion app.
They are not redistributed with this project. If you own the macOS app, this
module can extract them from *your* copy of the executable:

    rodecaster-cli extract-factory-sounds "/Applications/RODECaster Pro.app/Contents/MacOS/RODECaster Pro"

Locations were taken from the app's restore-factory-sounds routine (v2.1.2).
"""

from __future__ import annotations

import hashlib
import os
import struct

from .config import data_dir

FACTORY_SOUNDS = [
    # (file stem, pad name, virtual address, size) — RODECaster Pro.app 2.1.2 (x86_64)
    ("intro_outro", "Intro Outro", 0x1007C59A0, 0x14D65E),
    ("laughter", "Laughter", 0x100913000, 0x70BAF),
    ("applause", "Applause", 0x100983BC0, 0x259B7),
    ("sad_trombone", "Sad Trombone", 0x1009A9580, 0x28B08),
    ("rim_shot", "Rim Shot", 0x1009D2090, 0x116C9),
    ("scary", "Scary", 0x1009E3760, 0x1E978),
    ("crickets", "Crickets", 0x100A020E0, 0x3B6B2),
    ("harp", "Harp", 0x100A3D7A0, 0x270E8),
]


def factory_dir() -> str:
    return os.path.join(data_dir(), "factory-sounds")


def available_factory_sounds() -> list[tuple[str, str]]:
    """[(path, pad name)] for extracted sounds, in pad order."""
    out = []
    for stem, name, _va, _size in FACTORY_SOUNDS:
        p = os.path.join(factory_dir(), stem + ".mp3")
        if os.path.exists(p):
            out.append((p, name))
    return out


def _macho_segments(data: bytes) -> list[tuple[int, int, int, int]]:
    """Return [(vmaddr, vmsize, fileoff, filesize)] for a thin 64-bit Mach-O."""
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == 0xBEBAFECA or magic == 0xCAFEBABE:  # fat: pick x86_64 slice
        nfat = struct.unpack_from(">I", data, 4)[0]
        for i in range(nfat):
            cputype, _sub, off, size, _al = struct.unpack_from(">IIIII", data, 8 + i * 20)
            if cputype == 0x01000007:
                segs = _macho_segments(data[off:off + size])
                return [(va, vs, fo + off, fs) for va, vs, fo, fs in segs]
        raise ValueError("no x86_64 slice in universal binary")
    if magic != 0xFEEDFACF:
        raise ValueError("not a 64-bit Mach-O executable")
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    segs = []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from("<QQQQ", data, off + 24)
            segs.append((vmaddr, vmsize, fileoff, filesize))
        off += size
    return segs


def _va_to_off(segs, va: int) -> int:
    for vmaddr, vmsize, fileoff, filesize in segs:
        if vmaddr <= va < vmaddr + filesize:
            return fileoff + (va - vmaddr)
    raise ValueError(f"address {va:#x} not in file")


def _looks_like_mp3(b: bytes) -> bool:
    return b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)


def extract_factory_sounds(macho_path: str, out_dir: str | None = None) -> list[str]:
    out_dir = out_dir or factory_dir()
    with open(macho_path, "rb") as fh:
        data = fh.read()
    segs = _macho_segments(data)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for stem, _name, va, size in FACTORY_SOUNDS:
        off = _va_to_off(segs, va)
        blob = data[off:off + size]
        if len(blob) != size or not _looks_like_mp3(blob):
            raise ValueError(
                f"{stem}: data at {va:#x} is not an MP3 - this extractor matches "
                "RODECaster Pro.app v2.1.2 only")
        path = os.path.join(out_dir, stem + ".mp3")
        with open(path, "wb") as fh:
            fh.write(blob)
        written.append(path)
    with open(os.path.join(out_dir, "SOURCE.txt"), "w") as fh:
        fh.write(f"Extracted from {macho_path}\nsha256={hashlib.sha256(data).hexdigest()}\n")
    return written
