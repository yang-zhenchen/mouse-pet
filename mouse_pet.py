#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鼠标桌宠 MousePet
=================

一个只有标准库依赖的 Windows 桌面小挂件：

* 通过全局低级鼠标钩子（WH_MOUSE_LL）记录今天鼠标移动的累计距离
* 统计左键 / 右键点击次数，以及滚轮滚过的格数
* 无边框、置顶、半透明的小窗口，住着一只盯着鼠标看的小动物
* 可切换形象：大肥鱼（默认）/ 小猫 / 柴犬 / 企鹅 / 青蛙
* 数据按天保存在脚本旁边的 data/stats.json（纯本地，不联网）

交互：
    拖动        按住窗口拖动
    单击        在「完整」和「迷你」形态之间切换
    右键        菜单（形象、透明度、置顶、迷你、最近 7 天、重置、开机自启、退出）

用 python.exe 启动时会自动换成 pythonw 在后台运行，不留控制台黑框。

用法：
    pythonw mouse_pet.py
    python mouse_pet.py      # 也会自动转后台
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox

APP_NAME = "MousePet"
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:  # PyInstaller 打包后，数据要放在 exe 旁边
    BASE_DIR = Path(sys.executable).resolve().parent
    APP_PATH = Path(sys.executable).resolve()
else:
    BASE_DIR = Path(__file__).resolve().parent
    APP_PATH = Path(__file__).resolve()
__version__ = "2.1"


def _assets_dir() -> Path:
    local = BASE_DIR / "assets"
    if local.is_dir():
        return local
    bundle = getattr(sys, "_MEIPASS", None)   # onefile 解包目录
    if bundle and (Path(bundle) / "assets").is_dir():
        return Path(bundle) / "assets"
    return local


# --------------------------------------------------------------------------- #
# 路径 / 日志
# --------------------------------------------------------------------------- #
def _pick_data_dir() -> Path:
    """优先把数据放在脚本目录下的 data/，不可写时退回到 %APPDATA%。"""
    candidate = BASE_DIR / "data"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        fallback = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _pick_data_dir()
