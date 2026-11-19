"""Core multi-threaded download engine and queue manager for FluxDM."""

from __future__ import annotations

import collections
import concurrent.futures
import errno
import hashlib
import http.client
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import (
    AppConfig,
    DownloadItem,
    DownloadStatus,
    ProbeResult,
    SegmentInfo,
    sanitize_filename,
)
from .notifications import Notifier
from .storage import ConfigStore, DownloadDatabase, get_default_download_dir

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0
_DEFAULT_CHUNK_SIZE = 65536
_MIN_CHUNK_SIZE = 2048
_PROGRESS_INTERVAL = 0.2  # ~5 Hz
_DB_CHECKPOINT_INTERVAL = 1.5
_SPEED_WINDOW_SECONDS = 2.5

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_CONTENT_RANGE_UNSAT_RE = re.compile(r"bytes\s+\*/(\d+)", re.IGNORECASE)
_CD_FILENAME_STAR_RE = re.compile(
    r"""filename\*\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\s]+))""",
    re.IGNORECASE,
)
_CD_FILENAME_QUOTED_RE = re.compile(
    r"""filename\s*=\s*"((?:\\.|[^"\\])*)" """,
    re.IGNORECASE | re.VERBOSE,
)
_CD_FILENAME_PLAIN_RE = re.compile(
    r"""filename\s*=\s*([^;\s"']+)""",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class _RangeNotSupportedError(Exception):
    """Internal signal that the server ignored Range and returned HTTP 200."""


class _DownloadStopped(Exception):
    """Internal signal that a download was paused, cancelled, or shut down."""


class TokenBucket:
    """Thread-safe, interruptible token bucket rate limiter in bytes/sec."""

    def __init__(self, rate_kbps: int = 0) -> None:
        self._lock = threading.Lock()
        self._rate_bps: float = 0.0
        self._capacity: float = 0.0
        self._tokens: float = 0.0
        self._last_refill: float = time.monotonic()
        self.set_rate_kbps(rate_kbps)

    @property
    def rate_bps(self) -> float:
        with self._lock:
            return self._rate_bps

    def set_rate_kbps(self, kbps: int) -> None:
        rate_bps = max(0, int(kbps)) * 1024.0
        with self._lock:
            now = time.monotonic()
            self._refill_locked(now)
            self._rate_bps = rate_bps
            if rate_bps > 0:
                # Keep burst small so short rate-limited transfers remain accurate.
                self._capacity = max(4096.0, min(rate_bps * 0.15, 16384.0))
                self._tokens = min(self._tokens, self._capacity)
                if self._tokens == 0.0:
                    self._tokens = min(self._capacity, 4096.0)
            else:
                self._capacity = 0.0
                self._tokens = 0.0
            self._last_refill = now

    def _refill_locked(self, now: float) -> None:
        if self._rate_bps <= 0:
            self._last_refill = now
            return
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self._capacity, self._tokens + elapsed * self._rate_bps
            )
            self._last_refill = now

    def consume(
        self, num_bytes: int, stop_event: Optional[threading.Event] = None
    ) -> bool:
        """Wait until ``num_bytes`` tokens are available. Returns False if interrupted."""
        remaining = float(num_bytes)
        if remaining <= 0:
            return True

        while remaining > 0:
            if stop_event is not None and stop_event.is_set():
                return False

            with self._lock:
                if self._rate_bps <= 0:
                    return True
                now = time.monotonic()
                self._refill_locked(now)
                if self._tokens > 0:
                    take = min(remaining, self._tokens)
                    self._tokens -= take
                    remaining -= take
                    if remaining <= 0:
                        return True
                needed = min(remaining, self._capacity)
                sleep_time = min(max(0.005, needed / self._rate_bps), 0.25)

            if stop_event is not None:
                if stop_event.wait(sleep_time):
                    return False
            else:
                time.sleep(sleep_time)

        return True


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    """Atomically write all bytes at ``offset`` using Linux os.pwrite."""
    view = memoryview(data)
    written = 0
    total = len(view)
    while written < total:
        n = os.pwrite(fd, view[written:], offset + written)
        if n <= 0:
            raise OSError("os.pwrite wrote 0 bytes")
        written += n


