import datetime as dt
import math
import os
import struct
import subprocess
import sys
import threading

import pytest

from rodecaster import protocol as P
from rodecaster.device import Cancelled, DeviceError, RodeCaster
from rodecaster.effects import ChannelFx
from rodecaster.simulator import SimulatedDevice, SimulatedTransport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def rig():
    dev = SimulatedDevice()
    dev.preload_demo()
    return dev, RodeCaster(SimulatedTransport(dev))


def tone(frames, ch):
    return b"".join(struct.pack("<" + "h" * ch, *([int(9000 * math.sin(i / 7))] * ch)) for i in range(frames))


def test_info(rig):
    _dev, rc = rig
    info = rc.refresh_info()
    assert info.firmware == "2.1.2" and info.mode == "normal"


def test_pad_properties_roundtrip(rig):
    dev, rc = rig
    a = P.pad_address(2, 5)
    rc.set_pad_name(a, "Drum roll")
    rc.set_pad_colour(a, (1, 2, 3))
    rc.set_pad_mode(a, P.PadMode.TRASH_TALK)
    rc.set_pad_loop(a, True)
    st = rc.read_pad(2, 5)
    assert (st.name, st.colour, st.mode, st.loop) == ("Drum roll", (1, 2, 3), P.PadMode.TRASH_TALK, True)
    rc.set_bank_name(2, "Guests")
    assert rc.bank_name(2) == "Guests"


@pytest.mark.parametrize("ch,frames", [(1, 1), (1, 1024), (2, 513), (2, 48000)])
def test_upload_download_roundtrip(rig, ch, frames):
    dev, rc = rig
    a = P.pad_address(1, 3)
    pcm = tone(frames, ch)
    rc.upload_pcm(a, pcm, ch, "Test")
    assert dev.pads[a].pcm == pcm
    got_ch, got = rc.download_pcm(a)
    assert got_ch == ch and got == pcm
    assert not any(e.startswith("W g3 i1 16") for e in dev.log)  # 0x16 only on cancel


def test_upload_rejects_when_full(rig):
    dev, rc = rig
    dev.MEMORY = dev.used() + 100
    with pytest.raises(DeviceError, match="Not enough memory"):
        rc.upload_pcm(P.pad_address(0, 4), bytes(4096), 2, "big")


def test_upload_cancel(rig):
    _dev, rc = rig
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        rc.upload_pcm(P.pad_address(0, 4), bytes(8192), 2, "x", cancel=ev)


def test_clear_pad_and_bank(rig):
    dev, rc = rig
    rc.clear_pad(P.pad_address(0, 0))
    assert rc.pad_sound_size(P.pad_address(0, 0)) == 0
    rc.clear_bank(0)
    assert all(not dev.pads[P.pad_address(0, p)].pcm for p in range(8))


def test_sync_time_and_modes(rig):
    dev, rc = rig
    rc.sync_time(dt.datetime(2026, 1, 2, 3, 4, 5))
    assert dev.clock == dt.datetime(2026, 1, 2, 3, 4, 5)
    rc.set_transfer_mode(True)
    assert dev.transfer_mode
    rc.set_transfer_mode(False)
    assert not dev.transfer_mode
    rc.set_effects_edit_mode(True)
    assert rc.status() & P.STATUS_EFFECTS_EDIT


def test_effects_roundtrip(rig):
    dev, rc = rig
    fx = rc.read_fx(4)
    fx.values["CompThreshold"] = -100
    rc.write_fx(4, fx)
    assert ChannelFx.unpack(dev.fx[8]).values["CompThreshold"] == -100  # master maps to wire channel 8


def test_settings_backup_restore(rig):
    dev, rc = rig
    blob = rc.backup_settings()
    assert blob == dev.settings
    new = bytes(reversed(blob))
    rc.restore_settings(new)
    assert dev.settings == new


def test_soundboard_read(rig):
    _dev, rc = rig
    sb = rc.read_soundboard(banks=[0])
    assert sb.banks[0] == "Show Open"
    assert sb.pad(0, 0).name == "Intro"


def test_cli_simulate():
    out = subprocess.run([sys.executable, "-m", "rodecaster", "--simulate", "pads", "--bank", "1"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "Show Open" in out.stdout and "Laughter" in out.stdout


def test_download_cancel_sends_abort(rig):
    dev, rc = rig
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        rc.download_pcm(P.pad_address(0, 0), cancel=ev)
    assert "W g3 i1 16000000" in dev.log


def test_download_empty_pad_errors(rig):
    _dev, rc = rig
    with pytest.raises(DeviceError):
        rc.download_pcm(P.pad_address(0, 4))


def test_reboot_does_not_wait(rig):
    _dev, rc = rig
    assert rc.reboot() is None
