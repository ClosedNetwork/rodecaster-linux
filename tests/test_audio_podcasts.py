import os
import shutil
import subprocess

import pytest

from rodecaster import audio, podcasts

needs_ffmpeg = pytest.mark.skipif(not audio.have_ffmpeg(), reason="ffmpeg not installed")


def _make_wav(path, channels, seconds=1.0):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}:sample_rate=48000",
                    "-af", f"pan={hex((1 << channels) - 1)}|" + "|".join(f"c{i}=c0" for i in range(channels)),
                    "-c:a", "pcm_s24le", path], check=True)


@needs_ffmpeg
def test_decode_for_pad_resamples_and_downmixes(tmp_path):
    src = tmp_path / "six.wav"
    _make_wav(str(src), 6, 0.5)
    ch, pcm = audio.decode_for_pad(str(src))
    assert ch == 2
    assert abs(len(pcm) / 4 - 24000) < 50


@needs_ffmpeg
def test_list_and_export_multitrack(tmp_path):
    root = tmp_path / "RODE" / "PODCASTS"
    (root / "POD00001").mkdir(parents=True)
    _make_wav(str(root / "POD00001" / "POD00001.WAV"), 14, 1.0)
    _make_wav(str(root / "POD00001" / "POD00002.WAV"), 14, 1.0)
    _make_wav(str(root / "POD00003.WAV"), 2, 0.5)
    recs = podcasts.list_recordings(str(root))
    assert [r.name for r in recs] == ["POD00001", "POD00003"]
    assert recs[0].multitrack and len(recs[0].files) == 2
    assert abs(recs[0].duration - 2.0) < 0.1
    out = tmp_path / "out"
    files = podcasts.export_recording(recs[0], str(out), "FLAC", "None", split_tracks=True)
    assert len(files) == 9
    info = audio.probe(next(f for f in files if f.endswith("Mic 1.flac")))
    assert info.channels == 1 and abs(info.duration - 2.0) < 0.1
    mix = podcasts.export_recording(recs[1], str(out), "MP3 128 kbps", "Podcast (-16 LUFS)")
    assert mix[0].endswith(".mp3") and os.path.getsize(mix[0]) > 0


def test_case_insensitive_lookup(tmp_path):
    (tmp_path / "rode" / "Podcasts").mkdir(parents=True)
    assert podcasts._ci_join(str(tmp_path), podcasts.PODCAST_DIR) == str(tmp_path / "rode" / "Podcasts")


MAC_BIN = "/root/rc/bin"


@pytest.mark.skipif(not os.path.exists(MAC_BIN), reason="macOS app binary not available")
def test_factory_sound_extraction(tmp_path):
    from rodecaster.assets import FACTORY_SOUNDS, extract_factory_sounds
    files = extract_factory_sounds(MAC_BIN, str(tmp_path))
    assert len(files) == len(FACTORY_SOUNDS)
    if audio.have_ffmpeg():
        assert all(audio.probe(f).duration > 1 for f in files)
