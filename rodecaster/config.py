"""Small JSON config + XDG paths."""

from __future__ import annotations

import json
import os

APP_ID = "io.github.rodecaster_linux.RodeCasterPro"

DEFAULTS = {
    "auto_sync_time": True,
    "normalize_uploads": False,
    "export_dir": os.path.expanduser("~/Music/RODECaster"),
    "export_format": "WAV 24-bit",
    "export_loudness": "None",
    "export_split_tracks": False,
    "poll_interval_ms": 1500,
}


def _xdg(var: str, fallback: str) -> str:
    return os.environ.get(var) or os.path.expanduser(fallback)


def config_dir() -> str:
    return os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "rodecaster-linux")


def data_dir() -> str:
    return os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "rodecaster-linux")


def cache_dir() -> str:
    return os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), "rodecaster-linux")


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        self.path = os.path.join(config_dir(), "config.json")
        try:
            with open(self.path) as fh:
                self.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(dict(self), fh, indent=2)
        os.replace(tmp, self.path)
