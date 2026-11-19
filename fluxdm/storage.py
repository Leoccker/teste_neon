"""Persistent configuration and SQLite download history storage for FluxDM."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .models import AppConfig, DownloadItem, DownloadStatus, SegmentInfo


def get_config_dir() -> Path:
    """Return the XDG config directory for FluxDM."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        return Path(xdg_config).expanduser() / "fluxdm"
    return Path.home() / ".config" / "fluxdm"


def get_data_dir() -> Path:
    """Return the XDG data directory for FluxDM."""
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data).expanduser() / "fluxdm"
    return Path.home() / ".local" / "share" / "fluxdm"


_USER_DIR_DOWNLOAD_RE = re.compile(
    r'^\s*XDG_DOWNLOAD_DIR\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s#]+))'
)


def get_default_download_dir() -> Path:
    """Resolve the user's XDG Downloads directory safely, falling back to ~/Downloads."""
    home = Path.home()
    fallback = home / "Downloads"

    candidates: list[Path] = []
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        candidates.append(Path(xdg_config).expanduser() / "user-dirs.dirs")
    default_user_dirs = home / ".config" / "user-dirs.dirs"
    if default_user_dirs not in candidates:
        candidates.append(default_user_dirs)

    for user_dirs_file in candidates:
        try:
            if not user_dirs_file.is_file():
                continue
            content = user_dirs_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _USER_DIR_DOWNLOAD_RE.match(stripped)
            if not match:
                continue
            raw_value = next((g for g in match.groups() if g is not None), "").strip()
            if not raw_value:
                continue
            # Safely expand $HOME / ${HOME} without shell evaluation.
            home_str = str(home)
            if raw_value == "$HOME" or raw_value == "${HOME}":
                resolved = home
            elif raw_value.startswith("$HOME/"):
                resolved = Path(home_str + raw_value[len("$HOME"):])
            elif raw_value.startswith("${HOME}/"):
                resolved = Path(home_str + raw_value[len("${HOME}"):])
            elif raw_value.startswith("/"):
                resolved = Path(raw_value)
            else:
                resolved = home / raw_value
            return resolved

    return fallback


class ConfigStore:
    """Thread-safe JSON persistence for AppConfig."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path: Path = Path(path) if path is not None else get_config_dir() / "config.json"
        self._lock = threading.RLock()

    def load(self) -> AppConfig:
        with self._lock:
            config = AppConfig()
            if self.path.is_file():
                try:
                    raw = self.path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    config = AppConfig.from_dict(data)
                except (OSError, ValueError, TypeError):
                    config = AppConfig()

            if not config.download_dir:
                config.download_dir = str(get_default_download_dir())
            return config

    def save(self, config: AppConfig) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                    tmp_file.write(payload)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    dest_dir TEXT NOT NULL,
    status TEXT NOT NULL,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    speed_bps REAL NOT NULL DEFAULT 0.0,
    eta_seconds REAL,
    num_segments INTEGER NOT NULL DEFAULT 4,
    supports_range INTEGER NOT NULL DEFAULT 0,
    segments_json TEXT NOT NULL DEFAULT '[]',
    speed_limit_kbps INTEGER NOT NULL DEFAULT 0,
    expected_checksum TEXT NOT NULL DEFAULT '',
    computed_sha256 TEXT NOT NULL DEFAULT '',
    checksum_verified INTEGER,
    headers_json TEXT NOT NULL DEFAULT '{}',
    etag TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_downloads_created_at ON downloads (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads (status);
"""


