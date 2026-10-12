"""GTK4/libadwaita application shell."""

from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import __version__  # noqa: E402
from ..config import APP_ID, Config  # noqa: E402
from .controller import Controller  # noqa: E402
from .device_page import DevicePage  # noqa: E402
from .effects_page import EffectsPage  # noqa: E402
from .recordings import RecordingsPage  # noqa: E402
from .soundboard import SoundboardPage  # noqa: E402

log = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, ctl: Controller):
        super().__init__(application=app, title="RØDECaster Pro", default_width=1180, default_height=780)
        self.ctl = ctl
        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.status_icon = Gtk.Image(icon_name="network-offline-symbolic", tooltip_text="Not connected")
        header.pack_start(self.status_icon)
        self.spinner = Gtk.Spinner()
        header.pack_end(self._app_menu())
        header.pack_end(self.spinner)
        tv.add_top_bar(header)

        self.banner = Adw.Banner(title="Connect your RØDECaster Pro with USB…", revealed=True)
        if ctl.simulate:
            self.banner.set_title("Simulator mode — no hardware is being controlled")
        tv.add_top_bar(self.banner)

        self.progress = Gtk.ProgressBar(visible=False, show_text=True)
        self.cancel_btn = Gtk.Button(label="Cancel", visible=False, halign=Gtk.Align.END)
        self.cancel_btn.connect("clicked", lambda *_: ctl.cancel_event.set())
        pbox = Gtk.Box(spacing=12, margin_start=12, margin_end=12, margin_bottom=6)
        self.progress.set_hexpand(True)
        self.progress.set_valign(Gtk.Align.CENTER)
        pbox.append(self.progress)
        pbox.append(self.cancel_btn)
        tv.add_bottom_bar(pbox)

        self.soundboard = SoundboardPage(self, ctl)
        self.effects = EffectsPage(self, ctl)
        self.recordings = RecordingsPage(self, ctl)
        self.device = DevicePage(self, ctl)
        for page, name, title, icon in [
            (self.soundboard, "soundboard", "Soundboard", "view-app-grid-symbolic"),
            (self.effects, "effects", "Effects", "preferences-other-symbolic"),
            (self.recordings, "recordings", "Recordings", "media-record-symbolic"),
            (self.device, "device", "Device", "audio-card-symbolic"),
        ]:
            self.stack.add_titled_with_icon(page, name, title, icon)
        tv.set_content(self.stack)

        ctl.connect("connected", self._on_connected)
        ctl.connect("disconnected", self._on_disconnected)
        ctl.connect("busy-changed", self._on_busy)
        ctl.connect("progress", self._on_progress)
        ctl.connect("error", lambda _c, msg: self.error("Device error", msg))

    def _app_menu(self):
        menu = Gio.Menu()
        menu.append("Reconnect", "app.reconnect")
        menu.append("About", "app.about")
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True)

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))

    def error(self, heading: str, body: str):
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def _on_connected(self, ctl):
        rc = ctl.rc
        self.status_icon.set_from_icon_name("network-wired-symbolic")
        self.status_icon.set_tooltip_text(f"Connected: {rc.info.path} · firmware {rc.info.firmware}")
        if rc.in_transfer_mode:
            self.banner.set_title("The unit is in USB transfer mode. Leave transfer mode (Recordings page) to use the other controls.")
            self.banner.set_revealed(True)
        elif ctl.simulate:
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
            self.toast(f"RØDECaster Pro connected (firmware {rc.info.firmware})")

    def _on_disconnected(self, _ctl, reason):
        self.status_icon.set_from_icon_name("network-offline-symbolic")
        self.status_icon.set_tooltip_text("Not connected")
        title = "Connect your RØDECaster Pro with USB…"
        if reason and reason != "closed":
            title = f"Disconnected: {reason}"
        self.banner.set_title(title)
        self.banner.set_revealed(True)
        self.soundboard.refresh()

    def _on_busy(self, _ctl, busy, label):
        self.spinner.set_spinning(busy)
        if label:
            self.progress.set_text(label)
            self.progress.set_fraction(0)
            self.progress.set_visible(True)
            self.cancel_btn.set_visible(True)
        if not busy:
            self.progress.set_visible(False)
            self.cancel_btn.set_visible(False)

    def _on_progress(self, _ctl, frac):
        self.progress.set_visible(True)
        self.progress.set_fraction(max(0.0, min(1.0, frac)))


class App(Adw.Application):
    def __init__(self, simulate=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE if simulate
                         else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.simulate = simulate
        self.ctl = None
        self.win = None
        for name, cb in [("about", self._about), ("reconnect", self._reconnect), ("quit", lambda *_: self.quit())]:
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        if self.win is None:
            self.ctl = Controller(Config(), simulate=self.simulate)
            self.win = MainWindow(self, self.ctl)
        self.win.present()

    def do_shutdown(self):
        if self.ctl:
            self.ctl.shutdown()
        Adw.Application.do_shutdown(self)

    def _reconnect(self, *_):
        self.ctl.disconnect()

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name="RØDECaster Pro for Linux",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Community project",
            license_type=Gtk.License.MIT_X11,
            comments="Unofficial companion app for the original RØDECaster Pro (gen 1). "
                     "Not affiliated with or endorsed by RØDE Microphones.",
        )
        about.present(self.win)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    simulate = "--simulate" in argv
    if simulate:
        argv.remove("--simulate")
    verbose = "-v" in argv
    if verbose:
        argv.remove("-v")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return App(simulate=simulate).run(argv)


if __name__ == "__main__":
    sys.exit(main())
