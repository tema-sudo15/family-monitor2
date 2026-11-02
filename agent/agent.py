"""Агент родительского контроля для Windows (один exe).

Что делает:
  * раз в 5 секунд запоминает активную программу и заголовок окна,
    раз в минуту отправляет сводку на сервер родителя;
  * когда родитель открывает страницу «Смотреть экран», присылает снимки экрана
    примерно раз в секунду (в остальное время экран не передаётся);
  * не читает переписку и не записывает нажатия клавиш.

Запуск exe без параметров на ПК сына устанавливает агента (копирует в
%LOCALAPPDATA%\\FamilyMonitor и добавляет в автозапуск текущего пользователя).
Удаление: agent.exe --uninstall
"""
import ctypes
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import psutil
import requests

SAMPLE_INTERVAL = 5      # как часто проверять активное окно, сек
SEND_INTERVAL = 60       # как часто отправлять сводку, сек
IDLE_LIMIT = 120         # нет ввода дольше этого — считаем, что сына нет за ПК
MAX_PENDING = 5000       # сколько записей хранить, пока нет связи с сервером
POLL_INTERVAL = 2        # как часто спрашивать сервер, смотрит ли родитель экран
FRAME_INTERVAL = 0.35    # интервал между кадрами при просмотре, сек (~3 кадра/сек)
FRAME_WIDTH = 1280       # ширина кадра (больше — чётче, но тяжелее)
JPEG_QUALITY = 55

APP_NAME = "FamilyMonitor"
INSTALL_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetTickCount.restype = wintypes.DWORD

streaming = threading.Event()  # установлено, пока родитель смотрит экран


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


# ---------- Конфиг ----------

def exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config() -> dict:
    candidates = [
        os.path.join(exe_dir(), "config.json"),                         # можно переопределить файлом рядом
        os.path.join(getattr(sys, "_MEIPASS", exe_dir()), "embedded_config.json"),  # зашит в exe
        os.path.join(exe_dir(), "embedded_config.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg.setdefault("device_name", socket.gethostname())
            cfg.setdefault("show_indicator", True)
            cfg["server_url"] = cfg["server_url"].rstrip("/")
            return cfg
    raise SystemExit("Не найден конфиг (embedded_config.json)")


# ---------- Установка / удаление ----------

def message(text: str):
    user32.MessageBoxW(0, text, "Family Monitor", 0x40)


def in_install_dir() -> bool:
    return os.path.normcase(os.path.dirname(sys.executable)) == os.path.normcase(INSTALL_DIR)


def kill_other_agents():
    subprocess.run(
        ["taskkill", "/F", "/IM", "agent.exe", "/FI", f"PID ne {os.getpid()}"],
        capture_output=True, creationflags=0x08000000,
    )


def install():
    import winreg

    os.makedirs(INSTALL_DIR, exist_ok=True)
    target = os.path.join(INSTALL_DIR, "agent.exe")
    try:
        shutil.copy2(sys.executable, target)
    except PermissionError:
        kill_other_agents()
        time.sleep(1.5)
        shutil.copy2(sys.executable, target)

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE,
    )
    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{target}"')
    winreg.CloseKey(key)

    subprocess.Popen([target], creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED | NEW_GROUP
    message("Установлено. Агент запущен и будет стартовать вместе с Windows.\n\nУдалить: agent.exe --uninstall")


def uninstall():
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, APP_NAME)
        winreg.CloseKey(key)
    except OSError:
        pass
    kill_other_agents()
    # папку удаляем чуть позже, когда процесс завершится
    subprocess.Popen(
        f'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /S /Q "{INSTALL_DIR}"',
        creationflags=0x08000000, shell=True,
    )
    message("Агент удалён.")


def already_running() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\FamilyMonitorAgent")
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


# ---------- Учёт активности ----------

def idle_seconds() -> float:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0, kernel32.GetTickCount() - info.dwTime) / 1000.0


