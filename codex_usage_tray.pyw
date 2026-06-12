# -*- coding: utf-8 -*-
"""Codex 用量系統匣監控工具

讀取本機 Codex CLI 的 OAuth token，定時查詢 ChatGPT 後端的用量端點，
在系統匣顯示 5 小時 session 與每週額度的使用率，
並提供一個永遠置頂的懸浮小窗直接顯示數字。

對 ~/.codex 完全唯讀：只讀 auth.json、不寫回任何檔案。
Codex 的 access token 約一小時過期，401/403 時會用 refresh token
換一顆新的（只放在記憶體，不會動到 auth.json）。
"""

import ctypes
import json
import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import pystray

AUTH_PATH = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
CONFIG_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / "CodexUsage" / "config.json"
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex CLI 的官方 OAuth client id
POLL_SECONDS = 180            # 查太頻繁會被端點限流（429）
RATE_LIMIT_WAIT = 300         # 被限流時至少退避這麼多秒
STALE_AFTER_MIN = 30          # 超過這麼多分鐘沒成功更新才顯示「?」
NOTIFY_THRESHOLDS = (85, 95)  # 用量 % 達到時跳 toast 通知（每個重置週期各一次）

COLOR_OK = "#2e9e44"
COLOR_WARN = "#e0a000"
COLOR_DANGER = "#d22f2f"
COLOR_STALE = "#7a7a7a"

state_lock = threading.Lock()
state = {
    "ok": False,
    "session": None,        # 主要窗口（付費 5 小時、free 30 天）使用率 %
    "session_reset": None,  # datetime（本地時區）
    "session_win": None,    # 窗口長度（秒），用來決定顯示標籤 5h / 7d / 30d
    "weekly": None,         # 次要窗口（付費為每週；free 方案沒有）
    "weekly_reset": None,
    "weekly_win": None,
    "error": "尚未更新",
    "last_success": None,   # 最後一次成功更新的時間
}
# 已通知的門檻，key 為 (metric, resets_at_iso)，value 為 set of thresholds
notified = {}

# auth.json 裡的 token 過期後，refresh 換到的新 token 只放這裡（不寫回檔案）
fresh_token = {"access_token": None}

config = {"overlay": True, "x": None, "y": None}

stop_event = threading.Event()
refresh_event = threading.Event()


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"查詢過於頻繁，{retry_after} 秒後重試")
        self.retry_after = retry_after


def ensure_single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "CodexUsageTrayMutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)


def load_config():
    try:
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def save_config():
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    except OSError:
        pass


def load_tokens():
    try:
        return json.loads(AUTH_PATH.read_text(encoding="utf-8"))["tokens"]
    except FileNotFoundError:
        raise RuntimeError("找不到 ~/.codex/auth.json，先執行一次 codex 登入")


def refresh_access_token(refresh_token):
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def request_usage(access_token, account_id):
    return requests.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "Accept": "application/json",
        },
        timeout=10,
    )


def fetch_usage():
    """回傳 (主窗口, 次窗口)，每個窗口為 (used%, reset_dt, window_secs)，失敗丟例外。"""
    tokens = load_tokens()
    account_id = tokens.get("account_id", "")
    access_token = fresh_token["access_token"] or tokens["access_token"]

    resp = request_usage(access_token, account_id)
    if resp.status_code in (401, 403):
        # auth.json 的 token 過期（約一小時），用 refresh token 換新的再試一次
        fresh_token["access_token"] = refresh_access_token(tokens["refresh_token"])
        resp = request_usage(fresh_token["access_token"], account_id)
    if resp.status_code == 429:
        raise RateLimited(int(resp.headers.get("retry-after", RATE_LIMIT_WAIT)))
    resp.raise_for_status()
    rl = resp.json().get("rate_limit") or {}

    def parse(block):
        if not block:
            return None, None, None
        reset = None
        if block.get("reset_at"):
            reset = datetime.fromtimestamp(block["reset_at"], tz=timezone.utc).astimezone()
        return block.get("used_percent"), reset, block.get("limit_window_seconds")

    return parse(rl.get("primary_window")), parse(rl.get("secondary_window"))


