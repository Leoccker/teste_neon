"""Desktop notification support for FluxDM (libnotify via GI with notify-send fallback)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Any, Optional

from . import APP_NAME
from .models import DownloadItem, format_bytes


class Notifier:
    """Non-blocking, failure-tolerant Linux desktop notifier."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled: bool = bool(enabled)
        self._lock = threading.Lock()
        self._gi_notify: Any = None
        self._gi_state: Optional[bool] = None  # None=uninitialized, True=ready, False=unavailable

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def _is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def notify_completed(self, item: DownloadItem) -> None:
        if not self._is_enabled():
            return
        size_str = format_bytes(item.total_bytes or item.downloaded_bytes)
        title = f"{APP_NAME} — Download Complete"
        body = f"{item.filename} ({size_str})"
        self._dispatch(title, body, icon="document-save", urgency="normal")

    def notify_error(self, item: DownloadItem) -> None:
        if not self._is_enabled():
            return
        reason = item.error_message or "Download failed"
        title = f"{APP_NAME} — Download Failed"
        body = f"{item.filename}: {reason}"
        self._dispatch(title, body, icon="dialog-error", urgency="critical")

    def _dispatch(self, title: str, body: str, icon: str, urgency: str) -> None:
        # Skip spawning threads in clearly headless environments with no desktop session bus.
        if not any(
            os.environ.get(var)
            for var in ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY")
        ):
            return
        thread = threading.Thread(
            target=self._send_safe,
            args=(title, body, icon, urgency),
            name="fluxdm-notify",
            daemon=True,
        )
        thread.start()

    def _try_gi_notify(self, title: str, body: str, icon: str, urgency: str) -> bool:
        with self._lock:
            if self._gi_state is False:
                return False
            if self._gi_state is None:
                try:
                    import gi  # type: ignore[import-not-found]

                    gi.require_version("Notify", "0.7")
                    from gi.repository import Notify  # type: ignore[import-not-found]

                    if not Notify.is_initted():
                        if not Notify.init(APP_NAME):
                            self._gi_state = False
                            return False
                    self._gi_notify = Notify
                    self._gi_state = True
                except Exception:
                    self._gi_state = False
                    return False
            notify_mod = self._gi_notify

        try:
            notification = notify_mod.Notification.new(title, body, icon)
            if urgency == "critical" and hasattr(notify_mod, "Urgency"):
                notification.set_urgency(notify_mod.Urgency.CRITICAL)
            notification.show()
            return True
        except Exception:
            return False

    def _send_safe(self, title: str, body: str, icon: str, urgency: str) -> None:
        try:
            if self._try_gi_notify(title, body, icon, urgency):
                return
            notify_send = shutil.which("notify-send")
            if not notify_send:
                return
            subprocess.run(
                [
                    notify_send,
                    "--app-name",
                    APP_NAME,
                    "--icon",
                    icon,
                    "--urgency",
                    urgency,
                    title,
                    body,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
        except Exception:
            # Notifications must never disrupt downloads or raise in headless environments.
            pass
