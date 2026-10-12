import datetime as dt
import struct

import pytest

from rodecaster import protocol as P
from rodecaster.effects import BLOCK_LEN, FIELDS, ChannelFx


def test_header_layout_matches_app():
    # Bytes observed in the macOS app's command builder: 52 0e b0 3e dir group idx(le16) sel[4]
    h = P.build_header(P.DIR_READ, 3, 1, P.selector(0x07, 0x2A))
    assert h[:12] == bytes.fromhex("520eb03e" "01" "03" "0100" "072a0000")
    assert len(h) == 64 and h[12:] == bytes(52)
    assert P.parse_header(h) == (1, 3, 1, bytes([7, 0x2A, 0, 0]))


def test_simple_command_and_reply():
    assert P.build_simple(P.Simple.VERSION) == bytes([2, ord("V")]) + bytes(8)
    r = P.parse_simple_reply(bytes([ord("V"), ord("A"), 0x12, 0x02, 1, 0, 0, 0, 0]))
    assert r.ok and r.version == (2, 1, 2) and r.value == 1
    e = P.parse_simple_reply(bytes([ord("V"), ord("E")]) + bytes(7))
    assert not e.ok and e.version is None


def test_pad_address_roundtrip():
    for b in range(8):
        for p in range(8):
            assert P.split_pad_address(P.pad_address(b, p)) == (b, p)
    with pytest.raises(ValueError):
        P.pad_address(8, 0)


def test_names_are_nul_terminated_and_utf8_safe():
    raw = P.encode_name("Ünïcødé name that is long", 16)
    assert len(raw) == 16 and raw[-1] == 0
    raw.decode("utf-8", errors="strict").rstrip("\0")
    assert P.decode_name(P.encode_name("Intro", 16)) == "Intro"


def test_begin_upload_payload():
    pl = P.build_begin_upload(48000, "Applause")
    assert struct.unpack_from("<I", pl)[0] == 48000
    assert pl[4:16] == b"Applause".ljust(12, b"\0")
    assert P.build_begin_upload(1, "Twelve chars")[4:17] == b"Twelve chars\0"
    assert len(pl) == 64


def test_time_payload():
    pl = P.build_time_payload(dt.datetime(2026, 10, 12, 17, 5, 9))
    assert pl[:7] == bytes([17, 5, 9, 12, 10]) + struct.pack("<H", 2026)


def test_chunking_pads_last_chunk():
    pcm = bytes(range(256)) * 20  # 5120 bytes stereo s16 = 1280 frames
    chunks = list(P.chunk_pcm(pcm, 2))
    assert [len(c) for c in chunks] == [2048, 2048, 2048]
    assert b"".join(chunks)[: len(pcm)] == pcm
    assert P.frames_per_chunk(1) == 1024 and P.frames_per_chunk(2) == 512


def test_settings_file_format():
    blob = bytes(range(256)) * 4
    f = P.pack_settings_file(blob)
    assert f[:16] == b"RCP_SET[v1.0.0]\0"
    assert P.unpack_settings_file(f) == blob
    with pytest.raises(P.ProtocolError):
        P.unpack_settings_file(b"nope" + blob)


def test_effects_block_layout():
    fx = ChannelFx.defaults()
    blk = fx.pack()
    assert len(blk) == BLOCK_LEN
    # default block as written by the app's reset routine
    assert blk[:5] == bytes([0x3C]) + struct.pack("<hh", -522, 1585)
    assert struct.unpack_from("<h", blk, 19)[0] == 5856        # DeesserFrequency
    assert struct.unpack_from("<h", blk, 21)[0] == -192        # CompThreshold
    assert struct.unpack_from("<h", blk, 29)[0] == 3631        # ExciterTune
    assert blk[34] == 100                                      # BBTune
    assert blk[36:43] == bytes([1] * 7)                        # enables
    assert ChannelFx.unpack(blk).values == fx.values
    assert len(FIELDS) == 36


def test_effects_json_roundtrip():
    fx = ChannelFx.defaults()
    fx.values["CompRatio"] = 33
    assert ChannelFx.from_json(fx.to_json()).values["CompRatio"] == 33


def test_hid_descriptor_report_id_parser():
    from rodecaster.transport import _descriptor_report_ids
    desc = bytes([0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x01, 0x95, 0x08, 0x85, 0x05, 0xC0])
    assert _descriptor_report_ids(desc) == {1, 5}
