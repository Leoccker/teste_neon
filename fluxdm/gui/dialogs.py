"""Diálogos GTK4 + Libadwaita para o FluxDM."""

from __future__ import annotations

import datetime
import threading
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from fluxdm import APP_ID, APP_NAME, __version__
from fluxdm.engine import DownloadManager
from fluxdm.models import (
    AppConfig,
    DownloadItem,
    DownloadStatus,
    ProbeResult,
    format_bytes,
    format_eta,
    format_speed,
    sanitize_filename,
)

STATUS_LABELS: dict[DownloadStatus, str] = {
    DownloadStatus.QUEUED: "Na fila",
    DownloadStatus.DOWNLOADING: "Baixando",
    DownloadStatus.PAUSED: "Pausado",
    DownloadStatus.COMPLETED: "Concluído",
    DownloadStatus.ERROR: "Erro",
    DownloadStatus.CANCELLED: "Cancelado",
}


def copy_to_clipboard(text: str) -> bool:
    """Copia texto para a área de transferência padrão (Wayland/X11)."""
    display = Gdk.Display.get_default()
    if display is None:
        return False
    clipboard = display.get_clipboard()
    clipboard.set(text)
    return True


def _select_folder(
    parent_window: Optional[Gtk.Window],
    initial_dir: str,
    on_selected: Callable[[str], None],
) -> None:
    """Abre o seletor nativo de pastas via Gtk.FileDialog."""
    file_dialog = Gtk.FileDialog()
    file_dialog.set_title("Selecionar pasta de destino")
    file_dialog.set_modal(True)

    if initial_dir:
        path = Path(initial_dir).expanduser()
        if path.exists() and path.is_dir():
            file_dialog.set_initial_folder(Gio.File.new_for_path(str(path)))

    def _on_finish(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is not None:
            folder_path = folder.get_path()
            if folder_path:
                on_selected(folder_path)

    file_dialog.select_folder(parent_window, None, _on_finish)


def _parse_extra_headers(raw_text: str, user_agent: str) -> dict[str, str]:
    """Converte texto multilinha 'Chave: Valor' e User-Agent opcional em dicionário."""
    headers: dict[str, str] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            headers[key] = value
    ua = user_agent.strip()
    if ua:
        headers["User-Agent"] = ua
    return headers


def _format_timestamp(ts: Optional[float]) -> str:
    if ts is None or ts <= 0:
        return "—"
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


class AddDownloadDialog(Adw.Dialog):
    """Diálogo para adicionar um novo download com análise (probe) da URL."""

    def __init__(
        self,
        parent_window: Gtk.Window,
        manager: DownloadManager,
        initial_url: str = "",
        on_added: Optional[Callable[[DownloadItem], None]] = None,
    ) -> None:
        super().__init__()
        self._parent_window = parent_window
        self._manager = manager
        self._on_added = on_added
        self._probing = False
        self._probe_seq = 0
        self._dest_dir = (
            manager.config.download_dir
            or str(Path.home() / "Downloads")
        )

        self.set_title("Novo Download")
        self.set_content_width(560)
        self.set_content_height(650)

        self._build_ui()

        if initial_url:
            cleaned = initial_url.strip()
            self._url_row.set_text(cleaned)
            if cleaned.startswith(("http://", "https://")):
                GLib.idle_add(self._start_probe)

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancelar")
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)

        self._add_btn = Gtk.Button(label="Adicionar")
        self._add_btn.add_css_class("suggested-action")
        self._add_btn.connect("clicked", self._on_submit)
        header.pack_end(self._add_btn)

        toolbar_view.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(540)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(content)
        scrolled.set_child(clamp)
        toolbar_view.set_content(scrolled)

        # --- Grupo: Origem ---
        source_group = Adw.PreferencesGroup(title="Origem")
        content.append(source_group)

        self._url_row = Adw.EntryRow(title="URL do arquivo")
        self._url_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._url_row.connect("entry-activated", self._on_submit)
        self._url_row.connect("changed", self._on_url_changed)

        paste_btn = Gtk.Button(icon_name="edit-paste-symbolic")
        paste_btn.set_valign(Gtk.Align.CENTER)
        paste_btn.set_tooltip_text("Colar URL da área de transferência")
        paste_btn.add_css_class("flat")
        paste_btn.connect("clicked", self._on_paste_clicked)
        self._url_row.add_suffix(paste_btn)

        self._probe_spinner = Gtk.Spinner()
        self._probe_spinner.set_valign(Gtk.Align.CENTER)
        self._probe_spinner.set_visible(False)
        self._url_row.add_suffix(self._probe_spinner)

        self._probe_btn = Gtk.Button(label="Analisar")
        self._probe_btn.set_valign(Gtk.Align.CENTER)
        self._probe_btn.set_tooltip_text("Consultar nome, tamanho e suporte a múltiplas conexões")
        self._probe_btn.add_css_class("flat")
        self._probe_btn.connect("clicked", lambda *_: self._start_probe())
        self._url_row.add_suffix(self._probe_btn)

        source_group.add(self._url_row)

        # Banner de resultado da análise / validação
        self._probe_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._probe_banner.add_css_class("probe-banner")
        self._probe_banner.set_visible(False)

        self._probe_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self._probe_icon.set_valign(Gtk.Align.CENTER)
        self._probe_banner.append(self._probe_icon)

        self._probe_label = Gtk.Label(xalign=0)
        self._probe_label.set_wrap(True)
        self._probe_label.set_hexpand(True)
        self._probe_banner.append(self._probe_label)

        content.append(self._probe_banner)

        # --- Grupo: Destino ---
        dest_group = Adw.PreferencesGroup(title="Destino")
        content.append(dest_group)

        self._filename_row = Adw.EntryRow(title="Nome do arquivo (opcional)")
        dest_group.add(self._filename_row)

        self._dir_row = Adw.ActionRow(
            title="Pasta de destino",
            subtitle=self._dest_dir,
        )
        self._dir_row.set_subtitle_lines(1)
        choose_dir_btn = Gtk.Button(label="Escolher…")
        choose_dir_btn.set_valign(Gtk.Align.CENTER)
        choose_dir_btn.connect("clicked", self._on_choose_dir)
        self._dir_row.add_suffix(choose_dir_btn)
        self._dir_row.set_activatable_widget(choose_dir_btn)
        dest_group.add(self._dir_row)

        # --- Grupo: Transferência ---
        transfer_group = Adw.PreferencesGroup(title="Transferência")
        content.append(transfer_group)

        cfg = self._manager.config

        seg_adj = Gtk.Adjustment(
            value=max(1, min(16, cfg.default_segments)),
            lower=1,
            upper=16,
            step_increment=1,
            page_increment=4,
        )
        self._segments_row = Adw.SpinRow(
            title="Conexões simultâneas",
            subtitle="Número de segmentos paralelos para acelerar o download",
            adjustment=seg_adj,
        )
        transfer_group.add(self._segments_row)

        speed_adj = Gtk.Adjustment(
            value=0,
            lower=0,
            upper=1_000_000,
            step_increment=64,
            page_increment=512,
        )
        self._speed_row = Adw.SpinRow(
            title="Limite de velocidade (KB/s)",
            subtitle="0 = ilimitado",
            adjustment=speed_adj,
        )
        transfer_group.add(self._speed_row)

        self._start_now_row = Adw.SwitchRow(
            title="Iniciar imediatamente",
            subtitle="Se desativado, o download será adicionado pausado",
        )
        self._start_now_row.set_active(bool(cfg.auto_start_downloads))
        transfer_group.add(self._start_now_row)

        # --- Grupo: Integridade e Avançado ---
        adv_group = Adw.PreferencesGroup(title="Integridade e Opções Avançadas")
        content.append(adv_group)

        self._checksum_row = Adw.EntryRow(title="Checksum esperado (SHA-256 ou MD5)")
        adv_group.add(self._checksum_row)

        headers_expander = Adw.ExpanderRow(
            title="User-Agent e cabeçalhos HTTP",
            subtitle="Opcional — personalizar cabeçalhos da requisição",
        )
        adv_group.add(headers_expander)

        self._ua_row = Adw.EntryRow(title="User-Agent personalizado")
        headers_expander.add_row(self._ua_row)

        headers_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        headers_box.set_margin_top(8)
        headers_box.set_margin_bottom(10)
        headers_box.set_margin_start(12)
        headers_box.set_margin_end(12)

        headers_hint = Gtk.Label(
            label="Cabeçalhos extras (um por linha no formato Chave: Valor):",
            xalign=0,
        )
        headers_hint.add_css_class("dim-label")
        headers_hint.add_css_class("caption")
        headers_box.append(headers_hint)

        headers_frame = Gtk.Frame()
        headers_scroll = Gtk.ScrolledWindow()
        headers_scroll.set_min_content_height(78)
        headers_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._headers_view = Gtk.TextView()
        self._headers_view.set_monospace(True)
        self._headers_view.set_top_margin(6)
        self._headers_view.set_bottom_margin(6)
        self._headers_view.set_left_margin(8)
        self._headers_view.set_right_margin(8)
        headers_scroll.set_child(self._headers_view)
        headers_frame.set_child(headers_scroll)
        headers_box.append(headers_frame)

        headers_row = Gtk.ListBoxRow()
        headers_row.set_activatable(False)
        headers_row.set_selectable(False)
        headers_row.set_child(headers_box)
        headers_expander.add_row(headers_row)

    def _on_url_changed(self, *_args) -> None:
        if self._probe_banner.has_css_class("error"):
            self._probe_banner.set_visible(False)

    def _on_paste_clicked(self, *_args) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()

        def _on_read(cb: Gdk.Clipboard, res: Gio.AsyncResult) -> None:
            try:
                text = cb.read_text_finish(res)
            except GLib.Error:
                return
            if text:
                cleaned = text.strip().splitlines()[0].strip()
                self._url_row.set_text(cleaned)
                if cleaned.startswith(("http://", "https://")):
                    self._start_probe()

        clipboard.read_text_async(None, _on_read)

    def _on_choose_dir(self, *_args) -> None:
        def _apply(folder: str) -> None:
            self._dest_dir = folder
            self._dir_row.set_subtitle(folder)

        _select_folder(self._parent_window, self._dest_dir, _apply)

    def _get_headers(self) -> dict[str, str]:
        buf = self._headers_view.get_buffer()
        start, end = buf.get_bounds()
        raw = buf.get_text(start, end, False)
        return _parse_extra_headers(raw, self._ua_row.get_text())

    def _show_banner(self, message: str, is_error: bool = False) -> None:
        self._probe_banner.set_visible(True)
        if is_error:
            self._probe_banner.add_css_class("error")
            self._probe_icon.set_from_icon_name("dialog-error-symbolic")
        else:
            self._probe_banner.remove_css_class("error")
            self._probe_icon.set_from_icon_name("emblem-ok-symbolic")
        self._probe_label.set_text(message)

    def _start_probe(self) -> bool:
        url = self._url_row.get_text().strip()
        if not url.startswith(("http://", "https://")):
            self._show_banner(
                "Informe uma URL válida começando com http:// ou https://.",
                is_error=True,
            )
            return False

        self._probe_seq += 1
        seq = self._probe_seq
        self._probing = True
        self._probe_btn.set_sensitive(False)
        self._probe_spinner.set_visible(True)
        self._probe_spinner.start()

        headers = self._get_headers() or None

        def _worker() -> None:
            try:
                result = self._manager.probe_url(url, headers=headers, timeout=15.0)
                GLib.idle_add(self._on_probe_done, seq, result, None)
            except Exception as exc:
                GLib.idle_add(self._on_probe_done, seq, None, str(exc))

        threading.Thread(target=_worker, daemon=True, name="fluxdm-probe").start()
        return False

    def _on_probe_done(
        self,
        seq: int,
        result: Optional[ProbeResult],
        error: Optional[str],
    ) -> bool:
        if seq != self._probe_seq:
            return False

        self._probing = False
        self._probe_spinner.stop()
        self._probe_spinner.set_visible(False)
        self._probe_btn.set_sensitive(True)

        if error or result is None:
            self._show_banner(f"Não foi possível analisar a URL: {error or 'erro desconhecido'}", is_error=True)
            return False

        if result.filename and not self._filename_row.get_text().strip():
            self._filename_row.set_text(sanitize_filename(result.filename))

        size_str = format_bytes(result.total_bytes) if result.total_bytes > 0 else "Tamanho desconhecido"
        range_str = (
            "Suporta múltiplas conexões (HTTP Range)"
            if result.supports_range
            else "Conexão única (servidor sem suporte a Range)"
        )
        parts = [f"Tamanho: {size_str}", range_str]
        if result.content_type:
            parts.append(f"Tipo: {result.content_type}")
        self._show_banner(" • ".join(parts), is_error=False)

        if not result.supports_range:
            self._segments_row.set_value(1)

        return False

    def _on_submit(self, *_args) -> None:
        url = self._url_row.get_text().strip()
        if not url.startswith(("http://", "https://")):
            self._show_banner(
                "Informe uma URL válida começando com http:// ou https://.",
                is_error=True,
            )
            self._url_row.grab_focus()
            return

        filename_raw = self._filename_row.get_text().strip()
        filename = sanitize_filename(filename_raw) if filename_raw else None
        num_segments = int(self._segments_row.get_value())
        speed_limit_kbps = int(self._speed_row.get_value())
        expected_checksum = self._checksum_row.get_text().strip()
        headers = self._get_headers() or None
        start_immediately = bool(self._start_now_row.get_active())

        try:
            item = self._manager.add_download(
                url=url,
                dest_dir=self._dest_dir,
                filename=filename,
                num_segments=num_segments,
                speed_limit_kbps=speed_limit_kbps,
                expected_checksum=expected_checksum,
                headers=headers,
                start_immediately=start_immediately,
            )
        except Exception as exc:
            self._show_banner(f"Erro ao adicionar download: {exc}", is_error=True)
            return

        if self._on_added is not None:
            self._on_added(item)
        self.close()


