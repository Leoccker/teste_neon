"""Janela principal GTK4 + Libadwaita do FluxDM."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from fluxdm import APP_NAME
from fluxdm.engine import DownloadManager
from fluxdm.gui.dialogs import (
    STATUS_LABELS,
    AddDownloadDialog,
    DetailsDialog,
    PreferencesDialog,
    confirm_remove_dialog,
    copy_to_clipboard,
    show_about_dialog,
)
from fluxdm.models import (
    AppConfig,
    DownloadItem,
    DownloadStatus,
    format_bytes,
    format_eta,
    format_speed,
)

FILTER_ITEMS: list[tuple[str, str, str]] = [
    ("all", "Todos", "folder-download-symbolic"),
    ("active", "Ativos", "emblem-synchronizing-symbolic"),
    ("completed", "Concluídos", "emblem-ok-symbolic"),
    ("paused", "Pausados", "media-playback-pause-symbolic"),
    ("error", "Com Erro", "dialog-error-symbolic"),
]

_STATUS_CSS_CLASSES: dict[DownloadStatus, str] = {
    DownloadStatus.QUEUED: "status-queued",
    DownloadStatus.DOWNLOADING: "status-downloading",
    DownloadStatus.PAUSED: "status-paused",
    DownloadStatus.COMPLETED: "status-completed",
    DownloadStatus.ERROR: "status-error",
    DownloadStatus.CANCELLED: "status-cancelled",
}

_EXT_ICON_MAP: dict[str, str] = {
    # Compactados
    ".zip": "package-x-generic-symbolic",
    ".tar": "package-x-generic-symbolic",
    ".gz": "package-x-generic-symbolic",
    ".tgz": "package-x-generic-symbolic",
    ".xz": "package-x-generic-symbolic",
    ".bz2": "package-x-generic-symbolic",
    ".7z": "package-x-generic-symbolic",
    ".rar": "package-x-generic-symbolic",
    ".zst": "package-x-generic-symbolic",
    # Imagens de disco e pacotes
    ".iso": "media-optical-symbolic",
    ".img": "media-optical-symbolic",
    ".deb": "package-x-generic-symbolic",
    ".rpm": "package-x-generic-symbolic",
    ".appimage": "application-x-executable-symbolic",
    ".flatpak": "package-x-generic-symbolic",
    # Vídeo
    ".mp4": "video-x-generic-symbolic",
    ".mkv": "video-x-generic-symbolic",
    ".avi": "video-x-generic-symbolic",
    ".mov": "video-x-generic-symbolic",
    ".webm": "video-x-generic-symbolic",
    ".flv": "video-x-generic-symbolic",
    ".wmv": "video-x-generic-symbolic",
    # Áudio
    ".mp3": "audio-x-generic-symbolic",
    ".flac": "audio-x-generic-symbolic",
    ".ogg": "audio-x-generic-symbolic",
    ".opus": "audio-x-generic-symbolic",
    ".wav": "audio-x-generic-symbolic",
    ".m4a": "audio-x-generic-symbolic",
    ".aac": "audio-x-generic-symbolic",
    # Imagem
    ".png": "image-x-generic-symbolic",
    ".jpg": "image-x-generic-symbolic",
    ".jpeg": "image-x-generic-symbolic",
    ".gif": "image-x-generic-symbolic",
    ".webp": "image-x-generic-symbolic",
    ".svg": "image-x-generic-symbolic",
    ".bmp": "image-x-generic-symbolic",
    # Documentos
    ".pdf": "x-office-document-symbolic",
    ".doc": "x-office-document-symbolic",
    ".docx": "x-office-document-symbolic",
    ".odt": "x-office-document-symbolic",
    ".txt": "text-x-generic-symbolic",
    ".md": "text-x-generic-symbolic",
    ".csv": "x-office-spreadsheet-symbolic",
    ".xlsx": "x-office-spreadsheet-symbolic",
    ".ods": "x-office-spreadsheet-symbolic",
    # Código
    ".py": "text-x-script-symbolic",
    ".sh": "text-x-script-symbolic",
    ".js": "text-x-script-symbolic",
    ".ts": "text-x-script-symbolic",
    ".json": "text-x-script-symbolic",
    ".yaml": "text-x-script-symbolic",
    ".yml": "text-x-script-symbolic",
}


def _infer_file_icon(filename: str, content_type: str = "") -> str:
    ext = Path(filename).suffix.lower()
    if ext in _EXT_ICON_MAP:
        return _EXT_ICON_MAP[ext]
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return "video-x-generic-symbolic"
    if ct.startswith("audio/"):
        return "audio-x-generic-symbolic"
    if ct.startswith("image/"):
        return "image-x-generic-symbolic"
    if "pdf" in ct or "document" in ct:
        return "x-office-document-symbolic"
    if "zip" in ct or "compressed" in ct or "tar" in ct or "gzip" in ct:
        return "package-x-generic-symbolic"
    return "text-x-generic-symbolic"


def _shorten_home(path_str: str) -> str:
    try:
        home = str(Path.home())
        if path_str == home:
            return "~"
        if path_str.startswith(home + "/"):
            return "~" + path_str[len(home):]
    except Exception:
        pass
    return path_str


def _extract_http_url(text: str) -> Optional[str]:
    """Extrai a primeira URL HTTP(S) válida de texto solto ou lista de URIs."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            return line.split()[0]
    return None


