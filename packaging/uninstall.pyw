#!/usr/bin/env python3
from __future__ import annotations
import json, os, shutil, subprocess, sys, time, urllib.request
from pathlib import Path
import winreg
import tkinter as tk
from tkinter import messagebox

APP_NAME = "NovelAI Artist Ranker"
INSTALL_DIR = Path(__file__).resolve().parent.parent
LOCAL_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME

def request_shutdown():
    try:
        request = urllib.request.Request("http://127.0.0.1:7860/api/public/shutdown", data=b"{}", method="POST", headers={"Content-Type":"application/json"})
        urllib.request.urlopen(request, timeout=3).read()
        time.sleep(1.4)
    except Exception:
        pass

def remove_autostart():
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.DeleteValue(key, APP_NAME)
    except Exception:
        pass

def main():
    root=tk.Tk(); root.withdraw()
    preserve=messagebox.askyesno(
        "Uninstall Artist Ranker",
        "Preserve rankings, history, prompts, favorites, settings, and generated images?\n\n"
        "Choose Yes unless you want to erase the default local data folder too.",
        default="yes")
    if not messagebox.askokcancel("Confirm uninstall", "Remove the Artist Ranker program from this PC?"):
        return 0
    request_shutdown(); remove_autostart()
    cleanup=Path(os.environ.get("TEMP", str(Path.home()))) / f"artist-ranker-uninstall-{os.getpid()}.cmd"
    desktop_link = Path.home() / "Desktop" / "NovelAI Artist Ranker.lnk"
    programs_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
    lines=[
        "@echo off",
        "timeout /t 3 /nobreak >nul",
        f'del /q "{desktop_link}" 2>nul',
        f'rmdir /s /q "{programs_dir}" 2>nul',
        f'rmdir /s /q "{INSTALL_DIR}"',
    ]
    if not preserve:
        lines.append(f'rmdir /s /q "{LOCAL_ROOT}"')
    lines.append('del "%~f0"')
    cleanup.write_text("\r\n".join(lines)+"\r\n",encoding="utf-8")
    subprocess.Popen(["cmd.exe","/c",str(cleanup)],creationflags=0x08000000)
    messagebox.showinfo("Artist Ranker", "Uninstall scheduled. User data was preserved." if preserve else "Uninstall and default local-data removal scheduled.")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
