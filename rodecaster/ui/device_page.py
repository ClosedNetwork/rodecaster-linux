"""Device page: info, clock, settings backup/restore, factory sounds, reboot."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import protocol as P
from ..assets import available_factory_sounds, extract_factory_sounds, factory_dir
from .common import confirm, file_filter


class DevicePage(Adw.PreferencesPage):
    def __init__(self, window, ctl):
        super().__init__()
        self.win = window
        self.ctl = ctl

        g = Adw.PreferencesGroup(title="RØDECaster Pro")
        self.add(g)
        self.conn_row = Adw.ActionRow(title="Connection", subtitle="Not connected")
        self.mode_row = Adw.ActionRow(title="USB mode", subtitle="—")
        self.fw_row = Adw.ActionRow(title="Firmware version", subtitle="—")
        self.id_row = Adw.ActionRow(title="Identify response", subtitle="—")
        for r in (self.conn_row, self.mode_row, self.fw_row, self.id_row):
            r.add_css_class("property")
            g.add(r)

        c = Adw.PreferencesGroup(title="Clock")
        self.add(c)
        self.auto_row = Adw.SwitchRow(title="Automatically sync time to computer",
                                      subtitle="When the unit connects",
                                      active=bool(ctl.config.get("auto_sync_time")))
        self.auto_row.connect("notify::active", self._on_auto)
        c.add(self.auto_row)
        sync = Adw.ActionRow(title="Sync now", subtitle="Set the unit's date and time (used to stamp recordings)")
        self.sync_btn = Gtk.Button(label="Sync", valign=Gtk.Align.CENTER)
        self.sync_btn.connect("clicked", lambda *_: ctl.device_call(
            "sync_time", done=lambda _r: self.win.toast("Clock synchronised")))
        sync.add_suffix(self.sync_btn)
        c.add(sync)

        s = Adw.PreferencesGroup(title="Settings", description=".rcpsettings files are compatible with the "
                                                                "RØDE app's File ▸ Advanced ▸ Save/Load Settings")
        self.add(s)
        b = Adw.ActionRow(title="Save settings to file…")
        self.backup_btn = Gtk.Button(label="Save…", valign=Gtk.Align.CENTER)
        self.backup_btn.connect("clicked", self._backup)
        b.add_suffix(self.backup_btn)
        s.add(b)
        r = Adw.ActionRow(title="Load settings from file…")
        self.restore_btn = Gtk.Button(label="Load…", valign=Gtk.Align.CENTER)
        self.restore_btn.connect("clicked", self._restore)
        r.add_suffix(self.restore_btn)
        s.add(r)

        f = Adw.PreferencesGroup(title="Factory sounds",
                                 description="RØDE's factory pad sounds are not shipped with this app. If you own the "
                                             "macOS RØDECaster Pro app (v2.1.2), extract them from its executable.")
        self.add(f)
        self.fs_row = Adw.ActionRow(title="Extracted sounds", subtitle="")
        ex = Gtk.Button(label="Extract…", valign=Gtk.Align.CENTER)
        ex.connect("clicked", self._extract)
        self.fs_row.add_suffix(ex)
        f.add(self.fs_row)

        a = Adw.PreferencesGroup(title="Maintenance")
        self.add(a)
        rb = Adw.ActionRow(title="Restart the unit")
        self.reboot_btn = Gtk.Button(label="Restart", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        self.reboot_btn.connect("clicked", self._reboot)
        rb.add_suffix(self.reboot_btn)
        a.add(rb)
        a.add(Adw.ActionRow(title="Firmware updates",
                            subtitle="Not supported on Linux. Update with RØDE's app on macOS/Windows; "
                                     "the unit keeps working with this app afterwards."))

        ctl.connect("connected", lambda *_: self.refresh())
        ctl.connect("disconnected", lambda *_: self.refresh())
        self.refresh()

    def refresh(self):
        rc = self.ctl.rc
        on = rc is not None
        for w in (self.sync_btn, self.backup_btn, self.restore_btn, self.reboot_btn):
            w.set_sensitive(on and not (rc and rc.in_transfer_mode))
        if on:
            i = rc.info
            self.conn_row.set_subtitle(f"{i.path} (USB {P.VENDOR_ID:04x}:{i.product_id:04x})")
            self.mode_row.set_subtitle(i.mode)
            self.fw_row.set_subtitle(i.firmware)
            self.id_row.set_subtitle(f"{i.identify_version} · 0x{i.identify_value:08x}")
        else:
            self.conn_row.set_subtitle("Not connected")
            for r in (self.mode_row, self.fw_row, self.id_row):
                r.set_subtitle("—")
        n = len(available_factory_sounds())
        self.fs_row.set_subtitle(f"{n} of 8 available in {factory_dir()}")

    def _on_auto(self, row, _p):
        self.ctl.config["auto_sync_time"] = row.get_active()
        self.ctl.config.save()

    def _backup(self, *_):
        dlg = Gtk.FileDialog(title="Save settings", initial_name="RCP.rcpsettings")

        def done(d, res):
            try:
                path = d.save_finish(res).get_path()
            except GLib.Error:
                return

            def write(blob):
                with open(path, "wb") as fh:
                    fh.write(P.pack_settings_file(blob))
                self.win.toast("Settings saved")
            self.ctl.device_call("backup_settings", done=write,
                                 err=lambda e: self.win.error("Save failed", str(e)))
        dlg.save(self.win, None, done)

    def _restore(self, *_):
        dlg = Gtk.FileDialog(title="Load settings", filters=file_filter("RØDECaster settings", ["*.rcpsettings"]))

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            try:
                with open(path, "rb") as fh:
                    blob = P.unpack_settings_file(fh.read())
            except (OSError, P.ProtocolError) as exc:
                self.win.error("Can't load settings", str(exc))
                return
            confirm(self.win, "Load settings?", "This overwrites the unit's current settings.", "Load",
                    lambda: self.ctl.device_call("restore_settings", blob,
                                                 done=lambda _r: self.win.toast("Settings loaded"),
                                                 err=lambda e: self.win.error("Load failed", str(e))))
        dlg.open(self.win, None, done)

    def _extract(self, *_):
        dlg = Gtk.FileDialog(title="Select “RODECaster Pro” executable (Contents/MacOS inside the .app)")

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            try:
                extract_factory_sounds(path)
            except (OSError, ValueError) as exc:
                self.win.error("Extraction failed", str(exc))
                return
            self.refresh()
            self.win.toast("Factory sounds extracted")
        dlg.open(self.win, None, done)

    def _reboot(self, *_):
        confirm(self.win, "Restart the unit?", "Make sure it is not recording.", "Restart",
                lambda: self.ctl.device_call("reboot", done=lambda _r: self.win.toast("Restart requested")),
                destructive=True)
