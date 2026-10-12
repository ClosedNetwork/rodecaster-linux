"""Audio helpers built on ffmpeg/ffprobe (decode any format -> 48 kHz s16le)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass

from . import protocol as P

AUDIO_PATTERNS = ["*.wav", "*.mp3", "*.flac", "*.ogg", "*.opus", "*.m4a", "*.aac", "*.aif", "*.aiff", "*.wma"]


class AudioError(Exception):
    pass


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@dataclass
class ProbeInfo:
    channels: int
    sample_rate: int
    duration: float
    codec: str


def probe(path: str) -> ProbeInfo:
    if not have_ffmpeg():
        raise AudioError("ffmpeg/ffprobe not found - install the 'ffmpeg' package")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=channels,sample_rate,codec_name:format=duration", "-of", "json", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise AudioError(f"cannot read audio file: {out.stderr.strip() or 'unknown error'}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise AudioError("file contains no audio stream")
    s = streams[0]
    return ProbeInfo(int(s.get("channels", 2)), int(s.get("sample_rate", 0) or 0),
                     float(data.get("format", {}).get("duration", 0) or 0), s.get("codec_name", "?"))


def decode_for_pad(path: str, normalize: bool = False) -> tuple[int, bytes]:
    """Decode to interleaved s16le @48 kHz, mono stays mono, >2 channels -> stereo."""
    info = probe(path)
    channels = 1 if info.channels == 1 else 2
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", str(channels),
           "-ar", str(P.SAMPLE_RATE)]
    if normalize:
        cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "-"]
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode != 0:
        raise AudioError(out.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    pcm = out.stdout
    if not pcm:
        raise AudioError("Corrupt audio file (decoded to nothing)")
    return channels, pcm


def write_wav(path: str, channels: int, pcm: bytes, rate: int = P.SAMPLE_RATE) -> None:
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    os.replace(tmp, path)


def pad_name_from_path(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("_", " ").strip()[:P.PAD_NAME_LEN - 1] or "Sound"


# Loudness presets similar in spirit to the macOS app's per-platform export options
LOUDNESS_PRESETS = {
    "None": None,
    "Podcast (-16 LUFS)": -16.0,
    "Spotify (-14 LUFS)": -14.0,
    "Apple Podcasts (-16 LUFS)": -16.0,
    "YouTube (-14 LUFS)": -14.0,
    "Broadcast EBU R128 (-23 LUFS)": -23.0,
    "Mono speech (-19 LUFS)": -19.0,
}

EXPORT_FORMATS = {
    "WAV 24-bit": ("wav", ["-c:a", "pcm_s24le"]),
    "WAV 16-bit": ("wav", ["-c:a", "pcm_s16le"]),
    "FLAC": ("flac", ["-c:a", "flac"]),
    "MP3 320 kbps": ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
    "MP3 192 kbps": ("mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "MP3 128 kbps": ("mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]),
}