STATS_FILE = DATA_DIR / "stats.json"
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "mousepet.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# Win32
# --------------------------------------------------------------------------- #
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
MAX_JUMP = 3200.0  # 单次跳变超过这个像素数（分辨率切换/唤醒）就不计入

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    """系统范围内距上次键鼠输入过了多少秒（用于「困了」的表情）。"""
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            tick = ctypes.windll.kernel32.GetTickCount()
            return max(0.0, (tick - info.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def enable_dpi_awareness() -> None:
    """让 Tk 使用物理像素，窗口在高分屏上不糊。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def system_dpi() -> int:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if 48 <= dpi <= 480:
            return dpi
    except Exception:
        pass
    try:
        return int(ctypes.windll.user32.GetDeviceCaps(ctypes.windll.user32.GetDC(0), 88))
    except Exception:
        return 96


def is_elevated() -> bool:
    """当前进程是否以管理员身份运行（决定了能不能看到管理员窗口里的鼠标）。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def acquire_single_instance(quiet: bool = False):
    """同名互斥体，防止开出一群桌宠。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\MousePet_SingleInstance_v1")
    if not handle:
        return False
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if not quiet:
            ctypes.windll.user32.MessageBoxW(0, "鼠标桌宠已经在运行啦～", APP_NAME, 0x40)
        return None
    return handle


def relaunch_without_console() -> bool:
    """用 python.exe 启动时，自动换成 pythonw 在后台重启，避免留下黑框。"""
    if FROZEN:  # 打包后的 exe 是 --windowed 的，没有控制台
        return False
    if os.environ.get("MOUSEPET_NO_CONSOLE_RELAUNCH") == "1":
        return False
    if "pythonw" in Path(sys.executable).name.lower():
        return False
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.exists():
        return False
    try:
        env = os.environ.copy()
        env["MOUSEPET_NO_CONSOLE_RELAUNCH"] = "1"
        subprocess.Popen(
            [str(pyw), str(APP_PATH), *sys.argv[1:]],
            creationflags=0x00000008,  # DETACHED_PROCESS
            close_fds=True,
            cwd=str(BASE_DIR),
            env=env,
        )
        return True
    except OSError:
        log.exception("failed to relaunch with pythonw")
        return False


# --------------------------------------------------------------------------- #
# 统计核心：全局鼠标钩子
# --------------------------------------------------------------------------- #
class Tracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.day = dt.date.today().isoformat()
        self.dist = 0.0
        self.left = 0
        self.right = 0
        self.wheel = 0            # 滚动量，120 = 一格
        self.keys = {}            # 每个虚拟键码按下的次数
        self._pressed = set()     # 当前按住的键（用来忽略长按重复）
        self.combos = {}          # Ctrl+C / Alt+Tab 这类组合键
        self.hour_counts = [0] * 24
        self.kpm_peak = 0
        self.typing_seconds = 0.0
        self.backspace = 0
        self.session_start = None
        self.session_max = 0.0
        self._key_times = deque()
        self._mods = {"ctrl": False, "alt": False, "shift": False, "win": False}
        self._last_key_ts = 0.0
        self._bs_times = deque()
        self._click_times = deque()
        self._wheel_times = deque()
        self._repeat_vk = 0
        self._repeat_count = 0
        self._repeat_start = 0.0
        self._repeat_ts = 0.0
        self._last_combo = ""
        self._last_combo_ts = 0.0
        self._last = None
        self.recent = 0.0          # 给动画用的“最近移动量”
        self.last_click_ts = 0.0
        self.error = None
        self._hook = None
        self._key_hook = None
        self._tid = None
        self._thread = None
        self._proc = None
        self._proc_key = None

    # -- 钩子线程 ---------------------------------------------------------- #
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="MouseHook", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]

        self._user32 = user32
        self._tid = kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._on_mouse)  # 必须留着引用，不然会被 GC

        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        hmod = kernel32.GetModuleHandleW(None)
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0)
        if not hook:  # 低阶钩子允许 hMod 为 NULL，兜底再试一次
            hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        if not hook:
            self.error = ctypes.get_last_error() or -1
            log.error("SetWindowsHookExW failed: %s", self.error)
            return
        self._hook = hook
        log.info("mouse hook installed")

        # 键盘钩子；装不上不影响鼠标统计
        self._proc_key = HOOKPROC(self._on_key)
        key_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_key, hmod, 0)
        if not key_hook:
            key_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc_key, None, 0)
        if key_hook:
            self._key_hook = key_hook
            log.info("keyboard hook installed")
        else:
            log.error("keyboard hook failed: %s", ctypes.get_last_error())

        msg = wt.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._key_hook:
            user32.UnhookWindowsHookEx(self._key_hook)
        user32.UnhookWindowsHookEx(hook)
        log.info("hooks stopped")

    def stop(self) -> None:
        tid = self._tid
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    # -- 钩子回调（在钩子线程里，必须够快） -------------------------------- #
    def _on_mouse(self, n_code, w_param, l_param):
        try:
            if n_code == 0:
                data = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                msg = int(w_param)
                with self._lock:
                    self._roll_locked()
                    if msg == WM_MOUSEMOVE:
                        x, y = data.pt.x, data.pt.y
                        if self._last is not None:
                            d = math.hypot(x - self._last[0], y - self._last[1])
                            if 0.0 < d <= MAX_JUMP:
                                self.dist += d
                                self.recent += d
                        self._last = (x, y)
                    elif msg == WM_LBUTTONDOWN:
                        self.left += 1
                        self.last_click_ts = time.monotonic()
                        self._click_times.append(self.last_click_ts)
                    elif msg == WM_RBUTTONDOWN:
                        self.right += 1
                        self.last_click_ts = time.monotonic()
                        self._click_times.append(self.last_click_ts)
                    elif msg == WM_MOUSEWHEEL:
                        delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
                        # 上下滚都算“滚过的距离”，所以取绝对值
                        self.wheel += abs(delta)
                        self._wheel_times.append(time.monotonic())
        except Exception:
            log.exception("hook callback error")
        return self._user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _on_key(self, n_code, w_param, l_param):
        try:
            if n_code == 0:
                data = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                msg = int(w_param)
                vk = int(data.vkCode)
                if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._set_mod(vk, True)
                    if vk not in self._pressed:      # 长按产生的是重复 keydown，只算一次
                        self._pressed.add(vk)
                        self._record_key(vk)
                elif msg in (WM_KEYUP, WM_SYSKEYUP):
                    self._pressed.discard(vk)
                    self._set_mod(vk, False)
        except Exception:
            log.exception("key hook callback error")
        return self._user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _set_mod(self, vk: int, down: bool) -> None:
        if vk in MOD_CTRL_VKS:
            self._mods["ctrl"] = down
        elif vk in MOD_ALT_VKS:
            self._mods["alt"] = down
        elif vk in MOD_SHIFT_VKS:
            self._mods["shift"] = down
        elif vk in MOD_WIN_VKS:
            self._mods["win"] = down

    def _combo_label(self, vk: int):
        mods = []
        if self._mods["ctrl"]:
            mods.append("Ctrl")
        if self._mods["alt"]:
            mods.append("Alt")
        if self._mods["win"]:
            mods.append("Win")
        if not mods:
            return None
        if self._mods["shift"]:
            mods.append("Shift")
        return "+".join(mods + [vk_label(vk)])

    def _record_key(self, vk: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.keys[vk] = self.keys.get(vk, 0) + 1
            self.hour_counts[dt.datetime.now().hour] += 1

            self._key_times.append(now)
            cutoff = now - 60.0
            while self._key_times and self._key_times[0] < cutoff:
                self._key_times.popleft()
            if len(self._key_times) > self.kpm_peak:
                self.kpm_peak = len(self._key_times)

            if self.session_start is None:
                self.session_start = now
            elif now - self._last_key_ts > 60.0:
                self.session_max = max(self.session_max, self._last_key_ts - self.session_start)
                self.session_start = now
            if self._last_key_ts:
                self.typing_seconds += min(now - self._last_key_ts, 5.0)
            self._last_key_ts = now
            self.session_max = max(self.session_max, now - self.session_start)

            if vk == 0x08:
                self.backspace += 1
                self._bs_times.append(now)

            if vk == self._repeat_vk and now - self._repeat_start < 3.0:
                self._repeat_count += 1
            else:
                self._repeat_vk = vk
                self._repeat_count = 1
                self._repeat_start = now
            if self._repeat_count >= 6:
                self._repeat_ts = now

            if vk not in MOD_VKS:
                label = self._combo_label(vk)
                if label:
                    self.combos[label] = self.combos.get(label, 0) + 1
                    self._last_combo = label
                    self._last_combo_ts = now

    # -- 数据 -------------------------------------------------------------- #
    def _roll_locked(self):
        today = dt.date.today().isoformat()
        if today == self.day:
            return None
        old = {
            "day": self.day,
            "px": round(self.dist, 1),
            "left": self.left,
            "right": self.right,
            "wheel": self.wheel,
            "keys": dict(self.keys),
            "combos": dict(self.combos),
            "hours": list(self.hour_counts),
            "kpm_peak": self.kpm_peak,
            "typing_seconds": round(self.typing_seconds, 1),
            "backspace": self.backspace,
        }
        self.day = today
        self.dist = 0.0
        self.left = 0
        self.right = 0
        self.wheel = 0
        self.keys = {}
        self.combos = {}
        self.hour_counts = [0] * 24
        self.kpm_peak = 0
        self.typing_seconds = 0.0
        self.backspace = 0
        self.session_start = None
        self.session_max = 0.0
        self._key_times.clear()
        self._last = None
        log.info("day rolled over: %s", old)
        return old

    def snapshot(self):
        with self._lock:
            archived = self._roll_locked()
            now = time.monotonic()
            cutoff = now - 60.0
            while self._key_times and self._key_times[0] < cutoff:
                self._key_times.popleft()
            for dq, span in ((self._bs_times, 10.0), (self._click_times, 3.0), (self._wheel_times, 5.0)):
                edge = now - span
                while dq and dq[0] < edge:
                    dq.popleft()
            data = {
                "day": self.day,
                "px": self.dist,
                "left": self.left,
                "right": self.right,
                "wheel": self.wheel,
                "keys": dict(self.keys),
                "combos": dict(self.combos),
                "hours": list(self.hour_counts),
                "kpm": len(self._key_times),
                "kpm_peak": self.kpm_peak,
                "typing_seconds": round(self.typing_seconds, 1),
                "backspace": self.backspace,
                "session_max": round(self.session_max, 1),
                "keys_3s": sum(1 for t in self._key_times if t >= now - 3.0),
                "backspaces_10s": len(self._bs_times),
                "clicks_3s": len(self._click_times),
                "wheel_5s": len(self._wheel_times),
                "repeat_burst": bool(self._repeat_ts and now - self._repeat_ts < 1.5),
                "combo_pulse": self._last_combo if (self._last_combo and now - self._last_combo_ts < 2.0) else "",
            }
            recent = self.recent
            self.recent = 0.0
            click_ts = self.last_click_ts
        return data, recent, click_ts, archived

    def reset_today(self) -> None:
        with self._lock:
            self.dist = 0.0
            self.left = 0
            self.right = 0
            self.wheel = 0
            self.keys = {}
            self.combos = {}
            self.hour_counts = [0] * 24
            self.kpm_peak = 0
            self.typing_seconds = 0.0
            self.backspace = 0
            self.session_start = None
            self.session_max = 0.0
            self._key_times.clear()
            self._last = None

    def restore(self, rec: dict) -> None:
        """启动时把今天已保存的计数接上，避免重启后归零。"""
        if not isinstance(rec, dict):
            return
        with self._lock:
            if self.day != dt.date.today().isoformat():
                return
            try:
                self.dist = max(0.0, float(rec.get("px", 0.0) or 0.0))
                self.left = max(0, int(rec.get("left", 0) or 0))
                self.right = max(0, int(rec.get("right", 0) or 0))
                self.wheel = max(0, int(rec.get("wheel", 0) or 0))
                keys = {}
                for k, v in (rec.get("keys") or {}).items():
                    try:
                        keys[int(k)] = max(0, int(v))
                    except (TypeError, ValueError):
                        continue
                self.keys = keys
                combos = {}
                for k, v in (rec.get("combos") or {}).items():
                    try:
                        combos[str(k)] = max(0, int(v))
                    except (TypeError, ValueError):
                        continue
                self.combos = combos
                hours = [0] * 24
                raw_hours = rec.get("hours") or []
                for i, v in enumerate(raw_hours[:24]):
                    try:
                        hours[i] = max(0, int(v))
                    except (TypeError, ValueError):
                        pass
                self.hour_counts = hours
                self.kpm_peak = max(0, int(rec.get("kpm_peak", 0) or 0))
                self.typing_seconds = max(0.0, float(rec.get("typing_seconds", 0.0) or 0.0))
                self.backspace = max(0, int(rec.get("backspace", 0) or 0))
                self.session_max = max(0.0, float(rec.get("session_max", 0.0) or 0.0))
            except (TypeError, ValueError):
                pass


# --------------------------------------------------------------------------- #
# 配置 / 存档
# --------------------------------------------------------------------------- #
def read_json(path: Path, default):
    try:
        # utf-8-sig：兼容记事本 / PowerShell 写出的 BOM
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        log.exception("failed to write %s", path)


def startup_shortcut() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "MousePet.vbs"


def startup_enabled() -> bool:
    path = startup_shortcut()
    return bool(path and path.exists())


def _pythonw_path() -> Path:
    if FROZEN:
        return APP_PATH
    exe = Path(sys.executable)
    if exe.name.lower() != "pythonw.exe":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            exe = cand
    return exe


def _launch_command() -> str:
    """自启用的命令行（打包后直接是 exe）。"""
    if FROZEN:
        return f'"{APP_PATH}" --quiet'
    return f'"{_pythonw_path()}" "{APP_PATH}" --quiet'


def set_startup(enabled: bool) -> bool:
    path = startup_shortcut()
    if path is None:
        return False
    try:
        if enabled:
            escaped = _launch_command().replace('"', '""')
            content = (
                "' MousePet autostart (generated, delete this file to disable)\r\n"
                'Set sh = CreateObject("WScript.Shell")\r\n'
                f'sh.Run "{escaped}", 0, False\r\n'
            )
            path.write_text(content, encoding="ascii")
        elif path.exists():
            path.unlink()
        return True
    except OSError:
        log.exception("failed to toggle startup")
        return False


# -- 计划任务（管理员自启） ------------------------------------------------- #
TASK_NAME = "MousePet"
CREATE_NO_WINDOW = 0x08000000


def task_installed() -> bool:
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_task() -> bool:
    """创建登录时以最高权限启动的计划任务（需要管理员权限）。"""
    import xml.sax.saxutils as su

    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    if FROZEN:
        exe = su.escape(str(APP_PATH))
        args = "--quiet"
    else:
        exe = su.escape(str(_pythonw_path()))
        args = f'"{su.escape(str(APP_PATH))}" --quiet'
    user_x = su.escape(user)
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>MousePet desktop pet (runs elevated so it can see input to admin windows)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_x}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_x}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>
'''
    path = DATA_DIR / "mousepet_task.xml"
    try:
        path.write_text(xml, encoding="utf-16")
    except OSError:
        log.exception("failed to write task xml")
        return False
    try:
        proc = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("schtasks create failed")
        return False
    log.info("task create rc=%s err=%s", proc.returncode,
             proc.stderr.decode("mbcs", "ignore").strip())
    return proc.returncode == 0


def remove_task() -> bool:
    try:
        proc = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("schtasks delete failed")
        return False
    log.info("task delete rc=%s err=%s", proc.returncode,
             proc.stderr.decode("mbcs", "ignore").strip())
    return proc.returncode == 0 or not task_installed()


# --------------------------------------------------------------------------- #
# 配色 / 布局
# --------------------------------------------------------------------------- #
KEY_COLOR = "#010203"      # 透明色键（不要用在别的元素上）
COL_BG = "#171722"
COL_BORDER = "#33334d"
COL_TEXT = "#eef2ff"
COL_SUB = "#6f7796"
COL_ACCENT = "#8fe3d0"
COL_BODY = "#9fb6ff"
COL_BODY_DARK = "#7b90e0"
COL_BELLY = "#c2cfff"
COL_BLUSH = "#f49ab2"
COL_EYE = "#23233a"

# 大肥鱼：没有贴图时的兜底鲸鱼
COL_WHALE = "#4d6bfe"
COL_WHALE_DARK = "#3350d6"
COL_WHALE_BELLY = "#e9efff"
COL_WHALE_FIN = "#6d86ff"

# 柴犬
COL_SHIBA = "#f0b47c"
COL_SHIBA_DARK = "#c98245"
COL_CREAM = "#fff4e4"

# 企鹅
COL_PENG = "#3d4f74"
COL_PENG_DARK = "#2b3956"
COL_BEAK = "#f5a24b"

# 青蛙
COL_FROG = "#8fd98a"
COL_FROG_DARK = "#5aa858"
COL_FROG_BELLY = "#eaf9d6"

ANIMALS = [
    ("fish", "大肥鱼"),
    ("chatgpt", "ChatGPT Q版"),
    ("claude", "Claude Q版"),
]
ANIMAL_KEYS = {key for key, _ in ANIMALS}
SKIN_PREFIX = {"fish": "dfy", "chatgpt": "gpt", "claude": "claude"}
DEFAULT_ANIMAL = "fish"

POSES = [
    ("idle", "待机"), ("shy", "害羞"), ("cheer", "欢呼"), ("blush", "脸红"),
    ("smug", "得意"), ("side", "侧身"), ("orb", "抱球"), ("happy", "开心"),
    ("drowsy", "迷糊"), ("surprised", "惊讶"), ("pout", "鼓嘴"), ("sulk", "生气"),
    ("sing", "唱歌"), ("proud", "挺胸"), ("sleepy", "犯困"), ("sleep", "睡着"),
]
POSE_KEYS = {key for key, _ in POSES}

W_WIDE, H_WIDE = 380, 224
W_WIDE_IMG, H_WIDE_IMG = 380, 224
W_MINI, H_MINI = 128, 122
W_MINI_IMG, H_MINI_IMG = 128, 138
LEFT_CX = 119          # 左侧（角色 + 距离）中心
PANEL_X = 248          # 右侧按键栏
PANEL_R = 370
DIVIDER_X = 241
SKIN_H_FULL = 138
SKIN_H_MINI = 96
ASSETS_DIR = _assets_dir()


def fmt_distance(px: float, px_per_cm: float) -> str:
    cm = px / px_per_cm
    if cm < 1:
        return "0 cm"
    if cm < 100:
        return f"{cm:.0f} cm"
    if cm < 100_000:
        return f"{cm / 100:.2f} m"
    return f"{cm / 100_000:.2f} km"


def fmt_stats(left: int, right: int, wheel: int) -> str:
    notches = int(round(wheel / 120.0))
    return f"左 {left:,}  ·  滚 {notches:,}  ·  右 {right:,}"


VK_NAMES = {
    0x08: "退格", 0x09: "Tab", 0x0D: "回车", 0x10: "Shift", 0x11: "Ctrl",
    0x12: "Alt", 0x13: "Pause", 0x14: "Caps", 0x1B: "Esc", 0x20: "空格",
    0x21: "PgUp", 0x22: "PgDn", 0x23: "End", 0x24: "Home",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x2C: "PrtSc", 0x2D: "Ins", 0x2E: "Del", 0x5B: "Win", 0x5D: "菜单",
    0x6A: "*", 0x6B: "+", 0x6D: "-", 0x6E: ".", 0x6F: "/",
    0x90: "NumLock", 0x91: "ScrLk",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}

MOD_CTRL_VKS = {0x11, 0xA2, 0xA3}
MOD_ALT_VKS = {0x12, 0xA4, 0xA5}
MOD_SHIFT_VKS = {0x10, 0xA0, 0xA1}
MOD_WIN_VKS = {0x5B, 0x5C}
MOD_VKS = MOD_CTRL_VKS | MOD_ALT_VKS | MOD_SHIFT_VKS | MOD_WIN_VKS


def vk_label(vk: int) -> str:
    name = VK_NAMES.get(vk)
    if name:
        return name
    if 0x41 <= vk <= 0x5A:      # A-Z
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:      # 0-9
        return chr(vk)
    if 0x60 <= vk <= 0x69:      # 小键盘数字
        return f"Num{vk - 0x60}"
    if 0x70 <= vk <= 0x87:      # F1-F24
        return f"F{vk - 0x6F}"
    return f"0x{vk:02X}"


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MousePet:
    def __init__(self, root: tk.Tk, tracker: Tracker, dpi: int) -> None:
        self.root = root
        self.tracker = tracker
        self.dpi = dpi
        self.s = max(1.0, dpi / 96.0)

        cfg = read_json(CONFIG_FILE, {})
        self.cfg = cfg if isinstance(cfg, dict) else {}
        history = read_json(STATS_FILE, {})
        self.history = history if isinstance(history, dict) else {}

        self.compact = bool(self.cfg.get("compact", False))
        self.topmost = bool(self.cfg.get("topmost", True))
        self.animal = str(self.cfg.get("animal", DEFAULT_ANIMAL))
        if self.animal not in ANIMAL_KEYS:
            self.animal = DEFAULT_ANIMAL
        try:
            self.opacity = max(0.35, min(1.0, float(self.cfg.get("opacity", 0.86))))
        except (TypeError, ValueError):
            self.opacity = 0.86
        ppcm = self.cfg.get("px_per_cm")
        self.px_per_cm = float(ppcm) if isinstance(ppcm, (int, float)) and ppcm > 0 else dpi / 2.54

        self._closing = False
        self._drag = None
        self._moved = False
        self._dist_str = ""
        self._stats_str = ""
        self._speed = 0.0
        self._last_tick = time.monotonic()
        self._next_flush = time.monotonic() + 5.0
        self._next_blink = time.monotonic() + random.uniform(1.5, 4.0)
        self._blink_until = 0.0
        self._joy_until = 0.0
        self._last_clicks = 0
        self._hist_win = None
        self._opacity_win = None
        self._skin = None
        self._skin_h = None
        self._skin_cache = {}
        self._expr = "normal"
        self._expr_until = 0.0
        self._next_blink = time.monotonic() + random.uniform(2.0, 5.0)
        self._react = "idle"
        self._react_until = 0.0
        self._pulse_cd = {}
        self._hover_since = 0.0
        self._last_snap = {}

        root.title(APP_NAME)
        root.overrideredirect(True)
        root.configure(bg=KEY_COLOR)
        try:
            root.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            log.warning("transparentcolor not supported")
        root.attributes("-topmost", self.topmost)
        try:
            # 只有卡片层用 alpha；角色和数字放在另一个窗口里，保持不透明
            root.attributes("-alpha", self.opacity)
        except tk.TclError:
            log.warning("alpha not supported")

        self.front = tk.Toplevel(root)
        self.front.title(APP_NAME)
        self.front.overrideredirect(True)
        self.front.configure(bg=KEY_COLOR)
        try:
            self.front.attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            log.warning("transparentcolor not supported on front")
        self.front.attributes("-topmost", self.topmost)

        # 字体（用 pt，Windows 缩放交给 tk scaling）
        self.f_big = tkfont.Font(root=root, family="Segoe UI", size=17, weight="bold")
        self.f_clicks = tkfont.Font(root=root, family="Microsoft YaHei UI", size=9, weight="bold")
        self.f_wheel = tkfont.Font(root=root, family="Microsoft YaHei UI", size=8)
        self.f_mini = tkfont.Font(root=root, family="Segoe UI", size=11, weight="bold")
        self.f_tiny = tkfont.Font(root=root, family="Microsoft YaHei UI", size=8)
        self.f_panel = tkfont.Font(root=root, family="Microsoft YaHei UI", size=8, weight="bold")
        self.f_keys = tkfont.Font(root=root, family="Microsoft YaHei UI", size=8, weight="bold")

        self._skin = self._load_skin()
        self.w_log, self.h_log = self._layout_sizes()
        self.w_px = int(round(self.w_log * self.s))
        self.h_px = int(round(self.h_log * self.s))
        self.x, self.y = self._initial_position()

        self.card_canvas = tk.Canvas(
            root, width=self.w_px, height=self.h_px,
            bg=KEY_COLOR, highlightthickness=0, bd=0,
        )
        self.card_canvas.pack()

        self.canvas = tk.Canvas(
            self.front, width=self.w_px, height=self.h_px,
            bg=KEY_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        self.var_top = tk.BooleanVar(value=self.topmost)
        self.var_compact = tk.BooleanVar(value=self.compact)
        self.var_startup = tk.BooleanVar(value=startup_enabled())
        self.var_admin_start = tk.BooleanVar(value=task_installed())
        self.var_animal = tk.StringVar(value=self.animal)
        self._pinned = str(self.cfg.get("pose", "") or "")
        if self._pinned not in POSE_KEYS:
            self._pinned = ""
        if self._pinned and self._pinned not in {k for k, _ in self._available_poses()}:
            self._pinned = ""
            self.cfg["pose"] = ""
        self.var_pose = tk.StringVar(value=self._pinned)
        self.var_opacity = tk.IntVar(value=int(round(self.opacity * 100)))
        self._build_menu()

        for cv in (self.canvas, self.card_canvas):
            cv.bind("<ButtonPress-1>", self._on_press)
            cv.bind("<B1-Motion>", self._on_motion)
            cv.bind("<ButtonRelease-1>", self._on_release)
            cv.bind("<Button-3>", self._on_right)

        self._apply_geometry()
        self.build()
        self._update_texts(self.tracker.snapshot()[0])

        self.root.update_idletasks()
        log.info(
            "window screen=%sx%s pos=%s,%s size=%sx%s",
            self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
            self.x, self.y, self.w_px, self.h_px,
        )
        # 映射后再把窗口顶到最前，避免被别的置顶窗口压住
        self.root.after(100, self._raise)
        self.root.after(1200, self._raise)
        self.root.after(50, self.tick)

    # -- 窗口/布局 --------------------------------------------------------- #
    def _initial_position(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        pos = self.cfg.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                x, y = int(pos[0]), int(pos[1])
            except (TypeError, ValueError):
                x = y = -1
        else:
            x = y = -1
        if x < 0 or y < 0 or x > sw - 40 or y > sh - 40:
            x = sw - self.w_px - int(48 * self.s)
            y = sh - self.h_px - int(96 * self.s)
        x = max(0, min(x, sw - self.w_px))
        y = max(0, min(y, sh - self.h_px))
        return x, y

    def _apply_geometry(self) -> None:
        self.w_px = int(round(self.w_log * self.s))
        self.h_px = int(round(self.h_log * self.s))
        geo = f"{self.w_px}x{self.h_px}+{self.x}+{self.y}"
        self.root.geometry(geo)
        self.front.geometry(geo)
        self.card_canvas.config(width=self.w_px, height=self.h_px)
        self.canvas.config(width=self.w_px, height=self.h_px)

    def _uses_image(self) -> bool:
        return self.animal in SKIN_PREFIX and self._skin is not None

    def _layout_sizes(self):
        img = self._uses_image()
        if self.compact:
            return (W_MINI, H_MINI_IMG if img else H_MINI)
        return (W_WIDE, H_WIDE_IMG if img else H_WIDE)

    def _load_skin(self):
        self._skin_h = None
        self._skin_cache = {}
        prefix = SKIN_PREFIX.get(self.animal)
        if prefix is None:
            return None
        try:
            files = [p for p in ASSETS_DIR.glob(f"{prefix}_*.png") if self._is_normal_skin(p, prefix)]
        except OSError:
            return None
        if not files:
            return None
        target = int(round((SKIN_H_MINI if self.compact else SKIN_H_FULL) * self.s))
        best = None
        best_d = 1 << 30
        for p in files:
            try:
                h = int(p.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            d = abs(h - target)
            if d < best_d:
                best_d, best = d, p
        if best is None:
            return None
        self._skin_h = int(best.stem.split("_")[1])
        return self._skin_for("normal")

    @staticmethod
    def _is_normal_skin(path, prefix: str) -> bool:
        parts = path.stem.split("_")
        return len(parts) == 2 and parts[0] == prefix and parts[1].isdigit()

    def _skin_for(self, variant: str):
        if self._skin_h is None:
            return None
        prefix = SKIN_PREFIX.get(self.animal, "dfy")
        key = (prefix, variant, self._skin_h)
        cached = self._skin_cache.get(key)
        if cached is not None:
            return cached
        path = ASSETS_DIR / f"{prefix}_{self._skin_h}_{variant}.png"
        if not path.exists():
            path = ASSETS_DIR / f"{prefix}_{self._skin_h}.png"
        try:
            img = tk.PhotoImage(file=str(path))
        except tk.TclError:
            log.exception("failed to load skin %s", path)
            return self._skin
        self._skin_cache[key] = img
        return img

    def _set_expr(self, name: str, duration: float) -> None:
        self._expr = name
        self._expr_until = time.monotonic() + duration

    def _set_pose(self, key: str) -> None:
        if key and key not in {k for k, _ in self._available_poses()}:
            return
        self._pinned = key if key in POSE_KEYS else ""
        self.var_pose.set(self._pinned)
        self.cfg["pose"] = self._pinned
        self.save_position()
        self._expr = "normal"
        self._expr_until = 0.0
        self._panel_sig = None
        self._draw_pet(time.monotonic())

    def _pulse(self, name: str, now: float, hold: float = 2.0, cd: float = 8.0):
        if now < self._pulse_cd.get(name, 0.0):
            return None
        self._pulse_cd[name] = now + cd
        self._react = name
        self._react_until = now + hold
        return name

    def _auto_expr(self, now: float) -> str:
        snap = self._last_snap or {}
        idle = idle_seconds()
        if idle >= 900.0:
            return "sleep"
        if idle >= 300.0:
            return "sleepy"
        if self._drag is not None and self._moved:
            return "side"
        kpm = int(snap.get("kpm", 0))
        if kpm >= 420:
            return self._pulse("cheer", now, 2.2, 10.0) or "proud"
        if kpm >= 300:
            return "proud"
        if snap.get("repeat_burst"):
            hit = self._pulse("sulk", now, 2.0, 8.0)
            if hit:
                return hit
        if int(snap.get("backspaces_10s", 0)) >= 10:
            hit = self._pulse("pout", now, 2.0, 8.0)
            if hit:
                return hit
        if int(snap.get("clicks_3s", 0)) >= 6:
            hit = self._pulse("surprised", now, 1.6, 6.0)
            if hit:
                return hit
        if int(snap.get("wheel_5s", 0)) >= 12:
            hit = self._pulse("orb", now, 2.0, 8.0)
            if hit:
                return hit
        if snap.get("combo_pulse") in ("Ctrl+C", "Ctrl+V"):
            hit = self._pulse("smug", now, 2.0, 8.0)
            if hit:
                return hit
        if int(snap.get("keys_3s", 0)) >= 6:
            return "sing"
        if self._hover_since:
            if now - self._hover_since >= 6.0:
                return "blush"
            if now - self._hover_since >= 2.0:
                return "shy"
        if idle >= 60.0:
            return "drowsy"
        return "idle"

    def _current_expr(self, now: float) -> str:
        if self._pinned:
            return self._pinned
        if self._expr != "normal":
            if now < self._expr_until:
                return self._expr
            self._expr = "normal"
        if now < self._react_until:
            return self._react
        return self._auto_expr(now)

    def set_compact(self, value: bool) -> None:
        self.compact = bool(value)
        self.var_compact.set(self.compact)
        self.cfg["compact"] = self.compact
        self._skin = self._load_skin()
        self.w_log, self.h_log = self._layout_sizes()
        self._apply_geometry()
        self.build()
        self._update_texts(self.tracker.snapshot()[0], force=True)
        self._front_on_top()
        self.save_position()

    # -- 绘图辅助（全部用逻辑坐标，内部乘缩放） ---------------------------- #
    def _oval(self, *box, canvas=None, **kw):
        c = canvas if canvas is not None else self.canvas
        return c.create_oval(*[v * self.s for v in box], **kw)

    def _poly(self, pts, canvas=None, **kw):
        c = canvas if canvas is not None else self.canvas
        return c.create_polygon([v * self.s for v in pts], **kw)

    def _line(self, pts, canvas=None, **kw):
        c = canvas if canvas is not None else self.canvas
        return c.create_line([v * self.s for v in pts], **kw)

    def _text(self, x, y, canvas=None, **kw):
        c = canvas if canvas is not None else self.canvas
        return c.create_text(x * self.s, y * self.s, **kw)

    def _halo_text(self, x, y, halo="#0a0a12", halo_r=0.7, **kw):
        """带一圈深色描边的文字，卡片再透明也读得清。"""
        shadow_kw = dict(kw)
        shadow_kw["fill"] = halo
        shadows = [
            self._text(x + dx, y + dy, **shadow_kw)
            for dx, dy in ((-halo_r, 0), (halo_r, 0), (0, -halo_r), (0, halo_r))
        ]
        main = self._text(x, y, **kw)
        return {"main": main, "shadows": shadows}

    def _set_text(self, item, text: str) -> None:
        if isinstance(item, dict):
            self.canvas.itemconfig(item["main"], text=text)
            for s in item["shadows"]:
                self.canvas.itemconfig(s, text=text)
        else:
            self.canvas.itemconfig(item, text=text)

    def _round_rect(self, x1, y1, x2, y2, r, canvas=None, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self._poly(pts, canvas=canvas, smooth=True, **kw)

    def build(self) -> None:
        self.card_canvas.delete("all")
        self.canvas.delete("all")
        s = self.s
        W, H = self.w_log, self.h_log
        # 卡片画在底层窗口（只有它会跟着透明度变淡）
        self._round_rect(
            5, 5, W - 5, H - 5, 22, canvas=self.card_canvas,
            fill=COL_BG, outline=COL_BORDER, width=max(1, round(1.2 * s)),
        )
        img = self._uses_image()
        if self.compact:
            cx = W / 2
            y = 118 if img else 103
            self.item_dist_shadow = self._text(
                cx + 1, y + 1, text="0 cm", fill="#0a0a10",
                font=self.f_mini, anchor="center",
            )
            self.item_dist = self._text(
                cx, y, text="0 cm",
                fill=COL_TEXT, font=self.f_mini, anchor="center",
            )
            self.item_stats = None
            self.item_stats_shadow = None
            self.key_kpm = None
            self.key_peak = None
            self.spark_bars = None
            self.combo_labels = None
            self.combo_counts = None
            self.combo_bars = None
            self.key_persona = None
            self._pet_center = (cx, 53 if img else 58)
        else:
            cx = LEFT_CX
            y = 170 if img else 126
            self.item_dist_shadow = self._text(
                cx + 1, y + 1, text="0 cm", fill="#0a0a10",
                font=self.f_big, anchor="center",
            )
            self.item_dist = self._text(
                cx, y, text="0 cm",
                fill=COL_TEXT, font=self.f_big, anchor="center",
            )
            sy = 196 if img else 152
            self.item_stats_shadow = self._text(
                cx + 1, sy + 1, text="", fill="#0a0a10",
                font=self.f_clicks, anchor="center",
            )
            self.item_stats = self._text(
                cx, sy, text="",
                fill="#9aa3c7", font=self.f_clicks, anchor="center",
            )
            self._pet_center = (cx, 74 if img else 64)

            # 右侧键盘仪表盘
            self._line(
                [DIVIDER_X, 20, DIVIDER_X, H - 16], canvas=self.card_canvas,
                fill="#2b2b40", width=max(1, int(1.0 * s)),
            )
            self._halo_text(PANEL_X, 26, text="键盘", fill="#c3cbe6",
                            font=self.f_panel, anchor="w")
            self.key_kpm = self._halo_text(
                PANEL_X, 45, text="", fill="#ffffff", font=self.f_keys, anchor="w",
            )
            self.key_peak = self._halo_text(
                PANEL_X, 62, text="", fill="#d5dcf5", font=self.f_panel, anchor="w",
            )

            self.spark_bars = []
            for i in range(24):
                bx = PANEL_X + i * 5
                self.spark_bars.append(self.canvas.create_rectangle(
                    bx * s, 92 * s, (bx + 3.4) * s, 92 * s,
                    fill="#3a56b8", outline="",
                ))

            self.combo_bars = []
            self.combo_labels = []
            self.combo_counts = []
            for i in range(5):
                ry = 112 + i * 17
                self.combo_bars.append(self.canvas.create_rectangle(
                    (PANEL_X - 6) * s, (ry + 8) * s,
                    (PANEL_X - 6) * s, (ry + 11) * s,
                    fill="#3a56b8", outline="",
                ))
                self.combo_labels.append(self._halo_text(
                    PANEL_X, ry, text="", fill="#ffffff", font=self.f_keys, anchor="w",
                ))
                self.combo_counts.append(self._halo_text(
                    PANEL_R, ry, text="", fill="#e6ebff", font=self.f_keys, anchor="e",
                ))
            self.key_persona = self._halo_text(
                PANEL_X, 202, text="", fill="#ffd6a0", font=self.f_panel, anchor="w",
            )
        self._dist_str = ""
        self._stats_str = ""
        self._panel_sig = None
        self._draw_pet(time.monotonic())

    # -- 形象 -------------------------------------------------------------- #
    def _draw_pet(self, now: float) -> None:
        self.canvas.delete("pet")
        cx, cy = self._pet_center
        if self._speed > 40:
            cy += math.sin(now * 9.0) * min(2.5, self._speed / 900.0)
        if self._uses_image():
            img = self._skin_for(self._current_expr(now))
            if img is not None:
                self.canvas.create_image(
                    cx * self.s, cy * self.s, image=img,
                    anchor="center", tags="pet",
                )
                return
        look = self._look_vector(cx, cy)
        blink = now < self._blink_until
        joy = now < self._joy_until
        drawer = getattr(self, "_draw_" + self.animal, self._draw_fish)
        drawer(cx, cy, look, blink, joy, now)

    def _look_vector(self, cx, cy):
        mx, my = self.root.winfo_pointerxy()
        if mx < 0:
            return (0.0, 0.15)
        px = self.front.winfo_rootx() + cx * self.s
        py = self.front.winfo_rooty() + cy * self.s
        dx, dy = mx - px, my - py
        dist = math.hypot(dx, dy)
        return (dx / dist, dy / dist) if dist > 12 else (0.0, 0.2)

    def _eye(self, ex, ey, look, blink, joy, size=1.0, white=False):
        s = self.s
        if white:
            self._oval(
                ex - 6.8 * size, ey - 8.0 * size, ex + 6.8 * size, ey + 8.0 * size,
                fill="#ffffff", outline="", tags="pet",
            )
        if joy:
            self._line(
                [ex - 5 * size, ey + 1.5 * size, ex - 2 * size, ey - 2.5 * size,
                 ex + 2 * size, ey - 2.5 * size, ex + 5 * size, ey + 1.5 * size],
                smooth=True, width=max(1, int(2.2 * s * size)), fill=COL_EYE,
                capstyle="round", tags="pet",
            )
        elif blink:
            self._line(
                [ex - 5 * size, ey - 0.5 * size, ex - 2 * size, ey + 2 * size,
                 ex + 2 * size, ey + 2 * size, ex + 5 * size, ey - 0.5 * size],
                smooth=True, width=max(1, int(2.2 * s * size)), fill=COL_EYE,
                capstyle="round", tags="pet",
            )
        else:
            self._oval(
                ex - 5.2 * size, ey - 6.2 * size, ex + 5.2 * size, ey + 6.2 * size,
                fill=COL_EYE, outline="", tags="pet",
            )
            self._oval(
                ex - 2.6 * size, ey - 4.2 * size, ex - 0.2 * size, ey - 1.8 * size,
                fill="#ffffff", outline="", tags="pet",
            )

    def _eyes(self, cx, cy, look, blink, joy, spread=13.0, y=-5.0, size=1.0, white=False):
        for side in (-1, 1):
            self._eye(
                cx + spread * side + look[0] * 2.4 * size,
                cy + y + look[1] * 2.2 * size,
                look, blink, joy, size=size, white=white,
            )

    def _blush(self, cx, cy, spread=24.0, y=3.0):
        self._oval(cx - spread - 5, cy + y - 3, cx - spread + 5, cy + y + 3,
                   fill=COL_BLUSH, outline="", tags="pet")
        self._oval(cx + spread - 5, cy + y - 3, cx + spread + 5, cy + y + 3,
                   fill=COL_BLUSH, outline="", tags="pet")

    def _mouth(self, cx, cy, joy, y=8.0, style="w"):
        s = self.s
        if joy:
            self._oval(cx - 3.5, cy + y, cx + 3.5, cy + y + 6,
                       fill="#3a2c48", outline="", tags="pet")
        elif style == "o":
            self._oval(cx - 3, cy + y, cx + 3, cy + y + 6, fill="",
                       outline=COL_EYE, width=max(1, int(1.5 * s)), tags="pet")
        else:
            self._line(
                [cx - 5, cy + y, cx - 2.5, cy + y + 3, cx, cy + y + 1,
                 cx + 2.5, cy + y + 3, cx + 5, cy + y],
                smooth=True, width=max(1, int(1.6 * s)), fill=COL_EYE,
                capstyle="round", tags="pet",
            )

    def _draw_fish(self, cx, cy, look, blink, joy, now):
        """没有贴图时的兜底：一只简洁的蓝色胖鲸。"""
        s = self.s
        wag = math.sin(now * 3.6) * 5.0
        # 尾巴
        self._poly(
            [cx - 30, cy - 4, cx - 54, cy - 22 + wag, cx - 46, cy - 2,
             cx - 54, cy + 18 + wag, cx - 30, cy + 6],
            fill=COL_WHALE_FIN, outline=COL_WHALE_DARK, width=max(1, int(1.2 * s)),
            smooth=True, tags="pet",
        )
        # 背鳍
        self._poly(
            [cx - 16, cy - 27, cx - 7, cy - 40, cx + 3, cy - 27],
            fill=COL_WHALE_FIN, outline=COL_WHALE_DARK, width=max(1, int(1.2 * s)),
            smooth=True, tags="pet",
        )
        # 身体
        self._oval(
            cx - 40, cy - 30, cx + 40, cy + 30,
            fill=COL_WHALE, outline=COL_WHALE_DARK, width=max(1, int(1.6 * s)), tags="pet",
        )
        # 白肚皮
        self._oval(cx - 20, cy + 2, cx + 36, cy + 29, fill=COL_WHALE_BELLY, outline="", tags="pet")
        # 胸鳍
        self._oval(
            cx - 4, cy + 13, cx + 16, cy + 23,
            fill=COL_WHALE_FIN, outline=COL_WHALE_DARK, width=max(1, int(1.0 * s)), tags="pet",
        )
        # 眼睛 / 腮红 / 嘴
        self._eye(cx + 17 + look[0] * 2.4, cy - 6 + look[1] * 2.2, look, blink, joy, size=1.15)
        self._oval(cx + 2, cy + 6, cx + 12, cy + 11, fill=COL_BLUSH, outline="", tags="pet")
        self._line(
            [cx + 8, cy + 15, cx + 18, cy + 17, cx + 28, cy + 12],
            smooth=True, width=max(1, int(1.6 * s)), fill=COL_WHALE_DARK,
            capstyle="round", tags="pet",
        )
        # 喷水
        for i in range(3):
            ph = (now * 0.7 + i * 0.3) % 1.0
            bx = cx + 4 + math.sin(now * 2.0 + i * 1.7) * 4
            by = cy - 31 - ph * 15
            r = 1.0 + ph * 1.5
            self._oval(bx - r, by - r, bx + r, by + r,
                       outline="#bfd0ff", width=max(1, int(1.0 * s)), tags="pet")


    def _draw_shiba(self, cx, cy, look, blink, joy, now):
        s = self.s
        # 卷尾巴
        self._line(
            [cx + 30, cy + 16, cx + 45, cy + 10, cx + 48, cy - 4, cx + 38, cy - 10],
            smooth=True, width=max(2, int(7 * s)), fill=COL_SHIBA_DARK,
            capstyle="round", tags="pet",
        )
        # 耳朵
        self._poly(
            [cx - 33, cy - 16, cx - 26, cy - 44, cx - 9, cy - 25],
            fill=COL_SHIBA, outline=COL_SHIBA_DARK, width=max(1, int(1.4 * s)),
            joinstyle="round", tags="pet",
        )
        self._poly(
            [cx + 33, cy - 16, cx + 26, cy - 44, cx + 9, cy - 25],
            fill=COL_SHIBA, outline=COL_SHIBA_DARK, width=max(1, int(1.4 * s)),
            joinstyle="round", tags="pet",
        )
        self._poly([cx - 28, cy - 21, cx - 24, cy - 36, cx - 15, cy - 24],
                   fill=COL_CREAM, outline="", tags="pet")
        self._poly([cx + 28, cy - 21, cx + 24, cy - 36, cx + 15, cy - 24],
                   fill=COL_CREAM, outline="", tags="pet")
        # 身体
        self._oval(
            cx - 38, cy - 30, cx + 38, cy + 31,
            fill=COL_SHIBA, outline=COL_SHIBA_DARK, width=max(1, int(1.6 * s)), tags="pet",
        )
        # 嘴套
        self._oval(cx - 17, cy + 2, cx + 17, cy + 22, fill=COL_CREAM, outline="", tags="pet")
        # 鼻子
        self._oval(cx - 3.2, cy + 6, cx + 3.2, cy + 10.5, fill="#5a4033", outline="", tags="pet")
        # 眼睛 / 腮红 / 嘴
        self._eyes(cx, cy - 2, look, blink, joy, spread=13, y=-2)
        self._blush(cx, cy, spread=26, y=7)
        self._mouth(cx, cy + 13, joy, y=0, style="w")

    def _draw_penguin(self, cx, cy, look, blink, joy, now):
        s = self.s
        # 脚
        self._oval(cx - 18, cy + 26, cx - 3, cy + 34, fill=COL_BEAK, outline="", tags="pet")
        self._oval(cx + 3, cy + 26, cx + 18, cy + 34, fill=COL_BEAK, outline="", tags="pet")
        # 翅膀
        self._oval(cx - 42, cy - 10, cx - 27, cy + 20, fill=COL_PENG_DARK, outline="", tags="pet")
        self._oval(cx + 27, cy - 10, cx + 42, cy + 20, fill=COL_PENG_DARK, outline="", tags="pet")
        # 身体
        self._oval(
            cx - 35, cy - 33, cx + 35, cy + 32,
            fill=COL_PENG, outline=COL_PENG_DARK, width=max(1, int(1.6 * s)), tags="pet",
        )
        # 肚子
        self._oval(cx - 22, cy - 8, cx + 22, cy + 30, fill="#f4f8ff", outline="", tags="pet")
        # 脸
        self._oval(cx - 21, cy - 26, cx + 21, cy + 4, fill="#f4f8ff", outline="", tags="pet")
        # 眼睛 / 嘴 / 腮红
        self._eyes(cx, cy - 12, look, blink, joy, spread=11, y=0, size=0.92)
        self._poly([cx - 6, cy + 3, cx + 6, cy + 3, cx, cy + 11],
                   fill=COL_BEAK, outline="#d98a37", width=max(1, int(1.0 * s)),
                   joinstyle="round", tags="pet")
        self._blush(cx, cy, spread=27, y=0)

    def _draw_frog(self, cx, cy, look, blink, joy, now):
        s = self.s
        # 身体
        self._oval(
            cx - 36, cy - 16, cx + 36, cy + 32,
            fill=COL_FROG, outline=COL_FROG_DARK, width=max(1, int(1.6 * s)), tags="pet",
        )
        # 肚子
        self._oval(cx - 24, cy + 2, cx + 24, cy + 30, fill=COL_FROG_BELLY, outline="", tags="pet")
        # 眼睛鼓包
        self._oval(cx - 32, cy - 34, cx - 6, cy - 8,
                   fill=COL_FROG, outline=COL_FROG_DARK, width=max(1, int(1.4 * s)), tags="pet")
        self._oval(cx + 6, cy - 34, cx + 32, cy - 8,
                   fill=COL_FROG, outline=COL_FROG_DARK, width=max(1, int(1.4 * s)), tags="pet")
        # 眼睛 / 嘴 / 腮红
        self._eyes(cx, cy, look, blink, joy, spread=19, y=-21, size=0.95, white=True)
        self._line(
            [cx - 13, cy + 6, cx - 6, cy + 11, cx + 6, cy + 11, cx + 13, cy + 6],
            smooth=True, width=max(1, int(1.8 * s)), fill=COL_FROG_DARK,
            capstyle="round", tags="pet",
        )
        self._blush(cx, cy, spread=27, y=4)

    def _draw_cat(self, cx, cy, look, blink, joy, now):
        c = self.canvas
        s = self.s
        speed = self._speed
        wag = math.sin(now * 5.5) * (3.0 + min(5.0, speed / 150.0))

        # 尾巴
        self._line(
            [cx + 28, cy + 18, cx + 42, cy + 10 + wag * 0.4, cx + 50, cy - 2 + wag],
            smooth=True, width=max(2, int(7 * s)), fill=COL_BODY_DARK,
            capstyle="round", tags="pet",
        )
        # 耳朵
        self._poly(
            [cx - 33, cy - 18, cx - 25, cy - 46, cx - 8, cy - 26],
            fill=COL_BODY, outline=COL_BODY_DARK, width=max(1, int(1.4 * s)),
            joinstyle="round", tags="pet",
        )
        self._poly(
            [cx + 33, cy - 18, cx + 25, cy - 46, cx + 8, cy - 26],
            fill=COL_BODY, outline=COL_BODY_DARK, width=max(1, int(1.4 * s)),
            joinstyle="round", tags="pet",
        )
        self._poly(
            [cx - 29, cy - 22, cx - 24, cy - 38, cx - 15, cy - 25],
            fill=COL_BLUSH, outline="", tags="pet",
        )
        self._poly(
            [cx + 29, cy - 22, cx + 24, cy - 38, cx + 15, cy - 25],
            fill=COL_BLUSH, outline="", tags="pet",
        )
        # 身体
        self._oval(
            cx - 37, cy - 31, cx + 37, cy + 31,
            fill=COL_BODY, outline=COL_BODY_DARK, width=max(1, int(1.6 * s)), tags="pet",
        )
        # 肚子
        self._oval(cx - 17, cy + 3, cx + 17, cy + 25, fill=COL_BELLY, outline="", tags="pet")
        # 腮红
        self._oval(cx - 28, cy + 2, cx - 18, cy + 8, fill=COL_BLUSH, outline="", tags="pet")
        self._oval(cx + 18, cy + 2, cx + 28, cy + 8, fill=COL_BLUSH, outline="", tags="pet")

        # 眼睛（跟着鼠标看）
        for side in (-1, 1):
            ex = cx + 13 * side + look[0] * 2.4
            ey = cy - 5 + look[1] * 2.2
            if joy:
                self._line(
                    [ex - 5, ey + 1.5, ex - 2, ey - 2.5, ex + 2, ey - 2.5, ex + 5, ey + 1.5],
                    smooth=True, width=max(1, int(2.2 * s)), fill=COL_EYE,
                    capstyle="round", tags="pet",
                )
            elif blink:
                self._line(
                    [ex - 5, ey - 0.5, ex - 2, ey + 2, ex + 2, ey + 2, ex + 5, ey - 0.5],
                    smooth=True, width=max(1, int(2.2 * s)), fill=COL_EYE,
                    capstyle="round", tags="pet",
                )
            else:
                self._oval(ex - 5.2, ey - 6.2, ex + 5.2, ey + 6.2, fill=COL_EYE, outline="", tags="pet")
                self._oval(ex - 2.6, ey - 4.2, ex - 0.2, ey - 1.8, fill="#ffffff", outline="", tags="pet")

        # 嘴
        if joy:
            self._oval(cx - 3.5, cy + 8, cx + 3.5, cy + 14, fill="#3a2c48", outline="", tags="pet")
        else:
            self._line(
                [cx - 5, cy + 8, cx - 2.5, cy + 11, cx, cy + 9, cx + 2.5, cy + 11, cx + 5, cy + 8],
                smooth=True, width=max(1, int(1.6 * s)), fill=COL_EYE,
                capstyle="round", tags="pet",
            )

    def _update_texts(self, snap, force: bool = False) -> None:
        dist = fmt_distance(snap["px"], self.px_per_cm)
        stats = fmt_stats(snap["left"], snap["right"], snap.get("wheel", 0))
        if force or dist != self._dist_str:
            self._dist_str = dist
            self.canvas.itemconfig(self.item_dist, text=dist)
            self.canvas.itemconfig(self.item_dist_shadow, text=dist)
        if self.item_stats is not None and (force or stats != self._stats_str):
            self._stats_str = stats
            self.canvas.itemconfig(self.item_stats, text=stats)
            self.canvas.itemconfig(self.item_stats_shadow, text=stats)
        self._update_key_panel(snap)

    def _persona(self, snap) -> str:
        keys = snap.get("keys") or {}
        total = sum(keys.values())
        if total < 50:
            return "还在热身"
        combos = snap.get("combos") or {}
        backspace = int(snap.get("backspace", 0))
        copy = combos.get("Ctrl+C", 0)
        paste = combos.get("Ctrl+V", 0)
        undo = combos.get("Ctrl+Z", 0)
        hours = snap.get("hours") or [0] * 24
        night = sum(hours[0:6])
        if copy + paste >= 40:
            return "复制粘贴工程师"
        if backspace / max(1, total) > 0.12:
            return "退格狂魔"
        if undo >= 10:
            return "撤销依赖症"
        if night > total * 0.25:
            return "深夜键盘侠"
        if int(snap.get("kpm_peak", 0)) >= 360:
            return "键盘机关枪"
        if float(snap.get("typing_seconds", 0)) >= 3600:
            return "键盘马拉松"
        return "稳重型选手"

    def _update_key_panel(self, snap) -> None:
        if self.key_kpm is None:
            return
        hours = snap.get("hours") or [0] * 24
        combos = snap.get("combos") or {}
        keys = snap.get("keys") or {}
        total = sum(keys.values())
        kpm = int(snap.get("kpm", 0))
        peak = int(snap.get("kpm_peak", 0))
        persona = self._persona(snap)
        top = sorted(combos.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        sig = (kpm, peak, total, tuple(hours), tuple(top), persona)
        if sig == self._panel_sig:
            return
        self._panel_sig = sig

        self._set_text(self.key_kpm, f"手速 {kpm} 键/分")
        self._set_text(self.key_peak, f"峰值 {peak} · {total:,} 键")

        max_h = max(hours) or 1
        cur_hour = dt.datetime.now().hour
        for i in range(24):
            h = hours[i]
            bx = PANEL_X + i * 5
            height = 18.0 * (h / max_h) if h else 0.0
            top_y = 92.0 - max(0.8, height) if h else 92.0
            self.canvas.coords(
                self.spark_bars[i],
                bx * self.s, top_y * self.s,
                (bx + 3.4) * self.s, 92 * self.s,
            )
            self.canvas.itemconfig(
                self.spark_bars[i], fill="#b4c9ff" if i == cur_hour else "#5a78d8",
            )

        top_n = top[0][1] if top else 1
        x0 = PANEL_X - 6
        for i in range(5):
            ry = 112 + i * 17
            if i < len(top):
                label, n = top[i]
                self._set_text(self.combo_labels[i], label)
                self._set_text(self.combo_counts[i], f"{n:,}")
                width = max(2.0, (PANEL_R - x0) * (n / top_n))
                self.canvas.coords(
                    self.combo_bars[i],
                    x0 * self.s, (ry + 8) * self.s,
                    (x0 + width) * self.s, (ry + 11) * self.s,
                )
            else:
                self._set_text(self.combo_labels[i], "")
                self._set_text(self.combo_counts[i], "")
                self.canvas.coords(self.combo_bars[i], 0, 0, 0, 0)

        self._set_text(self.key_persona, f"今日：{persona}")

    # -- 主循环 ------------------------------------------------------------ #
    def tick(self) -> None:
        if self._closing:
            return
        now = time.monotonic()
        snap, recent, click_ts, archived = self.tracker.snapshot()

        if archived:
            self.history[archived["day"]] = {
                "px": archived["px"], "left": archived["left"],
                "right": archived["right"], "wheel": archived.get("wheel", 0),
                "keys": archived.get("keys", {}),
                "combos": archived.get("combos", {}),
                "hours": archived.get("hours", []),
                "kpm_peak": archived.get("kpm_peak", 0),
                "typing_seconds": archived.get("typing_seconds", 0),
                "backspace": archived.get("backspace", 0),
            }

        elapsed = max(1e-3, now - self._last_tick)
        self._last_tick = now
        inst = recent / elapsed
        self._speed = self._speed * 0.65 + inst * 0.35

        self._last_snap = snap
        try:
            mx, my = self.root.winfo_pointerxy()
            inside = (self.x <= mx <= self.x + self.w_px) and (self.y <= my <= self.y + self.h_px)
        except tk.TclError:
            inside = False
        if inside:
            if not self._hover_since:
                self._hover_since = now
        else:
            self._hover_since = 0.0

        total_clicks = snap["left"] + snap["right"]
        if total_clicks != self._last_clicks:
            self._last_clicks = total_clicks
            self._joy_until = now + 0.28

        if now >= self._next_blink:
            self._blink_until = now + 0.11
            self._next_blink = now + random.uniform(2.4, 6.0)
            if self._uses_image() and not self._pinned and self._current_expr(now) == "idle":
                self._set_expr("blink", 0.14)

        self._update_texts(snap)
        self._draw_pet(now)

        if now >= self._next_flush:
            self.flush()
            self._next_flush = now + 5.0

        self.root.after(50, self.tick)

    def _raise(self) -> None:
        if self._closing:
            return
        try:
            self.root.lift()
            self.front.lift()
            hwnd = self.front.winfo_id()
            # HWND_TOPMOST + SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except tk.TclError:
            pass

    def _front_on_top(self) -> None:
        """卡片层被点到时会冒到前面，这里把内容层重新顶回去。"""
        try:
            self.front.lift()
        except tk.TclError:
            pass

    # -- 鼠标交互 ---------------------------------------------------------- #
    def _on_press(self, event):
        self._front_on_top()
        self._drag = (event.x_root, event.y_root, self.x, self.y, time.monotonic())
        self._moved = False

    def _on_motion(self, event):
        if self._drag is None:
            return
        sx, sy, wx, wy, _ = self._drag
        dx, dy = event.x_root - sx, event.y_root - sy
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.x, self.y = wx + dx, wy + dy
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.x = max(-self.w_px + 40, min(self.x, sw - 40))
        self.y = max(0, min(self.y, sh - 40))
        geo = f"+{self.x}+{self.y}"
        self.root.geometry(geo)
        self.front.geometry(geo)

    def _on_release(self, event):
        if self._drag is None:
            return
        elapsed = time.monotonic() - self._drag[4]
        moved = self._moved
        self._drag = None
        if not moved and elapsed < 0.35:
            self.set_compact(not self.compact)
            self._set_expr("happy", 1.2)
        else:
            self.save_position()
            self._set_expr("happy", 0.9)

    def _on_right(self, event):
        self._front_on_top()
        try:
            self.var_startup.set(startup_enabled())
            self.var_admin_start.set(task_installed())
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # -- 菜单 -------------------------------------------------------------- #
    def _available_poses(self):
        prefix = SKIN_PREFIX.get(self.animal)
        if prefix is None or self._skin_h is None:
            return []
        out = []
        for key, label in POSES:
            if key == "idle":
                out.append((key, label))
            elif (ASSETS_DIR / f"{prefix}_{self._skin_h}_{key}.png").exists():
                out.append((key, label))
        return out

    def _rebuild_menu(self) -> None:
        try:
            self.menu.destroy()
        except (AttributeError, tk.TclError):
            pass
        self._build_menu()

    def _build_menu(self) -> None:
        self.menu = tk.Menu(
            self.root, tearoff=0, bg="#1e1e2e", fg="#e8ecff",
            activebackground="#33334d", activeforeground="#ffffff",
            bd=0, font=("Microsoft YaHei UI", 9),
        )
        animal_menu = tk.Menu(
            self.menu, tearoff=0, bg="#1e1e2e", fg="#e8ecff",
            activebackground="#33334d", activeforeground="#ffffff",
            bd=0, font=("Microsoft YaHei UI", 9),
        )
        for key, label in ANIMALS:
            animal_menu.add_radiobutton(
                label=label, value=key, variable=self.var_animal,
                command=lambda k=key: self._set_animal(k),
            )
        self.menu.add_cascade(label="形象", menu=animal_menu)

        pose_menu = tk.Menu(
            self.menu, tearoff=0, bg="#1e1e2e", fg="#e8ecff",
            activebackground="#33334d", activeforeground="#ffffff",
            bd=0, font=("Microsoft YaHei UI", 9),
        )
        pose_menu.add_radiobutton(
            label="自动", value="", variable=self.var_pose,
            command=lambda: self._set_pose(""),
        )
        pose_menu.add_separator()
        for key, label in self._available_poses():
            pose_menu.add_radiobutton(
                label=label, value=key, variable=self.var_pose,
                command=lambda k=key: self._set_pose(k),
            )
        self.menu.add_cascade(label="表情", menu=pose_menu)

        opacity_menu = tk.Menu(
            self.menu, tearoff=0, bg="#1e1e2e", fg="#e8ecff",
            activebackground="#33334d", activeforeground="#ffffff",
            bd=0, font=("Microsoft YaHei UI", 9),
        )
        for pct in (100, 92, 86, 75, 60):
            opacity_menu.add_radiobutton(
                label=f"{pct}%", value=pct, variable=self.var_opacity,
                command=lambda p=pct: self._set_opacity(p),
            )
        opacity_menu.add_separator()
        opacity_menu.add_command(label="自定义…", command=self._open_opacity_panel)
        self.menu.add_cascade(label="背景透明度", menu=opacity_menu)

        self.menu.add_separator()
        self.menu.add_checkbutton(label="总在最前", variable=self.var_top, command=self._toggle_top)
        self.menu.add_checkbutton(label="迷你模式", variable=self.var_compact, command=self._toggle_compact)
        self.menu.add_separator()
        self.menu.add_command(label="最近 7 天…", command=self.show_history)
        self.menu.add_command(label="重置今天…", command=self.reset_today)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="开机自启", variable=self.var_startup, command=self._toggle_startup)
        self.menu.add_checkbutton(label="开机自启（管理员）", variable=self.var_admin_start,
                                  command=self._toggle_admin_startup)
        self.menu.add_command(label="打开数据文件夹", command=self._open_data)
        if not is_elevated():
            self.menu.add_separator()
            self.menu.add_command(label="以管理员身份重启…", command=self._menu_restart_admin)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

    def _set_animal(self, key: str) -> None:
        if key not in ANIMAL_KEYS:
            return
        self.animal = key
        self.cfg["animal"] = key
        self._skin = self._load_skin()
        if self._pinned and self._pinned not in {k for k, _ in self._available_poses()}:
            self._pinned = ""
            self.cfg["pose"] = ""
        self.var_pose.set(self._pinned)
        self.w_log, self.h_log = self._layout_sizes()
        self._apply_geometry()
        self.build()
        self._update_texts(self.tracker.snapshot()[0], force=True)
        self.root.after(10, self._rebuild_menu)
        self._front_on_top()
        self.save_position()

    def _set_opacity(self, pct: int) -> None:
        self.opacity = max(0.35, min(1.0, pct / 100.0))
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            return
        self.cfg["opacity"] = round(self.opacity, 2)
        self.save_position()

    def _open_opacity_panel(self) -> None:
        if self._opacity_win is not None and self._opacity_win.winfo_exists():
            self._opacity_win.lift()
            self._opacity_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._opacity_win = win
        win.title("背景透明度")
        win.configure(bg=COL_BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        s = self.s
        W, H = 300, 104
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        wx = max(20, min(self.x, sw - int(W * s) - 20))
        wy = max(20, min(self.y - int(H * s) - 12, sh - int(H * s) - 20))
        win.geometry(f"{int(W * s)}x{int(H * s)}+{wx}+{wy}")

        tk.Label(
            win, text="拖动滑块，只调整卡片背景的透明度",
            bg=COL_BG, fg=COL_SUB, font=("Microsoft YaHei UI", 8),
        ).pack(anchor="w", padx=14, pady=(12, 0))

        self.var_opacity.set(int(round(self.opacity * 100)))
        scale = tk.Scale(
            win, from_=35, to=100, orient="horizontal",
            variable=self.var_opacity,
            showvalue=1, bg=COL_BG, fg=COL_TEXT, troughcolor="#2a2a40",
            highlightthickness=0, bd=0, sliderrelief="flat",
            activebackground=COL_ACCENT, font=("Segoe UI", 9),
        )
        scale.set(int(round(self.opacity * 100)))
        scale.configure(command=self._on_opacity_change)
        scale.pack(fill="x", padx=12, pady=(2, 6))
        self._opacity_scale = scale
        scale.bind("<ButtonRelease-1>", lambda _e: self.save_position())
        win.protocol("WM_DELETE_WINDOW", self._close_opacity_panel)

    def _on_opacity_change(self, value) -> None:
        try:
            pct = int(round(float(value)))
        except (TypeError, ValueError):
            return
        self.opacity = max(0.35, min(1.0, pct / 100.0))
        try:
            self.root.attributes("-alpha", self.opacity)
        except tk.TclError:
            return
        self.cfg["opacity"] = round(self.opacity, 2)

    def _close_opacity_panel(self) -> None:
        self.save_position()
        if self._opacity_win is not None:
            try:
                self._opacity_win.destroy()
            except tk.TclError:
                pass
            self._opacity_win = None

    def _toggle_top(self) -> None:
        self.topmost = bool(self.var_top.get())
        self.root.attributes("-topmost", self.topmost)
        self.front.attributes("-topmost", self.topmost)
        self.cfg["topmost"] = self.topmost
        self.save_position()

    def _toggle_compact(self) -> None:
        self.set_compact(bool(self.var_compact.get()))

    def _toggle_startup(self) -> None:
        want = bool(self.var_startup.get())
        if not set_startup(want):
            self.var_startup.set(not want)
            messagebox.showerror(APP_NAME, "设置开机自启失败，请查看 data/mousepet.log", parent=self.root)

    def _toggle_admin_startup(self) -> None:
        """开机自启（管理员）：用计划任务，以最高权限启动。"""
        want = bool(self.var_admin_start.get())
        if is_elevated():
            ok = install_task() if want else remove_task()
            if not ok:
                self.var_admin_start.set(not want)
                messagebox.showerror(
                    APP_NAME,
                    "设置计划任务失败，详见 data/mousepet.log。\n\n"
                    "如果程序运行在受限环境里（任务计划程序被拦截），"
                    "可以改成双击「设置开机自启_管理员.vbs」，"
                    "在弹窗里点「是」。",
                    parent=self.root,
                )
                return
            if want:
                # 关掉启动文件夹里的旧自启，避免登录时双开
                set_startup(False)
                self.var_startup.set(False)
            return

        if not messagebox.askyesno(
            APP_NAME,
            "这项设置需要管理员权限。\n\n"
            "现在以管理员身份重启并设置吗？（会弹一次 UAC）",
            parent=self.root,
        ):
            self.var_admin_start.set(not want)
            return

        flag = "--install-task" if want else "--remove-task"
        if self._restart_as_admin(flag):
            self.quit()
        else:
            self.var_admin_start.set(not want)
            messagebox.showwarning(
                APP_NAME, "没有获得管理员权限，设置未生效。", parent=self.root,
            )

    def _menu_restart_admin(self) -> None:
        if self._restart_as_admin():
            self.quit()
        else:
            messagebox.showwarning(
                APP_NAME,
                "没有获得管理员权限。\n\n"
                "Windows 的 UIPI 限制会让普通权限的鼠标钩子看不到发给"
                "管理员窗口的输入。如果经常在管理员程序里操作，"
                "需要以管理员身份运行桌宠。",
                parent=self.root,
            )

    def _open_data(self) -> None:
        try:
            os.startfile(str(DATA_DIR))  # noqa: S606
        except OSError:
            messagebox.showerror(APP_NAME, f"打不开文件夹：\n{DATA_DIR}", parent=self.root)

    def _restart_as_admin(self, extra: str = "") -> bool:
        if is_elevated():
            return True
        if FROZEN:
            target = str(APP_PATH)
            params = extra
        else:
            target = str(_pythonw_path())
            params = f'"{APP_PATH}"'
            if extra:
                params += f" {extra}"
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", target, params, str(BASE_DIR), 1,
            )
        except Exception:
            log.exception("ShellExecuteW runas failed")
            return False
        return rc > 32

    def reset_today(self) -> None:
        if messagebox.askyesno(APP_NAME, "把今天的数据清零？", parent=self.root):
            self.tracker.reset_today()
            self.history.pop(dt.date.today().isoformat(), None)
            self.flush()
            self._last_clicks = 0
            self._update_texts(self.tracker.snapshot()[0], force=True)

    # -- 最近 7 天 --------------------------------------------------------- #
    def _history_rows(self):
        today = dt.date.today()
        snap = self.tracker.snapshot()[0]
        rows = []
        for i in range(7):
            day = today - dt.timedelta(days=i)
            key = day.isoformat()
            if i == 0:
                rec = {
                    "px": snap["px"], "left": snap["left"],
                    "right": snap["right"], "wheel": snap.get("wheel", 0),
                }
            else:
                rec = self.history.get(key) or {}
            rows.append((
                day, float(rec.get("px", 0.0)), int(rec.get("left", 0)),
                int(rec.get("right", 0)), int(rec.get("wheel", 0)),
            ))
        return rows

    def show_history(self) -> None:
        if self._hist_win is not None and self._hist_win.winfo_exists():
            self._hist_win.lift()
            self._hist_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._hist_win = win
        win.title("MousePet · 最近 7 天")
        win.configure(bg=COL_BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        s = self.s
        W, H = 520, 340
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        wx = max(20, min(self.x - int(150 * s), sw - int(W * s) - 20))
        wy = max(20, min(self.y - int(360 * s), sh - int(H * s) - 20))
        win.geometry(f"{int(W * s)}x{int(H * s)}+{wx}+{wy}")

        c = tk.Canvas(win, width=int(W * s), height=int(H * s), bg=COL_BG, highlightthickness=0)
        c.pack(fill="both", expand=True)

        rows = self._history_rows()
        max_px = max([r[1] for r in rows] + [1.0])

        c.create_text(24, 28, anchor="w", text="最近 7 天", fill=COL_TEXT,
                      font=("Microsoft YaHei UI", 14, "bold"))
        c.create_text(W - 24, 30, anchor="e", text="移动距离 · 左 / 滚 / 右", fill=COL_SUB,
                      font=("Microsoft YaHei UI", 8))

        y = 66
        for idx, (day, px, left, right, wheel) in enumerate(rows):
            label = "今天" if idx == 0 else ("昨天" if idx == 1 else day.strftime("%m-%d"))
            color = COL_ACCENT if idx == 0 else "#5d648f"
            c.create_text(24, y, anchor="w", text=label, fill=COL_TEXT if idx == 0 else COL_SUB,
                          font=("Microsoft YaHei UI", 9, "bold" if idx == 0 else "normal"))
            bar_w = int(160 * (px / max_px)) if px > 0 else 0
            c.create_rectangle(86, y - 7, 246, y + 7, fill="#222233", outline="")
            if bar_w > 0:
                c.create_rectangle(86, y - 7, 86 + max(2, bar_w), y + 7, fill=color, outline="")
            c.create_text(256, y, anchor="w", text=fmt_distance(px, self.px_per_cm),
                          fill=COL_TEXT if idx == 0 else COL_SUB, font=("Microsoft YaHei UI", 9))
            c.create_text(350, y, anchor="w", text=fmt_stats(left, right, wheel),
                          fill=COL_SUB, font=("Microsoft YaHei UI", 8))
            y += 34

        c.create_text(24, H - 24, anchor="w",
                      text="数据只保存在本机 · 右键小窗可重置 / 开机自启",
                      fill="#565d7d", font=("Microsoft YaHei UI", 8))

        c.scale("all", 0, 0, s, s)
        win.protocol("WM_DELETE_WINDOW", win.destroy)

    # -- 保存 / 退出 ------------------------------------------------------- #
    def save_position(self) -> None:
        self.cfg.update({"pos": [self.x, self.y], "compact": self.compact, "topmost": self.topmost})
        write_json(CONFIG_FILE, self.cfg)

    def flush(self) -> None:
        snap, _, _, archived = self.tracker.snapshot()
        if archived:
            self.history[archived["day"]] = {
                "px": archived["px"], "left": archived["left"],
                "right": archived["right"], "wheel": archived.get("wheel", 0),
                "keys": archived.get("keys", {}),
                "combos": archived.get("combos", {}),
                "hours": archived.get("hours", []),
                "kpm_peak": archived.get("kpm_peak", 0),
                "typing_seconds": archived.get("typing_seconds", 0),
                "backspace": archived.get("backspace", 0),
            }
        self.history[snap["day"]] = {
            "px": round(snap["px"], 1), "left": snap["left"],
            "right": snap["right"], "wheel": snap.get("wheel", 0),
            "keys": {str(k): v for k, v in snap.get("keys", {}).items()},
            "combos": dict(snap.get("combos", {})),
            "hours": list(snap.get("hours", [])),
            "kpm_peak": snap.get("kpm_peak", 0),
            "typing_seconds": snap.get("typing_seconds", 0),
            "backspace": snap.get("backspace", 0),
        }
        if len(self.history) > 120:
            for key in sorted(self.history)[:-120]:
                self.history.pop(key, None)
        write_json(STATS_FILE, self.history)

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.save_position()
            self.flush()
        except Exception:
            log.exception("failed to save on quit")
        self.tracker.stop()
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def main() -> None:
    # 先用 python.exe 启动时，换成 pythonw 在后台跑，避免一直挂着一个黑框
    if relaunch_without_console():
        return

    quiet = "--quiet" in sys.argv
    task_error = None
    if "--install-task" in sys.argv:
        if install_task():
            set_startup(False)  # 关掉旧启动项，避免登录时双开
            log.info("admin autostart installed")
        else:
            task_error = "设置管理员自启失败，详见 data/mousepet.log"
    if "--remove-task" in sys.argv:
        if remove_task():
            log.info("admin autostart removed")
        else:
            task_error = "取消管理员自启失败，详见 data/mousepet.log"

    enable_dpi_awareness()
    if acquire_single_instance(quiet=quiet) is None:
        return

    dpi = system_dpi()
    log.info("starting MousePet %s (dpi=%s, elevated=%s, data=%s)",
             __version__, dpi, is_elevated(), DATA_DIR)

    history = read_json(STATS_FILE, {})
    today = dt.date.today().isoformat()
    saved_today = history.get(today, {}) if isinstance(history, dict) else {}

    tracker = Tracker()
    tracker.restore(saved_today)
    tracker.start()

    root = tk.Tk()
    root.tk.call("tk", "scaling", dpi / 72.0)
    app = MousePet(root, tracker, dpi)
    root.protocol("WM_DELETE_WINDOW", app.quit)
    if task_error and not quiet:
        root.after(500, lambda: messagebox.showerror(APP_NAME, task_error, parent=root))
    if "--history" in sys.argv:
        root.after(500, app.show_history)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.quit()


if __name__ == "__main__":
    main()