_PREFS_BASE = (
    Adw.PreferencesDialog
    if hasattr(Adw, "PreferencesDialog")
    else Adw.PreferencesWindow
)


class PreferencesDialog(_PREFS_BASE):
    """Diálogo de preferências do FluxDM com salvamento imediato."""

    def __init__(
        self,
        parent_window: Gtk.Window,
        manager: DownloadManager,
        on_config_changed: Optional[Callable[[AppConfig], None]] = None,
    ) -> None:
        super().__init__()
        self._parent_window = parent_window
        self._manager = manager
        self._on_config_changed = on_config_changed
        self._loading = True

        self.set_title("Preferências")
        if hasattr(self, "set_search_enabled"):
            self.set_search_enabled(False)
        if isinstance(self, Gtk.Window):
            self.set_transient_for(parent_window)
            self.set_modal(True)
            self.set_default_size(580, 560)

        self._build_pages()
        self._loading = False

    def _build_pages(self) -> None:
        cfg = self._manager.config

        # --- Página: Geral ---
        general_page = Adw.PreferencesPage(
            title="Geral",
            icon_name="preferences-system-symbolic",
        )
        self.add(general_page)

        files_group = Adw.PreferencesGroup(title="Arquivos e Comportamento")
        general_page.add(files_group)

        current_dir = cfg.download_dir or str(Path.home() / "Downloads")
        self._default_dir_row = Adw.ActionRow(
            title="Pasta padrão de downloads",
            subtitle=current_dir,
        )
        self._default_dir_row.set_subtitle_lines(1)
        choose_btn = Gtk.Button(label="Alterar…")
        choose_btn.set_valign(Gtk.Align.CENTER)
        choose_btn.connect("clicked", self._on_choose_default_dir)
        self._default_dir_row.add_suffix(choose_btn)
        self._default_dir_row.set_activatable_widget(choose_btn)
        files_group.add(self._default_dir_row)

        self._auto_start_row = Adw.SwitchRow(
            title="Iniciar downloads automaticamente",
            subtitle="Começar a transferir assim que um novo download for adicionado",
        )
        self._auto_start_row.set_active(bool(cfg.auto_start_downloads))
        self._auto_start_row.connect("notify::active", self._save_config)
        files_group.add(self._auto_start_row)

        self._notifications_row = Adw.SwitchRow(
            title="Notificações da área de trabalho",
            subtitle="Exibir aviso quando um download for concluído ou falhar",
        )
        self._notifications_row.set_active(bool(cfg.enable_notifications))
        self._notifications_row.connect("notify::active", self._save_config)
        files_group.add(self._notifications_row)

        # --- Página: Desempenho ---
        perf_page = Adw.PreferencesPage(
            title="Desempenho",
            icon_name="network-transmit-receive-symbolic",
        )
        self.add(perf_page)

        perf_group = Adw.PreferencesGroup(title="Conexões e Banda")
        perf_page.add(perf_group)

        self._max_concurrent_row = Adw.SpinRow(
            title="Downloads simultâneos",
            subtitle="Quantidade máxima de arquivos baixando ao mesmo tempo",
            adjustment=Gtk.Adjustment(
                value=max(1, min(10, cfg.max_concurrent_downloads)),
                lower=1,
                upper=10,
                step_increment=1,
                page_increment=2,
            ),
        )
        self._max_concurrent_row.connect("notify::value", self._save_config)
        perf_group.add(self._max_concurrent_row)

        self._default_segments_row = Adw.SpinRow(
            title="Segmentos padrão por download",
            subtitle="Conexões paralelas usadas quando o servidor suportar HTTP Range",
            adjustment=Gtk.Adjustment(
                value=max(1, min(16, cfg.default_segments)),
                lower=1,
                upper=16,
                step_increment=1,
                page_increment=4,
            ),
        )
        self._default_segments_row.connect("notify::value", self._save_config)
        perf_group.add(self._default_segments_row)

        self._global_limit_row = Adw.SpinRow(
            title="Limite global de velocidade (KB/s)",
            subtitle="0 = ilimitado",
            adjustment=Gtk.Adjustment(
                value=max(0, cfg.global_speed_limit_kbps),
                lower=0,
                upper=1_000_000,
                step_increment=64,
                page_increment=512,
            ),
        )
        self._global_limit_row.connect("notify::value", self._save_config)
        perf_group.add(self._global_limit_row)

        # --- Página: Rede ---
        net_page = Adw.PreferencesPage(
            title="Rede",
            icon_name="network-server-symbolic",
        )
        self.add(net_page)

        net_group = Adw.PreferencesGroup(title="Tentativas e Identificação")
        net_page.add(net_group)

        self._max_retries_row = Adw.SpinRow(
            title="Máximo de tentativas",
            subtitle="Repetições automáticas em caso de falha temporária de rede",
            adjustment=Gtk.Adjustment(
                value=max(0, min(10, cfg.max_retries)),
                lower=0,
                upper=10,
                step_increment=1,
                page_increment=2,
            ),
        )
        self._max_retries_row.connect("notify::value", self._save_config)
        net_group.add(self._max_retries_row)

        self._retry_delay_row = Adw.SpinRow(
            title="Intervalo entre tentativas (s)",
            subtitle="Tempo base de espera antes de tentar novamente",
            digits=1,
            adjustment=Gtk.Adjustment(
                value=max(0.0, min(60.0, float(cfg.retry_delay_seconds))),
                lower=0.0,
                upper=60.0,
                step_increment=0.5,
                page_increment=2.0,
            ),
        )
        self._retry_delay_row.connect("notify::value", self._save_config)
        net_group.add(self._retry_delay_row)

        self._user_agent_row = Adw.EntryRow(title="User-Agent padrão")
        self._user_agent_row.set_text(cfg.user_agent or "FluxDM/1.0 (Linux)")
        self._user_agent_row.connect("changed", self._save_config)
        net_group.add(self._user_agent_row)

    def _on_choose_default_dir(self, *_args) -> None:
        current = self._default_dir_row.get_subtitle() or str(Path.home() / "Downloads")

        def _apply(folder: str) -> None:
            self._default_dir_row.set_subtitle(folder)
            self._save_config()

        _select_folder(self._parent_window, current, _apply)

    def _save_config(self, *_args) -> None:
        if self._loading:
            return
        ua = self._user_agent_row.get_text().strip() or "FluxDM/1.0 (Linux)"
        new_cfg = AppConfig(
            download_dir=self._default_dir_row.get_subtitle() or "",
            max_concurrent_downloads=int(self._max_concurrent_row.get_value()),
            default_segments=int(self._default_segments_row.get_value()),
            global_speed_limit_kbps=int(self._global_limit_row.get_value()),
            enable_notifications=bool(self._notifications_row.get_active()),
            max_retries=int(self._max_retries_row.get_value()),
            retry_delay_seconds=round(float(self._retry_delay_row.get_value()), 1),
            user_agent=ua,
            auto_start_downloads=bool(self._auto_start_row.get_active()),
        )
        self._manager.update_config(new_cfg)
        if self._on_config_changed is not None:
            self._on_config_changed(new_cfg)

    def present_for(self, parent: Gtk.Window) -> None:
        if isinstance(self, Adw.Dialog):
            self.present(parent)
        else:
            self.present()


