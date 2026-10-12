"""rodecaster-cli: scriptable access to the RØDECaster Pro (gen 1) on Linux."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from . import protocol as P


def open_device(args):
    from .device import RodeCaster
    if args.simulate:
        from .simulator import SimulatedDevice, SimulatedTransport
        dev = SimulatedDevice()
        dev.preload_demo()
        return RodeCaster(SimulatedTransport(dev))
    from .transport import HidrawTransport
    if args.device:
        return RodeCaster(HidrawTransport(args.device))
    return RodeCaster(HidrawTransport.open_first())


def parse_colour(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError("colour must be #RRGGBB")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def parse_mode(s: str) -> int:
    key = s.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"censor": "CENSOR", "swear": "CENSOR", "trash": "TRASH_TALK", "trashtalk": "TRASH_TALK"}
    name = aliases.get(key, key.upper())
    try:
        return int(P.PadMode[name])
    except KeyError:
        if key.isdigit():
            return int(key)
        raise argparse.ArgumentTypeError("mode: latch|pause|replay|play|censor|trash-talk")


def _progress(label):
    last = [-1]

    def cb(frac):
        pct = int(frac * 100)
        if pct == last[0]:
            return
        last[0] = pct
        sys.stderr.write(f"\r{label}: {pct:3d}%")
        if frac >= 1:
            sys.stderr.write("\n")
        sys.stderr.flush()
    return cb


def cmd_list(args):
    from .transport import enumerate_hidraw
    devs = enumerate_hidraw()
    if not devs:
        print("No RØDE HID devices found (VID 19f7).")
        return 1
    for d in devs:
        mode = P.KNOWN_PIDS.get(d.product_id, "unknown")
        print(f"{d.path}\t{d.vendor_id:04x}:{d.product_id:04x}\t{mode}\t{d.name}"
              f"\t{'command-interface' if d.has_command_reports else 'other-interface'}")
    return 0


def cmd_info(args):
    rc = open_device(args)
    info = rc.refresh_info()
    print(f"Device path     : {info.path}")
    print(f"USB mode        : {info.mode}")
    print(f"Firmware (V)    : {info.firmware}")
    print(f"Version  (I)    : {info.identify_version}  value=0x{info.identify_value:08x}")
    if not rc.in_transfer_mode:
        print(f"Free sound mem  : {rc.free_memory() / 1e6:.1f} MB")
        print(f"Banks in use    : {rc.bank_count()}")
        print(f"Status flags    : 0x{rc.status():02x}")
    return 0


def cmd_pads(args):
    rc = open_device(args)
    banks = [args.bank - 1] if args.bank else range(P.BANKS)
    for b in banks:
        print(f"Bank {b + 1}: {rc.bank_name(b)!r}")
        for p in range(P.PADS_PER_BANK):
            st = rc.read_pad(b, p)
            r, g, bl = st.colour
            print(f"  pad {p + 1}: {st.name or '(empty)':16s} #{r:02x}{g:02x}{bl:02x} "
                  f"{P.PAD_MODE_LABELS.get(st.mode, st.mode)!s:14s} loop={'on' if st.loop else 'off'} "
                  f"{st.size / 1e6:6.2f} MB")
    return 0


def cmd_upload(args):
    from .audio import decode_for_pad, pad_name_from_path
    rc = open_device(args)
    addr = P.pad_address(args.bank - 1, args.pad - 1)
    channels, pcm = decode_for_pad(args.file, normalize=args.normalize)
    name = args.name or pad_name_from_path(args.file)
    crc = rc.upload_pcm(addr, pcm, channels, name, progress=_progress("upload"))
    rc.set_pad_name(addr, name)
    print(f"uploaded {len(pcm) / 1e6:.2f} MB ({channels} ch) to bank {args.bank} pad {args.pad}, crc=0x{crc:08x}")
    return 0


def cmd_download(args):
    from .audio import write_wav
    rc = open_device(args)
    addr = P.pad_address(args.bank - 1, args.pad - 1)
    channels, pcm = rc.download_pcm(addr, progress=_progress("download"))
    write_wav(args.out, channels, pcm)
    print(f"wrote {args.out} ({channels} ch, {len(pcm) / (channels * 2 * P.SAMPLE_RATE):.2f} s)")
    return 0


def cmd_clear(args):
    rc = open_device(args)
    rc.clear_pad(P.pad_address(args.bank - 1, args.pad - 1))
    print("pad cleared")
    return 0


def cmd_clear_bank(args):
    rc = open_device(args)
    rc.clear_bank(args.bank - 1)
    print("bank cleared")
    return 0


def cmd_set_pad(args):
    rc = open_device(args)
    addr = P.pad_address(args.bank - 1, args.pad - 1)
    if args.name is not None:
        rc.set_pad_name(addr, args.name)
    if args.colour is not None:
        rc.set_pad_colour(addr, args.colour)
    if args.mode is not None:
        rc.set_pad_mode(addr, args.mode)
    if args.loop is not None:
        rc.set_pad_loop(addr, args.loop == "on")
    return 0


def cmd_bank_name(args):
    rc = open_device(args)
    rc.set_bank_name(args.bank - 1, args.name)
    return 0


def cmd_sync_time(args):
    rc = open_device(args)
    rc.sync_time()
    print("clock synchronised to this computer")
    return 0


def cmd_transfer(args):
    rc = open_device(args)
    rc.set_transfer_mode(args.state == "on")
    print("Podcast Transfer Mode", "requested" if args.state == "on" else "left")
    return 0


def cmd_settings_backup(args):
    rc = open_device(args)
    blob = rc.backup_settings()
    with open(args.file, "wb") as fh:
        fh.write(P.pack_settings_file(blob))
    print(f"saved {args.file}")
    return 0


def cmd_settings_restore(args):
    rc = open_device(args)
    with open(args.file, "rb") as fh:
        blob = P.unpack_settings_file(fh.read())
    rc.restore_settings(blob)
    print("settings restored")
    return 0


def cmd_fx(args):
    from .effects import FIELDS
    rc = open_device(args)
    ch = args.channel - 1
    fx = rc.read_fx(ch)
    if args.set:
        for item in args.set:
            k, _, v = item.partition("=")
            if k not in fx.values:
                raise SystemExit(f"unknown field {k}; known: {', '.join(n for n, _, _ in FIELDS)}")
            fx.values[k] = int(v)
        rc.write_fx(ch, fx)
    for n, _, _ in FIELDS:
        print(f"{n:22s} {fx.values[n]}")
    return 0


def cmd_extract(args):
    from .assets import extract_factory_sounds
    for p in extract_factory_sounds(args.binary):
        print(p)
    return 0


def cmd_restore_factory(args):
    from .assets import available_factory_sounds
    from .audio import decode_for_pad
    sounds = available_factory_sounds()
    if len(sounds) != 8:
        raise SystemExit("factory sounds not extracted - run `rodecaster-cli extract-factory-sounds` first")
    rc = open_device(args)
    for b in reversed(range(P.BANKS)):
        rc.clear_bank(b)
    for i, (path, name) in enumerate(sounds):
        ch, pcm = decode_for_pad(path)
        rc.upload_pcm(P.pad_address(0, i), pcm, ch, name, progress=_progress(name))
        rc.set_pad_name(P.pad_address(0, i), name)
    print("factory sounds restored to bank 1")
    return 0


def cmd_podcasts(args):
    from .podcasts import export_recording, find_podcast_roots, list_recordings
    roots = find_podcast_roots()
    if not roots:
        print("No mounted RODECASTER card with RODE/PODCASTS found. Enable Podcast Transfer Mode.")
        return 1
    for root in roots:
        recs = list_recordings(root)
        for i, r in enumerate(recs, 1):
            print(f"{i:3d}. {r.name:20s} {r.channels:2d} ch {r.duration / 60:7.1f} min {len(r.files)} file(s)  {root}")
        if args.export:
            for idx in args.export:
                r = recs[idx - 1]
                for p in export_recording(r, args.out, args.format, args.loudness, args.split):
                    print("exported", p)
    return 0


def cmd_raw(args):
    rc = open_device(args)
    sel = bytes.fromhex(args.selector)
    if args.write is not None:
        resp = rc.command(P.DIR_WRITE, args.group, args.index, sel, bytes.fromhex(args.write))
    else:
        resp = rc.command(P.DIR_READ, args.group, args.index, sel)
    print(resp.hex())
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rodecaster-cli", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv HID hex dump")
    ap.add_argument("--simulate", action="store_true", help="use the built-in device emulator")
    ap.add_argument("--device", help="explicit /dev/hidrawN")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    add("list", cmd_list, "list RØDE HID interfaces")
    add("info", cmd_info, "firmware version and memory")
    p = add("pads", cmd_pads, "show pads"); p.add_argument("--bank", type=int, choices=range(1, 9))
    for name, fn, h in [("upload", cmd_upload, "load an audio file onto a pad"),
                        ("download", cmd_download, "save a pad's audio as WAV"),
                        ("clear", cmd_clear, "clear a pad"),
                        ("set-pad", cmd_set_pad, "edit pad name/colour/mode/loop")]:
        p = add(name, fn, h)
        p.add_argument("bank", type=int, choices=range(1, 9))
        p.add_argument("pad", type=int, choices=range(1, 9))
        if name == "upload":
            p.add_argument("file")
            p.add_argument("--name")
            p.add_argument("--normalize", action="store_true", help="loudness-normalise to -16 LUFS")
        if name == "download":
            p.add_argument("out")
        if name == "set-pad":
            p.add_argument("--name")
            p.add_argument("--colour", type=parse_colour)
            p.add_argument("--mode", type=parse_mode)
            p.add_argument("--loop", choices=["on", "off"])
    p = add("clear-bank", cmd_clear_bank, "clear all pads in a bank"); p.add_argument("bank", type=int, choices=range(1, 9))
    p = add("bank-name", cmd_bank_name, "rename a bank"); p.add_argument("bank", type=int, choices=range(1, 9)); p.add_argument("name")
    add("sync-time", cmd_sync_time, "set the unit's clock from this computer")
    p = add("transfer", cmd_transfer, "enter/leave Podcast Transfer Mode"); p.add_argument("state", choices=["on", "off"])
    p = add("settings-backup", cmd_settings_backup, "save settings (.rcpsettings)"); p.add_argument("file")
    p = add("settings-restore", cmd_settings_restore, "load settings (.rcpsettings)"); p.add_argument("file")
    p = add("fx", cmd_fx, "show/set channel processing (raw values)")
    p.add_argument("channel", type=int, choices=range(1, 6), help="1-4 mics, 5 master")
    p.add_argument("--set", nargs="*", metavar="FIELD=VALUE")
    p = add("extract-factory-sounds", cmd_extract, "extract factory sounds from your macOS app binary"); p.add_argument("binary")
    add("restore-factory-sounds", cmd_restore_factory, "clear all banks and load factory sounds into bank 1")
    p = add("podcasts", cmd_podcasts, "list/export recordings (transfer mode)")
    p.add_argument("--export", type=int, nargs="*", metavar="N")
    p.add_argument("--out", default=os.path.expanduser("~/Music/RODECaster"))
    p.add_argument("--format", default="WAV 24-bit")
    p.add_argument("--loudness", default="None")
    p.add_argument("--split", action="store_true", help="split multitrack into per-source files")
    p = add("raw", cmd_raw, "send a raw command (debugging)")
    p.add_argument("selector", help="4 bytes hex, e.g. 08000000")
    p.add_argument("--group", type=int, default=P.GROUP_DATA)
    p.add_argument("--index", type=int, default=1)
    p.add_argument("--write", metavar="HEX", help="payload for a write (omit for read)")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - 10 * min(args.verbose, 2)
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # user-facing
        if args.verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