def _is_retryable_error(exc: BaseException) -> bool:
    """Return True if an exception represents a transient network or server error."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code in (408, 429)
    if isinstance(
        exc,
        (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            socket.timeout,
            ConnectionError,
        ),
    ):
        return True
    if isinstance(exc, OSError):
        # Local filesystem permission / disk-full errors are not transient network issues.
        if exc.errno in (
            errno.EACCES,
            errno.EPERM,
            errno.ENOSPC,
            errno.EROFS,
            errno.EISDIR,
            errno.ENOTDIR,
            errno.ENOENT,
        ):
            return False
        return True
    return False


def _extract_filename_from_cd(content_disposition: str) -> Optional[str]:
    """Extract filename from a Content-Disposition header (supports RFC 5987)."""
    if not content_disposition:
        return None

    # Prefer RFC 5987 filename*=charset'lang'value
    star_match = _CD_FILENAME_STAR_RE.search(content_disposition)
    if star_match:
        raw_val = next((g for g in star_match.groups() if g), "")
        if "''" in raw_val:
            charset, _, rest = raw_val.partition("'")
            _, _, encoded = rest.partition("'")
            decoded = urllib.parse.unquote(
                encoded, encoding=charset.strip() or "utf-8", errors="replace"
            )
            if decoded.strip():
                return sanitize_filename(decoded)
        elif raw_val.strip():
            decoded = urllib.parse.unquote(raw_val, encoding="utf-8", errors="replace")
            return sanitize_filename(decoded)

    quoted_match = _CD_FILENAME_QUOTED_RE.search(content_disposition)
    if quoted_match:
        unescaped = re.sub(r"\\(.)", r"\1", quoted_match.group(1))
        if unescaped.strip():
            return sanitize_filename(unescaped)

    plain_match = _CD_FILENAME_PLAIN_RE.search(content_disposition)
    if plain_match:
        raw_name = plain_match.group(1).strip()
        if raw_name:
            return sanitize_filename(urllib.parse.unquote(raw_name))

    return None


def _filename_from_url(url: str) -> str:
    """Derive a clean provisional filename from a URL path."""
    try:
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parsed.path or "")
        basename = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
        if basename:
            return sanitize_filename(basename)
    except Exception:
        pass
    return "download"


def _deduplicate_filename(
    dest_dir: Path, filename: str, reserved: Optional[set[str]] = None
) -> str:
    """Return a non-colliding filename inside ``dest_dir``."""
    reserved_set = reserved or set()

    def _is_taken(candidate: str) -> bool:
        if candidate in reserved_set:
            return True
        if (dest_dir / candidate).exists():
            return True
        return False

    if not _is_taken(filename):
        return filename

    # Preserve common compound extensions like .tar.gz / .tar.xz / .tar.bz2
    lower = filename.lower()
    stem = filename
    suffix = ""
    for compound in (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst"):
        if lower.endswith(compound) and len(filename) > len(compound):
            stem = filename[: -len(compound)]
            suffix = filename[-len(compound) :]
            break
    else:
        p = Path(filename)
        if p.suffix and p.stem:
            stem = p.stem
            suffix = p.suffix

    counter = 1
    while True:
        candidate = f"{stem} ({counter}){suffix}"
        if not _is_taken(candidate):
            return candidate
        counter += 1


def _clone_item(item: DownloadItem) -> DownloadItem:
    """Create a thread-safe snapshot copy of a DownloadItem for callbacks."""
    return DownloadItem(
        id=item.id,
        url=item.url,
        filename=item.filename,
        dest_dir=item.dest_dir,
        status=item.status,
        total_bytes=item.total_bytes,
        downloaded_bytes=item.downloaded_bytes,
        speed_bps=item.speed_bps,
        eta_seconds=item.eta_seconds,
        num_segments=item.num_segments,
        supports_range=item.supports_range,
        segments=[
            SegmentInfo(
                index=s.index,
                start_byte=s.start_byte,
                end_byte=s.end_byte,
                downloaded_bytes=s.downloaded_bytes,
                completed=s.completed,
            )
            for s in item.segments
        ],
        speed_limit_kbps=item.speed_limit_kbps,
        expected_checksum=item.expected_checksum,
        computed_sha256=item.computed_sha256,
        checksum_verified=item.checksum_verified,
        headers=dict(item.headers),
        etag=item.etag,
        content_type=item.content_type,
        error_message=item.error_message,
        priority=item.priority,
        created_at=item.created_at,
        completed_at=item.completed_at,
    )


@dataclass
class _WorkerHandle:
    """Tracks a single active download execution."""

    download_id: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    desired_status: Optional[DownloadStatus] = None
    delete_part: bool = False
    thread: Optional[threading.Thread] = None
    bucket: TokenBucket = field(default_factory=TokenBucket)


class _SpeedTracker:
    """Sliding-window + EMA speed calculator for a single active download."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: collections.deque[tuple[float, int]] = collections.deque()
        self._start_time: float = time.monotonic()
        self._smoothed_bps: float = 0.0

    def record(self, num_bytes: int) -> None:
        if num_bytes <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, num_bytes))
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - _SPEED_WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def get_speed_bps(self) -> float:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            if not self._samples:
                self._smoothed_bps *= 0.5
                if self._smoothed_bps < 1.0:
                    self._smoothed_bps = 0.0
                return self._smoothed_bps
            total = sum(b for _, b in self._samples)
            window_start = max(self._start_time, now - _SPEED_WINDOW_SECONDS)
            elapsed = max(now - window_start, 0.2)
            instant = total / elapsed
            if self._smoothed_bps <= 0:
                self._smoothed_bps = instant
            else:
                self._smoothed_bps = 0.6 * instant + 0.4 * self._smoothed_bps
            return self._smoothed_bps


