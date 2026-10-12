"""Soundboard page: 8 banks x 8 pads, pad editor, sound transfer."""

from __future__ import annotations

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import audio
from .. import protocol as P
from ..assets import available_factory_sounds
from .common import confirm, file_filter, rgb_to_hex

PAD_CSS_CLASS = "rc-pad"


def _text_colour(rgb):
    r, g, b = rgb
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#f5f5f5"


class PadButton(Gtk.Button):
    def __init__(self, index: int, on_drop):
        super().__init__()
        self.index = index
        self.add_css_class(PAD_CSS_CLASS)
        self.set_name(f"rc-pad-{index}")
        self.set_size_request(150, 110)
        overlay = Gtk.Overlay()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.num = Gtk.Label(label=str(index + 1), halign=Gtk.Align.START, valign=Gtk.Align.START,
                             margin_start=4, margin_top=2, css_classes=["caption-heading", "dim-label"])
        self.title = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER,
                               css_classes=["title-3"], max_width_chars=14, ellipsize=3)
        self.sub = Gtk.Label(label="", css_classes=["caption", "dim-label"])
        box.append(self.title)
        box.append(self.sub)
        overlay.set_child(box)
        overlay.add_overlay(self.num)
        self.set_child(overlay)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, value, _x, _y: on_drop(self.index, value))
        self.add_controller(drop)

    def update(self, st):
        if st is None:
            self.title.set_label("…")
            self.sub.set_label("")
            return
        self.title.set_label(st.name or ("(unnamed)" if st.has_sound else "Empty"))
        bits = [P.PAD_MODE_LABELS.get(st.mode, f"mode {st.mode}").split(" ")[0]]
        if st.loop:
            bits.append("⟲ loop")
        if st.has_sound:
            bits.append(f"{st.size / 1e6:.1f} MB")
        self.sub.set_label(" · ".join(bits))
        self.set_opacity(1.0 if st.has_sound else 0.75)


