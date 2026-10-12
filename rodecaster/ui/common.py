from __future__ import annotations

from gi.repository import Adw, Gio, Gtk


def rgb_to_hex(rgb) -> str:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def file_filter(name: str, patterns: list[str]) -> Gio.ListStore:
    f = Gtk.FileFilter(name=name)
    for p in patterns:
        f.add_pattern(p)
        f.add_pattern(p.upper())
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(f)
    allf = Gtk.FileFilter(name="All files")
    allf.add_pattern("*")
    store.append(allf)
    return store


def confirm(parent, heading: str, body: str, action_label: str, on_yes, destructive=False):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("ok", action_label)
    dlg.set_response_appearance(
        "ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("cancel")
    dlg.connect("response", lambda _d, r: on_yes() if r == "ok" else None)
    dlg.present(parent)