class DownloadManager:
    """Thread-safe download manager supporting segmented transfers, pause/resume,
    rate limiting, checksum verification, priority queueing, and SQLite persistence."""

    def __init__(
        self,
        db: Optional[DownloadDatabase] = None,
        config_store: Optional[ConfigStore] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self._config_store = config_store if config_store is not None else ConfigStore()
        self._db = db if db is not None else DownloadDatabase()
        self.config: AppConfig = self._config_store.load()
        if not self.config.download_dir:
            self.config.download_dir = str(get_default_download_dir())

        self._notifier = (
            notifier
            if notifier is not None
            else Notifier(enabled=self.config.enable_notifications)
        )
        self._notifier.set_enabled(self.config.enable_notifications)

        self._lock = threading.RLock()
        self._state_cv = threading.Condition(self._lock)
        self._global_bucket = TokenBucket(self.config.global_speed_limit_kbps)

        self._items: dict[str, DownloadItem] = {}
        self._user_filenames: set[str] = set()
        self._active_workers: dict[str, _WorkerHandle] = {}
        self._dying_workers: dict[str, _WorkerHandle] = {}
        self._subscribers: list[Callable[[str, DownloadItem], None]] = []
        self._shutdown: bool = False

        # Restore persisted downloads; recover interrupted DOWNLOADING items as PAUSED.
        for item in self._db.list_all():
            if item.status == DownloadStatus.DOWNLOADING:
                item.status = DownloadStatus.PAUSED
                item.speed_bps = 0.0
                item.eta_seconds = None
                self._db.upsert(item)
            self._items[item.id] = item
            if item.segments or item.part_filepath.exists():
                self._user_filenames.add(item.id)

    # ------------------------------------------------------------------
    # Configuration & Event Subscriptions
    # ------------------------------------------------------------------

    def update_config(self, config: AppConfig) -> None:
        if not config.download_dir:
            config.download_dir = str(get_default_download_dir())
        config.max_concurrent_downloads = max(1, int(config.max_concurrent_downloads))
        config.default_segments = max(1, min(32, int(config.default_segments)))
        config.global_speed_limit_kbps = max(0, int(config.global_speed_limit_kbps))
        config.max_retries = max(0, int(config.max_retries))
        config.retry_delay_seconds = max(0.0, float(config.retry_delay_seconds))

        with self._lock:
            self.config = config
            self._global_bucket.set_rate_kbps(config.global_speed_limit_kbps)
            self._notifier.set_enabled(config.enable_notifications)
        self._config_store.save(config)
        self._schedule_next()

    def subscribe(self, callback: Callable[[str, DownloadItem], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, DownloadItem], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def _emit(self, event: str, item: DownloadItem) -> None:
        with self._lock:
            callbacks = list(self._subscribers)
            snapshot = _clone_item(item)
        for cb in callbacks:
            try:
                cb(event, snapshot)
            except Exception:
                logger.exception("Error in DownloadManager subscriber callback")

    # ------------------------------------------------------------------
    # HTTP Helpers & URL Probing
    # ------------------------------------------------------------------

    def _build_headers(
        self, extra_headers: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        with self._lock:
            ua = self.config.user_agent or "FluxDM/1.0 (Linux)"
        merged: dict[str, str] = {"User-Agent": ua}
        if extra_headers:
            existing_lower = {k.lower(): k for k in merged}
            for k, v in extra_headers.items():
                key_str = str(k).strip()
                if not key_str:
                    continue
                lower_k = key_str.lower()
                if lower_k in existing_lower:
                    del merged[existing_lower[lower_k]]
                merged[key_str] = str(v)
                existing_lower[lower_k] = key_str
        return merged

    def _validate_url(self, url: str) -> str:
        cleaned = (url or "").strip()
        parsed = urllib.parse.urlsplit(cleaned)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Unsupported or invalid URL: {url!r}")
        return cleaned

    def probe_url(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> ProbeResult:
        """Probe remote metadata using HEAD with fallback to ranged GET (bytes=0-0)."""
        clean_url = self._validate_url(url)
        req_headers = self._build_headers(headers)

        final_url = clean_url
        total_bytes = 0
        supports_range = False
        etag = ""
        content_type = ""
        content_disposition = ""
        head_succeeded = False

        # Step 1: Try HEAD
        head_req = urllib.request.Request(
            clean_url, headers=dict(req_headers), method="HEAD"
        )
        try:
            with urllib.request.urlopen(head_req, timeout=timeout) as resp:
                head_succeeded = True
                final_url = resp.geturl() or clean_url
                hdrs = resp.headers
                cl_raw = hdrs.get("Content-Length")
                if cl_raw is not None:
                    try:
                        parsed_cl = int(cl_raw.strip())
                        if parsed_cl >= 0:
                            total_bytes = parsed_cl
                    except ValueError:
                        pass
                accept_ranges = (hdrs.get("Accept-Ranges") or "").lower()
                if "bytes" in accept_ranges:
                    supports_range = True
                etag = (hdrs.get("ETag") or "").strip()
                content_type = (hdrs.get("Content-Type") or "").split(";")[0].strip()
                content_disposition = hdrs.get("Content-Disposition") or ""
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 403, 405, 501):
                raise

        # Step 2: Fallback to GET with Range: bytes=0-0 when HEAD failed or lacked details
        need_range_probe = (
            not head_succeeded
            or total_bytes <= 0
            or not supports_range
        )
        if need_range_probe:
            range_headers = dict(req_headers)
            range_headers["Range"] = "bytes=0-0"
            get_req = urllib.request.Request(
                final_url, headers=range_headers, method="GET"
            )
            try:
                with urllib.request.urlopen(get_req, timeout=timeout) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    final_url = resp.geturl() or final_url
                    hdrs = resp.headers
                    if not etag:
                        etag = (hdrs.get("ETag") or "").strip()
                    if not content_type:
                        content_type = (
                            (hdrs.get("Content-Type") or "").split(";")[0].strip()
                        )
                    if not content_disposition:
                        content_disposition = hdrs.get("Content-Disposition") or ""

                    if status == 206:
                        supports_range = True
                        cr = hdrs.get("Content-Range") or ""
                        m = _CONTENT_RANGE_RE.search(cr)
                        if m and m.group(3) != "*":
                            total_bytes = int(m.group(3))
                    elif status == 200:
                        supports_range = False
                        cl_raw = hdrs.get("Content-Length")
                        if cl_raw is not None:
                            try:
                                parsed_cl = int(cl_raw.strip())
                                if parsed_cl >= 0:
                                    total_bytes = parsed_cl
                            except ValueError:
                                pass
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    # 416 on bytes=0-0 typically indicates a 0-byte empty resource.
                    cr = (exc.headers.get("Content-Range") if exc.headers else "") or ""
                    m = _CONTENT_RANGE_UNSAT_RE.search(cr)
                    if m:
                        total_bytes = int(m.group(1))
                    supports_range = False
                    if not head_succeeded and exc.headers:
                        etag = etag or (exc.headers.get("ETag") or "").strip()
                        content_type = content_type or (
                            (exc.headers.get("Content-Type") or "").split(";")[0].strip()
                        )
                        content_disposition = content_disposition or (
                            exc.headers.get("Content-Disposition") or ""
                        )
                elif not head_succeeded:
                    raise

        filename = (
            _extract_filename_from_cd(content_disposition)
            or _filename_from_url(final_url)
            or _filename_from_url(clean_url)
        )
        return ProbeResult(
            url=final_url,
            filename=sanitize_filename(filename),
            total_bytes=max(0, total_bytes),
            supports_range=supports_range,
            etag=etag,
            content_type=content_type,
        )

    # ------------------------------------------------------------------
    # Public Download Management API
    # ------------------------------------------------------------------

    def add_download(
        self,
        url: str,
        dest_dir: Optional[str] = None,
        filename: Optional[str] = None,
        num_segments: Optional[int] = None,
        speed_limit_kbps: int = 0,
        expected_checksum: str = "",
        headers: Optional[dict[str, str]] = None,
        start_immediately: Optional[bool] = None,
        priority: int = 0,
    ) -> DownloadItem:
        clean_url = self._validate_url(url)

        with self._lock:
            resolved_dir = (
                str(Path(dest_dir).expanduser())
                if dest_dir
                else (self.config.download_dir or str(get_default_download_dir()))
            )
            user_specified = bool(filename and filename.strip())
            resolved_name = (
                sanitize_filename(filename.strip())
                if user_specified
                else _filename_from_url(clean_url)
            )
            resolved_segments = max(
                1,
                min(
                    32,
                    int(
                        num_segments
                        if num_segments is not None
                        else self.config.default_segments
                    ),
                ),
            )
            should_start = (
                self.config.auto_start_downloads
                if start_immediately is None
                else bool(start_immediately)
            )
            initial_status = (
                DownloadStatus.QUEUED if should_start else DownloadStatus.PAUSED
            )

            # Avoid sharing a .fluxdm.part filename with another active/incomplete item in dest_dir.
            reserved = self._reserved_filenames_locked(resolved_dir)
            if resolved_name in reserved:
                resolved_name = _deduplicate_filename(
                    Path(resolved_dir), resolved_name, reserved=reserved
                )

            item = DownloadItem(
                id=str(uuid.uuid4()),
                url=clean_url,
                filename=resolved_name,
                dest_dir=resolved_dir,
                status=initial_status,
                num_segments=resolved_segments,
                speed_limit_kbps=max(0, int(speed_limit_kbps)),
                expected_checksum=(expected_checksum or "").strip(),
                headers=dict(headers) if headers else {},
                priority=int(priority),
                created_at=time.time(),
            )
            self._items[item.id] = item
            if user_specified:
                self._user_filenames.add(item.id)
            self._db.upsert(item)

        self._emit("added", item)
        if should_start:
            self._schedule_next()
        return item

    def _reserved_filenames_locked(
        self, dest_dir: str, exclude_id: Optional[str] = None
    ) -> set[str]:
        reserved: set[str] = set()
        for item in self._items.values():
            if item.id == exclude_id:
                continue
            if item.dest_dir == dest_dir and item.status in (
                DownloadStatus.QUEUED,
                DownloadStatus.DOWNLOADING,
                DownloadStatus.PAUSED,
            ):
                reserved.add(item.filename)
        return reserved

    def start_download(self, download_id: str) -> bool:
        with self._lock:
            item = self._items.get(download_id)
            if item is None:
                return False
            if item.status in (DownloadStatus.DOWNLOADING, DownloadStatus.COMPLETED):
                return False
            item.status = DownloadStatus.QUEUED
            item.error_message = ""
            self._db.upsert(item)
        self._schedule_next()
        return True

    def pause_download(self, download_id: str) -> bool:
        handle_to_join: Optional[_WorkerHandle] = None
        with self._lock:
            item = self._items.get(download_id)
            if item is None:
                return False
            if item.status == DownloadStatus.QUEUED:
                item.status = DownloadStatus.PAUSED
                item.speed_bps = 0.0
                item.eta_seconds = None
                self._db.upsert(item)
                self._state_cv.notify_all()
                paused_item = item
            elif item.status == DownloadStatus.DOWNLOADING:
                handle_to_join = self._active_workers.get(download_id)
                if handle_to_join is not None:
                    handle_to_join.desired_status = DownloadStatus.PAUSED
                    handle_to_join.stop_event.set()
                    self._dying_workers[download_id] = handle_to_join
                item.status = DownloadStatus.PAUSED
                item.speed_bps = 0.0
                item.eta_seconds = None
                self._db.upsert(item)
                paused_item = item
            else:
                return False

        if (
            handle_to_join is not None
            and handle_to_join.thread is not None
            and handle_to_join.thread is not threading.current_thread()
        ):
            handle_to_join.thread.join(timeout=2.0)

        with self._lock:
            if download_id in self._items:
                self._db.upsert(self._items[download_id])
            self._state_cv.notify_all()

        self._emit("paused", paused_item)
        self._schedule_next()
        return True

    def resume_download(self, download_id: str) -> bool:
        with self._lock:
            item = self._items.get(download_id)
            if item is None:
                return False
            if item.status not in (DownloadStatus.PAUSED, DownloadStatus.ERROR):
                return False
            item.status = DownloadStatus.QUEUED
            item.error_message = ""
            self._db.upsert(item)
            resumed_item = item

        self._emit("resumed", resumed_item)
        self._schedule_next()
        return True

    def cancel_download(self, download_id: str, delete_part: bool = True) -> bool:
        handle_to_join: Optional[_WorkerHandle] = None
        part_path: Optional[Path] = None

        with self._lock:
            item = self._items.get(download_id)
            if item is None:
                return False
            if item.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                return False

            handle_to_join = self._active_workers.get(download_id)
            if handle_to_join is not None:
                handle_to_join.desired_status = DownloadStatus.CANCELLED
                handle_to_join.delete_part = delete_part
                handle_to_join.stop_event.set()
                self._dying_workers[download_id] = handle_to_join

            item.status = DownloadStatus.CANCELLED
            item.speed_bps = 0.0
            item.eta_seconds = None
            part_path = item.part_filepath
            if delete_part:
                item.segments = []
                item.downloaded_bytes = 0
            self._db.upsert(item)
            cancelled_item = item

        if (
            handle_to_join is not None
            and handle_to_join.thread is not None
            and handle_to_join.thread is not threading.current_thread()
        ):
            handle_to_join.thread.join(timeout=2.0)

        if delete_part and part_path is not None:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass

        with self._lock:
            if download_id in self._items:
                if delete_part:
                    self._items[download_id].segments = []
                    self._items[download_id].downloaded_bytes = 0
                self._db.upsert(self._items[download_id])
            self._state_cv.notify_all()

        self._emit("cancelled", cancelled_item)
        self._schedule_next()
        return True

    def retry_download(self, download_id: str) -> bool:
        with self._lock:
            item = self._items.get(download_id)
            if item is None:
                return False
            if item.status not in (
                DownloadStatus.ERROR,
                DownloadStatus.CANCELLED,
                DownloadStatus.PAUSED,
            ):
                return False

            reset_all = (
                item.status == DownloadStatus.CANCELLED
                or item.checksum_verified is False
                or not item.part_filepath.exists()
            )
            if reset_all:
                try:
                    item.part_filepath.unlink(missing_ok=True)
                except OSError:
                    pass
                item.segments = []
                item.downloaded_bytes = 0

            item.status = DownloadStatus.QUEUED
            item.error_message = ""
            item.checksum_verified = None
            item.computed_sha256 = ""
            item.speed_bps = 0.0
            item.eta_seconds = None
            self._db.upsert(item)

        self._schedule_next()
        return True

    def remove_download(self, download_id: str, delete_file: bool = False) -> bool:
        handle_to_join: Optional[_WorkerHandle] = None
        with self._lock:
            item = self._items.get(download_id)
            if item is None:
                return False

            handle_to_join = self._active_workers.get(download_id)
            if handle_to_join is not None:
                handle_to_join.desired_status = DownloadStatus.CANCELLED
                handle_to_join.delete_part = True
                handle_to_join.stop_event.set()
                self._dying_workers[download_id] = handle_to_join

            part_path = item.part_filepath
            final_path = item.filepath
            item.status = DownloadStatus.CANCELLED
            item.speed_bps = 0.0
            item.eta_seconds = None
            removed_item = _clone_item(item)

        if (
            handle_to_join is not None
            and handle_to_join.thread is not None
            and handle_to_join.thread is not threading.current_thread()
        ):
            handle_to_join.thread.join(timeout=2.0)

        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        if delete_file:
            try:
                final_path.unlink(missing_ok=True)
            except OSError:
                pass

        with self._lock:
            self._items.pop(download_id, None)
            self._user_filenames.discard(download_id)
            self._active_workers.pop(download_id, None)
            self._dying_workers.pop(download_id, None)
            self._db.delete(download_id)
            self._state_cv.notify_all()

        self._emit("removed", removed_item)
        self._schedule_next()
        return True

    def pause_all(self) -> int:
        # Pause QUEUED items first so pausing DOWNLOADING items cannot trigger them.
        with self._lock:
            queued_ids = [
                it.id
                for it in self._items.values()
                if it.status == DownloadStatus.QUEUED
            ]
            downloading_ids = [
                it.id
                for it in self._items.values()
                if it.status == DownloadStatus.DOWNLOADING
            ]

        count = 0
        for dl_id in queued_ids:
            if self.pause_download(dl_id):
                count += 1
        for dl_id in downloading_ids:
            if self.pause_download(dl_id):
                count += 1
        return count

    def resume_all(self) -> int:
        with self._lock:
            paused_ids = [
                it.id
                for it in sorted(
                    self._items.values(),
                    key=lambda x: (-x.priority, x.created_at),
                )
                if it.status == DownloadStatus.PAUSED
            ]
            resumed: list[DownloadItem] = []
            for dl_id in paused_ids:
                item = self._items.get(dl_id)
                if item is not None and item.status == DownloadStatus.PAUSED:
                    item.status = DownloadStatus.QUEUED
                    item.error_message = ""
                    self._db.upsert(item)
                    resumed.append(item)

        for item in resumed:
            self._emit("resumed", item)
        if resumed:
            self._schedule_next()
        return len(resumed)

    def clear_completed(self) -> int:
        with self._lock:
            completed_items = [
                _clone_item(it)
                for it in self._items.values()
                if it.status == DownloadStatus.COMPLETED
            ]
            for it in completed_items:
                self._items.pop(it.id, None)
                self._user_filenames.discard(it.id)
            db_count = self._db.clear_completed()
            self._state_cv.notify_all()

        for it in completed_items:
            self._emit("removed", it)
        return max(len(completed_items), db_count)

    def get_download(self, download_id: str) -> Optional[DownloadItem]:
        with self._lock:
            return self._items.get(download_id)

    def list_downloads(
        self, status: Optional[DownloadStatus] = None
    ) -> list[DownloadItem]:
        with self._lock:
            items = list(self._items.values())
            if status is not None:
                items = [it for it in items if it.status == status]
            items.sort(key=lambda it: it.created_at, reverse=True)
            return items

    def get_total_speed_bps(self) -> float:
        with self._lock:
            return sum(
                it.speed_bps
                for it in self._items.values()
                if it.status == DownloadStatus.DOWNLOADING
            )

    def get_active_count(self) -> int:
        with self._lock:
            return sum(
                1
                for it in self._items.values()
                if it.status == DownloadStatus.DOWNLOADING
            )

    def wait_for_download(
        self, download_id: str, timeout: Optional[float] = None
    ) -> Optional[DownloadItem]:
        deadline = None if timeout is None else (time.monotonic() + max(0.0, timeout))
        with self._state_cv:
            while True:
                item = self._items.get(download_id)
                if item is None:
                    return None
                active = download_id in self._active_workers
                if (
                    item.status
                    not in (DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING)
                    and not active
                ):
                    return item
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return item
                    self._state_cv.wait(timeout=remaining)
                else:
                    self._state_cv.wait()

    def wait_all(self, timeout: Optional[float] = None) -> None:
        deadline = None if timeout is None else (time.monotonic() + max(0.0, timeout))
        with self._state_cv:
            while True:
                has_pending = bool(self._active_workers) or any(
                    it.status in (DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING)
                    for it in self._items.values()
                )
                if not has_pending:
                    return
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    self._state_cv.wait(timeout=remaining)
                else:
                    self._state_cv.wait()

    def shutdown(self) -> None:
        handles: list[_WorkerHandle] = []
        with self._lock:
            self._shutdown = True
            for dl_id, handle in list(self._active_workers.items()):
                handle.desired_status = DownloadStatus.PAUSED
                handle.stop_event.set()
                handles.append(handle)
                item = self._items.get(dl_id)
                if item is not None and item.status == DownloadStatus.DOWNLOADING:
                    item.status = DownloadStatus.PAUSED
                    item.speed_bps = 0.0
                    item.eta_seconds = None
                    self._db.upsert(item)

        for handle in handles:
            if (
                handle.thread is not None
                and handle.thread is not threading.current_thread()
            ):
                handle.thread.join(timeout=5.0)

        with self._lock:
            self._state_cv.notify_all()
            self._db.close()

    # ------------------------------------------------------------------
    # Queue Scheduler & Worker Execution
    # ------------------------------------------------------------------

    def _schedule_next(self) -> None:
        to_start: list[tuple[DownloadItem, _WorkerHandle, Optional[_WorkerHandle]]] = []
        with self._lock:
            if self._shutdown:
                return
            max_concurrent = max(1, int(self.config.max_concurrent_downloads))
            available = max_concurrent - len(self._active_workers)
            if available <= 0:
                return

            queued = [
                it
                for it in self._items.values()
                if it.status == DownloadStatus.QUEUED
                and it.id not in self._active_workers
            ]
            queued.sort(key=lambda it: (-it.priority, it.created_at))

            for item in queued[:available]:
                item.status = DownloadStatus.DOWNLOADING
                item.error_message = ""
                item.speed_bps = 0.0
                self._db.upsert(item)

                prev_dying = self._dying_workers.pop(item.id, None)
                handle = _WorkerHandle(
                    download_id=item.id,
                    bucket=TokenBucket(item.speed_limit_kbps),
                )
                thread = threading.Thread(
                    target=self._worker_entry,
                    args=(item.id, handle, prev_dying),
                    name=f"fluxdm-dl-{item.id[:8]}",
                    daemon=True,
                )
                handle.thread = thread
                self._active_workers[item.id] = handle
                to_start.append((item, handle, prev_dying))

        for item, handle, _ in to_start:
            self._emit("started", item)
            assert handle.thread is not None
            handle.thread.start()

    def _compute_chunk_size(self, per_dl_kbps: int) -> int:
        global_bps = self._global_bucket.rate_bps
        per_bps = max(0, int(per_dl_kbps)) * 1024.0
        limits = [r for r in (global_bps, per_bps) if r > 0]
        if not limits:
            return _DEFAULT_CHUNK_SIZE
        effective_bps = min(limits)
        return max(_MIN_CHUNK_SIZE, min(_DEFAULT_CHUNK_SIZE, int(effective_bps / 8)))

    def _worker_entry(
        self,
        download_id: str,
        handle: _WorkerHandle,
        prev_dying: Optional[_WorkerHandle],
    ) -> None:
        # Ensure any previous worker for the same item has fully released its file descriptor.
        if (
            prev_dying is not None
            and prev_dying.thread is not None
            and prev_dying.thread is not threading.current_thread()
        ):
            prev_dying.thread.join(timeout=5.0)

        try:
            self._run_download(download_id, handle)
        except _DownloadStopped:
            pass
        except Exception as exc:
            self._handle_worker_error(download_id, handle, exc)
        finally:
            cleanup_part: Optional[Path] = None
            with self._lock:
                if self._active_workers.get(download_id) is handle:
                    del self._active_workers[download_id]
                if self._dying_workers.get(download_id) is handle:
                    del self._dying_workers[download_id]
                item = self._items.get(download_id)
                if item is not None:
                    if handle.stop_event.is_set():
                        item.speed_bps = 0.0
                        item.eta_seconds = None
                        if handle.desired_status is not None:
                            item.status = handle.desired_status
                        if handle.delete_part:
                            cleanup_part = item.part_filepath
                            item.segments = []
                            item.downloaded_bytes = 0
                        self._db.upsert(item)
                self._state_cv.notify_all()

            if cleanup_part is not None:
                try:
                    cleanup_part.unlink(missing_ok=True)
                except OSError:
                    pass

            self._schedule_next()

    def _handle_worker_error(
        self, download_id: str, handle: _WorkerHandle, exc: Exception
    ) -> None:
        with self._lock:
            if handle.stop_event.is_set():
                return
            item = self._items.get(download_id)
            if item is None or item.status != DownloadStatus.DOWNLOADING:
                return
            item.status = DownloadStatus.ERROR
            item.speed_bps = 0.0
            item.eta_seconds = None
            item.error_message = str(exc) or exc.__class__.__name__
            self._db.upsert(item)
            errored = item

        self._emit("error", errored)
        self._notifier.notify_error(errored)

    def _probe_with_retries(
        self, url: str, headers: dict[str, str], stop_event: threading.Event
    ) -> ProbeResult:
        with self._lock:
            max_retries = self.config.max_retries
            base_delay = self.config.retry_delay_seconds

        attempt = 0
        while True:
            if stop_event.is_set():
                raise _DownloadStopped()
            try:
                return self.probe_url(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
            except Exception as exc:
                if stop_event.is_set():
                    raise _DownloadStopped() from exc
                if not _is_retryable_error(exc) or attempt >= max_retries:
                    raise
                delay = min(60.0, base_delay * (2**attempt))
                attempt += 1
                if delay > 0 and stop_event.wait(delay):
                    raise _DownloadStopped()

    def _run_download(self, download_id: str, handle: _WorkerHandle) -> None:
        with self._lock:
            item = self._items.get(download_id)
            if item is None or handle.stop_event.is_set():
                raise _DownloadStopped()
            url = item.url
            headers = dict(item.headers)
            has_resumable_state = (
                bool(item.segments)
                and item.total_bytes > 0
                and item.part_filepath.is_file()
            )
            part_path = item.part_filepath
            dest_dir = Path(item.dest_dir)

        dest_dir.mkdir(parents=True, exist_ok=True)

        # Phase 1: Probe URL unless we already have valid resumable metadata + .part file.
        if not has_resumable_state:
            probe = self._probe_with_retries(url, headers, handle.stop_event)
            if handle.stop_event.is_set():
                raise _DownloadStopped()

            with self._lock:
                item = self._items.get(download_id)
                if item is None or handle.stop_event.is_set():
                    raise _DownloadStopped()
                item.total_bytes = probe.total_bytes
                item.supports_range = probe.supports_range
                item.etag = probe.etag
                item.content_type = probe.content_type
                if (
                    download_id not in self._user_filenames
                    and probe.filename
                    and not item.part_filepath.exists()
                ):
                    reserved = self._reserved_filenames_locked(
                        item.dest_dir, exclude_id=download_id
                    )
                    new_name = probe.filename
                    if new_name in reserved:
                        new_name = _deduplicate_filename(
                            Path(item.dest_dir), new_name, reserved=reserved
                        )
                    item.filename = new_name
                self._db.upsert(item)
                part_path = item.part_filepath
                target_url = probe.url or item.url
            self._emit("progress", item)
        else:
            target_url = url

        speed_tracker = _SpeedTracker()
        progress_lock = threading.Lock()
        last_emit_time = [0.0]
        last_db_time = [time.monotonic()]

        def on_bytes_written(n_bytes: int) -> None:
            speed_tracker.record(n_bytes)
            now = time.monotonic()
            should_emit = False
            should_db = False
            with progress_lock:
                if now - last_emit_time[0] >= _PROGRESS_INTERVAL:
                    last_emit_time[0] = now
                    should_emit = True
                if now - last_db_time[0] >= _DB_CHECKPOINT_INTERVAL:
                    last_db_time[0] = now
                    should_db = True

            if should_emit or should_db:
                with self._lock:
                    cur = self._items.get(download_id)
                    if cur is None or cur.status != DownloadStatus.DOWNLOADING:
                        return
                    cur.downloaded_bytes = sum(
                        s.downloaded_bytes for s in cur.segments
                    )
                    speed = speed_tracker.get_speed_bps()
                    cur.speed_bps = speed
                    if cur.total_bytes > 0 and speed > 0:
                        rem = max(0, cur.total_bytes - cur.downloaded_bytes)
                        cur.eta_seconds = rem / speed
                    else:
                        cur.eta_seconds = None
                    if should_db:
                        self._db.upsert(cur)
                if should_emit:
                    self._emit("progress", cur)

        # Phase 2: Execute Segmented or Single-Stream transfer.
        with self._lock:
            item = self._items.get(download_id)
            if item is None or handle.stop_event.is_set():
                raise _DownloadStopped()
            use_segmented = (
                item.supports_range
                and item.num_segments > 1
                and item.total_bytes >= item.num_segments * 4096
            )

        if use_segmented:
            try:
                self._download_segmented(
                    download_id, target_url, handle, on_bytes_written
                )
            except _RangeNotSupportedError:
                if handle.stop_event.is_set():
                    raise _DownloadStopped()
                with self._lock:
                    item = self._items.get(download_id)
                    if item is not None:
                        item.supports_range = False
                        item.segments = []
                        item.downloaded_bytes = 0
                        self._db.upsert(item)
                self._download_single_stream(
                    download_id, target_url, handle, on_bytes_written
                )
        else:
            self._download_single_stream(
                download_id, target_url, handle, on_bytes_written
            )

        if handle.stop_event.is_set():
            raise _DownloadStopped()

        # Phase 3: Checksum Verification & Atomic Finalization.
        self._verify_and_finalize(download_id, handle)

    def _download_segmented(
        self,
        download_id: str,
        target_url: str,
        handle: _WorkerHandle,
        on_bytes_written: Callable[[int], None],
    ) -> None:
        with self._lock:
            item = self._items.get(download_id)
            if item is None or handle.stop_event.is_set():
                raise _DownloadStopped()
            total_bytes = item.total_bytes
            num_segments = item.num_segments
            part_path = item.part_filepath
            req_headers = self._build_headers(item.headers)

            # Reuse existing segment plan if .part exists with expected size.
            can_resume = (
                bool(item.segments)
                and part_path.is_file()
                and part_path.stat().st_size == total_bytes
                and sum(s.total_bytes for s in item.segments) == total_bytes
            )
            if not can_resume:
                base = total_bytes // num_segments
                rem = total_bytes % num_segments
                segments: list[SegmentInfo] = []
                offset = 0
                for idx in range(num_segments):
                    seg_len = base + (1 if idx < rem else 0)
                    segments.append(
                        SegmentInfo(
                            index=idx,
                            start_byte=offset,
                            end_byte=offset + seg_len - 1,
                            downloaded_bytes=0,
                            completed=False,
                        )
                    )
                    offset += seg_len
                item.segments = segments
                item.downloaded_bytes = 0
                with open(part_path, "wb") as f:
                    f.truncate(total_bytes)
                self._db.upsert(item)
            else:
                item.downloaded_bytes = sum(s.downloaded_bytes for s in item.segments)

            incomplete_segments = [s for s in item.segments if not s.completed]

        if not incomplete_segments:
            return

        # Local cancel event stops sibling segment threads on range fallback or fatal error.
        seg_abort_event = threading.Event()
        fd = os.open(str(part_path), os.O_RDWR | os.O_CREAT, 0o644)

        def is_stopped() -> bool:
            return handle.stop_event.is_set() or seg_abort_event.is_set()

        def wait_interruptible(seconds: float) -> bool:
            deadline = time.monotonic() + seconds
            while True:
                if is_stopped():
                    return True
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False
                handle.stop_event.wait(min(rem, 0.1))

        def segment_worker(seg: SegmentInfo) -> None:
            with self._lock:
                max_retries = self.config.max_retries
                base_delay = self.config.retry_delay_seconds

            attempt = 0
            while True:
                if is_stopped():
                    return
                current_pos = seg.start_byte + seg.downloaded_bytes
                if current_pos > seg.end_byte:
                    seg.completed = True
                    return

                seg_headers = dict(req_headers)
                seg_headers["Range"] = f"bytes={current_pos}-{seg.end_byte}"
                req = urllib.request.Request(
                    target_url, headers=seg_headers, method="GET"
                )
                try:
                    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
                        status = getattr(resp, "status", resp.getcode())
                        if status == 200:
                            seg_abort_event.set()
                            raise _RangeNotSupportedError(
                                "Server ignored Range header and returned 200"
                            )
                        if status != 206:
                            raise http.client.HTTPException(
                                f"Unexpected HTTP status {status} for range request"
                            )

                        while current_pos <= seg.end_byte:
                            if is_stopped():
                                return
                            with self._lock:
                                cur_item = self._items.get(download_id)
                                per_kbps = (
                                    cur_item.speed_limit_kbps if cur_item else 0
                                )
                            chunk_size = self._compute_chunk_size(per_kbps)
                            remaining_seg = seg.end_byte - current_pos + 1
                            to_read = min(chunk_size, remaining_seg)

                            chunk = resp.read(to_read)
                            if not chunk:
                                break
                            if len(chunk) > remaining_seg:
                                chunk = chunk[:remaining_seg]

                            if not handle.bucket.consume(len(chunk), handle.stop_event):
                                return
                            if not self._global_bucket.consume(
                                len(chunk), handle.stop_event
                            ):
                                return
                            if is_stopped():
                                return

                            _pwrite_all(fd, chunk, current_pos)
                            written = len(chunk)
                            current_pos += written
                            with self._lock:
                                seg.downloaded_bytes += written
                            on_bytes_written(written)

                    if is_stopped():
                        return
                    if seg.start_byte + seg.downloaded_bytes > seg.end_byte:
                        seg.completed = True
                        return
                    raise ConnectionError(
                        f"Premature EOF in segment {seg.index} "
                        f"({seg.downloaded_bytes}/{seg.total_bytes} bytes)"
                    )
                except _RangeNotSupportedError:
                    raise
                except Exception as exc:
                    if is_stopped():
                        return
                    if not _is_retryable_error(exc) or attempt >= max_retries:
                        seg_abort_event.set()
                        raise
                    delay = min(60.0, base_delay * (2**attempt))
                    attempt += 1
                    if delay > 0 and wait_interruptible(delay):
                        return

        range_fallback = False
        first_error: Optional[BaseException] = None
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(incomplete_segments),
                thread_name_prefix=f"fluxdm-seg-{download_id[:6]}",
            ) as executor:
                futures = [
                    executor.submit(segment_worker, seg) for seg in incomplete_segments
                ]
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        fut.result()
                    except _RangeNotSupportedError:
                        range_fallback = True
                        seg_abort_event.set()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                        seg_abort_event.set()
        finally:
            try:
                os.fsync(fd)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

            with self._lock:
                cur_item = self._items.get(download_id)
                if cur_item is not None and cur_item.segments:
                    cur_item.downloaded_bytes = sum(
                        s.downloaded_bytes for s in cur_item.segments
                    )
                    self._db.upsert(cur_item)

        if handle.stop_event.is_set():
            raise _DownloadStopped()
        if range_fallback:
            raise _RangeNotSupportedError()
        if first_error is not None:
            raise first_error

    def _download_single_stream(
        self,
        download_id: str,
        target_url: str,
        handle: _WorkerHandle,
        on_bytes_written: Callable[[int], None],
    ) -> None:
        with self._lock:
            item = self._items.get(download_id)
            if item is None or handle.stop_event.is_set():
                raise _DownloadStopped()
            part_path = item.part_filepath
            req_headers = self._build_headers(item.headers)
            max_retries = self.config.max_retries
            base_delay = self.config.retry_delay_seconds

            end_byte = item.total_bytes - 1 if item.total_bytes > 0 else -1
            if (
                item.supports_range
                and part_path.is_file()
                and part_path.stat().st_size > 0
                and (item.total_bytes <= 0 or part_path.stat().st_size <= item.total_bytes)
            ):
                existing = part_path.stat().st_size
            else:
                existing = 0

            seg = SegmentInfo(
                index=0,
                start_byte=0,
                end_byte=end_byte,
                downloaded_bytes=existing,
                completed=(item.total_bytes > 0 and existing >= item.total_bytes),
            )
            item.segments = [seg]
            item.downloaded_bytes = existing
            self._db.upsert(item)

        if seg.completed:
            return

        attempt = 0
        while True:
            if handle.stop_event.is_set():
                raise _DownloadStopped()

            with self._lock:
                cur_item = self._items.get(download_id)
                if cur_item is None:
                    raise _DownloadStopped()
                can_range = cur_item.supports_range and seg.downloaded_bytes > 0
                expected_total = cur_item.total_bytes

            stream_headers = dict(req_headers)
            if can_range:
                stream_headers["Range"] = f"bytes={seg.downloaded_bytes}-"

            req = urllib.request.Request(
                target_url, headers=stream_headers, method="GET"
            )
            try:
                with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    if can_range and status == 206:
                        mode = "r+b"
                        seek_pos = seg.downloaded_bytes
                    elif status in (200, 206):
                        mode = "wb"
                        seek_pos = 0
                        with self._lock:
                            seg.downloaded_bytes = 0
                            if cur_item is not None:
                                cur_item.downloaded_bytes = 0
                                cl_raw = resp.headers.get("Content-Length")
                                if cl_raw and cur_item.total_bytes <= 0:
                                    try:
                                        parsed_cl = int(cl_raw.strip())
                                        if parsed_cl >= 0:
                                            cur_item.total_bytes = parsed_cl
                                            expected_total = parsed_cl
                                            seg.end_byte = (
                                                parsed_cl - 1 if parsed_cl > 0 else -1
                                            )
                                    except ValueError:
                                        pass
                    else:
                        raise http.client.HTTPException(
                            f"Unexpected HTTP status {status}"
                        )

                    with open(part_path, mode) as out_file:
                        if seek_pos > 0:
                            out_file.seek(seek_pos)
                        while True:
                            if handle.stop_event.is_set():
                                out_file.flush()
                                os.fsync(out_file.fileno())
                                raise _DownloadStopped()

                            with self._lock:
                                cur_item = self._items.get(download_id)
                                per_kbps = (
                                    cur_item.speed_limit_kbps if cur_item else 0
                                )
                            chunk_size = self._compute_chunk_size(per_kbps)
                            chunk = resp.read(chunk_size)
                            if not chunk:
                                break

                            if not handle.bucket.consume(len(chunk), handle.stop_event):
                                out_file.flush()
                                os.fsync(out_file.fileno())
                                raise _DownloadStopped()
                            if not self._global_bucket.consume(
                                len(chunk), handle.stop_event
                            ):
                                out_file.flush()
                                os.fsync(out_file.fileno())
                                raise _DownloadStopped()

                            out_file.write(chunk)
                            written = len(chunk)
                            with self._lock:
                                seg.downloaded_bytes += written
                            on_bytes_written(written)

                        out_file.flush()
                        os.fsync(out_file.fileno())

                if handle.stop_event.is_set():
                    raise _DownloadStopped()

                if expected_total > 0 and seg.downloaded_bytes < expected_total:
                    raise ConnectionError(
                        f"Premature EOF ({seg.downloaded_bytes}/{expected_total} bytes)"
                    )

                with self._lock:
                    cur_item = self._items.get(download_id)
                    seg.completed = True
                    if cur_item is not None:
                        if cur_item.total_bytes <= 0:
                            cur_item.total_bytes = seg.downloaded_bytes
                            seg.end_byte = (
                                seg.downloaded_bytes - 1
                                if seg.downloaded_bytes > 0
                                else -1
                            )
                        cur_item.downloaded_bytes = seg.downloaded_bytes
                        self._db.upsert(cur_item)
                return

            except _DownloadStopped:
                with self._lock:
                    cur_item = self._items.get(download_id)
                    if cur_item is not None:
                        cur_item.downloaded_bytes = seg.downloaded_bytes
                        self._db.upsert(cur_item)
                raise
            except Exception as exc:
                if handle.stop_event.is_set():
                    raise _DownloadStopped() from exc
                if not _is_retryable_error(exc) or attempt >= max_retries:
                    raise
                delay = min(60.0, base_delay * (2**attempt))
                attempt += 1
                if delay > 0 and handle.stop_event.wait(delay):
                    raise _DownloadStopped()

    def _verify_and_finalize(
        self, download_id: str, handle: _WorkerHandle
    ) -> None:
        with self._lock:
            item = self._items.get(download_id)
            if item is None or handle.stop_event.is_set():
                raise _DownloadStopped()
            part_path = item.part_filepath
            expected_raw = item.expected_checksum.strip()

        # Determine whether MD5 is requested alongside SHA-256.
        expected_algo = ""
        expected_hex = ""
        need_md5 = False
        if expected_raw:
            lowered = expected_raw.lower()
            if lowered.startswith("md5:"):
                expected_algo = "md5"
                expected_hex = lowered[4:].strip()
                need_md5 = True
            elif lowered.startswith("sha256:"):
                expected_algo = "sha256"
                expected_hex = lowered[7:].strip()
            elif len(lowered) == 32 and _HEX_RE.match(lowered):
                expected_algo = "md5"
                expected_hex = lowered
                need_md5 = True
            else:
                expected_algo = "sha256"
                expected_hex = lowered

        sha256_hasher = hashlib.sha256()
        md5_hasher = hashlib.md5() if need_md5 else None
        with open(part_path, "rb") as f:
            while True:
                if handle.stop_event.is_set():
                    raise _DownloadStopped()
                buf = f.read(131072)
                if not buf:
                    break
                sha256_hasher.update(buf)
                if md5_hasher is not None:
                    md5_hasher.update(buf)

        computed_sha256 = sha256_hasher.hexdigest()
        computed_md5 = md5_hasher.hexdigest() if md5_hasher is not None else ""

        checksum_verified: Optional[bool] = None
        if expected_raw:
            actual_hex = computed_md5 if expected_algo == "md5" else computed_sha256
            if actual_hex.lower() != expected_hex.lower():
                with self._lock:
                    cur = self._items.get(download_id)
                    if cur is None or handle.stop_event.is_set():
                        raise _DownloadStopped()
                    cur.computed_sha256 = computed_sha256
                    cur.checksum_verified = False
                    cur.status = DownloadStatus.ERROR
                    cur.speed_bps = 0.0
                    cur.eta_seconds = None
                    cur.error_message = (
                        f"Checksum mismatch ({expected_algo}): "
                        f"expected {expected_hex}, got {actual_hex}"
                    )
                    self._db.upsert(cur)
                    failed_item = cur
                self._emit("error", failed_item)
                self._notifier.notify_error(failed_item)
                return
            checksum_verified = True

        # Atomically rename .fluxdm.part -> final filepath under lock to prevent clobbering.
        with self._lock:
            cur = self._items.get(download_id)
            if (
                cur is None
                or handle.stop_event.is_set()
                or cur.status != DownloadStatus.DOWNLOADING
            ):
                raise _DownloadStopped()

            dest_dir = Path(cur.dest_dir)
            final_name = cur.filename
            if (dest_dir / final_name).exists():
                final_name = _deduplicate_filename(dest_dir, final_name)
                cur.filename = final_name
            final_path = dest_dir / final_name

            os.replace(part_path, final_path)

            final_size = final_path.stat().st_size
            cur.total_bytes = final_size
            cur.downloaded_bytes = final_size
            cur.computed_sha256 = computed_sha256
            cur.checksum_verified = checksum_verified
            cur.status = DownloadStatus.COMPLETED
            cur.speed_bps = 0.0
            cur.eta_seconds = None
            cur.error_message = ""
            cur.completed_at = time.time()
            self._db.upsert(cur)
            completed_item = cur

        self._emit("completed", completed_item)
        self._notifier.notify_completed(completed_item)
