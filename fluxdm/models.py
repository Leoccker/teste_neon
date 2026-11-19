"""Data models and formatting helpers for FluxDM."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class DownloadStatus(str, Enum):
    """Lifecycle states of a download item."""

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class SegmentInfo:
    """Tracks byte range and progress for a single download segment."""

    index: int
    start_byte: int
    end_byte: int  # Inclusive; -1 if total length is unknown
    downloaded_bytes: int = 0
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "start_byte": int(self.start_byte),
            "end_byte": int(self.end_byte),
            "downloaded_bytes": int(self.downloaded_bytes),
            "completed": bool(self.completed),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SegmentInfo:
        return cls(
            index=int(data["index"]),
            start_byte=int(data["start_byte"]),
            end_byte=int(data["end_byte"]),
            downloaded_bytes=int(data.get("downloaded_bytes", 0)),
            completed=bool(data.get("completed", False)),
        )

    @property
    def total_bytes(self) -> int:
        if self.end_byte < 0 or self.end_byte < self.start_byte:
            return 0
        return self.end_byte - self.start_byte + 1

    @property
    def progress(self) -> float:
        if self.completed:
            return 1.0
        total = self.total_bytes
        if total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.downloaded_bytes / total))


@dataclass
class DownloadItem:
    """Represents a managed download task and its persisted state."""

    id: str
    url: str
    filename: str
    dest_dir: str
    status: DownloadStatus = DownloadStatus.QUEUED
    total_bytes: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: Optional[float] = None
    num_segments: int = 4
    supports_range: bool = False
    segments: list[SegmentInfo] = field(default_factory=list)
    speed_limit_kbps: int = 0
    expected_checksum: str = ""
    computed_sha256: str = ""
    checksum_verified: Optional[bool] = None
    headers: dict[str, str] = field(default_factory=dict)
    etag: str = ""
    content_type: str = ""
    error_message: str = ""
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    @property
    def filepath(self) -> Path:
        return Path(self.dest_dir) / self.filename

    @property
    def part_filepath(self) -> Path:
        return Path(self.dest_dir) / f"{self.filename}.fluxdm.part"

    @property
    def progress(self) -> float:
        if self.status == DownloadStatus.COMPLETED:
            return 1.0
        if self.total_bytes <= 0:
            return 0.0
        return min(1.0, max(0.0, self.downloaded_bytes / self.total_bytes))


@dataclass
class ProbeResult:
    """Metadata discovered by probing a remote URL."""

    url: str
    filename: str
    total_bytes: int
    supports_range: bool
    etag: str = ""
    content_type: str = ""


@dataclass
class AppConfig:
    """Persistent application configuration."""

    download_dir: str = ""
    max_concurrent_downloads: int = 3
    default_segments: int = 4
    global_speed_limit_kbps: int = 0
    enable_notifications: bool = True
    max_retries: int = 3
    retry_delay_seconds: float = 2.0
    user_agent: str = "FluxDM/1.0 (Linux)"
    auto_start_downloads: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_dir": str(self.download_dir),
            "max_concurrent_downloads": int(self.max_concurrent_downloads),
            "default_segments": int(self.default_segments),
            "global_speed_limit_kbps": int(self.global_speed_limit_kbps),
            "enable_notifications": bool(self.enable_notifications),
            "max_retries": int(self.max_retries),
            "retry_delay_seconds": float(self.retry_delay_seconds),
            "user_agent": str(self.user_agent),
            "auto_start_downloads": bool(self.auto_start_downloads),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        return cls(
            download_dir=str(data.get("download_dir", defaults.download_dir) or ""),
            max_concurrent_downloads=max(
                1,
                int(
                    data.get(
                        "max_concurrent_downloads", defaults.max_concurrent_downloads
                    )
                ),
            ),
            default_segments=max(
                1,
                min(
                    32,
                    int(data.get("default_segments", defaults.default_segments)),
                ),
            ),
            global_speed_limit_kbps=max(
                0,
                int(
                    data.get(
                        "global_speed_limit_kbps", defaults.global_speed_limit_kbps
                    )
                ),
            ),
            enable_notifications=bool(
                data.get("enable_notifications", defaults.enable_notifications)
            ),
            max_retries=max(0, int(data.get("max_retries", defaults.max_retries))),
            retry_delay_seconds=max(
                0.0,
                float(
                    data.get("retry_delay_seconds", defaults.retry_delay_seconds)
                ),
            ),
            user_agent=str(data.get("user_agent", defaults.user_agent) or defaults.user_agent),
            auto_start_downloads=bool(
                data.get("auto_start_downloads", defaults.auto_start_downloads)
            ),
        )


def format_bytes(num_bytes: float) -> str:
    """Format a byte count into a human-readable string (e.g. '1.5 MB')."""
    if num_bytes is None or math.isnan(num_bytes) or math.isinf(num_bytes) or num_bytes <= 0:
        return "0 B"

    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(num_bytes)
    unit_idx = 0
    while value >= 1024.0 and unit_idx < len(units) - 1:
        value /= 1024.0
        unit_idx += 1

    if unit_idx == 0:
        return f"{int(round(value))} B"
    if unit_idx >= 3:
        return f"{value:.2f} {units[unit_idx]}"
    return f"{value:.1f} {units[unit_idx]}"


def format_speed(bps: float) -> str:
    """Format transfer speed in bytes/sec into a human-readable string."""
    if bps is None or math.isnan(bps) or math.isinf(bps) or bps <= 0:
        return "0 B/s"
    return f"{format_bytes(bps)}/s"


def format_eta(seconds: Optional[float]) -> str:
    """Format an estimated time remaining in seconds into a compact string."""
    if seconds is None or math.isnan(seconds) or math.isinf(seconds) or seconds < 0:
        return "--"

    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs}s"
    if total < 86400:
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}h {minutes}m"
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days}d {hours}h"


_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
    """Return a safe single-component filename stripped of path traversal and invalid chars."""
    if not name or not isinstance(name, str):
        return "download"

    # Extract only the final path component across POSIX and Windows separators.
    normalized = name.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    candidate = parts[-1] if parts else ""

    # Replace invalid / control characters and collapse whitespace.
    candidate = _INVALID_FILENAME_CHARS_RE.sub("_", candidate)
    candidate = _WHITESPACE_RE.sub(" ", candidate).strip()

    # Strip leading dots/spaces to prevent hidden files or '.'/'..' traversal.
    candidate = candidate.lstrip(". ").rstrip(" ")
    if not candidate or candidate in (".", ".."):
        return "download"

    # Enforce filesystem-safe byte length (keep room for '.fluxdm.part' suffix).
    max_bytes = 220
    encoded = candidate.encode("utf-8")
    if len(encoded) > max_bytes:
        stem, dot, ext = candidate.rpartition(".")
        if dot and stem and len(ext.encode("utf-8")) <= 32:
            ext_part = f".{ext}"
            budget = max_bytes - len(ext_part.encode("utf-8"))
            truncated_stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")
            candidate = f"{truncated_stem or 'download'}{ext_part}"
        else:
            candidate = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .") or "download"

    return candidate