class DownloadDatabase:
    """Thread-safe SQLite store (WAL mode) for DownloadItem records."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path: Path = (
            Path(db_path) if db_path is not None else get_data_dir() / "downloads.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.commit()

    def _row_to_item(self, row: sqlite3.Row) -> DownloadItem:
        try:
            raw_segments = json.loads(row["segments_json"] or "[]")
            segments = [
                SegmentInfo.from_dict(entry)
                for entry in raw_segments
                if isinstance(entry, dict)
            ]
        except (ValueError, KeyError, TypeError):
            segments = []

        try:
            raw_headers = json.loads(row["headers_json"] or "{}")
            headers = (
                {str(k): str(v) for k, v in raw_headers.items()}
                if isinstance(raw_headers, dict)
                else {}
            )
        except (ValueError, TypeError):
            headers = {}

        raw_verified = row["checksum_verified"]
        checksum_verified: Optional[bool]
        if raw_verified is None:
            checksum_verified = None
        else:
            checksum_verified = bool(raw_verified)

        try:
            status = DownloadStatus(row["status"])
        except ValueError:
            status = DownloadStatus.ERROR

        return DownloadItem(
            id=str(row["id"]),
            url=str(row["url"]),
            filename=str(row["filename"]),
            dest_dir=str(row["dest_dir"]),
            status=status,
            total_bytes=int(row["total_bytes"]),
            downloaded_bytes=int(row["downloaded_bytes"]),
            speed_bps=float(row["speed_bps"]),
            eta_seconds=(
                float(row["eta_seconds"]) if row["eta_seconds"] is not None else None
            ),
            num_segments=int(row["num_segments"]),
            supports_range=bool(row["supports_range"]),
            segments=segments,
            speed_limit_kbps=int(row["speed_limit_kbps"]),
            expected_checksum=str(row["expected_checksum"] or ""),
            computed_sha256=str(row["computed_sha256"] or ""),
            checksum_verified=checksum_verified,
            headers=headers,
            etag=str(row["etag"] or ""),
            content_type=str(row["content_type"] or ""),
            error_message=str(row["error_message"] or ""),
            priority=int(row["priority"]),
            created_at=float(row["created_at"]),
            completed_at=(
                float(row["completed_at"]) if row["completed_at"] is not None else None
            ),
        )

    def upsert(self, item: DownloadItem) -> None:
        with self._lock:
            if self._closed:
                return
            segments_json = json.dumps([seg.to_dict() for seg in item.segments])
            headers_json = json.dumps(item.headers or {})
            checksum_val: Optional[int]
            if item.checksum_verified is None:
                checksum_val = None
            else:
                checksum_val = 1 if item.checksum_verified else 0

            self._conn.execute(
                """
                INSERT INTO downloads (
                    id, url, filename, dest_dir, status,
                    total_bytes, downloaded_bytes, speed_bps, eta_seconds,
                    num_segments, supports_range, segments_json,
                    speed_limit_kbps, expected_checksum, computed_sha256,
                    checksum_verified, headers_json, etag, content_type,
                    error_message, priority, created_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    filename = excluded.filename,
                    dest_dir = excluded.dest_dir,
                    status = excluded.status,
                    total_bytes = excluded.total_bytes,
                    downloaded_bytes = excluded.downloaded_bytes,
                    speed_bps = excluded.speed_bps,
                    eta_seconds = excluded.eta_seconds,
                    num_segments = excluded.num_segments,
                    supports_range = excluded.supports_range,
                    segments_json = excluded.segments_json,
                    speed_limit_kbps = excluded.speed_limit_kbps,
                    expected_checksum = excluded.expected_checksum,
                    computed_sha256 = excluded.computed_sha256,
                    checksum_verified = excluded.checksum_verified,
                    headers_json = excluded.headers_json,
                    etag = excluded.etag,
                    content_type = excluded.content_type,
                    error_message = excluded.error_message,
                    priority = excluded.priority,
                    created_at = excluded.created_at,
                    completed_at = excluded.completed_at
                """,
                (
                    item.id,
                    item.url,
                    item.filename,
                    item.dest_dir,
                    item.status.value,
                    int(item.total_bytes),
                    int(item.downloaded_bytes),
                    float(item.speed_bps),
                    item.eta_seconds,
                    int(item.num_segments),
                    1 if item.supports_range else 0,
                    segments_json,
                    int(item.speed_limit_kbps),
                    item.expected_checksum,
                    item.computed_sha256,
                    checksum_val,
                    headers_json,
                    item.etag,
                    item.content_type,
                    item.error_message,
                    int(item.priority),
                    float(item.created_at),
                    item.completed_at,
                ),
            )
            self._conn.commit()

    def get(self, download_id: str) -> Optional[DownloadItem]:
        with self._lock:
            if self._closed:
                return None
            cur = self._conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            )
            row = cur.fetchone()
            return self._row_to_item(row) if row is not None else None

    def list_all(self) -> list[DownloadItem]:
        with self._lock:
            if self._closed:
                return []
            cur = self._conn.execute(
                "SELECT * FROM downloads ORDER BY created_at DESC"
            )
            return [self._row_to_item(row) for row in cur.fetchall()]

    def delete(self, download_id: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            cur = self._conn.execute(
                "DELETE FROM downloads WHERE id = ?", (download_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def clear_completed(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            cur = self._conn.execute(
                "DELETE FROM downloads WHERE status = ?",
                (DownloadStatus.COMPLETED.value,),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
