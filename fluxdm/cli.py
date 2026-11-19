"""Interface de linha de comando (CLI) para o FluxDM."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from . import APP_NAME, __version__
from .models import (
    AppConfig,
    DownloadItem,
    DownloadStatus,
    format_bytes,
    format_eta,
    format_speed,
)

STATUS_LABELS_PT: dict[DownloadStatus, str] = {
    DownloadStatus.QUEUED: "Na fila",
    DownloadStatus.DOWNLOADING: "Baixando",
    DownloadStatus.PAUSED: "Pausado",
    DownloadStatus.COMPLETED: "Concluído",
    DownloadStatus.ERROR: "Erro",
    DownloadStatus.CANCELLED: "Cancelado",
}

TERMINAL_STATUSES = {
    DownloadStatus.COMPLETED,
    DownloadStatus.ERROR,
    DownloadStatus.CANCELLED,
}


def _short_id(download_id: str, length: int = 8) -> str:
    return download_id[:length]


def _format_timestamp(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    try:
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    head = (max_len - 3) // 2
    tail = max_len - 3 - head
    return f"{text[:head]}...{text[-tail:]}" if tail > 0 else f"{text[:max_len - 3]}..."


def _parse_headers(raw_headers: Optional[list[str]]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for entry in raw_headers or []:
        if ":" not in entry:
            raise ValueError(
                f"Cabeçalho inválido: {entry!r}. Use o formato 'Chave: Valor'."
            )
        key, value = entry.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(
                f"Nome de cabeçalho vazio em {entry!r}. Use o formato 'Chave: Valor'."
            )
        headers[key] = value
    return headers


def _item_to_dict(item: DownloadItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "short_id": _short_id(item.id),
        "url": item.url,
        "filename": item.filename,
        "dest_dir": item.dest_dir,
        "filepath": str(item.filepath),
        "status": item.status.value,
        "status_label": STATUS_LABELS_PT.get(item.status, item.status.value),
        "progress": round(item.progress * 100.0, 2),
        "downloaded_bytes": item.downloaded_bytes,
        "total_bytes": item.total_bytes,
        "speed_bps": round(item.speed_bps, 2),
        "eta_seconds": item.eta_seconds,
        "num_segments": item.num_segments,
        "supports_range": item.supports_range,
        "speed_limit_kbps": item.speed_limit_kbps,
        "expected_checksum": item.expected_checksum,
        "computed_sha256": item.computed_sha256,
        "checksum_verified": item.checksum_verified,
        "etag": item.etag,
        "content_type": item.content_type,
        "error_message": item.error_message,
        "priority": item.priority,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
        "segments": [seg.to_dict() for seg in item.segments],
    }


def _resolve_download(
    manager: Any,
    query: str,
    err_stream: TextIO = sys.stderr,
) -> Optional[DownloadItem]:
    """Resolve um download por ID completo ou prefixo curto."""
    cleaned = query.strip()
    if not cleaned:
        print("Erro: informe um ID ou prefixo de download válido.", file=err_stream)
        return None

    exact = manager.get_download(cleaned)
    if exact is not None:
        return exact

    needle = cleaned.lower()
    matches = [
        item
        for item in manager.list_downloads()
        if item.id.lower().startswith(needle)
    ]

    if not matches:
        print(
            f"Erro: nenhum download encontrado com o ID ou prefixo '{cleaned}'.",
            file=err_stream,
        )
        return None

    if len(matches) > 1:
        print(
            f"Erro: o prefixo '{cleaned}' é ambíguo e corresponde a {len(matches)} downloads:",
            file=err_stream,
        )
        for item in matches[:10]:
            status_str = STATUS_LABELS_PT.get(item.status, item.status.value)
            print(
                f"  {_short_id(item.id, 12)}  [{status_str}]  {item.filename}",
                file=err_stream,
            )
        print("Informe mais caracteres do ID para desambiguar.", file=err_stream)
        return None

    return matches[0]


def _make_bar(progress: float, width: int = 20, unicode_blocks: bool = True) -> str:
    clamped = min(1.0, max(0.0, progress))
    filled = int(round(clamped * width))
    empty = width - filled
    if unicode_blocks:
        return f"[{'█' * filled}{'░' * empty}]"
    return f"[{'#' * filled}{'-' * empty}]"


def _supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "█░".encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


class _ProgressRenderer:
    """Renderiza progresso ao vivo em terminal ANSI com degradação limpa sem TTY."""

    def __init__(
        self,
        item_ids: list[str],
        stream: TextIO = sys.stdout,
    ) -> None:
        self._item_ids = list(item_ids)
        self._stream = stream
        self._lock = threading.Lock()
        self._is_tty = bool(
            hasattr(stream, "isatty")
            and stream.isatty()
            and os.environ.get("TERM", "") != "dumb"
        )
        self._unicode = _supports_unicode(stream)
        self._last_lines = 0
        self._last_render_time = 0.0
        self._last_logged_bucket: dict[str, int] = {}
        self._last_logged_status: dict[str, DownloadStatus] = {}
        self._items: dict[str, DownloadItem] = {}

    def update_item(self, item: DownloadItem) -> None:
        with self._lock:
            if item.id in self._item_ids:
                self._items[item.id] = item

    def _format_line(self, item: DownloadItem, max_width: int) -> str:
        pct = item.progress * 100.0
        bar_width = 20 if max_width >= 90 else 12
        bar = _make_bar(item.progress, width=bar_width, unicode_blocks=self._unicode)

        downloaded = format_bytes(item.downloaded_bytes)
        if item.total_bytes > 0:
            size_part = f"{downloaded} / {format_bytes(item.total_bytes)}"
        else:
            size_part = downloaded

        seg_count = len(item.segments) if item.segments else max(1, item.num_segments)
        seg_part = f"{seg_count} seg"

        if item.status == DownloadStatus.DOWNLOADING:
            speed_part = format_speed(item.speed_bps)
            eta_part = f"ETA {format_eta(item.eta_seconds)}"
            metrics = f"{bar} {pct:5.1f}%  {size_part}  {speed_part}  {eta_part}  {seg_part}"
        elif item.status == DownloadStatus.COMPLETED:
            metrics = f"{bar} 100.0%  {size_part}  Concluído  {seg_part}"
        elif item.status == DownloadStatus.ERROR:
            err = _truncate(item.error_message or "Falha no download", 36)
            metrics = f"{bar} {pct:5.1f}%  Erro: {err}"
        else:
            label = STATUS_LABELS_PT.get(item.status, item.status.value)
            metrics = f"{bar} {pct:5.1f}%  {size_part}  {label}"

        available_name = max(12, max_width - len(metrics) - 2)
        name_col = _truncate(item.filename or _short_id(item.id), min(28, available_name))
        line = f"{name_col:<{min(28, available_name)}}  {metrics}"
        if len(line) > max_width:
            line = line[:max_width]
        return line

    def render(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and (now - self._last_render_time) < 0.08:
                return
            self._last_render_time = now

            ordered = [self._items[iid] for iid in self._item_ids if iid in self._items]
            if not ordered:
                return

            if self._is_tty:
                cols = shutil.get_terminal_size((100, 24)).columns
                max_width = max(50, cols - 1)
                lines = [self._format_line(item, max_width) for item in ordered]

                if self._last_lines > 1:
                    self._stream.write(f"\x1b[{self._last_lines - 1}A")
                if self._last_lines >= 1:
                    self._stream.write("\r")

                for idx, line in enumerate(lines):
                    self._stream.write(f"\x1b[2K{line}")
                    if idx < len(lines) - 1:
                        self._stream.write("\n")

                self._stream.flush()
                self._last_lines = len(lines)
            else:
                # Modo sem TTY: imprime marcos legíveis sem códigos ANSI
                for item in ordered:
                    bucket = int(item.progress * 100) // 20
                    prev_bucket = self._last_logged_bucket.get(item.id, -1)
                    prev_status = self._last_logged_status.get(item.id)
                    should_log = (
                        force
                        or prev_status != item.status
                        or (
                            item.status == DownloadStatus.DOWNLOADING
                            and item.total_bytes > 0
                            and bucket > prev_bucket
                        )
                    )
                    if should_log:
                        self._last_logged_bucket[item.id] = bucket
                        self._last_logged_status[item.id] = item.status
                        line = self._format_line(item, 120)
                        self._stream.write(f"{line}\n")
                        self._stream.flush()

    def finish(self) -> None:
        with self._lock:
            if self._is_tty and self._last_lines > 0:
                self._stream.write("\n")
                self._stream.flush()
                self._last_lines = 0


def _wait_with_progress(
    manager: Any,
    item_ids: list[str],
    out_stream: TextIO = sys.stdout,
    err_stream: TextIO = sys.stderr,
) -> int:
    """Acompanha downloads ativos com barra de progresso e trata Ctrl+C com segurança."""
    renderer = _ProgressRenderer(item_ids, stream=out_stream)

    for iid in item_ids:
        current = manager.get_download(iid)
        if current is not None:
            renderer.update_item(current)

    def _on_event(_event: str, item: DownloadItem) -> None:
        if item.id in item_ids:
            renderer.update_item(item)

    manager.subscribe(_on_event)
    try:
        renderer.render(force=True)
        while True:
            all_done = True
            for iid in item_ids:
                current = manager.get_download(iid)
                if current is None:
                    continue
                renderer.update_item(current)
                if current.status not in TERMINAL_STATUSES and current.status != DownloadStatus.PAUSED:
                    all_done = False

            renderer.render(force=all_done)
            if all_done:
                break
            time.sleep(0.12)
    except KeyboardInterrupt:
        renderer.finish()
        paused_ids: list[str] = []
        for iid in item_ids:
            current = manager.get_download(iid)
            if current is not None and current.status not in TERMINAL_STATUSES:
                try:
                    manager.pause_download(iid)
                    paused_ids.append(iid)
                except Exception:  # noqa: BLE001
                    pass

        print("\nInterrompido pelo usuário (Ctrl+C).", file=err_stream)
        if paused_ids:
            print(
                "Download(s) pausado(s) com segurança. Retome posteriormente com:",
                file=err_stream,
            )
            for iid in paused_ids:
                print(f"  fluxdm resume {_short_id(iid)}", file=err_stream)
        return 130
    finally:
        manager.unsubscribe(_on_event)
        renderer.finish()

    # Resumo final
    has_failure = False
    for iid in item_ids:
        item = manager.get_download(iid)
        if item is None:
            continue
        if item.status == DownloadStatus.COMPLETED:
            msg = f"Concluído: {item.filepath} ({format_bytes(item.downloaded_bytes)})"
            if item.computed_sha256:
                if item.checksum_verified is True:
                    msg += " [SHA-256 verificado]"
                elif item.checksum_verified is False:
                    msg += " [ATENÇÃO: SHA-256 divergente!]"
                    has_failure = True
            print(msg, file=out_stream)
        elif item.status == DownloadStatus.ERROR:
            has_failure = True
            print(
                f"Erro [{_short_id(item.id)}] {item.filename}: "
                f"{item.error_message or 'falha desconhecida'}",
                file=err_stream,
            )
        elif item.status == DownloadStatus.CANCELLED:
            has_failure = True
            print(
                f"Cancelado [{_short_id(item.id)}] {item.filename}",
                file=err_stream,
            )
        elif item.status == DownloadStatus.PAUSED:
            print(
                f"Pausado [{_short_id(item.id)}] {item.filename} "
                f"(retome com: fluxdm resume {_short_id(item.id)})",
                file=out_stream,
            )

    return 1 if has_failure else 0


def _cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui.app import run_gui
    except (ImportError, ValueError) as exc:
        print(
            "Erro: interface gráfica indisponível (GTK4 / Libadwaita não encontrado).",
            file=sys.stderr,
        )
        print(f"Detalhe: {exc}", file=sys.stderr)
        print(
            "Instale python3-gi, gir1.2-gtk-4.0 e gir1.2-adw-1, ou use os comandos CLI.",
            file=sys.stderr,
        )
        return 1

    try:
        return int(run_gui(urls=args.urls or None))
    except Exception as exc:  # noqa: BLE001
        print(f"Erro ao iniciar a interface gráfica: {exc}", file=sys.stderr)
        return 1


def _cmd_add(args: argparse.Namespace, manager: Any) -> int:
    urls: list[str] = list(args.urls)

    if args.output and len(urls) > 1:
        print(
            "Erro: a opção -o/--output só pode ser usada com uma única URL.",
            file=sys.stderr,
        )
        return 2

    if args.checksum and len(urls) > 1:
        print(
            "Erro: a opção --checksum só pode ser usada com uma única URL.",
            file=sys.stderr,
        )
        return 2

    if args.segments is not None and not (1 <= args.segments <= 32):
        print(
            "Erro: o número de segmentos (-s/--segments) deve estar entre 1 e 32.",
            file=sys.stderr,
        )
        return 2

    if args.limit < 0:
        print(
            "Erro: o limite de velocidade (-l/--limit) deve ser >= 0 KB/s.",
            file=sys.stderr,
        )
        return 2

    try:
        headers = _parse_headers(args.header)
    except ValueError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    dest_dir: Optional[str] = None
    if args.dir:
        dest_dir = str(Path(args.dir).expanduser())

    start_immediately = not args.no_start
    if args.no_start:
        should_wait = False
    elif args.wait is None:
        should_wait = True
    else:
        should_wait = bool(args.wait)

    added_items: list[DownloadItem] = []
    for url in urls:
        try:
            item = manager.add_download(
                url=url,
                dest_dir=dest_dir,
                filename=args.output or None,
                num_segments=args.segments,
                speed_limit_kbps=args.limit,
                expected_checksum=args.checksum or "",
                headers=headers or None,
                start_immediately=start_immediately,
            )
            added_items.append(item)
        except Exception as exc:  # noqa: BLE001
            print(f"Erro ao adicionar '{url}': {exc}", file=sys.stderr)
            return 1

    if not should_wait:
        for item in added_items:
            status_str = STATUS_LABELS_PT.get(item.status, item.status.value)
            print(
                f"Adicionado [{_short_id(item.id)}] ({status_str}): "
                f"{item.filename} -> {item.dest_dir}"
            )
        return 0

    return _wait_with_progress(manager, [item.id for item in added_items])


def _cmd_probe(args: argparse.Namespace, manager: Any) -> int:
    try:
        headers = _parse_headers(args.header)
    except ValueError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    try:
        result = manager.probe_url(args.url, headers=headers or None)
    except Exception as exc:  # noqa: BLE001
        print(f"Erro ao inspecionar URL: {exc}", file=sys.stderr)
        return 1

    if result.total_bytes > 0:
        size_str = f"{format_bytes(result.total_bytes)} ({result.total_bytes} bytes)"
    else:
        size_str = "Desconhecido"

    print(f"URL final       : {result.url}")
    print(f"Arquivo         : {result.filename}")
    print(f"Tamanho         : {size_str}")
    print(f"Suporte a Range : {'Sim' if result.supports_range else 'Não'}")
    print(f"Content-Type    : {result.content_type or 'Desconhecido'}")
    print(f"ETag            : {result.etag or '—'}")
    return 0


def _cmd_list(args: argparse.Namespace, manager: Any) -> int:
    status_filter: Optional[DownloadStatus] = None
    if args.status != "all":
        status_filter = DownloadStatus(args.status)

    items: list[DownloadItem] = manager.list_downloads(status=status_filter)

    if args.json:
        payload = [_item_to_dict(item) for item in items]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not items:
        print("Nenhum download encontrado.")
        return 0

    headers = ("ID", "STATUS", "PROGRESSO", "TAMANHO", "ARQUIVO")
    rows: list[tuple[str, str, str, str, str]] = []
    for item in items:
        short = _short_id(item.id)
        status_str = STATUS_LABELS_PT.get(item.status, item.status.value)
        prog_str = f"{item.progress * 100.0:5.1f}%"
        if item.total_bytes > 0:
            size_str = f"{format_bytes(item.downloaded_bytes)} / {format_bytes(item.total_bytes)}"
        else:
            size_str = format_bytes(item.downloaded_bytes)
        rows.append((short, status_str, prog_str, size_str, item.filename))

    widths = [
        max(len(headers[col]), *(len(r[col]) for r in rows))
        for col in range(4)
    ]

    header_line = (
        f"{headers[0]:<{widths[0]}}  "
        f"{headers[1]:<{widths[1]}}  "
        f"{headers[2]:>{widths[2]}}  "
        f"{headers[3]:<{widths[3]}}  "
        f"{headers[4]}"
    )
    print(header_line)
    print("-" * max(len(header_line), 64))
    for r in rows:
        print(
            f"{r[0]:<{widths[0]}}  "
            f"{r[1]:<{widths[1]}}  "
            f"{r[2]:>{widths[2]}}  "
            f"{r[3]:<{widths[3]}}  "
            f"{r[4]}"
        )
    return 0


def _cmd_info(args: argparse.Namespace, manager: Any) -> int:
    item = _resolve_download(manager, args.id)
    if item is None:
        return 1

    status_str = STATUS_LABELS_PT.get(item.status, item.status.value)
    if item.total_bytes > 0:
        size_str = (
            f"{format_bytes(item.downloaded_bytes)} / {format_bytes(item.total_bytes)} "
            f"({item.progress * 100.0:.1f}%)"
        )
    else:
        size_str = f"{format_bytes(item.downloaded_bytes)} (tamanho total desconhecido)"

    limit_str = (
        f"{item.speed_limit_kbps} KB/s" if item.speed_limit_kbps > 0 else "Ilimitado"
    )

    if item.checksum_verified is True:
        checksum_status = "Verificado (OK)"
    elif item.checksum_verified is False:
        checksum_status = "INVÁLIDO (divergente)"
    elif item.expected_checksum:
        checksum_status = "Aguardando conclusão"
    else:
        checksum_status = "Não informado"

    print(f"ID                : {item.id}")
    print(f"Arquivo           : {item.filename}")
    print(f"Caminho           : {item.filepath}")
    print(f"URL               : {item.url}")
    print(f"Status            : {status_str} ({item.status.value})")
    print(f"Progresso         : {size_str}")
    if item.status == DownloadStatus.DOWNLOADING:
        print(f"Velocidade        : {format_speed(item.speed_bps)}")
        print(f"Tempo restante    : {format_eta(item.eta_seconds)}")
    print(f"Limite de banda   : {limit_str}")
    print(f"Suporte a Range   : {'Sim' if item.supports_range else 'Não'}")
    print(f"Content-Type      : {item.content_type or 'Desconhecido'}")
    print(f"ETag              : {item.etag or '—'}")
    print(f"Criado em         : {_format_timestamp(item.created_at)}")
    print(f"Concluído em      : {_format_timestamp(item.completed_at)}")
    print(f"SHA-256 calculado : {item.computed_sha256 or '—'}")
    if item.expected_checksum:
        print(f"SHA-256 esperado  : {item.expected_checksum}")
    print(f"Verificação hash  : {checksum_status}")
    if item.error_message:
        print(f"Último erro       : {item.error_message}")

    if item.segments:
        use_unicode = _supports_unicode(sys.stdout)
        print(f"\nSegmentos ({len(item.segments)}):")
        for seg in item.segments:
            bar = _make_bar(seg.progress, width=16, unicode_blocks=use_unicode)
            end_str = str(seg.end_byte) if seg.end_byte >= 0 else "?"
            seg_total = format_bytes(seg.total_bytes) if seg.total_bytes > 0 else "?"
            seg_state = "concluído" if seg.completed else "em andamento"
            print(
                f"  #{seg.index + 1:02d}  {bar} {seg.progress * 100.0:5.1f}%  "
                f"{format_bytes(seg.downloaded_bytes)} / {seg_total}  "
                f"(bytes {seg.start_byte}-{end_str})  [{seg_state}]"
            )
    else:
        print(f"\nSegmentos configurados: {item.num_segments} (ainda não divididos)")

    return 0


def _cmd_resume(args: argparse.Namespace, manager: Any) -> int:
    item = _resolve_download(manager, args.id)
    if item is None:
        return 1

    if item.status == DownloadStatus.COMPLETED:
        print(f"O download [{_short_id(item.id)}] ({item.filename}) já está concluído.")
        return 0

    try:
        if item.status in (DownloadStatus.ERROR, DownloadStatus.CANCELLED):
            manager.retry_download(item.id)
        elif item.status == DownloadStatus.QUEUED:
            manager.start_download(item.id)
        else:
            manager.resume_download(item.id)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Erro ao retomar download [{_short_id(item.id)}]: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.no_wait:
        print(f"Download [{_short_id(item.id)}] ({item.filename}) retomado em segundo plano.")
        return 0

    return _wait_with_progress(manager, [item.id])


def _cmd_pause(args: argparse.Namespace, manager: Any) -> int:
    item = _resolve_download(manager, args.id)
    if item is None:
        return 1

    if item.status == DownloadStatus.COMPLETED:
        print(
            f"O download [{_short_id(item.id)}] ({item.filename}) já foi concluído.",
            file=sys.stderr,
        )
        return 1

    if item.status == DownloadStatus.PAUSED:
        print(f"O download [{_short_id(item.id)}] ({item.filename}) já está pausado.")
        return 0

    try:
        manager.pause_download(item.id)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Erro ao pausar download [{_short_id(item.id)}]: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"Download pausado: [{_short_id(item.id)}] {item.filename}")
    return 0


def _cmd_remove(args: argparse.Namespace, manager: Any) -> int:
    item = _resolve_download(manager, args.id)
    if item is None:
        return 1

    try:
        manager.remove_download(item.id, delete_file=bool(args.delete_file))
    except Exception as exc:  # noqa: BLE001
        print(
            f"Erro ao remover download [{_short_id(item.id)}]: {exc}",
            file=sys.stderr,
        )
        return 1

    suffix = " (e arquivo removido do disco)" if args.delete_file else ""
    print(f"Download removido: [{_short_id(item.id)}] {item.filename}{suffix}")
    return 0


def _cmd_clear(_args: argparse.Namespace, manager: Any) -> int:
    try:
        removed = manager.clear_completed()
    except Exception as exc:  # noqa: BLE001
        print(f"Erro ao limpar downloads concluídos: {exc}", file=sys.stderr)
        return 1

    count = int(removed) if removed is not None else 0
    if count == 0:
        print("Nenhum download concluído para limpar.")
    elif count == 1:
        print("1 download concluído removido do histórico.")
    else:
        print(f"{count} downloads concluídos removidos do histórico.")
    return 0


def _print_config(cfg: AppConfig) -> None:
    limit_str = (
        f"{cfg.global_speed_limit_kbps} KB/s"
        if cfg.global_speed_limit_kbps > 0
        else "Ilimitado (0)"
    )
    print(f"Pasta de downloads      : {cfg.download_dir}")
    print(f"Downloads simultâneos   : {cfg.max_concurrent_downloads}")
    print(f"Segmentos padrão        : {cfg.default_segments}")
    print(f"Limite global de banda  : {limit_str}")
    print(
        f"Notificações de desktop : {'Ativadas' if cfg.enable_notifications else 'Desativadas'}"
    )
    print(f"User-Agent              : {cfg.user_agent}")
    print(f"Máximo de tentativas    : {cfg.max_retries}")
    print(f"Iniciar automaticamente : {'Sim' if cfg.auto_start_downloads else 'Não'}")


def _cmd_config(args: argparse.Namespace, manager: Any) -> int:
    current: AppConfig = manager.config
    updated = AppConfig.from_dict(current.to_dict())
    changed = False

    if args.dir is not None:
        updated.download_dir = str(Path(args.dir).expanduser())
        changed = True

    if args.max_concurrent is not None:
        if args.max_concurrent < 1:
            print(
                "Erro: --max-concurrent deve ser maior ou igual a 1.",
                file=sys.stderr,
            )
            return 2
        updated.max_concurrent_downloads = args.max_concurrent
        changed = True

    if args.segments is not None:
        if not (1 <= args.segments <= 32):
            print(
                "Erro: --segments deve estar entre 1 e 32.",
                file=sys.stderr,
            )
            return 2
        updated.default_segments = args.segments
        changed = True

    if args.speed_limit is not None:
        if args.speed_limit < 0:
            print(
                "Erro: --speed-limit deve ser maior ou igual a 0 (0 = ilimitado).",
                file=sys.stderr,
            )
            return 2
        updated.global_speed_limit_kbps = args.speed_limit
        changed = True

    if args.notifications is not None:
        updated.enable_notifications = args.notifications == "on"
        changed = True

    if args.user_agent is not None:
        ua = args.user_agent.strip()
        if not ua:
            print("Erro: --user-agent não pode ser vazio.", file=sys.stderr)
            return 2
        updated.user_agent = ua
        changed = True

    if changed:
        try:
            manager.update_config(updated)
        except Exception as exc:  # noqa: BLE001
            print(f"Erro ao salvar configurações: {exc}", file=sys.stderr)
            return 1
        print("Configurações atualizadas com sucesso:\n")
        _print_config(manager.config)
        return 0

    _print_config(current)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fluxdm",
        description=f"{APP_NAME} — Gerenciador de downloads rápido e moderno para Linux.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"{APP_NAME} {__version__}",
        help="Exibe a versão do FluxDM e sai.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="Comandos disponíveis",
        metavar="COMANDO",
    )

    # gui
    p_gui = subparsers.add_parser(
        "gui",
        help="Inicia a interface gráfica (GTK4).",
        description="Inicia a interface gráfica do FluxDM, opcionalmente pré-preenchendo URLs.",
    )
    p_gui.add_argument(
        "urls",
        nargs="*",
        help="URL(s) opcionais para abrir no diálogo de novo download.",
    )

    # add
    p_add = subparsers.add_parser(
        "add",
        help="Adiciona e inicia o download de uma ou mais URLs.",
        description=(
            "Adiciona uma ou mais URLs e acompanha o progresso em tempo real. "
            "Pressione Ctrl+C para pausar com segurança."
        ),
    )
    p_add.add_argument(
        "urls",
        nargs="+",
        metavar="URL",
        help="URL(s) para download.",
    )
    p_add.add_argument(
        "-d",
        "--dir",
        metavar="PASTA",
        help="Pasta de destino do download.",
    )
    p_add.add_argument(
        "-o",
        "--output",
        metavar="ARQUIVO",
        help="Nome do arquivo de saída (válido apenas para uma única URL).",
    )
    p_add.add_argument(
        "-s",
        "--segments",
        type=int,
        metavar="N",
        help="Número de segmentos paralelos (1 a 32).",
    )
    p_add.add_argument(
        "-l",
        "--limit",
        type=int,
        default=0,
        metavar="KBPS",
        help="Limite de velocidade em KB/s (0 = ilimitado).",
    )
    p_add.add_argument(
        "--checksum",
        default="",
        metavar="SHA256",
        help="Checksum SHA-256 esperado para validação ao concluir.",
    )
    p_add.add_argument(
        "-H",
        "--header",
        action="append",
        metavar="'Chave: Valor'",
        help="Cabeçalho HTTP personalizado (pode ser repetido).",
    )
    p_add.add_argument(
        "--no-start",
        action="store_true",
        help="Apenas enfileira como pausado, sem iniciar nem aguardar.",
    )
    p_add.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Aguarda a conclusão exibindo barra de progresso (padrão: ativo).",
    )

    # probe
    p_probe = subparsers.add_parser(
        "probe",
        help="Inspeciona metadados de uma URL (HEAD/Range) sem baixar.",
        description="Consulta URL final, nome detectado, tamanho, suporte a Range, Content-Type e ETag.",
    )
    p_probe.add_argument("url", metavar="URL", help="URL a inspecionar.")
    p_probe.add_argument(
        "-H",
        "--header",
        action="append",
        metavar="'Chave: Valor'",
        help="Cabeçalho HTTP personalizado (pode ser repetido).",
    )

    # list
    p_list = subparsers.add_parser(
        "list",
        help="Lista os downloads registrados.",
        description="Exibe uma tabela (ou JSON) dos downloads salvos no banco de dados.",
    )
    p_list.add_argument(
        "--status",
        choices=[
            "all",
            "downloading",
            "queued",
            "paused",
            "completed",
            "error",
            "cancelled",
        ],
        default="all",
        help="Filtra downloads por status (padrão: all).",
    )
    p_list.add_argument(
        "--json",
        action="store_true",
        help="Formata a saída como JSON.",
    )

    # info
    p_info = subparsers.add_parser(
        "info",
        help="Exibe detalhes completos de um download e seus segmentos.",
        description="Mostra metadados, hash SHA-256 e progresso por segmento de um download.",
    )
    p_info.add_argument(
        "id",
        metavar="ID",
        help="ID completo ou prefixo curto do download.",
    )

    # resume
    p_resume = subparsers.add_parser(
        "resume",
        help="Retoma um download pausado ou com erro.",
        description="Retoma um download pausado/falho e exibe o progresso até concluir.",
    )
    p_resume.add_argument(
        "id",
        metavar="ID",
        help="ID completo ou prefixo curto do download.",
    )
    p_resume.add_argument(
        "--no-wait",
        action="store_true",
        help="Apenas marca para retomar sem acompanhar o progresso no terminal.",
    )

    # pause
    p_pause = subparsers.add_parser(
        "pause",
        help="Marca um download como pausado no banco de dados.",
        description="Pausa um download ativo ou enfileirado.",
    )
    p_pause.add_argument(
        "id",
        metavar="ID",
        help="ID completo ou prefixo curto do download.",
    )

    # remove
    p_remove = subparsers.add_parser(
        "remove",
        help="Remove um download do histórico.",
        description="Remove um registro do banco de dados, opcionalmente apagando o arquivo do disco.",
    )
    p_remove.add_argument(
        "id",
        metavar="ID",
        help="ID completo ou prefixo curto do download.",
    )
    p_remove.add_argument(
        "--delete-file",
        action="store_true",
        help="Apaga também o arquivo baixado (ou parcial) no disco.",
    )

    # clear
    subparsers.add_parser(
        "clear",
        help="Remove todos os downloads concluídos do histórico.",
        description="Limpa do banco de dados todas as entradas com status concluído.",
    )

    # config
    p_cfg = subparsers.add_parser(
        "config",
        help="Exibe ou atualiza as configurações persistentes.",
        description="Visualiza ou altera as preferências salvas em config.json.",
    )
    p_cfg.add_argument(
        "--show",
        action="store_true",
        help="Exibe as configurações atuais.",
    )
    p_cfg.add_argument(
        "--dir",
        metavar="PASTA",
        help="Define a pasta padrão de downloads.",
    )
    p_cfg.add_argument(
        "--max-concurrent",
        type=int,
        metavar="N",
        help="Número máximo de downloads simultâneos.",
    )
    p_cfg.add_argument(
        "--segments",
        type=int,
        metavar="N",
        help="Número padrão de segmentos por download (1 a 32).",
    )
    p_cfg.add_argument(
        "--speed-limit",
        type=int,
        metavar="KBPS",
        help="Limite global de velocidade em KB/s (0 = ilimitado).",
    )
    p_cfg.add_argument(
        "--notifications",
        choices=["on", "off"],
        help="Ativa ou desativa notificações de desktop (on|off).",
    )
    p_cfg.add_argument(
        "--user-agent",
        metavar="UA",
        help="Define o cabeçalho User-Agent padrão.",
    )

    return parser


def main(
    argv: Optional[list[str]] = None,
    manager_factory: Optional[Callable[[], Any]] = None,
) -> int:
    """Ponto de entrada principal da CLI do FluxDM."""
    parser = build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)

    if not args_list:
        parser.print_help()
        return 0

    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "gui":
        return _cmd_gui(args)

    owns_manager = manager_factory is None
    if manager_factory is not None:
        manager = manager_factory()
    else:
        from .engine import DownloadManager

        manager = DownloadManager()

    try:
        if args.command == "add":
            return _cmd_add(args, manager)
        if args.command == "probe":
            return _cmd_probe(args, manager)
        if args.command == "list":
            return _cmd_list(args, manager)
        if args.command == "info":
            return _cmd_info(args, manager)
        if args.command == "resume":
            return _cmd_resume(args, manager)
        if args.command == "pause":
            return _cmd_pause(args, manager)
        if args.command == "remove":
            return _cmd_remove(args, manager)
        if args.command == "clear":
            return _cmd_clear(args, manager)
        if args.command == "config":
            return _cmd_config(args, manager)

        parser.print_help()
        return 2
    finally:
        if owns_manager:
            try:
                manager.shutdown()
            except Exception:  # noqa: BLE001
                pass