def pick_color(session, weekly):
    worst = max(x for x in (session, weekly, 0) if x is not None)
    if worst >= 85:
        return COLOR_DANGER
    if worst >= 60:
        return COLOR_WARN
    return COLOR_OK


def load_font(size):
    for name in ("arialbd.ttf", "arial.ttf", "msyhbd.ttc", "msyh.ttc"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_icon(text, color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, 63, 63], radius=14, fill=color)
    font = load_font(40 if len(text) <= 2 else 30)
    left, top, right, bottom = d.textbbox((0, 0), text, font=font)
    d.text(
        ((64 - (right - left)) / 2 - left, (64 - (bottom - top)) / 2 - top),
        text, font=font, fill="white",
    )
    return img


def fmt_pct(v):
    return "?" if v is None else f"{v:.0f}%"


def win_label(secs, fallback):
    """把窗口長度轉成顯示標籤：18000→5h、604800→7d、2592000→30d。"""
    if not secs:
        return fallback
    if secs < 48 * 3600:
        return f"{secs / 3600:.0f}h"
    return f"{secs / 86400:.0f}d"


def fmt_reset(dt, with_date=False):
    if dt is None:
        return "?"
    return dt.strftime("%m/%d %H:%M") if with_date else dt.strftime("%H:%M")


def maybe_notify(icon):
    """超過門檻時通知，每個 (指標, 重置週期) 只通知一次。"""
    with state_lock:
        items = [
            (f"{win_label(state['session_win'], '主要')} 額度",
             state["session"], state["session_reset"]),
            (f"{win_label(state['weekly_win'], '次要')} 額度",
             state["weekly"], state["weekly_reset"]),
        ]
    for name, pct, reset in items:
        if pct is None:
            continue
        # 取整到分鐘：API 回傳的重置時間每次查詢可能略有不同，
        # 直接拿原值當 key 會把同一個週期誤判成新週期而重複通知
        key = (name, reset.strftime("%Y-%m-%d %H:%M") if reset else "")
        done = notified.setdefault(key, set())
        for th in NOTIFY_THRESHOLDS:
            if pct >= th and th not in done:
                done.add(th)
                try:
                    icon.notify(
                        f"{name}已用 {pct:.0f}%（{fmt_reset(reset, True)} 重置）",
                        "Codex 用量提醒",
                    )
                except Exception:
                    pass


def is_fresh():
    """最後一次成功更新是否還夠新（呼叫端需持有 state_lock）。"""
    return (state["last_success"] is not None and
            (datetime.now() - state["last_success"]).total_seconds() < STALE_AFTER_MIN * 60)


def update_once(icon):
    """更新一次，回傳下次輪詢前要等的秒數。"""
    wait = POLL_SECONDS
    try:
        (s_pct, s_reset, s_win), (w_pct, w_reset, w_win) = fetch_usage()
        with state_lock:
            state.update(ok=True, error=None,
                         session=s_pct, session_reset=s_reset, session_win=s_win,
                         weekly=w_pct, weekly_reset=w_reset, weekly_win=w_win,
                         last_success=datetime.now())
    except RateLimited as e:
        wait = max(e.retry_after, RATE_LIMIT_WAIT)
        with state_lock:
            state.update(ok=False, error=str(e))
    except Exception as e:
        with state_lock:
            state.update(ok=False, error=str(e)[:80])

    with state_lock:
        if state["ok"] or is_fresh():
            # 暫時抓不到新資料時沿用最後一次成功的數字，避免一直閃問號
            icon.icon = render_icon(
                f"{state['session']:.0f}" if state["session"] is not None else "?",
                pick_color(state["session"], state["weekly"]),
            )
            parts = []
            for pct, reset, win in (
                (state["session"], state["session_reset"], state["session_win"]),
                (state["weekly"], state["weekly_reset"], state["weekly_win"]),
            ):
                if pct is not None or win is not None:
                    parts.append(f"{win_label(win, '?')} {fmt_pct(pct)}"
                                 f" ({fmt_reset(reset, bool(win and win > 86400))})")
            icon.title = ("Codex 用量  " + " | ".join(parts)
                          + ("" if state["ok"] else "（暫停更新）"))
        else:
            icon.icon = render_icon("?", COLOR_STALE)
            icon.title = f"Codex 用量：{state['error']}"
    icon.update_menu()

    if state["ok"]:
        maybe_notify(icon)
    return wait