class SoundboardPage(Gtk.Box):
    def __init__(self, window, ctl):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = window
        self.ctl = ctl
        self.bank = 0
        self.selected = 0
        self._updating = False
        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END, min_sidebar_width=320,
                                     max_sidebar_width=380, vexpand=True)
        self.append(split)

        # ---- main area
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        split.set_content(main)

        bankbar = Gtk.Box(spacing=6)
        main.append(bankbar)
        self.bank_buttons = []
        group = None
        for b in range(P.BANKS):
            btn = Gtk.ToggleButton(label=str(b + 1), css_classes=["circular"])
            if group:
                btn.set_group(group)
            group = group or btn
            btn.connect("toggled", self._on_bank_toggled, b)
            bankbar.append(btn)
            self.bank_buttons.append(btn)
        self.bank_entry = Gtk.Entry(placeholder_text="Bank name", max_length=P.PAD_NAME_LEN - 1,
                                    hexpand=True, margin_start=12)
        self.bank_entry.connect("activate", self._on_bank_name)
        bankbar.append(self.bank_entry)
        menu = Gio.Menu()
        menu.append("Reload bank", "sb.reload")
        menu.append("Clear this bank…", "sb.clear-bank")
        menu.append("Restore factory sounds…", "sb.factory")
        bankbar.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                                      tooltip_text="Bank actions"))
        actions = Gio.SimpleActionGroup()
        for name, cb in [("reload", lambda *_: self.reload()), ("clear-bank", self._clear_bank),
                         ("factory", self._factory)]:
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            actions.add_action(a)
        self.insert_action_group("sb", actions)

        grid = Gtk.Grid(row_spacing=12, column_spacing=12, row_homogeneous=True,
                        column_homogeneous=True, vexpand=True, hexpand=True)
        self.pads = []
        for i in range(P.PADS_PER_BANK):
            pb = PadButton(i, self._on_drop)
            pb.connect("clicked", self._on_pad_clicked)
            grid.attach(pb, i % 4, i // 4, 1, 1)
            self.pads.append(pb)
        main.append(grid)

        foot = Gtk.Box(spacing=12)
        self.mem_label = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label"])
        foot.append(self.mem_label)
        foot.append(Gtk.Label(label="Drop audio files onto a pad to load them", css_classes=["dim-label", "caption"]))
        main.append(foot)

        # ---- pad editor sidebar
        side = Adw.ToolbarView()
        side_header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.side_title = Adw.WindowTitle(title="Pad 1")
        side_header.set_title_widget(self.side_title)
        side.add_top_bar(side_header)
        page = Adw.PreferencesPage()
        side.set_content(page)
        split.set_sidebar(side)

        g = Adw.PreferencesGroup(title="Pad")
        page.add(g)
        self.name_row = Adw.EntryRow(title="Name", show_apply_button=True)
        self.name_row.connect("apply", self._on_name)
        g.add(self.name_row)

        colour_row = Adw.ActionRow(title="Colour")
        self.colour_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        self.colour_btn.connect("notify::rgba", self._on_colour)
        colour_row.add_suffix(self.colour_btn)
        g.add(colour_row)

        self.mode_row = Adw.ComboRow(title="Playback style",
                                     model=Gtk.StringList.new([P.PAD_MODE_LABELS[m] for m in P.PadMode]))
        self.mode_row.connect("notify::selected", self._on_mode)
        g.add(self.mode_row)

        self.loop_row = Adw.SwitchRow(title="Loop", subtitle="Repeat the sound while playing")
        self.loop_row.connect("notify::active", self._on_loop)
        g.add(self.loop_row)

        g2 = Adw.PreferencesGroup(title="Sound")
        page.add(g2)
        self.info_row = Adw.ActionRow(title="Empty", subtitle="")
        g2.add(self.info_row)
        self.normalize_row = Adw.SwitchRow(title="Normalise loudness on load",
                                           subtitle="−16 LUFS via ffmpeg loudnorm",
                                           active=bool(ctl.config.get("normalize_uploads")))
        self.normalize_row.connect("notify::active", self._on_normalize_pref)
        g2.add(self.normalize_row)
        btns = Gtk.Box(spacing=6, homogeneous=True, margin_top=12)
        self.load_btn = Gtk.Button(label="Load…", css_classes=["suggested-action"])
        self.load_btn.connect("clicked", self._on_load)
        self.save_btn = Gtk.Button(label="Save…")
        self.save_btn.connect("clicked", self._on_save)
        self.clear_btn = Gtk.Button(label="Clear", css_classes=["destructive-action"])
        self.clear_btn.connect("clicked", self._on_clear)
        for b in (self.load_btn, self.save_btn, self.clear_btn):
            btns.append(b)
        g2.add(btns)

        ctl.connect("soundboard-changed", lambda *_: self.refresh())
        ctl.connect("pad-changed", lambda *_: self.refresh())
        ctl.connect("connected", lambda *_: self._on_connected())
        self.bank_buttons[0].set_active(True)
        self.refresh()

    # ------------------------------------------------------------- refresh
    def _on_connected(self):
        self.ctl.load_bank(self.bank)

    def reload(self):
        self.ctl.load_bank(self.bank)

    def _addr(self, pad=None):
        return P.pad_address(self.bank, self.selected if pad is None else pad)

    def refresh(self):
        self._updating = True
        try:
            loaded = self.bank in self.ctl.loaded_banks
            css = []
            for i, pb in enumerate(self.pads):
                st = self.ctl.sb.pads.get(self._addr(i)) if loaded else None
                pb.update(st)
                rgb = st.colour if st else (90, 90, 90)
                hx = rgb_to_hex(rgb)
                lit = st is not None and st.has_sound
                sel = i == self.selected
                css.append(
                    f"#rc-pad-{i} {{ border-radius: 16px; border: {'3px' if sel else '2px'} solid "
                    f"{hx if lit else 'alpha(' + hx + ', .45)'};"
                    f" background-image: linear-gradient(to bottom, alpha({hx}, {'.38' if lit else '.10'}),"
                    f" alpha({hx}, {'.16' if lit else '.04'}));"
                    f" box-shadow: {'0 0 18px alpha(' + hx + ', .55)' if sel else 'none'}; }}"
                    f" #rc-pad-{i}:hover {{ background-image: linear-gradient(to bottom, alpha({hx}, .48),"
                    f" alpha({hx}, .22)); }}")
                pb.set_sensitive(self.ctl.connected and loaded)
            self.css.load_from_string("\n".join(css)) if hasattr(self.css, "load_from_string") \
                else self.css.load_from_data("\n".join(css).encode())
            if loaded:
                self.bank_entry.set_text(self.ctl.sb.banks[self.bank])
                mem = self.ctl.sb.free_memory
                self.mem_label.set_label(f"{mem / 1e6:.1f} MB free for sounds · "
                                         f"{self.ctl.sb.bank_count} banks enabled on the unit")
            else:
                self.mem_label.set_label("Not connected" if not self.ctl.connected else "Reading…")
            st = self.ctl.sb.pads.get(self._addr()) if loaded else None
            self.side_title.set_title(f"Bank {self.bank + 1} · Pad {self.selected + 1}")
            enabled = st is not None and self.ctl.connected
            for w in (self.name_row, self.colour_btn, self.mode_row, self.loop_row,
                      self.load_btn, self.save_btn, self.clear_btn):
                w.set_sensitive(enabled)
            if st:
                self.name_row.set_text(st.name)
                rgba = Gdk.RGBA()
                rgba.parse(rgb_to_hex(st.colour))
                self.colour_btn.set_rgba(rgba)
                self.mode_row.set_selected(st.mode if st.mode < len(P.PadMode) else 0)
                self.loop_row.set_active(st.loop)
                if st.has_sound:
                    self.info_row.set_title(f"{st.size / 1e6:.2f} MB on the unit")
                    self.info_row.set_subtitle(
                        f"≈ {st.duration_seconds(2):.1f} s stereo / {st.duration_seconds(1):.1f} s mono · "
                        f"CRC {st.crc:08x}")
                else:
                    self.info_row.set_title("Empty")
                    self.info_row.set_subtitle("Load a WAV, MP3, FLAC, OGG… or drop it on the pad")
                self.save_btn.set_sensitive(st.has_sound)
                self.clear_btn.set_sensitive(st.has_sound)
        finally:
            self._updating = False

    # -------------------------------------------------------------- events
    def _on_bank_toggled(self, btn, bank):
        if not btn.get_active():
            return
        self.bank = bank
        if self.ctl.connected and bank not in self.ctl.loaded_banks:
            self.ctl.load_bank(bank)
        self.refresh()

    def _on_pad_clicked(self, pb):
        self.selected = pb.index
        self.refresh()

    def _on_bank_name(self, entry):
        name = entry.get_text()
        b = self.bank
        self.ctl.device_call("set_bank_name", b, name,
                             done=lambda _r: self.ctl.sb.banks.__setitem__(b, name) or self.win.toast("Bank renamed"))

    def _on_name(self, row):
        addr = self._addr()
        self.ctl.device_call("set_pad_name", addr, row.get_text(), refresh_pad=addr)

    def _on_colour(self, btn, _p):
        if self._updating:
            return
        c = btn.get_rgba()
        rgb = (round(c.red * 255), round(c.green * 255), round(c.blue * 255))
        addr = self._addr()
        self.ctl.device_call("set_pad_colour", addr, rgb, refresh_pad=addr)

    def _on_mode(self, row, _p):
        if self._updating:
            return
        addr = self._addr()
        self.ctl.device_call("set_pad_mode", addr, row.get_selected(), refresh_pad=addr)

    def _on_loop(self, row, _p):
        if self._updating:
            return
        addr = self._addr()
        self.ctl.device_call("set_pad_loop", addr, row.get_active(), refresh_pad=addr)

    def _on_normalize_pref(self, row, _p):
        self.ctl.config["normalize_uploads"] = row.get_active()
        self.ctl.config.save()

    # ------------------------------------------------------- sound transfer
    def _on_drop(self, pad_index, value):
        files = value.get_files()
        if not files:
            return False
        self.selected = pad_index
        self.refresh()
        self.upload(files[0].get_path(), pad_index)
        return True

    def _on_load(self, _btn):
        dlg = Gtk.FileDialog(title="Load sound onto pad", filters=file_filter("Audio", audio.AUDIO_PATTERNS))

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            self.upload(f.get_path(), self.selected)
        dlg.open(self.win, None, done)

    def upload(self, path: str, pad_index: int, name: str | None = None):
        if not audio.have_ffmpeg():
            self.win.toast("ffmpeg is required to load sounds (pacman -S ffmpeg)")
            return
        rc = self.ctl.rc
        if rc is None:
            return
        addr = self._addr(pad_index)
        name = name or audio.pad_name_from_path(path)
        normalize = self.normalize_row.get_active()
        self.ctl.cancel_event.clear()

        def work():
            ch, pcm = audio.decode_for_pad(path, normalize=normalize)
            rc.upload_pcm(addr, pcm, ch, name, progress=self.ctl.progress_cb, cancel=self.ctl.cancel_event)
            rc.set_pad_name(addr, name)
            return name

        def ok(n):
            self.ctl.refresh_pad(addr)
            self.win.toast(f"Loaded “{n}” onto pad {pad_index + 1}")
        self.ctl.submit(work, ok, err=lambda e: self.win.error("Couldn't load sound", str(e)),
                        label=f"Sending “{os.path.basename(path)}”…")

    def _on_save(self, _btn):
        st = self.ctl.sb.pads.get(self._addr())
        dlg = Gtk.FileDialog(title="Save pad sound", initial_name=f"{(st.name if st else '') or 'pad'}.wav")

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            self.download(self._addr(), f.get_path())
        dlg.save(self.win, None, done)

    def download(self, addr, path):
        rc = self.ctl.rc
        self.ctl.cancel_event.clear()

        def work():
            ch, pcm = rc.download_pcm(addr, progress=self.ctl.progress_cb, cancel=self.ctl.cancel_event)
            audio.write_wav(path, ch, pcm)
            return path
        self.ctl.submit(work, lambda p: self.win.toast(f"Saved {os.path.basename(p)}"),
                        err=lambda e: self.win.error("Error downloading sound", str(e)),
                        label="Receiving sound…")

    def _on_clear(self, _btn):
        addr = self._addr()
        confirm(self.win, "Clear pad?", "Are you sure you want to clear the pad?", "Clear",
                lambda: self.ctl.device_call("clear_pad", addr, refresh_pad=addr), destructive=True)

    def _clear_bank(self, *_):
        b = self.bank
        confirm(self.win, f"Clear bank {b + 1}?", "Are you sure you want to clear all sounds on the bank?",
                "Clear bank", lambda: self.ctl.device_call("clear_bank", b, done=lambda _r: self.ctl.load_bank(b)),
                destructive=True)

    def _factory(self, *_):
        sounds = available_factory_sounds()
        if len(sounds) != 8:
            self.win.error("Factory sounds not available",
                           "RØDE's factory sounds aren't bundled. Extract them from your copy of the macOS app "
                           "on the Device page (Factory sounds → Extract), then try again.")
            return
        rc = self.ctl.rc

        def work():
            for b in reversed(range(P.BANKS)):
                rc.clear_bank(b)
            for i, (path, name) in enumerate(sounds):
                ch, pcm = audio.decode_for_pad(path)
                rc.upload_pcm(P.pad_address(0, i), pcm, ch, name,
                              progress=lambda f, i=i: self.ctl.progress_cb((i + f) / 8))
                rc.set_pad_name(P.pad_address(0, i), name)

        def ok(_r):
            self.ctl.loaded_banks.clear()
            self.bank_buttons[0].set_active(True)
            self.ctl.load_bank(0)
            self.win.toast("Factory sounds restored")
        confirm(self.win, "Restore factory sounds?",
                "Are you sure you want to restore the factory sounds? This clears all 8 banks.",
                "Restore", lambda: self.ctl.submit(work, ok, label="Restoring factory sounds…",
                                                   err=lambda e: self.win.error("Restore failed", str(e))),
                destructive=True)