def _matches_filter(item: DownloadItem, filter_key: str) -> bool:
    if filter_key == "all":
        return True
    if filter_key == "active":
        return item.status in (DownloadStatus.DOWNLOADING, DownloadStatus.QUEUED)
    if filter_key == "completed":
        return item.status == DownloadStatus.COMPLETED
    if filter_key == "paused":
        return item.status == DownloadStatus.PAUSED
    if filter_key == "error":
        return item.status in (DownloadStatus.ERROR, DownloadStatus.CANCELLED)
    return True


class DownloadRow(Gtk.ListBoxRow):
    """Linha rica customizada para exibir e controlar um DownloadItem."""

    def __init__(
        self,
        item: DownloadItem,
        on_primary_action: Callable[[str], None],
        on_details: Callable[[str], None],
        on_open_folder: Callable[[str], None],
        on_open_file: Callable[[str], None],
        on_copy_url: Callable[[str], None],
        on_remove: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.download_id = item.id
        self._item = item
        self._on_primary_action = on_primary_action
        self._on_details = on_details
        self._on_open_folder = on_open_folder
        self._on_open_file = on_open_file
        self._on_copy_url = on_copy_url
        self._on_remove = on_remove

        self.set_activatable(True)
        self._build_ui()
        self.update_item(item)

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        root.add_css_class("download-row-box")
        self.set_child(root)

        # Caixa de ícone do arquivo
        self._icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._icon_box.add_css_class("file-icon-box")
        self._icon_box.set_valign(Gtk.Align.CENTER)
        self._icon_box.set_halign(Gtk.Align.CENTER)

        self._file_icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        self._file_icon.set_pixel_size(22)
        self._file_icon.set_valign(Gtk.Align.CENTER)
        self._file_icon.set_halign(Gtk.Align.CENTER)
        self._file_icon.set_vexpand(True)
        self._file_icon.set_hexpand(True)
        self._icon_box.append(self._file_icon)
        root.append(self._icon_box)

        # Coluna central (título, caminho, progresso, estatísticas)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_hexpand(True)
        root.append(body)

        # Linha superior: nome + status pill + ações
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        body.append(top_row)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(True)
        top_row.append(title_box)

        name_and_badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.append(name_and_badge)

        self._filename_label = Gtk.Label(xalign=0)
        self._filename_label.add_css_class("download-filename")
        self._filename_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._filename_label.set_hexpand(False)
        name_and_badge.append(self._filename_label)

        self._status_pill = Gtk.Label()
        self._status_pill.add_css_class("status-pill")
        self._status_pill.set_valign(Gtk.Align.CENTER)
        name_and_badge.append(self._status_pill)

        self._path_label = Gtk.Label(xalign=0)
        self._path_label.add_css_class("download-path")
        self._path_label.set_ellipsize(Pango.EllipsizeMode.START)
        title_box.append(self._path_label)

        # Botões de ação inline
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        actions_box.set_valign(Gtk.Align.CENTER)
        top_row.append(actions_box)

        self._primary_btn = Gtk.Button()
        self._primary_btn.add_css_class("flat")
        self._primary_btn.connect("clicked", lambda *_: self._on_primary_action(self.download_id))
        actions_box.append(self._primary_btn)

        self._open_file_btn = Gtk.Button(icon_name="document-open-symbolic")
        self._open_file_btn.add_css_class("flat")
        self._open_file_btn.set_tooltip_text("Abrir arquivo")
        self._open_file_btn.connect("clicked", lambda *_: self._on_open_file(self.download_id))
        actions_box.append(self._open_file_btn)

        open_dir_btn = Gtk.Button(icon_name="folder-open-symbolic")
        open_dir_btn.add_css_class("flat")
        open_dir_btn.set_tooltip_text("Abrir pasta")
        open_dir_btn.connect("clicked", lambda *_: self._on_open_folder(self.download_id))
        actions_box.append(open_dir_btn)

        copy_url_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_url_btn.add_css_class("flat")
        copy_url_btn.set_tooltip_text("Copiar URL")
        copy_url_btn.connect("clicked", lambda *_: self._on_copy_url(self.download_id))
        actions_box.append(copy_url_btn)

        details_btn = Gtk.Button(icon_name="dialog-information-symbolic")
        details_btn.add_css_class("flat")
        details_btn.set_tooltip_text("Detalhes do download")
        details_btn.connect("clicked", lambda *_: self._on_details(self.download_id))
        actions_box.append(details_btn)

        remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
        remove_btn.add_css_class("flat")
        remove_btn.set_tooltip_text("Remover download")
        remove_btn.connect("clicked", lambda *_: self._on_remove(self.download_id))
        actions_box.append(remove_btn)

        # Barra de progresso + porcentagem
        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        body.append(progress_row)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.add_css_class("download-progress")
        self._progress_bar.set_hexpand(True)
        self._progress_bar.set_valign(Gtk.Align.CENTER)
        progress_row.append(self._progress_bar)

        self._percent_label = Gtk.Label(xalign=1)
        self._percent_label.add_css_class("mono-percent")
        progress_row.append(self._percent_label)

        # Linha de estatísticas / erro
        self._stats_label = Gtk.Label(xalign=0)
        self._stats_label.add_css_class("mono-stats")
        self._stats_label.set_ellipsize(Pango.EllipsizeMode.END)
        body.append(self._stats_label)

    def update_item(self, item: DownloadItem) -> None:
        self._item = item

        # Ícone e cores de estado
        self._file_icon.set_from_icon_name(_infer_file_icon(item.filename, item.content_type))
        for cls in ("completed", "error", "paused"):
            self._icon_box.remove_css_class(cls)
            self._progress_bar.remove_css_class(cls)

        if item.status == DownloadStatus.COMPLETED:
            self._icon_box.add_css_class("completed")
            self._progress_bar.add_css_class("completed")
        elif item.status == DownloadStatus.ERROR:
            self._icon_box.add_css_class("error")
            self._progress_bar.add_css_class("error")
        elif item.status == DownloadStatus.PAUSED:
            self._icon_box.add_css_class("paused")
            self._progress_bar.add_css_class("paused")

        # Nome e caminho
        self._filename_label.set_text(item.filename)
        self._filename_label.set_tooltip_text(item.filename)
        self._path_label.set_text(_shorten_home(item.dest_dir))
        self._path_label.set_tooltip_text(str(item.filepath))

        # Status pill
        self._status_pill.set_text(STATUS_LABELS.get(item.status, item.status.value))
        for cls in _STATUS_CSS_CLASSES.values():
            self._status_pill.remove_css_class(cls)
        self._status_pill.add_css_class(
            _STATUS_CSS_CLASSES.get(item.status, "status-queued")
        )

        # Botões sensíveis ao contexto
        if item.status in (DownloadStatus.DOWNLOADING, DownloadStatus.QUEUED):
            self._primary_btn.set_visible(True)
            self._primary_btn.set_icon_name("media-playback-pause-symbolic")
            self._primary_btn.set_tooltip_text("Pausar")
            self._open_file_btn.set_visible(False)
        elif item.status == DownloadStatus.PAUSED:
            self._primary_btn.set_visible(True)
            self._primary_btn.set_icon_name("media-playback-start-symbolic")
            self._primary_btn.set_tooltip_text("Retomar")
            self._open_file_btn.set_visible(False)
        elif item.status in (DownloadStatus.ERROR, DownloadStatus.CANCELLED):
            self._primary_btn.set_visible(True)
            self._primary_btn.set_icon_name("view-refresh-symbolic")
            self._primary_btn.set_tooltip_text("Tentar novamente")
            self._open_file_btn.set_visible(False)
        else:  # COMPLETED
            self._primary_btn.set_visible(False)
            self._open_file_btn.set_visible(True)

        # Progresso
        if item.status == DownloadStatus.COMPLETED:
            self._progress_bar.set_fraction(1.0)
            self._percent_label.set_text("100%")
        elif item.total_bytes > 0:
            frac = item.progress
            self._progress_bar.set_fraction(frac)
            self._percent_label.set_text(f"{frac * 100:.1f}%")
        elif item.status == DownloadStatus.DOWNLOADING:
            self._progress_bar.pulse()
            self._percent_label.set_text("•••")
        else:
            self._progress_bar.set_fraction(0.0)
            self._percent_label.set_text("--%")

        # Estatísticas / mensagem de erro
        if item.status == DownloadStatus.ERROR and item.error_message:
            self._stats_label.remove_css_class("mono-stats")
            self._stats_label.add_css_class("error-text")
            msg = f"Erro: {item.error_message}"
            self._stats_label.set_text(msg)
            self._stats_label.set_tooltip_text(msg)
        else:
            self._stats_label.remove_css_class("error-text")
            self._stats_label.add_css_class("mono-stats")
            self._stats_label.set_tooltip_text(None)
            self._stats_label.set_text(self._format_stats(item))

    def pulse_if_indeterminate(self) -> None:
        if (
            self._item.status == DownloadStatus.DOWNLOADING
            and self._item.total_bytes <= 0
        ):
            self._progress_bar.pulse()

    @staticmethod
    def _format_stats(item: DownloadItem) -> str:
        downloaded = format_bytes(item.downloaded_bytes)
        total = format_bytes(item.total_bytes) if item.total_bytes > 0 else "Desconhecido"
        seg_count = len(item.segments) if item.segments else item.num_segments
        seg_label = "1 conexão" if seg_count == 1 else f"{seg_count} conexões"

        if item.status == DownloadStatus.DOWNLOADING:
            parts = [
                f"{downloaded} / {total}",
                format_speed(item.speed_bps),
                f"ETA {format_eta(item.eta_seconds)}",
                seg_label,
            ]
            if item.speed_limit_kbps > 0:
                parts.append(f"Limite {item.speed_limit_kbps} KB/s")
            return "  •  ".join(parts)

        if item.status == DownloadStatus.COMPLETED:
            final_size = format_bytes(item.total_bytes or item.downloaded_bytes)
            parts = [final_size, "Concluído"]
            if item.checksum_verified is True:
                parts.append("Checksum OK")
            elif item.checksum_verified is False:
                parts.append("Checksum inválido")
            return "  •  ".join(parts)

        if item.status == DownloadStatus.PAUSED:
            return f"{downloaded} / {total}  •  Pausado  •  {seg_label}"

        if item.status == DownloadStatus.QUEUED:
            return f"{downloaded} / {total}  •  Aguardando na fila  •  {seg_label}"

        return f"{downloaded} / {total}"


class MainWindow(Adw.ApplicationWindow):
    """Janela principal do FluxDM com sidebar de filtros, lista de downloads e barra de status."""

    def __init__(
        self,
        application: Adw.Application,
        manager: DownloadManager,
    ) -> None:
        super().__init__(application=application)
        self._manager = manager
        self._current_filter: str = "all"
        self._search_query: str = ""
        self._items_by_id: dict[str, DownloadItem] = {}
        self._rows_by_id: dict[str, DownloadRow] = {}
        self._filter_badges: dict[str, Gtk.Label] = {}
        self._filter_rows: dict[str, Gtk.ListBoxRow] = {}
        self._timeout_id: Optional[int] = None

        self.set_title(APP_NAME)
        self.set_default_size(1060, 680)
        self.set_size_request(720, 480)

        self._build_ui()
        self._setup_drop_target()
        self._load_initial_items()

        self._manager.subscribe(self._on_manager_event)
        self._timeout_id = GLib.timeout_add_seconds(1, self._on_periodic_refresh)
        self.connect("close-request", self._on_close_request)

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        self._split_view = Adw.NavigationSplitView()
        self._split_view.set_min_sidebar_width(210)
        self._split_view.set_max_sidebar_width(260)
        self._toast_overlay.set_child(self._split_view)

        # --- Sidebar ---
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_title = Adw.WindowTitle(title=APP_NAME, subtitle="Downloads")
        sidebar_header.set_title_widget(sidebar_title)
        sidebar_toolbar.add_top_bar(sidebar_header)

        self._sidebar_list = Gtk.ListBox()
        self._sidebar_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._sidebar_list.add_css_class("navigation-sidebar")
        self._sidebar_list.add_css_class("sidebar-list")
        self._sidebar_list.connect("row-selected", self._on_filter_selected)

        for key, label, icon_name in FILTER_ITEMS:
            row = Gtk.ListBoxRow()
            row._filter_key = key

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)

            icon = Gtk.Image.new_from_icon_name(icon_name)
            box.append(icon)

            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_hexpand(True)
            box.append(lbl)

            badge = Gtk.Label(label="0")
            badge.add_css_class("sidebar-badge")
            badge.set_valign(Gtk.Align.CENTER)
            box.append(badge)

            row.set_child(box)
            self._sidebar_list.append(row)
            self._filter_badges[key] = badge
            self._filter_rows[key] = row

        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_child(self._sidebar_list)
        sidebar_toolbar.set_content(sidebar_scroll)

        sidebar_page = Adw.NavigationPage(title=APP_NAME, child=sidebar_toolbar)
        self._split_view.set_sidebar(sidebar_page)

        # --- Área principal ---
        self._content_toolbar = Adw.ToolbarView()
        content_header = Adw.HeaderBar()

        self._content_title = Adw.WindowTitle(title="Todos os Downloads", subtitle="")
        content_header.set_title_widget(self._content_title)

        # Botão primário "+ Novo"
        new_btn = Gtk.Button()
        new_btn.add_css_class("suggested-action")
        new_btn.set_tooltip_text("Novo download (Ctrl+N)")
        new_btn_content = Adw.ButtonContent(
            icon_name="list-add-symbolic",
            label="Novo",
        )
        new_btn.set_child(new_btn_content)
        new_btn.connect("clicked", lambda *_: self.open_add_dialog())
        content_header.pack_start(new_btn)

        # Menu principal
        menu = Gio.Menu()
        menu.append("Preferências", "app.preferences")
        menu.append("Sobre o FluxDM", "app.about")
        menu.append("Sair", "app.quit")

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_btn.set_tooltip_text("Menu principal")
        content_header.pack_end(menu_btn)

        # Botão Preferências
        prefs_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        prefs_btn.set_tooltip_text("Preferências (Ctrl+,)")
        prefs_btn.connect("clicked", lambda *_: self.open_preferences_dialog())
        content_header.pack_end(prefs_btn)

        # Botão Buscar
        self._search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic")
        self._search_btn.set_tooltip_text("Buscar downloads (Ctrl+F)")
        content_header.pack_end(self._search_btn)

        # Ações rápidas em lote
        clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic")
        clear_btn.set_tooltip_text("Limpar concluídos")
        clear_btn.connect("clicked", self._on_clear_completed)
        content_header.pack_end(clear_btn)

        resume_all_btn = Gtk.Button(icon_name="media-playback-start-symbolic")
        resume_all_btn.set_tooltip_text("Retomar todos")
        resume_all_btn.connect("clicked", self._on_resume_all)
        content_header.pack_end(resume_all_btn)

        pause_all_btn = Gtk.Button(icon_name="media-playback-pause-symbolic")
        pause_all_btn.set_tooltip_text("Pausar todos")
        pause_all_btn.connect("clicked", self._on_pause_all)
        content_header.pack_end(pause_all_btn)

        self._content_toolbar.add_top_bar(content_header)

        # Barra de busca
        self._search_bar = Gtk.SearchBar()
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Buscar por nome, URL ou pasta…")
        self._search_entry.set_hexpand(True)
        self._search_entry.set_size_request(340, -1)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        self._search_btn.bind_property(
            "active",
            self._search_bar,
            "search-mode-enabled",
            Gio.BindingFlags.BIDIRECTIONAL | Gio.BindingFlags.SYNC_CREATE,
        )
        self._content_toolbar.add_top_bar(self._search_bar)

        # Pilha de conteúdo: lista vs estado vazio
        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._content_stack.set_transition_duration(150)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(1100)
        clamp.set_tightening_threshold(800)

        self._download_list = Gtk.ListBox()
        self._download_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._download_list.add_css_class("download-list")
        self._download_list.connect("row-activated", self._on_row_activated)
        clamp.set_child(self._download_list)
        scrolled.set_child(clamp)

        self._content_stack.add_named(scrolled, "list")

        # Estado vazio
        self._empty_page = Adw.StatusPage()
        self._empty_page.set_icon_name("folder-download-symbolic")
        self._empty_page.set_title("Nenhum download ainda")
        self._empty_page.set_description(
            "Clique em “+ Novo” ou arraste uma URL para esta janela para começar."
        )
        self._empty_action_btn = Gtk.Button(label="Adicionar Download")
        self._empty_action_btn.add_css_class("suggested-action")
        self._empty_action_btn.add_css_class("pill")
        self._empty_action_btn.set_halign(Gtk.Align.CENTER)
        self._empty_action_btn.connect("clicked", self._on_empty_action_clicked)
        self._empty_page.set_child(self._empty_action_btn)

        self._content_stack.add_named(self._empty_page, "empty")
        self._content_toolbar.set_content(self._content_stack)

        # --- Barra de status inferior ---
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        status_bar.add_css_class("status-bar")

        self._active_status_lbl = Gtk.Label(label="Ativos: 0", xalign=0)
        self._active_status_lbl.add_css_class("status-bar-metric")
        status_bar.append(self._active_status_lbl)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        status_bar.append(sep1)

        self._speed_status_lbl = Gtk.Label(label="Velocidade: 0 B/s", xalign=0)
        self._speed_status_lbl.add_css_class("status-bar-metric")
        status_bar.append(self._speed_status_lbl)

        self._limit_chip = Gtk.Label(label="")
        self._limit_chip.add_css_class("speed-limit-chip")
        self._limit_chip.set_visible(False)
        status_bar.append(self._limit_chip)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        status_bar.append(spacer)

        self._disk_space_lbl = Gtk.Label(label="Espaço livre: --", xalign=1)
        self._disk_space_lbl.add_css_class("status-bar-metric")
        self._disk_space_lbl.add_css_class("dim-label")
        status_bar.append(self._disk_space_lbl)

        self._content_toolbar.add_bottom_bar(status_bar)

        content_page = Adw.NavigationPage(title="Downloads", child=self._content_toolbar)
        self._split_view.set_content(content_page)

        # Seleciona "Todos" inicialmente
        self._sidebar_list.select_row(self._filter_rows["all"])

    def _setup_drop_target(self) -> None:
        drop_target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        drop_target.connect("drop", self._on_drop_value)
        self._content_toolbar.add_controller(drop_target)

    def _on_drop_enter(self, *_args) -> Gdk.DragAction:
        self._content_toolbar.add_css_class("drop-highlight")
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, *_args) -> None:
        self._content_toolbar.remove_css_class("drop-highlight")

    def _on_drop_value(
        self,
        _target: Gtk.DropTarget,
        value: object,
        _x: float,
        _y: float,
    ) -> bool:
        self._content_toolbar.remove_css_class("drop-highlight")
        if not isinstance(value, str):
            return False
        url = _extract_http_url(value)
        if not url:
            self.show_toast("O texto solto não contém uma URL HTTP/HTTPS válida.")
            return False
        self.open_add_dialog(initial_url=url)
        return True

    # --- Ações públicas (também chamadas por atalhos do aplicativo) ---

    def show_toast(self, message: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self._toast_overlay.add_toast(toast)

    def open_add_dialog(self, initial_url: str = "") -> None:
        dialog = AddDownloadDialog(
            parent_window=self,
            manager=self._manager,
            initial_url=initial_url,
            on_added=lambda item: self.show_toast(f"Download adicionado: {item.filename}"),
        )
        dialog.present(self)

    def open_preferences_dialog(self) -> None:
        dialog = PreferencesDialog(
            parent_window=self,
            manager=self._manager,
            on_config_changed=lambda _cfg: self._refresh_status_bar(),
        )
        dialog.present_for(self)

    def open_about_dialog(self) -> None:
        show_about_dialog(self)

    def toggle_search(self) -> None:
        active = not self._search_bar.get_search_mode()
        self._search_bar.set_search_mode(active)
        if active:
            self._search_entry.grab_focus()

    # --- Carregamento e eventos do DownloadManager ---

    def _load_initial_items(self) -> None:
        items = self._manager.list_downloads()
        items.sort(key=lambda it: it.created_at, reverse=True)
        for item in items:
            self._items_by_id[item.id] = item
            self._create_row(item, prepend=False)
        self._refresh_counts_and_visibility()
        self._refresh_status_bar()

    def _create_row(self, item: DownloadItem, prepend: bool = True) -> DownloadRow:
        row = DownloadRow(
            item=item,
            on_primary_action=self._on_row_primary_action,
            on_details=self._on_row_details,
            on_open_folder=self._on_row_open_folder,
            on_open_file=self._on_row_open_file,
            on_copy_url=self._on_row_copy_url,
            on_remove=self._on_row_remove,
        )
        self._rows_by_id[item.id] = row
        if prepend:
            self._download_list.prepend(row)
        else:
            self._download_list.append(row)
        return row

    def _on_manager_event(self, event: str, item: DownloadItem) -> None:
        GLib.idle_add(self._handle_manager_event, event, item)

    def _handle_manager_event(self, event: str, item: DownloadItem) -> bool:
        if event == "removed":
            self._items_by_id.pop(item.id, None)
            row = self._rows_by_id.pop(item.id, None)
            if row is not None:
                self._download_list.remove(row)
            self._refresh_counts_and_visibility()
            self._refresh_status_bar()
            return False

        prev = self._items_by_id.get(item.id)
        status_changed = prev is None or prev.status != item.status
        self._items_by_id[item.id] = item

        row = self._rows_by_id.get(item.id)
        if row is None:
            self._create_row(item, prepend=True)
        else:
            row.update_item(item)

        if event in ("added", "completed", "error") or status_changed:
            self._refresh_counts_and_visibility()
            if event == "completed":
                self.show_toast(f"Download concluído: {item.filename}")
            elif event == "error":
                self.show_toast(f"Falha no download: {item.filename}")

        self._refresh_status_bar()
        return False

    # --- Filtros, busca e barra de status ---

    def _on_filter_selected(
        self,
        _listbox: Gtk.ListBox,
        row: Optional[Gtk.ListBoxRow],
    ) -> None:
        if row is None or not hasattr(row, "_filter_key"):
            return
        self._current_filter = row._filter_key
        titles = {
            "all": "Todos os Downloads",
            "active": "Downloads Ativos",
            "completed": "Downloads Concluídos",
            "paused": "Downloads Pausados",
            "error": "Downloads com Erro",
        }
        self._content_title.set_title(titles.get(self._current_filter, "Downloads"))
        self._refresh_counts_and_visibility()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_query = entry.get_text().strip().lower()
        self._refresh_counts_and_visibility()

    def _item_matches_search(self, item: DownloadItem) -> bool:
        if not self._search_query:
            return True
        haystack = f"{item.filename} {item.url} {item.dest_dir}".lower()
        return self._search_query in haystack

    def _refresh_counts_and_visibility(self) -> None:
        counts = {key: 0 for key, _, _ in FILTER_ITEMS}
        for item in self._items_by_id.values():
            for key, _, _ in FILTER_ITEMS:
                if _matches_filter(item, key):
                    counts[key] += 1

        for key, badge in self._filter_badges.items():
            count = counts[key]
            badge.set_text(str(count))
            if key == "active" and count > 0:
                badge.add_css_class("active-badge")
            else:
                badge.remove_css_class("active-badge")

        visible_count = 0
        for download_id, row in self._rows_by_id.items():
            item = self._items_by_id.get(download_id)
            visible = (
                item is not None
                and _matches_filter(item, self._current_filter)
                and self._item_matches_search(item)
            )
            row.set_visible(visible)
            if visible:
                visible_count += 1

        total_items = len(self._items_by_id)
        if total_items > 0:
            self._content_title.set_subtitle(
                f"{visible_count} de {total_items} item(ns)"
                if visible_count != total_items
                else f"{total_items} item(ns)"
            )
        else:
            self._content_title.set_subtitle("")

        if visible_count > 0:
            self._content_stack.set_visible_child_name("list")
        else:
            self._update_empty_state(total_items)
            self._content_stack.set_visible_child_name("empty")

    def _update_empty_state(self, total_items: int) -> None:
        if total_items == 0:
            self._empty_page.set_icon_name("folder-download-symbolic")
            self._empty_page.set_title("Nenhum download ainda")
            self._empty_page.set_description(
                "Clique em “+ Novo” (Ctrl+N) ou arraste uma URL para esta janela."
            )
            self._empty_action_btn.set_label("Adicionar Download")
            self._empty_action_btn.add_css_class("suggested-action")
        elif self._search_query:
            self._empty_page.set_icon_name("system-search-symbolic")
            self._empty_page.set_title("Nenhum resultado encontrado")
            self._empty_page.set_description(
                f"Nenhum download corresponde a “{self._search_query}”."
            )
            self._empty_action_btn.set_label("Limpar busca")
            self._empty_action_btn.remove_css_class("suggested-action")
        else:
            self._empty_page.set_icon_name("emblem-ok-symbolic")
            self._empty_page.set_title("Nenhum item nesta categoria")
            self._empty_page.set_description(
                "Não há downloads neste filtro no momento."
            )
            self._empty_action_btn.set_label("Mostrar todos")
            self._empty_action_btn.remove_css_class("suggested-action")

    def _on_empty_action_clicked(self, *_args) -> None:
        if len(self._items_by_id) == 0:
            self.open_add_dialog()
        elif self._search_query:
            self._search_entry.set_text("")
            self._search_bar.set_search_mode(False)
        else:
            self._sidebar_list.select_row(self._filter_rows["all"])

    def _refresh_status_bar(self) -> None:
        try:
            active_count = self._manager.get_active_count()
            total_speed = self._manager.get_total_speed_bps()
        except Exception:
            active_count = 0
            total_speed = 0.0

        self._active_status_lbl.set_text(f"Ativos: {active_count}")
        self._speed_status_lbl.set_text(f"Velocidade: {format_speed(total_speed)}")

        limit_kbps = self._manager.config.global_speed_limit_kbps
        if limit_kbps > 0:
            self._limit_chip.set_text(f"Limite global: {limit_kbps} KB/s")
            self._limit_chip.set_visible(True)
        else:
            self._limit_chip.set_visible(False)

        self._refresh_disk_space()

    def _refresh_disk_space(self) -> None:
        target_dir = Path(self._manager.config.download_dir or (Path.home() / "Downloads"))
        probe_path = target_dir
        while not probe_path.exists() and probe_path != probe_path.parent:
            probe_path = probe_path.parent
        try:
            usage = shutil.disk_usage(probe_path)
            self._disk_space_lbl.set_text(f"Espaço livre: {format_bytes(usage.free)}")
            self._disk_space_lbl.set_tooltip_text(
                f"{format_bytes(usage.free)} livres de {format_bytes(usage.total)} em {probe_path}"
            )
        except OSError:
            self._disk_space_lbl.set_text("Espaço livre: --")

    def _on_periodic_refresh(self) -> bool:
        for row in self._rows_by_id.values():
            row.pulse_if_indeterminate()
        self._refresh_status_bar()
        return True

    # --- Ações em lote e por linha ---

    def _on_pause_all(self, *_args) -> None:
        self._manager.pause_all()
        self.show_toast("Todos os downloads ativos foram pausados.")

    def _on_resume_all(self, *_args) -> None:
        self._manager.resume_all()
        self.show_toast("Downloads pausados retomados.")

    def _on_clear_completed(self, *_args) -> None:
        removed = self._manager.clear_completed()
        if isinstance(removed, int):
            msg = (
                f"{removed} download(s) concluído(s) removido(s)."
                if removed > 0
                else "Não há downloads concluídos para limpar."
            )
        else:
            msg = "Lista de concluídos limpa."
        self.show_toast(msg)

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, DownloadRow):
            self._on_row_details(row.download_id)

    def _on_row_primary_action(self, download_id: str) -> None:
        item = self._items_by_id.get(download_id)
        if item is None:
            return
        try:
            if item.status in (DownloadStatus.DOWNLOADING, DownloadStatus.QUEUED):
                self._manager.pause_download(download_id)
            elif item.status == DownloadStatus.PAUSED:
                self._manager.resume_download(download_id)
            elif item.status in (DownloadStatus.ERROR, DownloadStatus.CANCELLED):
                self._manager.retry_download(download_id)
        except Exception as exc:
            self.show_toast(f"Erro na operação: {exc}")

    def _on_row_details(self, download_id: str) -> None:
        dialog = DetailsDialog(
            manager=self._manager,
            download_id=download_id,
            on_toast=self.show_toast,
        )
        dialog.present(self)

    def _open_with_xdg(self, path: Path, label: str) -> None:
        if not path.exists():
            self.show_toast(f"{label} não encontrado: {path}")
            return
        try:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.show_toast(f"Não foi possível abrir {label.lower()}: {exc}")

    def _on_row_open_folder(self, download_id: str) -> None:
        item = self._items_by_id.get(download_id)
        if item is not None:
            self._open_with_xdg(Path(item.dest_dir), "Pasta")

    def _on_row_open_file(self, download_id: str) -> None:
        item = self._items_by_id.get(download_id)
        if item is not None:
            self._open_with_xdg(item.filepath, "Arquivo")

    def _on_row_copy_url(self, download_id: str) -> None:
        item = self._items_by_id.get(download_id)
        if item is not None and copy_to_clipboard(item.url):
            self.show_toast("URL copiada para a área de transferência.")

    def _on_row_remove(self, download_id: str) -> None:
        item = self._items_by_id.get(download_id)
        if item is None:
            return

        def _do_remove(delete_file: bool) -> None:
            try:
                self._manager.remove_download(download_id, delete_file=delete_file)
                self.show_toast(f"Removido: {item.filename}")
            except Exception as exc:
                self.show_toast(f"Erro ao remover download: {exc}")

        confirm_remove_dialog(self, item, _do_remove)

    def _on_close_request(self, *_args) -> bool:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        self._manager.unsubscribe(self._on_manager_event)
        return False