def foreground():
    """Возвращает (имя процесса, заголовок окна) или None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value.strip()

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        name = "unknown"
    return name, title[:200]


def send(cfg: dict, events: list) -> bool:
    try:
        r = requests.post(
            cfg["server_url"] + "/api/report",
            json={"device": cfg["device_name"], "events": events},
            headers={"X-Agent-Key": cfg["agent_key"]},
            timeout=15,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def activity_loop(cfg: dict):
    pending: list = []
    bucket: dict = {}
    bucket_start = int(time.time())
    last_send = time.time()

    while True:
        time.sleep(SAMPLE_INTERVAL)
        try:
            if idle_seconds() < IDLE_LIMIT:
                fg = foreground()
                if fg:
                    bucket[fg] = bucket.get(fg, 0) + SAMPLE_INTERVAL

            if time.time() - last_send >= SEND_INTERVAL:
                for (proc, title), secs in bucket.items():
                    pending.append({"ts": bucket_start, "seconds": secs, "process": proc, "title": title})
                bucket = {}
                bucket_start = int(time.time())
                last_send = time.time()

                if pending:
                    if send(cfg, pending):
                        pending = []
                    elif len(pending) > MAX_PENDING:
                        pending = pending[-MAX_PENDING:]
        except Exception:
            time.sleep(1)


# ---------- Трансляция экрана ----------

def stream_loop(cfg: dict):
    headers = {"X-Agent-Key": cfg["agent_key"]}
    base = cfg["server_url"]
    device = cfg["device_name"]

    while True:
        try:
            import mss
            from PIL import Image

            sess = requests.Session()
            with mss.mss() as sct:
                while True:
                    try:
                        r = sess.post(base + "/api/agent/poll", json={"device": device},
                                      headers=headers, timeout=10)
                        want = r.status_code == 200 and bool(r.json().get("stream"))
                    except Exception:
                        want = False

                    if not want:
                        streaming.clear()
                        time.sleep(POLL_INTERVAL)
                        continue

                    streaming.set()
                    end = time.time() + POLL_INTERVAL
                    while time.time() < end:
                        t0 = time.time()
                        try:
                            shot = sct.grab(sct.monitors[1])
                            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                            if img.width > FRAME_WIDTH:
                                img = img.resize((FRAME_WIDTH, int(img.height * FRAME_WIDTH / img.width)))
                            buf = io.BytesIO()
                            img.save(buf, "JPEG", quality=JPEG_QUALITY)
                            sess.post(base + "/api/frame", params={"device": device},
                                      data=buf.getvalue(),
                                      headers={**headers, "Content-Type": "image/jpeg"}, timeout=10)
                        except Exception:
                            time.sleep(1)
                        time.sleep(max(0.0, FRAME_INTERVAL - (time.time() - t0)))
        except Exception:
            streaming.clear()
            time.sleep(5)


def indicator_loop():
    """Маленькая красная плашка в углу экрана, пока родитель смотрит экран."""
    try:
        import tkinter as tk
    except Exception:
        return
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#c62828")
    tk.Label(root, text="● Родитель видит экран", fg="white", bg="#c62828",
             font=("Segoe UI", 9, "bold"), padx=8, pady=3).pack()
    root.update_idletasks()
    root.geometry(f"+{root.winfo_screenwidth() - root.winfo_reqwidth() - 10}+10")
    root.withdraw()
    state = {"visible": False}

    def check():
        want = streaming.is_set()
        if want and not state["visible"]:
            root.deiconify()
            root.attributes("-topmost", True)
            state["visible"] = True
        elif not want and state["visible"]:
            root.withdraw()
            state["visible"] = False
        root.after(500, check)

    check()
    root.mainloop()


# ---------- Запуск ----------

def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    args = sys.argv[1:]
    if "--uninstall" in args:
        uninstall()
        return

    # Двойной клик по exe вне папки установки = установка
    if getattr(sys, "frozen", False) and "--run" not in args and not in_install_dir():
        install()
        return

    if already_running():
        return

    cfg = load_config()
    threading.Thread(target=stream_loop, args=(cfg,), daemon=True).start()
    if cfg.get("show_indicator", True):
        threading.Thread(target=indicator_loop, daemon=True).start()
    activity_loop(cfg)


if __name__ == "__main__":
    main()
