"""Recordings page: Podcast Transfer Mode + SD-card browsing/export."""

from __future__ import annotations

import datetime as _dt
import os
import subprocess

from gi.repository import Adw, GLib, Gtk

from .. import audio, podcasts
from .common import confirm


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class RecordingsPage(Gtk.Box):
    def __init__(self, window, ctl):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = window
        self.ctl = ctl
        self.cfg = ctl.config
        self.recs: list[podcasts.Recording] = []
        self.checks: list[Gtk.CheckButton] = []

        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)

        g = Adw.PreferencesGroup(
            title="Podcast Transfer Mode",
            description="While in transfer mode the unit exposes its microSD card as a USB drive "
                        "named RODECASTER and its other functions are paused.")
        page.add(g)
        row = Adw.ActionRow(title="Transfer mode", subtitle="Switch the unit in or out of transfer mode")
        self.enter_btn = Gtk.Button(label="Enter", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self.enter_btn.connect("clicked", self._enter)
        self.leave_btn = Gtk.Button(label="Leave", valign=Gtk.Align.CENTER)
        self.leave_btn.connect("clicked", self._leave)
        row.add_suffix(self.enter_btn)
        row.add_suffix(self.leave_btn)
        g.add(row)
        self.card_row = Adw.ActionRow(title="microSD card", subtitle="Not found")
        self.mount_btn = Gtk.Button(label="Mount", valign=Gtk.Align.CENTER, visible=False)
        self.mount_btn.connect("clicked", self._mount)
        rescan = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Rescan")
        rescan.connect("clicked", lambda *_: self.scan())
        self.card_row.add_suffix(self.mount_btn)
        self.card_row.add_suffix(rescan)
        g.add(self.card_row)

        self.list_group = Adw.PreferencesGroup(title="Recordings")
        page.add(self.list_group)
        self.listbox = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.list_group.add(self.listbox)

        eg = Adw.PreferencesGroup(title="Export")
        page.add(eg)
        self.fmt_row = Adw.ComboRow(title="Format", model=Gtk.StringList.new(list(audio.EXPORT_FORMATS)))
        fmts = list(audio.EXPORT_FORMATS)
        if self.cfg.get("export_format") in fmts:
            self.fmt_row.set_selected(fmts.index(self.cfg["export_format"]))
        eg.add(self.fmt_row)
        self.loud_row = Adw.ComboRow(title="Loudness", subtitle="Normalise with ffmpeg loudnorm",
                                     model=Gtk.StringList.new(list(audio.LOUDNESS_PRESETS)))
        if self.cfg.get("export_loudness") in audio.LOUDNESS_PRESETS:
            self.loud_row.set_selected(list(audio.LOUDNESS_PRESETS).index(self.cfg["export_loudness"]))
        eg.add(self.loud_row)
        self.split_row = Adw.SwitchRow(title="Split multitrack recordings",
                                       subtitle="One file per source: mix, mics 1–4, USB, TRRS, Bluetooth, sounds",
                                       active=bool(self.cfg.get("export_split_tracks")))
        eg.add(self.split_row)
        self.dest_row = Adw.ActionRow(title="Destination", subtitle=self.cfg["export_dir"])
        choose = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_dest)
        self.dest_row.add_suffix(choose)
        eg.add(self.dest_row)
        btns = Gtk.Box(spacing=8, margin_top=12, halign=Gtk.Align.END)
        openb = Gtk.Button(label="Open folder")
        openb.connect("clicked", lambda *_: subprocess.Popen(["xdg-open", self.cfg["export_dir"]]))
        self.export_btn = Gtk.Button(label="Export selected", css_classes=["suggested-action"])
        self.export_btn.connect("clicked", self._export)
        btns.append(openb)
        btns.append(self.export_btn)
        eg.add(btns)

        ctl.connect("connected", lambda *_: self._sync())
        ctl.connect("disconnected", lambda *_: (self._sync(), GLib.timeout_add_seconds(3, self._scan_once)))
        self._sync()
        GLib.idle_add(self._scan_once)

    def _scan_once(self):
        self.scan()
        return False

    def _sync(self):
        self.enter_btn.set_sensitive(self.ctl.connected)
        self.leave_btn.set_sensitive(self.ctl.connected)

    # ------------------------------------------------------- transfer mode
    def _enter(self, *_):
        confirm(self.win, "Switch to Podcast Transfer Mode?",
                "Are you sure you would like to switch to Podcast Transfer Mode?", "Switch",
                lambda: self.ctl.device_call(
                    "set_transfer_mode", True,
                    done=lambda _r: (self.win.toast("Transfer mode requested — waiting for the card…"),
                                     GLib.timeout_add_seconds(4, self._scan_once))))

    def _leave(self, *_):
        def go():
            for r in podcasts.find_podcast_roots():
                podcasts.unmount_card(os.path.dirname(os.path.dirname(r)))
            self.ctl.device_call("set_transfer_mode", False, done=lambda _r: self.win.toast("Left transfer mode"))
        confirm(self.win, "Leave Podcast Transfer Mode?",
                "Are you sure you would like to leave Podcast Transfer Mode?", "Leave", go)

    # ------------------------------------------------------------ card scan
    def scan(self):
        roots = podcasts.find_podcast_roots()
        unmounted = podcasts.find_unmounted_cards()
        self.mount_btn.set_visible(bool(unmounted) and not roots)
        self._unmounted = unmounted
        if roots:
            self.card_row.set_subtitle(", ".join(roots))
        elif unmounted:
            self.card_row.set_subtitle(f"{unmounted[0].path} ({unmounted[0].label}) is not mounted")
        else:
            self.card_row.set_subtitle("Not found — enter transfer mode, then rescan")
        self.root = roots[0] if roots else None
        self._fill([])
        if self.root:
            root = self.root
            self.ctl.submit(lambda: podcasts.list_recordings(root), self._fill, label="Reading recordings…")

    def _mount(self, *_):
        if not getattr(self, "_unmounted", None):
            return
        dev = self._unmounted[0]
        self.ctl.submit(lambda: podcasts.mount_card(dev), lambda _m: self.scan(),
                        err=lambda e: self.win.error("Couldn't mount the card", str(e)), label="Mounting…")

    def _fill(self, recs):
        self.recs = recs
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)
        self.checks = []
        if not recs:
            self.listbox.append(Adw.ActionRow(title="No recordings", subtitle="Nothing to show"))
            return
        for r in recs:
            when = _dt.datetime.fromtimestamp(r.mtime).strftime("%Y-%m-%d %H:%M")
            kind = "multitrack" if r.multitrack else ("stereo" if r.channels == 2 else f"{r.channels} ch")
            row = Adw.ActionRow(title=r.name,
                                subtitle=f"{when} · {_fmt_dur(r.duration)} · {kind} · "
                                         f"{r.size / 1e9:.2f} GB · {len(r.files)} file(s)")
            chk = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            row.add_prefix(chk)
            row.set_activatable_widget(chk)
            self.checks.append(chk)
            self.listbox.append(row)

    # --------------------------------------------------------------- export
    def _choose_dest(self, *_):
        dlg = Gtk.FileDialog(title="Export destination")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.cfg["export_dir"] = f.get_path()
            self.cfg.save()
            self.dest_row.set_subtitle(f.get_path())
        dlg.select_folder(self.win, None, done)

    def _export(self, *_):
        chosen = [r for r, c in zip(self.recs, self.checks) if c.get_active()]
        if not chosen:
            self.win.toast("Select one or more recordings first")
            return
        fmt = list(audio.EXPORT_FORMATS)[self.fmt_row.get_selected()]
        loud = list(audio.LOUDNESS_PRESETS)[self.loud_row.get_selected()]
        split = self.split_row.get_active()
        out = self.cfg["export_dir"]
        self.cfg.update(export_format=fmt, export_loudness=loud, export_split_tracks=split)
        self.cfg.save()

        def work():
            written = []
            for i, r in enumerate(chosen):
                self.ctl.progress_cb(i / len(chosen))
                written += podcasts.export_recording(r, out, fmt, loud, split)
            self.ctl.progress_cb(1.0)
            return written
        self.ctl.submit(work, lambda w: self.win.toast(f"Exported {len(w)} file(s) to {out}"),
                        err=lambda e: self.win.error("Export failed", str(e)), label="Exporting…")
