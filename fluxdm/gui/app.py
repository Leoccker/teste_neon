"""Aplicação GTK4 + Libadwaita principal do FluxDM."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from fluxdm import APP_ID, APP_NAME
from fluxdm.engine import DownloadManager
from fluxdm.gui.window import MainWindow


class FluxDMApplication(Adw.Application):
    """Aplicação principal GTK4/Libadwaita que gerencia ciclo de vida, CSS e atalhos."""

    def __init__(self, initial_urls: Optional[list[str]] = None) -> None:
        flags = getattr(
            Gio.ApplicationFlags,
            "DEFAULT_FLAGS",
            Gio.ApplicationFlags.FLAGS_NONE,
        )
        super().__init__(application_id=APP_ID, flags=flags)

        self._initial_urls: list[str] = [
            u.strip() for u in (initial_urls or []) if u and u.strip()
        ]
        self._manager: Optional[DownloadManager] = None
        self._window: Optional[MainWindow] = None
        self._css_provider: Optional[Gtk.CssProvider] = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)

        self._load_css()
        self._register_actions_and_shortcuts()

        if self._manager is None:
            self._manager = DownloadManager()

    def do_activate(self) -> None:
        if self._manager is None:
            self._manager = DownloadManager()

        if self._window is None:
            self._window = MainWindow(application=self, manager=self._manager)

        self._window.present()

        if self._initial_urls:
            urls = list(self._initial_urls)
            self._initial_urls.clear()
            GLib.idle_add(self._handle_initial_urls, urls)

    def do_shutdown(self) -> None:
        if self._manager is not None:
            try:
                self._manager.shutdown()
            finally:
                self._manager = None
        Adw.Application.do_shutdown(self)

    def _load_css(self) -> None:
        css_path = Path(__file__).with_name("style.css")
        if not css_path.exists():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self._css_provider = provider

    def _register_actions_and_shortcuts(self) -> None:
        self._add_action("new-download", self._on_new_download, ["<Primary>n"])
        self._add_action("search", self._on_search, ["<Primary>f"])
        self._add_action("preferences", self._on_preferences, ["<Primary>comma"])
        self._add_action("about", self._on_about)
        self._add_action("quit", self._on_quit, ["<Primary>q"])

    def _add_action(
        self,
        name: str,
        callback,
        accels: Optional[list[str]] = None,
    ) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def _handle_initial_urls(self, urls: list[str]) -> bool:
        if self._window is None or not urls:
            return False
        first, *rest = urls
        self._window.open_add_dialog(initial_url=first)
        if rest and self._manager is not None:
            for extra_url in rest:
                if extra_url.startswith(("http://", "https://")):
                    try:
                        self._manager.add_download(url=extra_url)
                    except Exception:
                        pass
        return False

    def _on_new_download(self, *_args) -> None:
        if self._window is not None:
            self._window.open_add_dialog()

    def _on_search(self, *_args) -> None:
        if self._window is not None:
            self._window.toggle_search()

    def _on_preferences(self, *_args) -> None:
        if self._window is not None:
            self._window.open_preferences_dialog()

    def _on_about(self, *_args) -> None:
        if self._window is not None:
            self._window.open_about_dialog()

    def _on_quit(self, *_args) -> None:
        self.quit()


def run_gui(urls: Optional[list[str]] = None) -> int:
    """Inicia a interface gráfica do FluxDM e retorna o código de saída."""
    app = FluxDMApplication(initial_urls=urls)
    return int(app.run(["fluxdm"]))
