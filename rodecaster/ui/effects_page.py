"""Effects page: per-channel processing (raw values) + presets."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import protocol as P
from ..effects import GROUPS, ChannelFx, field_range
from .common import confirm, file_filter

PRETTY = {
    "EqCutoff": "High-pass cutoff", "GateThreshold": "Threshold", "GateAttack": "Attack",
    "GateRelease": "Release", "GateDecay": "Decay", "GateRange": "Range", "GateHysteresis": "Hysteresis",
    "DeesserThreshold": "Threshold", "DeesserRatio": "Ratio", "DeesserAttack": "Attack",
    "DeesserRelease": "Release", "DeesserGain": "Gain", "DeesserFrequency": "Frequency",
    "CompThreshold": "Threshold", "CompRatio": "Ratio", "CompAttack": "Attack", "CompRelease": "Release",
    "CompGain": "Make-up gain", "CompKnee": "Knee", "ExciterTune": "Tune", "ExciterHarmonics": "Harmonics",
    "ExciterMix": "Mix", "BBDrive": "Drive", "BBTune": "Tune", "BBMix": "Mix",
    "ReverbSendLevel": "Send level", "ReverbModel": "Model", "ReverbDecay": "Decay", "ReverbCutoff": "Cutoff",
    "AllProcessingEnable": "All processing", "DynamicsEnable": "Enabled", "HPFilterEnable": "Enabled",
    "NoiseGateEnable": "Enabled", "DeesserEnable": "Enabled", "AuralExciterEnable": "Enabled",
    "BigBottomEnable": "Enabled",
}


class EffectsPage(Gtk.Box):
    def __init__(self, window, ctl):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = window
        self.ctl = ctl
        self.fx: ChannelFx | None = None
        self.rows: dict[str, Gtk.Widget] = {}
        self._updating = False

        bar = Gtk.Box(spacing=8, margin_top=12, margin_start=18, margin_end=18)
        self.chan = Gtk.DropDown.new_from_strings(list(P.FX_CHANNELS.values()))
        self.chan.connect("notify::selected", lambda *_: self.read())
        bar.append(Gtk.Label(label="Channel"))
        bar.append(self.chan)
        self.edit_switch = Gtk.Switch(valign=Gtk.Align.CENTER,
                                      tooltip_text="Effects Edit Mode on the unit (required to change effects)")
        self.edit_switch.connect("notify::active", self._on_edit_mode)
        bar.append(Gtk.Label(label="Effects edit mode", margin_start=12))
        bar.append(self.edit_switch)
        bar.append(Gtk.Box(hexpand=True))
        for label, cb, cls in [("Read", lambda *_: self.read(), None),
                               ("Defaults", self._defaults, None),
                               ("Load preset…", self._load_preset, None),
                               ("Save preset…", self._save_preset, None),
                               ("Apply to unit", self._apply, "suggested-action")]:
            b = Gtk.Button(label=label)
            if cls:
                b.add_css_class(cls)
            b.connect("clicked", cb)
            bar.append(b)
        self.append(bar)

        self.banner = Adw.Banner(
            title="Values are the raw numbers the unit stores (units not yet verified). "
                  "Change them in small steps and listen.", revealed=True)
        self.append(self.banner)

        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)
        for title, names in GROUPS.items():
            g = Adw.PreferencesGroup(title=title)
            page.add(g)
            for n in names:
                if n.endswith("Enable"):
                    row = Adw.SwitchRow(title=PRETTY.get(n, n), subtitle=n)
                    row.connect("notify::active", self._on_change, n)
                else:
                    lo, hi = field_range(n)
                    row = Adw.SpinRow.new_with_range(lo, hi, 1)
                    row.set_title(PRETTY.get(n, n))
                    row.set_subtitle(n)
                    row.connect("notify::value", self._on_change, n)
                g.add(row)
                self.rows[n] = row
        self.dirty_label = Gtk.Label(css_classes=["dim-label"], margin_bottom=8)
        self.append(self.dirty_label)

        ctl.connect("connected", lambda *_: self.read())
        ctl.connect("status-changed", self._on_status)
        self._sync_sensitivity()

    @property
    def channel(self) -> int:
        return self.chan.get_selected()

    def _sync_sensitivity(self):
        on = self.ctl.connected and self.fx is not None
        for r in self.rows.values():
            r.set_sensitive(on)

    def _on_status(self, _ctl, flags):
        self._updating = True
        self.edit_switch.set_active(bool(flags & P.STATUS_EFFECTS_EDIT))
        self._updating = False

    def _on_edit_mode(self, sw, _p):
        if self._updating or not self.ctl.connected:
            return
        self.ctl.device_call("set_effects_edit_mode", sw.get_active())

    def read(self):
        if not self.ctl.connected or self.ctl.rc.in_transfer_mode:
            return
        ch = self.channel
        self.ctl.device_call("read_fx", ch, done=self._show, label=f"Reading {P.FX_CHANNELS[ch]} effects…")

    def _show(self, fx: ChannelFx):
        self.fx = fx
        self._updating = True
        try:
            for n, row in self.rows.items():
                v = fx.values[n]
                if isinstance(row, Adw.SwitchRow):
                    row.set_active(bool(v))
                else:
                    row.set_value(v)
        finally:
            self._updating = False
        self.dirty_label.set_label("")
        self._sync_sensitivity()

    def _on_change(self, row, _p, name):
        if self._updating or self.fx is None:
            return
        self.fx.values[name] = int(row.get_active() if isinstance(row, Adw.SwitchRow) else row.get_value())
        self.dirty_label.set_label("Unsaved changes — press “Apply to unit”")

    def _apply(self, *_):
        if self.fx is None:
            return
        if not (self.ctl.status_flags & P.STATUS_EFFECTS_EDIT):
            self.win.toast("To edit effects enable effects edit mode above or on your RØDECaster Pro")
            return
        ch, fx = self.channel, self.fx
        self.ctl.device_call("write_fx", ch, fx, done=lambda _r: (self.read(), self.win.toast("Effects sent")))

    def _defaults(self, *_):
        confirm(self.win, "Reset to defaults?",
                "Are you sure you want to set the effect parameters to default values?", "Reset",
                lambda: (self._show(ChannelFx.defaults()), self.dirty_label.set_label(
                    "Defaults loaded — press “Apply to unit”")))

    def _save_preset(self, *_):
        if self.fx is None:
            return
        dlg = Gtk.FileDialog(title="Save effects preset", initial_name="preset.rcfx.json")

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            with open(f.get_path(), "w") as fh:
                fh.write(self.fx.to_json())
            self.win.toast("Preset saved")
        dlg.save(self.win, None, done)

    def _load_preset(self, *_):
        dlg = Gtk.FileDialog(title="Load effects preset", filters=file_filter("Effects preset", ["*.json"]))

        def done(d, res):
            try:
                f = d.open_finish(res)
            except GLib.Error:
                return
            try:
                with open(f.get_path()) as fh:
                    fx = ChannelFx.from_json(fh.read())
            except (OSError, ValueError) as exc:
                self.win.error("Not a preset", str(exc))
                return
            self._show(fx)
            self.dirty_label.set_label("Preset loaded — press “Apply to unit”")
        dlg.open(self.win, None, done)
