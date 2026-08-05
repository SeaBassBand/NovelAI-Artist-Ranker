#!/usr/bin/env python3
"""Windows desktop launcher and tray controller for NovelAI Artist Ranker."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import datetime as _dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import winreg

APP_NAME = "NovelAI Artist Ranker"
APP_VERSION = "2.6.0"
SERVER = "http://127.0.0.1:7860"
MUTEX_NAME = r"Local\NovelAIArtistRankerLauncher"
AUTOSTART_VALUE = "NovelAI Artist Ranker"
WM_TRAY = 0x8000 + 21
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0800, 0x0008
TPM_RIGHTBUTTON = 0x0002
IDI_APPLICATION = 32512
ERROR_ALREADY_EXISTS = 183
CMD_OPEN = 1001
CMD_DUEL = 1002
CMD_PAUSE = 1003
CMD_STATUS = 1004
CMD_DIAGNOSTICS = 1005
CMD_AUTOSTART = 1006
CMD_LOG = 1007
CMD_EXIT = 1008

INSTALL_DIR = Path(__file__).resolve().parent.parent
APP_DIR = INSTALL_DIR / "app"
RUNTIME_DIR = INSTALL_DIR / "runtime"
PYTHON_EXE = RUNTIME_DIR / "python.exe"
RANKER_SCRIPT = APP_DIR / "artist_elo_ranker_buffered.py"

def _apply_local_path_overrides() -> None:
    """Load optional machine-local paths without baking them into public packages."""
    path = RUNTIME_DIR / "user_paths.json"
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    for field, environment_name in (
        ("data_root", "ARTIST_RANKER_DATA_DIR"),
        ("local_app_data", "LOCALAPPDATA"),
    ):
        value = str(payload.get(field, "") or "").strip()
        if not value:
            continue
        candidate = Path(os.path.expandvars(value)).expanduser()
        if candidate.is_absolute():
            os.environ[environment_name] = str(candidate)

_apply_local_path_overrides()
LOCAL_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME
LOG_DIR = LOCAL_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / ("launcher-" + _dt.date.today().isoformat() + ".log")

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

def _log(message: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")

def _request(path: str, *, method: str = "GET", timeout: float = 4.0) -> dict:
    request = urllib.request.Request(
        SERVER + path,
        data=b"{}" if method != "GET" else None,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def _healthy() -> bool:
    try:
        return bool(_request("/api/public/health", timeout=1.2).get("ok"))
    except Exception:
        return False

def _message(title: str, message: str, error: bool = False) -> None:
    flags = 0x10 if error else 0x40
    user32.MessageBoxW(None, str(message), str(title), flags)

def _open_path(path: Path) -> None:
    if path.exists():
        subprocess.Popen(["explorer.exe", str(path)], creationflags=0x08000000)

def _autostart_command() -> str:
    return f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}"'

def _autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE)
        return str(value).strip().casefold() == _autostart_command().casefold()
    except Exception:
        return False

def _set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass

def _start_ranker() -> subprocess.Popen | None:
    if _healthy():
        _log("Attached to an already-running ranker process.")
        return None
    if not PYTHON_EXE.is_file() or not RANKER_SCRIPT.is_file():
        raise FileNotFoundError("The bundled Python runtime or ranker program is missing. Reinstall Artist Ranker.")
    env = os.environ.copy()
    env.update({
        "ARTIST_RANKER_NO_AUTO_BROWSER": "1",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    })
    stream = LOG_FILE.open("a", encoding="utf-8", buffering=1)
    _log(f"Starting bundled server with {PYTHON_EXE}")
    try:
        return subprocess.Popen(
            [str(PYTHON_EXE), str(RANKER_SCRIPT)],
            cwd=str(APP_DIR),
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000 | 0x00000200,
        )
    finally:
        stream.close()

def _wait_for_health(process: subprocess.Popen | None, timeout: float = 100.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy():
            return True
        if process is not None and process.poll() is not None:
            _log(f"Server exited during startup with code {process.returncode}.")
            return False
        time.sleep(0.45)
    _log("Server startup health check timed out.")
    return False

def _health_window(process: subprocess.Popen | None) -> bool:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return _wait_for_health(process)

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("520x230")
    root.resizable(False, False)
    root.configure(padx=24, pady=20)
    title = ttk.Label(root, text="Starting Artist Ranker", font=("Segoe UI", 17, "bold"))
    title.pack(anchor="w")
    detail = ttk.Label(root, text="Checking the bundled runtime and local server…", wraplength=465)
    detail.pack(anchor="w", pady=(10, 15))
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x")
    bar.start(12)
    path_label = ttk.Label(root, text=str(INSTALL_DIR), foreground="#666666", wraplength=465)
    path_label.pack(anchor="w", pady=(13, 0))
    state = {"ready": False, "done": False}
    started = time.monotonic()

    def poll() -> None:
        if _healthy():
            state.update(ready=True, done=True)
            detail.configure(text="Artist Ranker is ready. Opening it in your browser…")
            bar.stop()
            root.after(450, root.destroy)
            return
        if process is not None and process.poll() is not None:
            state["done"] = True
            detail.configure(text=f"Startup failed (exit code {process.returncode}). Open the log for details.")
            bar.stop()
            return
        elapsed = time.monotonic() - started
        if elapsed > 100:
            state["done"] = True
            detail.configure(text="Startup timed out. Open the log for details, then restart the app.")
            bar.stop()
            return
        detail.configure(text=f"Starting local server… {int(elapsed)} seconds")
        root.after(450, poll)

    buttons = ttk.Frame(root)
    buttons.pack(fill="x", side="bottom")
    ttk.Button(buttons, text="Open log", command=lambda: _open_path(LOG_FILE)).pack(side="left")
    ttk.Button(buttons, text="Exit", command=root.destroy).pack(side="right")
    root.after(120, poll)
    root.mainloop()
    return bool(state["ready"])

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
    ]

# Explicit Win32 signatures prevent pointer-sized handles from being truncated on 64-bit Python.
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
kernel32.ReleaseMutex.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

class TrayController:
    def __init__(self) -> None:
        self.hwnd = None
        self.nid = None
        self._wndproc = WNDPROC(self._window_proc)
        self.class_name = "NovelAIArtistRankerTrayV253"

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == WM_TRAY and int(lparam) in (WM_RBUTTONUP, WM_LBUTTONUP):
            self._show_menu()
            return 0
        if message == WM_COMMAND:
            self._command(int(wparam) & 0xFFFF)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        paused = False
        try:
            paused = bool(_request("/api/public/status").get("generation_paused"))
        except Exception:
            pass
        user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "Open ranker")
        user32.AppendMenuW(menu, MF_STRING, CMD_DUEL, "Open Duel")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_PAUSE, "Resume generation" if paused else "Pause generation")
        user32.AppendMenuW(menu, MF_STRING, CMD_STATUS, "View status")
        user32.AppendMenuW(menu, MF_STRING, CMD_DIAGNOSTICS, "Export diagnostics")
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if _autostart_enabled() else 0), CMD_AUTOSTART, "Start with Windows")
        user32.AppendMenuW(menu, MF_STRING, CMD_LOG, "Open launcher log")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "Exit safely")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)

    def _command(self, command: int) -> None:
        try:
            if command == CMD_OPEN:
                webbrowser.open(SERVER + "/ranker/")
            elif command == CMD_DUEL:
                webbrowser.open(SERVER + "/duel")
            elif command == CMD_PAUSE:
                result = _request("/api/public/generation/toggle", method="POST")
                _message(APP_NAME, "Generation paused." if result.get("generation_paused") else "Generation resumed.")
            elif command == CMD_STATUS:
                status = _request("/api/public/status")
                _message(APP_NAME, (
                    f"Version: {status.get('version')}\n"
                    f"Generation: {'Paused' if status.get('generation_paused') else 'Running'}\n"
                    f"Buffer: {status.get('buffer_ready')} / {status.get('buffer_target')}\n"
                    f"Completed duels: {status.get('completed_duels')}\n"
                    f"API key: {'Configured' if status.get('api_key_configured') else 'Not configured'}\n"
                    f"Data: {status.get('data_root')}"
                ))
            elif command == CMD_DIAGNOSTICS:
                result = _request("/api/public/diagnostics/export", method="POST", timeout=40)
                path = Path(str(result.get("path", "")))
                _message(APP_NAME, f"Diagnostics exported:\n{path}")
                _open_path(path.parent)
            elif command == CMD_AUTOSTART:
                enabled = not _autostart_enabled()
                _set_autostart(enabled)
                _message(APP_NAME, "Start with Windows enabled." if enabled else "Start with Windows disabled.")
            elif command == CMD_LOG:
                _open_path(LOG_FILE)
            elif command == CMD_EXIT:
                try:
                    _request("/api/public/shutdown", method="POST")
                except Exception:
                    pass
                self.stop()
        except Exception as exc:
            _log(f"Tray command {command} failed: {type(exc).__name__}: {exc}")
            _message(APP_NAME, f"{type(exc).__name__}: {exc}", error=True)

    def run(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = instance
        wc.hIcon = user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
        wc.lpszClassName = self.class_name
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, self.class_name, APP_NAME, 0, 0, 0, 0, 0, None, None, instance, None)
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))
        nid.szTip = APP_NAME
        self.nid = nid
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        if self.nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)

def main() -> int:
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex:
        raise ctypes.WinError()
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        webbrowser.open(SERVER + "/ranker/")
        return 0
    try:
        process = _start_ranker()
        if not _health_window(process):
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=4)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            _message(APP_NAME, f"Artist Ranker could not start.\n\nLog:\n{LOG_FILE}", error=True)
            _open_path(LOG_FILE)
            return 1
        webbrowser.open(SERVER + "/ranker/")
        TrayController().run()
        return 0
    except Exception as exc:
        _log(f"Fatal launcher error: {type(exc).__name__}: {exc}")
        _message(APP_NAME, f"{type(exc).__name__}: {exc}\n\nLog:\n{LOG_FILE}", error=True)
        return 1
    finally:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)

if __name__ == "__main__":
    raise SystemExit(main())