def poll_loop(icon):
    while not stop_event.is_set():
        wait = update_once(icon)
        refresh_event.wait(wait)
        refresh_event.clear()


def menu_session(_item):
    with state_lock:
        return (f"{win_label(state['session_win'], '主要')}："
                f"{fmt_pct(state['session'])}"
                f"（{fmt_reset(state['session_reset'], bool(state['session_win'] and state['session_win'] > 86400))} 重置）")


def menu_weekly(_item):
    with state_lock:
        if state["weekly"] is None and state["weekly_win"] is None:
            return "次要額度：無（free 方案只有單一窗口）"
        return (f"{win_label(state['weekly_win'], '次要')}："
                f"{fmt_pct(state['weekly'])}"
                f"（{fmt_reset(state['weekly_reset'], True)} 重置）")


def on_refresh(icon, _item):
    refresh_event.set()


def on_toggle_overlay(_icon, _item):
    config["overlay"] = not config["overlay"]
    save_config()


def on_quit(icon, _item):
    stop_event.set()
    refresh_event.set()
    icon.stop()


# ---------- 懸浮顯示窗（tkinter，跑在主執行緒） ----------

class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)        # 無邊框、不出現在工作列
        self.root.attributes("-topmost", True)  # 永遠置頂
        self.root.attributes("-alpha", 0.88)
        self.label = tk.Label(
            self.root, text="Codex …", fg="white", bg=COLOR_STALE,
            font=("Segoe UI", 11, "bold"), padx=10, pady=4,
        )
        self.label.pack()
        self._drag = None
        self.label.bind("<ButtonPress-1>", self._drag_start)
        self.label.bind("<B1-Motion>", self._drag_move)
        self.label.bind("<ButtonRelease-1>", self._drag_end)
        self.label.bind("<Button-3>", self._hide)  # 右鍵：隱藏（可從系統匣選單再開）

        self.root.update_idletasks()
        if config["x"] is not None and config["y"] is not None:
            self.root.geometry(f"+{config['x']}+{config['y']}")
        else:
            # 預設貼在右下角、工作列上方
            x = self.root.winfo_screenwidth() - self.root.winfo_width() - 16
            y = self.root.winfo_screenheight() - self.root.winfo_height() - 56
            self.root.geometry(f"+{x}+{y}")
        self._tick()

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _drag_end(self, _e):
        self._drag = None
        config["x"], config["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config()

    def _hide(self, _e):
        config["overlay"] = False
        save_config()

    def _tick(self):
        if stop_event.is_set():
            save_config()
            self.root.destroy()
            return
        with state_lock:
            if state["ok"] or is_fresh():
                segs = []
                for pct, win in ((state["session"], state["session_win"]),
                                 (state["weekly"], state["weekly_win"])):
                    if pct is not None or win is not None:
                        segs.append(f"{win_label(win, '?')} {fmt_pct(pct)}")
                text = "   ".join(segs) or "Codex ?"
                if not state["ok"]:
                    text += " *"  # 星號＝顯示的是最後一次成功的數字
                color = pick_color(state["session"], state["weekly"])
            else:
                text = "Codex ?"
                color = COLOR_STALE
        self.label.config(text=text, bg=color)
        if config["overlay"]:
            self.root.deiconify()
        else:
            self.root.withdraw()
        self.root.after(500, self._tick)

    def run(self):
        self.root.mainloop()


def main():
    ensure_single_instance()
    load_config()
    icon = pystray.Icon(
        "CodexUsage",
        icon=render_icon("…", COLOR_STALE),
        title="Codex 用量：載入中",
        menu=pystray.Menu(
            pystray.MenuItem(menu_session, None, enabled=False),
            pystray.MenuItem(menu_weekly, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("懸浮顯示", on_toggle_overlay,
                             checked=lambda _i: config["overlay"]),
            pystray.MenuItem("立即更新", on_refresh),
            pystray.MenuItem("結束", on_quit),
        ),
    )
    threading.Thread(target=poll_loop, args=(icon,), daemon=True).start()
    icon.run_detached()  # 系統匣跑在獨立執行緒，主執行緒留給 tkinter
    Overlay().run()


if __name__ == "__main__":
    main()
