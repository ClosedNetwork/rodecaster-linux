"""Podcast (SD card) browsing and export while the unit is in Podcast Transfer Mode.

In transfer mode the RØDECaster Pro exposes its microSD card as a USB drive
labelled "RODECASTER" with recordings under /RODE/PODCASTS. The macOS app scans
every mounted volume for that folder; we do the same with Linux mount points and
can ask udisks2 to mount the card when nothing has automounted it.
"""

from __future__ import annotations

import getpass
import glob
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from . import audio

log = logging.getLogger(__name__)

PODCAST_DIR = os.path.join("RODE", "PODCASTS")

# Multitrack polyWAV layout (14 channels) per the official user guide.
MULTITRACK_LAYOUT = [
    ("Stereo Mix", [0, 1]),
    ("Mic 1", [2]),
    ("Mic 2", [3]),
    ("Mic 3", [4]),
    ("Mic 4", [5]),
    ("USB", [6, 7]),
    ("TRRS", [8, 9]),
    ("Bluetooth", [10, 11]),
    ("Sounds", [12, 13]),
]


def _ci_join(root: str, rel: str) -> str | None:
    """Case-insensitive path resolution (FAT volumes may differ in case)."""
    cur = root
    for part in rel.split(os.sep):
        try:
            entries = os.listdir(cur)
        except OSError:
            return None
        match = next((e for e in entries if e.lower() == part.lower()), None)
        if match is None:
            return None
        cur = os.path.join(cur, match)
    return cur


def candidate_mounts() -> list[str]:
    user = getpass.getuser()
    roots = [f"/run/media/{user}", f"/media/{user}", "/media", "/mnt"]
    mounts = []
    for r in roots:
        mounts += [p for p in glob.glob(os.path.join(r, "*")) if os.path.isdir(p)]
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                mp = line.split()[1].encode().decode("unicode_escape")
                if mp not in mounts and mp.startswith(("/run/media", "/media", "/mnt")):
                    mounts.append(mp)
    except OSError:
        pass
    return mounts


def find_podcast_roots() -> list[str]:
    roots = []
    for m in candidate_mounts():
        p = _ci_join(m, PODCAST_DIR)
        if p and os.path.isdir(p):
            roots.append(p)
    return roots


@dataclass
class BlockDev:
    path: str
    label: str
    mountpoint: str | None


def find_unmounted_cards() -> list[BlockDev]:
    """Partitions labelled RODECASTER that are not mounted yet."""
    if not shutil.which("lsblk"):
        return []
    out = subprocess.run(["lsblk", "-J", "-o", "PATH,LABEL,MOUNTPOINT"], capture_output=True, text=True)
    if out.returncode != 0:
        return []
    found = []

    def walk(nodes):
        for n in nodes:
            if (n.get("label") or "").upper().startswith("RODECASTER") and not n.get("mountpoint"):
                found.append(BlockDev(n["path"], n.get("label") or "", None))
            walk(n.get("children") or [])

    walk(json.loads(out.stdout).get("blockdevices", []))
    return found


def mount_card(dev: BlockDev) -> str:
    if not shutil.which("udisksctl"):
        raise RuntimeError("udisksctl not found (install udisks2) - mount the card manually")
    out = subprocess.run(["udisksctl", "mount", "-b", dev.path], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "mount failed")
    m = re.search(r" at (.+?)\.?$", out.stdout.strip())
    return m.group(1) if m else ""


def unmount_card(mountpoint: str) -> None:
    src = _mount_source(mountpoint)
    if src and shutil.which("udisksctl"):
        out = subprocess.run(["udisksctl", "unmount", "-b", src], capture_output=True, text=True)
        if out.returncode != 0:
            log.warning("unmount %s failed: %s", src, out.stderr.strip())


def _mount_source(mountpoint: str) -> str | None:
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if parts[1] == mountpoint:
                    return parts[0]
    except OSError:
        pass
    return None


@dataclass
class Recording:
    name: str
    files: list[str] = field(default_factory=list)
    channels: int = 0
    duration: float = 0.0
    size: int = 0
    mtime: float = 0.0

    @property
    def multitrack(self) -> bool:
        return self.channels >= 14


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_recordings(root: str, probe: bool = True) -> list[Recording]:
    """Group WAVs: each sub-folder is one recording; loose WAVs are one each.

    The exact on-card naming of split files was not observed in the binary; files
    inside one folder are concatenated in natural sort order on export.
    """
    recs: list[Recording] = []
    for entry in sorted(os.listdir(root), key=_natural_key):
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            wavs = sorted((os.path.join(full, f) for f in os.listdir(full)
                           if f.lower().endswith(".wav") and not f.startswith(".")), key=_natural_key)
            if wavs:
                recs.append(Recording(entry, wavs))
        elif entry.lower().endswith(".wav") and not entry.startswith("."):
            recs.append(Recording(os.path.splitext(entry)[0], [full]))
    for r in recs:
        r.size = sum(os.path.getsize(f) for f in r.files)
        r.mtime = max(os.path.getmtime(f) for f in r.files)
        if probe and audio.have_ffmpeg():
            for f in r.files:
                try:
                    info = audio.probe(f)
                    r.channels = info.channels
                    r.duration += info.duration
                except audio.AudioError:
                    pass
    return recs


def export_recording(rec: Recording, out_dir: str, fmt: str = "WAV 24-bit",
                     loudness: str = "None", split_tracks: bool = False,
                     base_name: str | None = None) -> list[str]:
    """Stitch split files, optionally split multitrack, encode with ffmpeg."""
    if not audio.have_ffmpeg():
        raise audio.AudioError("ffmpeg is required for export")
    ext, codec = audio.EXPORT_FORMATS[fmt]
    lufs = audio.LOUDNESS_PRESETS.get(loudness)
    base = base_name or rec.name
    os.makedirs(out_dir, exist_ok=True)

    inputs: list[str]
    listfile = None
    if len(rec.files) > 1:
        listfile = os.path.join(out_dir, f".{base}.concat.txt")
        with open(listfile, "w") as fh:
            for f in rec.files:
                fh.write("file '%s'\n" % f.replace("'", "'\\''"))
        inputs = ["-f", "concat", "-safe", "0", "-i", listfile]
    else:
        inputs = ["-i", rec.files[0]]

    outputs: list[tuple[str, str]] = []  # (filter, path)
    if split_tracks and rec.multitrack:
        for label, chans in MULTITRACK_LAYOUT:
            layout = "stereo" if len(chans) == 2 else "mono"
            pan = f"pan={layout}|" + "|".join(f"c{i}=c{c}" for i, c in enumerate(chans))
            outputs.append((pan, os.path.join(out_dir, f"{base} - {label}.{ext}")))
    elif rec.multitrack:
        outputs.append(("pan=stereo|c0=c0|c1=c1", os.path.join(out_dir, f"{base}.{ext}")))
    else:
        outputs.append(("", os.path.join(out_dir, f"{base}.{ext}")))

    written = []
    try:
        for flt, path in outputs:
            chain = [flt] if flt else []
            if lufs is not None:
                chain.append(f"loudnorm=I={lufs}:TP=-1.0:LRA=11")
            cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", *inputs]
            if chain:
                cmd += ["-af", ",".join(chain)]
            if lufs is not None:
                cmd += ["-ar", "48000"]
            cmd += [*codec, path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise audio.AudioError(res.stderr.strip() or f"export failed for {path}")
            written.append(path)
    finally:
        if listfile and os.path.exists(listfile):
            os.remove(listfile)
    return written