class DetailsDialog(Adw.Dialog):
    """Diálogo de metadados completos e progresso por segmento em tempo real."""

    def __init__(
        self,
        manager: DownloadManager,
        download_id: str,
        on_toast: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._download_id = download_id
        self._on_toast = on_toast
        self._segment_widgets: dict[int, tuple[Gtk.ProgressBar, Gtk.Label, Gtk.Label]] = {}

        self.set_title("Detalhes do Download")
        self.set_content_width(600)
        self.set_content_height(660)

        self._build_ui()

        item = self._manager.get_download(download_id)
        if item is not None:
            self._populate(item)

        self._manager.subscribe(self._on_manager_event)
        self.connect("closed", self._on_closed)

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(580)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(content)
        scrolled.set_child(clamp)
        toolbar_view.set_content(scrolled)

        # --- Resumo ---
        summary_group = Adw.PreferencesGroup(title="Resumo")
        content.append(summary_group)

        self._status_row = self._make_info_row("Status")
        self._size_row = self._make_info_row("Progresso")
        self._speed_row = self._make_info_row("Velocidade / ETA")
        self._range_row = self._make_info_row("Conexões")
        for row in (self._status_row, self._size_row, self._speed_row, self._range_row):
            summary_group.add(row)

        # --- Erro (visível apenas se houver) ---
        self._error_group = Adw.PreferencesGroup(title="Mensagem de Erro")
        self._error_group.set_visible(False)
        self._error_row = Adw.ActionRow(title="Detalhes")
        self._error_row.add_css_class("error-text")
        self._error_row.set_subtitle_selectable(True)
        self._error_group.add(self._error_row)
        content.append(self._error_group)

        # --- Arquivo e Origem ---
        meta_group = Adw.PreferencesGroup(title="Arquivo e Metadados")
        content.append(meta_group)

        self._url_row, self._copy_url_btn = self._make_copyable_row("URL")
        self._path_row, self._copy_path_btn = self._make_copyable_row("Caminho final")
        self._content_type_row = self._make_info_row("Content-Type")
        self._etag_row = self._make_info_row("ETag")
        self._dates_row = self._make_info_row("Datas")
        for row in (
            self._url_row,
            self._path_row,
            self._content_type_row,
            self._etag_row,
            self._dates_row,
        ):
            meta_group.add(row)

        # --- Integridade ---
        integrity_group = Adw.PreferencesGroup(title="Integridade (Checksum)")
        content.append(integrity_group)

        self._expected_hash_row = self._make_info_row("Checksum esperado")
        self._sha256_row, self._copy_sha_btn = self._make_copyable_row("SHA-256 calculado")
        self._verified_row = self._make_info_row("Verificação")
        for row in (self._expected_hash_row, self._sha256_row, self._verified_row):
            integrity_group.add(row)

        # --- Segmentos ---
        self._segments_group = Adw.PreferencesGroup(
            title="Segmentos",
            description="Progresso individual de cada conexão",
        )
        content.append(self._segments_group)

        self._segments_box = Gtk.ListBox()
        self._segments_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._segments_box.add_css_class("boxed-list")
        self._segments_group.add(self._segments_box)

        self._no_segments_label = Gtk.Label(label="Nenhum segmento ativo registrado.")
        self._no_segments_label.add_css_class("dim-label")
        self._no_segments_label.set_margin_top(12)
        self._no_segments_label.set_margin_bottom(12)

    def _make_info_row(self, title: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle="—")
        row.add_css_class("property")
        row.set_subtitle_selectable(True)
        return row

    def _make_copyable_row(self, title: str) -> tuple[Adw.ActionRow, Gtk.Button]:
        row = self._make_info_row(title)
        btn = Gtk.Button(icon_name="edit-copy-symbolic")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.set_tooltip_text(f"Copiar {title}")
        row.add_suffix(btn)
        return row, btn

    def _bind_copy(self, button: Gtk.Button, text: str, label: str) -> None:
        if hasattr(button, "_copy_handler_id"):
            button.disconnect(button._copy_handler_id)

        def _clicked(_btn: Gtk.Button) -> None:
            if text and copy_to_clipboard(text) and self._on_toast:
                self._on_toast(f"{label} copiado para a área de transferência.")

        button._copy_handler_id = button.connect("clicked", _clicked)
        button.set_sensitive(bool(text))

    def _populate(self, item: DownloadItem) -> None:
        self.set_title(item.filename or "Detalhes do Download")

        status_text = STATUS_LABELS.get(item.status, item.status.value)
        self._status_row.set_subtitle(status_text)

        if item.total_bytes > 0:
            pct = item.progress * 100.0
            size_text = (
                f"{format_bytes(item.downloaded_bytes)} / {format_bytes(item.total_bytes)} "
                f"({pct:.1f}%)"
            )
        else:
            size_text = f"{format_bytes(item.downloaded_bytes)} / Desconhecido"
        self._size_row.set_subtitle(size_text)

        speed_part = format_speed(item.speed_bps)
        eta_part = format_eta(item.eta_seconds)
        limit_part = (
            f" • Limite: {item.speed_limit_kbps} KB/s"
            if item.speed_limit_kbps > 0
            else ""
        )
        self._speed_row.set_subtitle(f"{speed_part} • ETA: {eta_part}{limit_part}")

        range_text = "Suporta Range" if item.supports_range else "Sem suporte a Range"
        seg_count = len(item.segments) if item.segments else item.num_segments
        self._range_row.set_subtitle(f"{seg_count} conexão(ões) • {range_text}")

        if item.error_message:
            self._error_group.set_visible(True)
            self._error_row.set_subtitle(item.error_message)
        else:
            self._error_group.set_visible(False)

        self._url_row.set_subtitle(item.url or "—")
        self._bind_copy(self._copy_url_btn, item.url, "URL")

        final_path = str(item.filepath)
        self._path_row.set_subtitle(final_path)
        self._bind_copy(self._copy_path_btn, final_path, "Caminho")

        self._content_type_row.set_subtitle(item.content_type or "—")
        self._etag_row.set_subtitle(item.etag or "—")
        self._dates_row.set_subtitle(
            f"Criado: {_format_timestamp(item.created_at)}  •  "
            f"Concluído: {_format_timestamp(item.completed_at)}"
        )

        self._expected_hash_row.set_subtitle(item.expected_checksum or "Não informado")
        self._sha256_row.set_subtitle(item.computed_sha256 or "Ainda não calculado")
        self._bind_copy(self._copy_sha_btn, item.computed_sha256, "SHA-256")

        if item.checksum_verified is True:
            ver_text = "Válido (corresponde ao checksum esperado)"
        elif item.checksum_verified is False:
            ver_text = "Falhou (checksum diferente do esperado!)"
        elif item.expected_checksum:
            ver_text = "Pendente de conclusão"
        else:
            ver_text = "Não solicitado"
        self._verified_row.set_subtitle(ver_text)

        self._update_segments(item)

    def _update_segments(self, item: DownloadItem) -> None:
        segments = item.segments
        if not segments:
            if not self._segment_widgets:
                child = self._segments_box.get_first_child()
                if child is None:
                    row = Gtk.ListBoxRow()
                    row.set_activatable(False)
                    row.set_selectable(False)
                    row.set_child(self._no_segments_label)
                    self._segments_box.append(row)
            return

        existing_indices = set(self._segment_widgets.keys())
        current_indices = {seg.index for seg in segments}

        if existing_indices != current_indices:
            while (child := self._segments_box.get_first_child()) is not None:
                self._segments_box.remove(child)
            self._segment_widgets.clear()

            for seg in segments:
                row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                row_box.set_margin_top(10)
                row_box.set_margin_bottom(10)
                row_box.set_margin_start(12)
                row_box.set_margin_end(12)

                top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                title_lbl = Gtk.Label(xalign=0)
                title_lbl.set_hexpand(True)
                title_lbl.add_css_class("heading")
                title_lbl.set_ellipsize(Pango.EllipsizeMode.END)

                stat_lbl = Gtk.Label(xalign=1)
                stat_lbl.add_css_class("mono-stats")

                top.append(title_lbl)
                top.append(stat_lbl)
                row_box.append(top)

                pbar = Gtk.ProgressBar()
                pbar.add_css_class("segment-progress")
                row_box.append(pbar)

                list_row = Gtk.ListBoxRow()
                list_row.set_activatable(False)
                list_row.set_selectable(False)
                list_row.set_child(row_box)
                self._segments_box.append(list_row)

                self._segment_widgets[seg.index] = (pbar, title_lbl, stat_lbl)

        for seg in segments:
            pbar, title_lbl, stat_lbl = self._segment_widgets[seg.index]
            if seg.end_byte >= 0:
                range_desc = f"{format_bytes(seg.start_byte)} – {format_bytes(seg.end_byte)}"
            else:
                range_desc = f"{format_bytes(seg.start_byte)} – fim"
            title_lbl.set_text(f"Segmento #{seg.index + 1}  ({range_desc})")

            if seg.total_bytes > 0:
                pbar.set_fraction(seg.progress)
                stat_lbl.set_text(
                    f"{format_bytes(seg.downloaded_bytes)} / {format_bytes(seg.total_bytes)} "
                    f"({seg.progress * 100:.0f}%)"
                )
            else:
                pbar.set_fraction(1.0 if seg.completed else 0.0)
                stat_lbl.set_text(format_bytes(seg.downloaded_bytes))

    def _on_manager_event(self, event: str, item: DownloadItem) -> None:
        if item.id != self._download_id:
            return
        if event == "removed":
            GLib.idle_add(self.close)
            return
        GLib.idle_add(self._populate_safe, item)

    def _populate_safe(self, item: DownloadItem) -> bool:
        self._populate(item)
        return False

    def _on_closed(self, *_args) -> None:
        self._manager.unsubscribe(self._on_manager_event)


def confirm_remove_dialog(
    parent: Gtk.Window,
    item: DownloadItem,
    on_confirm: Callable[[bool], None],
) -> None:
    """Confirma remoção de um download com opção para excluir o arquivo do disco."""
    dialog = Adw.AlertDialog(
        heading="Remover download?",
        body=f"Deseja remover “{item.filename}” da lista do FluxDM?",
    )
    dialog.add_response("cancel", "Cancelar")
    dialog.add_response("remove", "Remover")
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    delete_check = Gtk.CheckButton(label="Também excluir o arquivo do disco")
    delete_check.set_margin_top(6)
    dialog.set_extra_child(delete_check)

    def _on_response(_dlg: Adw.AlertDialog, response: str) -> None:
        if response == "remove":
            on_confirm(bool(delete_check.get_active()))

    dialog.connect("response", _on_response)
    dialog.present(parent)


def show_about_dialog(parent: Gtk.Window) -> None:
    """Exibe o diálogo Sobre o FluxDM."""
    if hasattr(Adw, "AboutDialog"):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="folder-download-symbolic",
            version=__version__,
            developer_name="Equipe FluxDM",
            comments=(
                "Gerenciador de downloads moderno e acelerado para Linux "
                "com suporte a múltiplas conexões, pausa/retomada e verificação "
                "de integridade SHA-256/MD5."
            ),
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/fluxdm/fluxdm",
            issue_url="https://github.com/fluxdm/fluxdm/issues",
        )
        about.present(parent)
    else:
        about = Adw.AboutWindow(
            transient_for=parent,
            application_name=APP_NAME,
            application_icon="folder-download-symbolic",
            version=__version__,
            developer_name="Equipe FluxDM",
            comments=(
                "Gerenciador de downloads moderno e acelerado para Linux "
                "com suporte a múltiplas conexões e verificação de integridade."
            ),
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/fluxdm/fluxdm",
        )
        about.present()
