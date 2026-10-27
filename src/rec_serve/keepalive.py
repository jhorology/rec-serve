"""macOS sleep prevention (KeepAlive) manager using IOKit power assertions.

Maintains system sleep suppression while active (display sleep is allowed).
Refreshes sleep suppression on each incoming API request and automatically
releases it after an idle timeout (default: 1 hour) without polling overhead.
"""

from __future__ import annotations

import asyncio
import ctypes
import platform
import time
from typing import Any

from rich.console import Console

console = Console(soft_wrap=True)


class KeepAliveManager:
    """Manages macOS power assertions to prevent system sleep with idle timeout."""

    _cf_str_fn: Any
    _cf_release_fn: Any
    _create_fn: Any
    _release_fn: Any

    def __init__(self, timeout_sec: float = 3600.0, enabled: bool = True):
        self.timeout_sec: float = float(timeout_sec)
        self.enabled: bool = enabled and (platform.system() == "Darwin")
        self.last_activity: float = time.monotonic()
        self._assertion_ids: list[int] = []
        self._iokit: ctypes.CDLL | None = None
        self._cf: ctypes.CDLL | None = None
        self._cf_str_fn = None
        self._create_fn = None
        self._release_fn = None

        if self.enabled:
            self._init_iokit()

    def _init_iokit(self) -> None:
        try:
            self._iokit = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/IOKit.framework/IOKit"
            )
            self._cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )

            self._cf_str_fn = self._cf.CFStringCreateWithCString
            self._cf_str_fn.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            self._cf_str_fn.restype = ctypes.c_void_p

            self._create_fn = self._iokit.IOPMAssertionCreateWithName
            self._create_fn.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            self._create_fn.restype = ctypes.c_int

            self._release_fn = self._iokit.IOPMAssertionRelease
            self._release_fn.argtypes = [ctypes.c_uint32]
            self._release_fn.restype = ctypes.c_int

            self._cf_release_fn = self._cf.CFRelease
            self._cf_release_fn.argtypes = [ctypes.c_void_p]
            self._cf_release_fn.restype = None
        except Exception as e:
            console.print(
                f"[dim yellow]⚠ IOKit power assertion 初期化失敗 (スリープ抑制無効): {e}[/dim yellow]"
            )
            self.enabled = False

    def _cf_str(self, string: str):
        if not self._cf_str_fn:
            return None
        return self._cf_str_fn(None, string.encode("utf-8"), 0x08000100)

    def _cf_release(self, ref: Any) -> None:
        """CFString を解放 (Create Rule: 呼び出し側が所有権を持つ)。"""
        if ref is not None and self._cf_release_fn is not None:
            self._cf_release_fn(ref)

    @property
    def is_active(self) -> bool:
        return bool(self._assertion_ids)

    @property
    def remaining_sec(self) -> float:
        return max(0.0, self.timeout_sec - (time.monotonic() - self.last_activity))

    def touch(self) -> None:
        """API リクエスト受信時に呼び出し、最終アクティビティ時刻を更新。"""
        self.last_activity = time.monotonic()
        if not self.is_active and self.enabled:
            self.acquire()

    def acquire(self) -> bool:
        """システムスリープ抑制アサーションを取得 (ディスプレイのオフは許容)。"""
        if not self.enabled or self.is_active or not self._create_fn:
            return False

        # PreventUserIdleSystemSleep: システムのスリープのみ抑制 (画面オフは許可)
        assertion_types = [
            "PreventUserIdleSystemSleep",
        ]
        kIOPMAssertionLevelOn = 255
        name_ref = self._cf_str("rec-serve API keepalive")

        success_ids = []
        try:
            for atype in assertion_types:
                type_ref = self._cf_str(atype)
                aid = ctypes.c_uint32(0)
                res = self._create_fn(
                    type_ref, kIOPMAssertionLevelOn, name_ref, ctypes.byref(aid)
                )
                self._cf_release(type_ref)
                if res == 0:
                    success_ids.append(aid.value)
        finally:
            self._cf_release(name_ref)

        self._assertion_ids = success_ids
        if success_ids:
            hours = int(self.timeout_sec // 3600)
            mins = int((self.timeout_sec % 3600) // 60)
            dur_str = (
                f"{hours}時間" if hours and not mins else f"{int(self.timeout_sec)}秒"
            )
            console.print(
                f"[green]⚡ システムスリープ抑制を有効化しました "
                f"(最終アクセスから {dur_str} 維持、画面オフは許容)[/green]"
            )
            return True
        return False

    def release(self) -> bool:
        """システムスリープ抑制アサーションを解除。"""
        if not self._assertion_ids or not self._release_fn:
            return False

        for aid in self._assertion_ids:
            try:
                self._release_fn(aid)
            except Exception:
                pass
        self._assertion_ids = []
        console.print(
            "[yellow]💤 アイドルタイムアウト: スリープ抑制を解除しました "
            "(macOS 通常スリープ復帰)[/yellow]"
        )
        return True

    def status(self) -> dict[str, Any]:
        """状態の辞書表現を返す (health エンドポイント向け)。"""
        return {
            "enabled": self.enabled,
            "active": self.is_active,
            "timeout_sec": int(self.timeout_sec),
            "remaining_sec": round(self.remaining_sec, 1),
            "idle_sec": round(time.monotonic() - self.last_activity, 1),
        }

    async def monitor_loop(self, check_interval: float = 30.0) -> None:
        """定期監視ループ (バックグラウンドで非同期実行)。"""
        while True:
            try:
                if self.is_active and (
                    time.monotonic() - self.last_activity >= self.timeout_sec
                ):
                    self.release()
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                console.print(f"[dim red]KeepAlive monitor エラー: {e}[/dim red]")
                await asyncio.sleep(check_interval)
