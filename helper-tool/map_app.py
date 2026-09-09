"""Lands of Lords — Map Scanner: a standalone desktop app for running the
continent wave-scan (locally or on a remote server over SSH) and viewing
the results on an interactive map.

Run:
    python map_app.py

Needs: Pillow, keyring, paramiko (pip install -r requirements.txt).
"""
import csv
import gzip
import json
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
import urllib.error
import urllib.request
import uuid
import webbrowser
import winsound
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox, filedialog, simpledialog

from lol_api import LolClient, ProtocolError, format_map_coords, MINERAL_GROUND_TYPES
import continent_scan_cli as cli
import profiles
from remote import RemoteScanManager, RemoteError
import styles
from fast_map import FastMapView

try:
    import keyring
    import keyring.errors
except ImportError:
    keyring = None

# В PyInstaller --onefile-сборке __file__ указывает на временную папку
# распаковки (_MEIPASS) — она создаётся заново при каждом запуске и
# удаляется при выходе. Всё, что должно переживать перезапуск (прогресс
# скана, сохранённый логин, лог-файл), обязано лежать рядом с настоящим
# .exe (sys.executable), а не рядом с этим временным __file__ — иначе
# каждый новый запуск тихо начинает с нуля, теряя весь накопленный
# прогресс из предыдущего сеанса (обнаружено вживую: помощник накопил
# 20057/22991 точек, перезапустил на новой версии — счётчик слетел на
# 216/21371, старый прогресс оказался заперт в уже удалённой temp-папке).
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

PROFILE_DATA_DIR = APP_DIR / "profile_data"
PROFILE_DATA_DIR.mkdir(exist_ok=True)

KEYRING_SERVICE = "landsoflords-map-scanner"

# Приём точек в реальном времени от помощников — POST /api/submit-batch на
# том же воркере, что раздаёт карту (см. cloudflare_worker/src/index.js).
# Общий разделяемый токен (не персональный на помощника) — соответствует
# масштабу: маленький круг доверенных людей, не публичный API. Батчи не
# сливаются автоматически — review_submission.py проверяет их вручную
# (см. pull_submissions.py) прежде чем что-либо попадает в основной скан.
SUBMIT_URL = "https://lol-continent-map.rammthaok.workers.dev/api/submit-batch"
SUBMIT_TOKEN = "wrgUQa_ymUQrpLm1WYmzbDqlDlBqwEOT"
SUBMIT_BATCH_SIZE = 5000  # точек — что раньше наступит, то и шлём
SUBMIT_INTERVAL_SECONDS = 3600

# Список уже известных координат основного скана (см. build_known_cells.py)
# — скачивается перед стартом локального скана и подмешивается в "seen",
# чтобы фронтир помощника сразу шёл в неисследованное, а не гонял повторные
# запросы по территории, которую основной скан уже покрыл. Не жёсткая
# зависимость: если скачать не удалось (сеть, файл не обновлён и т.п.),
# скан просто стартует без подсказки, как раньше.
KNOWN_CELLS_URL = "https://lol-continent-map.rammthaok.workers.dev/known_cells.json.gz"


def load_saved_password(username):
    """Без гарантий — вернёт None, если keyring недоступен, не настроен
    (нет системного хранилища) или для этого логина ничего не сохранено."""
    if not keyring or not username:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except keyring.errors.KeyringError:
        return None


def save_password(username, password):
    if not keyring or not username:
        return
    try:
        keyring.set_password(KEYRING_SERVICE, username, password)
    except keyring.errors.KeyringError:
        pass


def forget_password(username):
    if not keyring or not username:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, username)
    except keyring.errors.KeyringError:
        pass


# Логин и адрес прокси — не игровой пароль, поэтому просто локальный
# JSON-файл, а не keyring (пароль отдельно, в системном хранилище — см.
# выше). Запоминаем только последние использованные значения, чтобы поля
# не приходилось перепечатывать каждый раз, а не полноценный список
# профилей.
LOCAL_LOGIN_PATH = APP_DIR / "local_login.json"
# Дублирует всё, что попадает в окошко "Лог" в GUI, построчно с меткой
# времени — чтобы помощник мог прислать этот файл при проблемах, не
# копируя текст из окна руками (и чтобы история не терялась при
# закрытии приложения, в отличие от log_text).
LOG_FILE_PATH = APP_DIR / "map_scanner.log"


def _load_local_settings():
    try:
        return json.loads(LOCAL_LOGIN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_saved_username():
    return _load_local_settings().get("username") or ""


def load_saved_proxy():
    return _load_local_settings().get("proxy") or ""


def load_saved_share_progress():
    # По умолчанию True (не False!) — этот exe вообще существует только
    # ради шаринга прогресса на общий проект, так что "забыл" не должно
    # молча превращаться в "не делюсь". Раньше галочка не сохранялась
    # вовсе и каждый перезапуск сбрасывалась на выключено — час с лишним
    # реального скана уходил в никуда без единой ошибки в логе, потому
    # что отправка просто не пыталась начаться.
    value = _load_local_settings().get("share_progress")
    return True if value is None else bool(value)


def save_username(username):
    save_local_settings(username=username)


def save_local_settings(**updates):
    data = _load_local_settings()
    data.update(updates)
    try:
        LOCAL_LOGIN_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


BG = "#10151c"
PANEL = "#1a222c"
BORDER = "#2c3846"
INK = "#e9e3d2"
INK_DIM = "#93a0ac"
ACCENT = "#c98a3e"
ACCENT_INK = "#14100a"
MAP_BG = "#0b0f14"


def _set_placeholder(entry, text):
    """Grey hint text shown while the ttk.Entry is empty — cleared on focus,
    restored on blur if the user left it empty again. entry._is_placeholder
    tracks whether the current content IS the placeholder (vs. real user
    input that happens to be empty, which can't exist, but vs. having been
    typed and then deleted) — _entry_real_value() below uses that flag
    rather than just checking for emptiness, since the placeholder text
    itself is non-empty."""
    entry._is_placeholder = True
    entry.insert(0, text)
    entry.config(foreground=INK_DIM)

    def on_focus_in(_e):
        if entry._is_placeholder:
            entry.delete(0, "end")
            entry.config(foreground=INK)
            entry._is_placeholder = False

    def on_focus_out(_e):
        if not entry.get():
            entry.insert(0, text)
            entry.config(foreground=INK_DIM)
            entry._is_placeholder = True

    entry.bind("<FocusIn>", on_focus_in, add="+")
    entry.bind("<FocusOut>", on_focus_out, add="+")


def _entry_real_value(entry):
    """entry.get(), but "" if what's showing is only the placeholder set by
    _set_placeholder() rather than something the user actually typed."""
    if getattr(entry, "_is_placeholder", False):
        return ""
    return entry.get().strip()


def notify(title, message):
    try:
        winsound.MessageBeep()
    except Exception:
        pass
    messagebox.showinfo(title, message)


def setup_dark_style(root):
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=PANEL, foreground=INK, fieldbackground=BG, bordercolor=BORDER)
    style.configure("TFrame", background=PANEL)
    style.configure("Map.TFrame", background=MAP_BG)
    style.configure("TLabel", background=PANEL, foreground=INK)
    style.configure("Dim.TLabel", background=PANEL, foreground=INK_DIM)
    style.configure("Section.TLabel", background=PANEL, foreground="#718196",
                    font=("Segoe UI Semibold", 8))
    style.configure("TButton", background=BG, foreground=INK, bordercolor=BORDER, padding=6)
    style.map("TButton", background=[("active", BORDER)])
    style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_INK)
    style.map("Accent.TButton", background=[("active", "#d99a4e")])
    style.configure("TEntry", fieldbackground=BG, foreground=INK, bordercolor=BORDER, insertcolor=INK)
    style.configure("TCombobox", fieldbackground=BG, foreground=INK, background=BG)
    style.configure("TNotebook", background=PANEL, bordercolor=BORDER)
    style.configure("TNotebook.Tab", background=BG, foreground=INK_DIM, padding=(12, 8))
    style.map("TNotebook.Tab", background=[("selected", "#2b3f5c")], foreground=[("selected", INK)])
    style.configure("TProgressbar", background=styles.OWN_COLOR, troughcolor=BG, bordercolor=BORDER)
    style.configure("TCheckbutton", background=PANEL, foreground=INK)
    style.map("TCheckbutton", background=[("active", PANEL)])
    style.configure("TRadiobutton", background=PANEL, foreground=INK)


def state_path_for(cx, cy, step=1):
    return PROFILE_DATA_DIR / f"continent_{cx}_{cy}_s{step}.json"


def load_points(state):
    """Flattens state["results"] into a list of dicts the map view/legend
    can use directly, with the 3-way legend group already resolved."""
    pts = []
    for hit in state.get("results", {}).values():
        pts.append({
            "x": hit["x"], "y": hit["y"], "type": hit["type"],
            "name": hit.get("name", hit["type"]),
            "quality_pct": hit.get("quality_pct") or 0,
            "status": hit.get("status", "free"),
            "owner": hit.get("owner_org_name"),
            "group": styles.legend_group(hit["type"]),
            "color": styles.marker_color(hit["type"]),
            "mineral": hit["type"] in MINERAL_GROUND_TYPES,
        })
    return pts


def known_cells_boundary(known_cells, near=None):
    """Клетки на самой границе уже известной территории — соседи любой
    известной клетки, которые сами неизвестны. Используется как
    дополнительные точки старта фронтира локального скана: без этого волна,
    начавшись из точки внутри огромного уже известного массива, никогда бы
    из него не выбралась — код помечает известные клетки как "уже видели",
    но раз они пропускаются (не фетчатся), их соседи никогда не
    открываются обычным путём. Явный обход границы — единственный способ
    "перепрыгнуть" через уже пройденную территорию к настоящему
    неизвестному краю.

    near=(x, y), если передан — сортирует результат по удалённости от этой
    точки, ближайшее сначала. Без этого граница обходится в произвольном
    порядке (порядок итерации set), и первым в очередь на фетч мог
    случайно попасть участок на другом конце континента — ровно там, где
    в этот момент мог работать основной скан или другой помощник. С
    сортировкой каждый помощник естественно расходится в сторону СВОЕГО
    домена, а не в произвольную точку — без явной координации между
    процессами это самый дешёвый способ развести их по разным участкам."""
    boundary, seen_boundary = [], set()
    for key in known_cells:
        x, y = map(int, key.split(","))
        for nx, ny in cli.grid_neighbors(x, y, 1):
            nkey = f"{nx},{ny}"
            if nkey not in known_cells and nkey not in seen_boundary:
                seen_boundary.add(nkey)
                boundary.append((nx, ny))
    if near is not None:
        nx0, ny0 = near
        boundary.sort(key=lambda p: (p[0] - nx0) ** 2 + (p[1] - ny0) ** 2)
    return boundary


def fetch_known_cells(proxy=None):
    """Множество "x,y" уже известных основному скану координат — см.
    KNOWN_CELLS_URL. Пустое множество при любой сетевой/форматной ошибке
    (нет файла, сервер недоступен и т.п.) — вызывающий код тогда просто
    работает как раньше, без подсказки.

    proxy: та же настройка, что пользователь указал для игровых запросов
    (LolClient) и submit_batch(). Раньше эта функция всегда шла напрямую,
    игнорируя прокси — на сети, где прямой выход в интернет не работает
    (весь трафик обязан идти через прокси/VPN), это не просто "не находит
    файл": прямое TCP-соединение может зависнуть без ответа/разрыва вместо
    явной ошибки, ровно то же поведение, что уже ловили у LolClient
    (см. login_with_timeout в continent_scan_cli.py) — только здесь
    отсутствие прокси было причиной само по себе, не только недостающей
    защитой от таймаута."""
    req = urllib.request.Request(KNOWN_CELLS_URL, headers={"User-Agent": "Mozilla/5.0"})
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if proxy else urllib.request.build_opener()
    )
    try:
        with opener.open(req, timeout=20) as resp:
            raw = gzip.decompress(resp.read())
        flat = json.loads(raw)
        return {f"{flat[i]},{flat[i + 1]}" for i in range(0, len(flat), 2)}
    except (urllib.error.URLError, OSError, ValueError, gzip.BadGzipFile):
        return set()


def submit_batch(points, submitter, session_id, domain, proxy=None):
    """Шлёт один батч точек на приёмный воркер — вызывается из фонового
    потока (см. _flush_submit_buffer в App), никогда из потока самого
    скана, чтобы сетевой сбой/задержка тут не тормозили сам скан.

    Возвращает (ok, detail) вместо того, чтобы просто проглатывать ошибку —
    раньше отправка была полностью "молчаливой", и когда она часами не
    работала (см. proxy ниже), в интерфейсе не было ни единого следа
    проблемы. Вызывающий код (flush_submit_buffer) логирует результат —
    именно то, что можно прислать при проблемах вместо долгой переписки.

    proxy: та же строка, что пользователь указал для игровых запросов
    (LolClient). Раньше эта функция всегда шла напрямую, игнорируя
    настройку прокси — если у пользователя нет прямого выхода в интернет
    (сеть разрешает трафик только через прокси), отправка молча падала
    на КАЖДОМ батче часами, а сам скан продолжал работать как ни в чём
    не бывало (игровые запросы шли через LolClient со своим прокси,
    отдельным от этого)."""
    payload = json.dumps({
        "submitter": submitter, "sessionId": session_id, "domain": list(domain),
        "points": points,
    }).encode("utf-8")
    req = urllib.request.Request(
        SUBMIT_URL, data=payload, method="POST",
        # Python urllib's default User-Agent ("Python-urllib/3.x") gets
        # blocked by Cloudflare's edge bot heuristics on *.workers.dev
        # (403 error 1010) even for our own domain — a plain browser-like
        # UA sidesteps it, same as lol_api.py already does for the game API.
        headers={
            "Content-Type": "application/json", "X-Submit-Token": SUBMIT_TOKEN,
            "User-Agent": "Mozilla/5.0",
        },
    )
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if proxy else urllib.request.build_opener()
    )
    try:
        with opener.open(req, timeout=15) as resp:
            resp.read()
        return True, None
    except (urllib.error.URLError, OSError) as e:
        return False, str(e)


class CollapsibleGroup(ttk.Frame):
    def __init__(self, parent, title, on_row_click, **kw):
        super().__init__(parent, style="TFrame", **kw)
        self.expanded = True
        self.on_row_click = on_row_click
        self.header = tk.Label(self, text=f"▾ {title}", bg=PANEL, fg=ACCENT,
                                font=("", 9), anchor="w", cursor="hand2")
        self.header.pack(fill="x", pady=(6, 2))
        self.header.bind("<Button-1>", self.toggle)
        self.body = ttk.Frame(self)
        self.body.pack(fill="x")
        self.title = title
        self.rows = {}

    def toggle(self, _e=None):
        self.expanded = not self.expanded
        arrow = "▾" if self.expanded else "▸"
        self.header.configure(text=f"{arrow} {self.title}")
        if self.expanded:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()

    def set_items(self, items):
        """items: list of (type_code, color, name, count)"""
        for w in self.body.winfo_children():
            w.destroy()
        self.rows = {}
        for type_code, color, name, count, mineral in items:
            row = tk.Frame(self.body, bg=PANEL, cursor="hand2")
            row.pack(fill="x", pady=1)
            sw = tk.Canvas(row, width=10, height=10, bg=PANEL, highlightthickness=0)
            sw.create_rectangle(0, 0, 10, 10, fill=color, outline="")
            sw.pack(side="left", padx=(14, 6))
            star = " ★" if mineral else ""
            lbl = tk.Label(row, text=f"{name}{star} ({count})", bg=PANEL, fg=INK_DIM, font=("", 9), anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            for w in (row, sw, lbl):
                w.bind("<Button-1>", lambda e, tc=type_code: self.on_row_click(tc))
            self.rows[type_code] = row

    def highlight(self, type_code):
        for tc, row in self.rows.items():
            bg = "#2b3f5c" if tc == type_code else PANEL
            row.configure(bg=bg)
            for w in row.winfo_children():
                if isinstance(w, tk.Label):
                    w.configure(bg=bg)
                elif isinstance(w, tk.Canvas):
                    w.configure(bg=bg)


class MapView(ttk.Frame):
    """Canvas map with pan/zoom, grouped legend, owner spotlight, search,
    jump-to-coords, and the failed/free-only/territory toggles."""

    def __init__(self, parent, on_coord_search=None):
        super().__init__(parent, style="Map.TFrame")
        self.points = []
        self.failed = []
        self.domain = None
        self.min_x = self.max_x = self.min_y = self.max_y = 0
        self.scale = 1.0
        self.off_x = self.off_y = 0.0
        self.selected_type = None
        self.selected_owner = None
        self.show_failed = tk.BooleanVar(value=True)
        self.show_territory = tk.BooleanVar(value=False)
        self._drag = None
        self._build()

    def _build(self):
        toolbar = tk.Frame(self, bg=PANEL)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="+", width=3, command=lambda: self.zoom_by(1.3)).pack(side="left", padx=2, pady=2)
        ttk.Button(toolbar, text="−", width=3, command=lambda: self.zoom_by(1 / 1.3)).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Сброс", command=self.fit_to_view).pack(side="left", padx=2)
        self.coord_entry = ttk.Entry(toolbar, width=18)
        self.coord_entry.pack(side="left", padx=(10, 2))
        self.coord_entry.insert(0, "10957E28048N")
        ttk.Button(toolbar, text="→", width=3, command=self._jump_from_entry).pack(side="left")

        self.canvas = tk.Canvas(self, bg=MAP_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Motion>", self._on_hover)
        self.tooltip = None

    def load_state(self, state):
        self.points = load_points(state)
        self.failed = [tuple(p) for p in state.get("failed", [])]
        self.domain = (state.get("x"), state.get("y"))
        if self.points:
            xs = [p["x"] for p in self.points]
            ys = [p["y"] for p in self.points]
            self.min_x, self.max_x = min(xs), max(xs)
            self.min_y, self.max_y = min(ys), max(ys)
        self.fit_to_view()

    def fit_to_view(self):
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        world_w = max(1, (self.max_x - self.min_x) * 3 + 3)
        world_h = max(1, (self.max_y - self.min_y) * 3 + 3)
        self.scale = min(w / world_w, h / world_h, 4) * 0.92
        self.off_x = (w - world_w * self.scale) / 2
        self.off_y = (h - world_h * self.scale) / 2
        self.render()

    def zoom_by(self, factor, anchor=None):
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        ax, ay = anchor if anchor else (w / 2, h / 2)
        wx, wy = self._screen_to_world(ax, ay)
        self.scale = max(0.1, min(30, self.scale * factor))
        self.off_x = ax - (wx - self.min_x) * 3 * self.scale
        self.off_y = ay - (wy - self.min_y) * 3 * self.scale
        self.render()

    def jump_to(self, wx, wy):
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        self.scale = max(self.scale, 2.5)
        self.off_x = w / 2 - (wx - self.min_x) * 3 * self.scale
        self.off_y = h / 2 - (wy - self.min_y) * 3 * self.scale
        self.render()

    def _world_to_screen(self, wx, wy):
        return (wx - self.min_x) * 3 * self.scale + self.off_x, (wy - self.min_y) * 3 * self.scale + self.off_y

    def _screen_to_world(self, sx, sy):
        return (sx - self.off_x) / (3 * self.scale) + self.min_x, (sy - self.off_y) / (3 * self.scale) + self.min_y

    def _jump_from_entry(self):
        coords = self._parse_coords(self.coord_entry.get())
        if coords:
            self.jump_to(*coords)

    @staticmethod
    def _parse_coords(text):
        import re
        m = re.search(r"(\d+)\s*([EWew])\s*(\d+)\s*([NSns])", text)
        if not m:
            return None
        x = int(m.group(1)) * (1 if m.group(2).upper() == "E" else -1)
        y = int(m.group(3)) * (-1 if m.group(4).upper() == "N" else 1)
        return x, y

    def set_type_filter(self, type_code):
        self.selected_type = None if self.selected_type == type_code else type_code
        self.selected_owner = None
        self.render()

    def set_owner_filter(self, owner_name, jump_coords=None):
        self.selected_owner = None if self.selected_owner == owner_name else owner_name
        self.selected_type = None
        if self.selected_owner and jump_coords:
            self.jump_to(*jump_coords)
        else:
            self.render()

    def render(self):
        self.canvas.delete("all")
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        sz = max(1, 3 * self.scale)
        dim = self.selected_type is not None or self.selected_owner is not None
        for p in self.points:
            sx, sy = self._world_to_screen(p["x"], p["y"])
            if sx < -sz or sy < -sz or sx > w + sz or sy > h + sz:
                continue
            is_sel = (p["type"] == self.selected_type) or (self.selected_owner and p["owner"] == self.selected_owner)
            color = p["color"]
            if dim and not is_sel:
                color = self._dimmed(color)
            s = sz + 2 if is_sel else sz
            self.canvas.create_rectangle(sx, sy, sx + s, sy + s, fill=color, outline="")
        if self.show_territory.get():
            for p in self.points:
                if p["status"] not in ("ours", "occupied"):
                    continue
                sx, sy = self._world_to_screen(p["x"], p["y"])
                if sx < -sz or sy < -sz or sx > w + sz or sy > h + sz:
                    continue
                color = styles.OWN_COLOR if p["status"] == "ours" else "#e84393"
                self.canvas.create_rectangle(sx, sy, sx + sz, sy + sz, fill=color, outline="", stipple="gray50")
        if self.show_failed.get():
            fsz = max(2, sz + 1)
            for wx, wy in self.failed:
                sx, sy = self._world_to_screen(wx, wy)
                if sx < -fsz or sy < -fsz or sx > w + fsz or sy > h + fsz:
                    continue
                self.canvas.create_rectangle(sx, sy, sx + fsz, sy + fsz, fill=styles.FAILED_COLOR, outline="")
        if self.domain and self.domain[0] is not None:
            dx, dy = self._world_to_screen(*self.domain)
            self.canvas.create_oval(dx - 7, dy - 7, dx + 7, dy + 7, outline=INK, width=2)

    @staticmethod
    def _dimmed(hexcolor):
        hexcolor = hexcolor.lstrip("#")
        r, g, b = int(hexcolor[0:2], 16), int(hexcolor[2:4], 16), int(hexcolor[4:6], 16)
        r, g, b = [int(c * 0.22 + 11 * 0.78) for c in (r, g, b)]
        return f"#{r:02x}{g:02x}{b:02x}"

    def _on_press(self, e):
        self._drag = (e.x, e.y, False)

    def _on_drag(self, e):
        if not self._drag:
            return
        x0, y0, _ = self._drag
        self.off_x += e.x - x0
        self.off_y += e.y - y0
        self._drag = (e.x, e.y, True)
        self.render()

    def _on_release(self, _e):
        self._drag = None

    def _on_double_click(self, e):
        wx, wy = self._screen_to_world(e.x, e.y)
        webbrowser.open(f"https://www.landsoflords.com/map/{format_map_coords(round(wx), round(wy))}")

    def _on_wheel(self, e):
        factor = 1.15 if e.delta > 0 else 1 / 1.15
        self.zoom_by(factor, anchor=(e.x, e.y))

    def _on_hover(self, e):
        wx, wy = self._screen_to_world(e.x, e.y)
        best, best_d = None, 3
        for p in self.points:
            d = abs(p["x"] - round(wx)) + abs(p["y"] - round(wy))
            if d < best_d:
                best, best_d = p, d
        if self.tooltip:
            self.canvas.delete(self.tooltip)
            self.tooltip = None
        if best:
            status_text = {"ours": "наш домен", "occupied": f"занято: {best['owner'] or '?'}"}.get(best["status"], "свободно")
            text = f"{best['name']}  {best['quality_pct']}%\n{status_text}"
            self.tooltip = self.canvas.create_text(
                e.x + 14, e.y + 10, text=text, anchor="nw", fill=INK, font=("", 9),
            )


class App:
    def __init__(self, root):
        self.root = root
        root.title("Lands of Lords — Map Scanner")
        root.geometry("1200x780")
        root.minsize(800, 500)
        setup_dark_style(root)

        self.events = queue.Queue()
        self.mode = tk.StringVar(value="local")
        self.local_client = None
        self.local_stop_event = None
        # Guards against starting a second local operation (scan or any
        # recheck mode) while one is already running — nothing previously
        # stopped a click on "Перепроверка занятых" while the main scan
        # thread was still alive, so two ThreadPoolExecutor(max_workers=
        # CONCURRENCY) pools ended up hammering the same proxy/account at
        # once, and — worse — both threads called cli.load_state(path) on
        # the same file concurrently while the OTHER was also writing to
        # it via flush_state(); a torn read there raises inside a plain
        # background thread, which in a --windowed PyInstaller build has no
        # console for the traceback to land on — the thread just vanishes,
        # looking exactly like "infinite подключение..." with zero
        # explanation. See _local_crash below for the other half of this
        # fix (catching and logging exactly that class of silent death).
        self.local_busy = False
        self.remote = None
        self.remote_stop_event = None
        self.remote_awaiting_password = False
        self.current_state_path = None
        self.current_state = None
        self.current_map_model = None
        self._map_load_generation = 0

        self._build()
        root.after(150, self._poll_events)
        # Since the jsonl migration (d30dbb1), a scan's actual data lives in
        # continent_X_Y_sN.jsonl and only the small x/y/frontier snapshot is
        # continent_X_Y_sN.meta.json — the single-file continent_X_Y_sN.json
        # this used to glob for is now retired to .json.pre-jsonl-bak right
        # after the first flush. Globbing "continent_*.json" therefore no
        # longer matches the real state file; it matches the *.meta.json
        # sidecar instead (its name still ends in ".json"), and load_state()
        # on THAT path derives "continent_X_Y_sN.meta.jsonl" (wrong, doesn't
        # exist) and falls back to treating the meta blob itself as legacy
        # state — which has no "results" key, crashing any recheck with
        # KeyError('results') the instant it's clicked. Look for the meta
        # sidecar (current format) and legacy single-file blobs (pre-
        # migration, not yet touched by a flush) separately, and reconstruct
        # the canonical continent_X_Y_sN.json identifier load_state() expects.
        candidates = [
            (p.stat().st_mtime, p.with_suffix("").with_suffix(".json"))
            for p in PROFILE_DATA_DIR.glob("continent_*.meta.json")
        ] + [
            (p.stat().st_mtime, p)
            for p in PROFILE_DATA_DIR.glob("continent_*.json")
            if not p.name.endswith(".meta.json")
        ]
        if candidates:
            candidates.sort(key=lambda t: t[0], reverse=True)
            self._refresh_map_from_disk(candidates[0][1])

    # ---------- layout ----------

    def _build(self):
        header = tk.Frame(self.root, bg="#111923", height=58)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="LANDS OF LORDS", bg="#111923", fg=ACCENT,
                 font=("Segoe UI Semibold", 11)).pack(side="left", padx=(18, 8))
        tk.Label(header, text="Помощник разведки карты", bg="#111923", fg=INK,
                 font=("Segoe UI", 15)).pack(side="left")
        tk.Label(header, text="быстрый режим", bg="#20372f", fg="#71d6a7",
                 font=("Segoe UI Semibold", 9), padx=9, pady=4).pack(side="right", padx=18)

        paned = tk.PanedWindow(self.root, orient="horizontal", bg=BORDER, sashwidth=5, bd=0)
        paned.pack(fill="both", expand=True)

        self.sidebar = ttk.Frame(paned, width=350)
        paned.add(self.sidebar, minsize=330)

        self.map_view = FastMapView(paned)
        paned.add(self.map_view, minsize=400)

        self._build_sidebar()

    def _build_sidebar(self):
        s = self.sidebar
        pad = dict(padx=14, pady=(9, 3))

        self.tabs = ttk.Notebook(s)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        scan = ttk.Frame(self.tabs)
        filters = ttk.Frame(self.tabs)
        journal = ttk.Frame(self.tabs)
        self.tabs.add(scan, text="  Скан  ")
        self.tabs.add(filters, text="  Карта  ")
        self.tabs.add(journal, text="  Журнал  ")

        mode_row = ttk.Frame(scan)
        mode_row.pack(fill="x", padx=14, pady=(14, 8))
        ttk.Radiobutton(mode_row, text="Локально", value="local", variable=self.mode,
                         command=self._on_mode_change).pack(side="left", expand=True, fill="x")
        ttk.Radiobutton(mode_row, text="Сервер", value="remote", variable=self.mode,
                         command=self._on_mode_change).pack(side="left", expand=True, fill="x")

        self.health_var = tk.StringVar(value="")
        ttk.Label(scan, textvariable=self.health_var, style="Dim.TLabel").pack(fill="x", padx=14)

        # --- local frame ---
        self.local_frame = ttk.Frame(scan)
        ttk.Label(self.local_frame, text="X / Y домена (пусто = свой)", style="Dim.TLabel").pack(fill="x", **pad)
        xy = ttk.Frame(self.local_frame)
        xy.pack(fill="x", padx=12)
        self.local_x = ttk.Entry(xy, width=8)
        self.local_x.pack(side="left")
        self.local_y = ttk.Entry(xy, width=8)
        self.local_y.pack(side="left", padx=(4, 0))
        ttk.Label(self.local_frame, text="Логин", style="Dim.TLabel").pack(fill="x", **pad)
        self.local_username = ttk.Entry(self.local_frame)
        self.local_username.pack(fill="x", padx=12)
        ttk.Label(self.local_frame, text="Пароль", style="Dim.TLabel").pack(fill="x", **pad)
        self.local_password = ttk.Entry(self.local_frame, show="*")
        self.local_password.pack(fill="x", padx=12)
        ttk.Label(self.local_frame, text="Прокси (необязательно)", style="Dim.TLabel").pack(fill="x", **pad)
        self.local_proxy = ttk.Entry(self.local_frame)
        self.local_proxy.pack(fill="x", padx=12)
        _set_placeholder(self.local_proxy, "http://host:port или http://user:pass@host:port")
        # remember_password_var должна существовать ДО первого вызова
        # _on_local_username_changed() ниже — он её трогает (set(True) при
        # найденном сохранённом пароле). Раньше эта переменная создавалась
        # после того вызова — падало у любого, кто уже когда-то сохранял
        # пароль (AttributeError: 'App' object has no attribute
        # 'remember_password_var'), пойман вживую по трейсбеку от помощника.
        self.remember_password_var = tk.BooleanVar(value=False)
        self.local_username.bind("<FocusOut>", self._on_local_username_changed)
        saved_username = load_saved_username()
        if saved_username:
            self.local_username.insert(0, saved_username)
            self._on_local_username_changed()  # подтягивает пароль из keyring, раз логин уже известен
        saved_proxy = load_saved_proxy()
        if saved_proxy:
            self.local_proxy.delete(0, "end")
            self.local_proxy.insert(0, saved_proxy)
            self.local_proxy.config(foreground=INK)
            self.local_proxy._is_placeholder = False
        ttk.Checkbutton(
            self.local_frame, text="Запомнить пароль (в системном хранилище)",
            variable=self.remember_password_var,
        ).pack(fill="x", padx=12, pady=(2, 0))
        self.share_progress_var = tk.BooleanVar(value=load_saved_share_progress())
        ttk.Checkbutton(
            self.local_frame,
            text="Делиться прогрессом с проектом (отправлять точки во время скана)",
            variable=self.share_progress_var,
        ).pack(fill="x", padx=12, pady=(2, 0))
        btns = ttk.Frame(self.local_frame)
        btns.pack(fill="x", padx=12, pady=8)
        self.local_start_btn = ttk.Button(btns, text="Запустить", style="Accent.TButton", command=self._local_start)
        self.local_start_btn.pack(side="left", fill="x", expand=True)
        self.local_stop_btn = ttk.Button(btns, text="Стоп", command=self._local_stop, state="disabled")
        self.local_stop_btn.pack(side="left", padx=(4, 0))

        # --- remote frame ---
        self.remote_frame = ttk.Frame(scan)
        prow = ttk.Frame(self.remote_frame)
        prow.pack(fill="x", padx=12, pady=(6, 2))
        self.profile_combo = ttk.Combobox(prow, values=profiles.list_profiles(), state="readonly", width=16)
        self.profile_combo.pack(side="left", fill="x", expand=True)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        ttk.Button(prow, text="+", width=2, command=self._new_profile).pack(side="left", padx=(4, 0))

        ttk.Label(self.remote_frame, text="Хост", style="Dim.TLabel").pack(fill="x", **pad)
        self.remote_host = ttk.Entry(self.remote_frame)
        self.remote_host.pack(fill="x", padx=12)
        ttk.Label(self.remote_frame, text="SSH-пользователь / ключ", style="Dim.TLabel").pack(fill="x", **pad)
        sshrow = ttk.Frame(self.remote_frame)
        sshrow.pack(fill="x", padx=12)
        self.remote_ssh_user = ttk.Entry(sshrow, width=10)
        self.remote_ssh_user.insert(0, "root")
        self.remote_ssh_user.pack(side="left")
        self.remote_key_path = ttk.Entry(sshrow)
        self.remote_key_path.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(sshrow, text="…", width=2, command=self._browse_key).pack(side="left")
        ttk.Button(self.remote_frame, text="Подключиться", command=self._remote_connect).pack(fill="x", padx=12, pady=(4, 6))

        ttk.Label(self.remote_frame, text="Игровой логин", style="Dim.TLabel").pack(fill="x", **pad)
        self.remote_username = ttk.Entry(self.remote_frame)
        self.remote_username.pack(fill="x", padx=12)
        rbtns = ttk.Frame(self.remote_frame)
        rbtns.pack(fill="x", padx=12, pady=8)
        self.remote_start_btn = ttk.Button(rbtns, text="Запустить", style="Accent.TButton",
                                            command=self._remote_start, state="disabled")
        self.remote_start_btn.pack(side="left", fill="x", expand=True)
        self.remote_stop_btn = ttk.Button(rbtns, text="Стоп", command=self._remote_stop, state="disabled")
        self.remote_stop_btn.pack(side="left", padx=(4, 0))

        self.remote_password_row = ttk.Frame(self.remote_frame)
        self.remote_password_entry = ttk.Entry(self.remote_password_row, show="*")
        self.remote_password_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(self.remote_password_row, text="Отправить", command=self._remote_send_password).pack(side="left", padx=(4, 0))

        # --- progress + recheck (shared) ---
        self.progress_var = tk.StringVar(value="")
        self.progress_label = ttk.Label(scan, textvariable=self.progress_var, style="Dim.TLabel")
        self.progress_label.pack(fill="x", padx=14, pady=(10, 3))
        self.progress_bar = ttk.Progressbar(scan, mode="determinate", maximum=1)
        self.progress_bar.pack(fill="x", padx=14)

        ttk.Label(scan, text="ОБНОВЛЕНИЕ ДАННЫХ", style="Section.TLabel").pack(fill="x", padx=14, pady=(18, 5))
        recheck_row = ttk.Frame(scan)
        recheck_row.pack(fill="x", padx=14, pady=4)
        ttk.Button(recheck_row, text="Перепроверка свободных", command=lambda: self._start_recheck("free")).pack(fill="x")
        ttk.Button(recheck_row, text="Перепроверка занятых", command=lambda: self._start_recheck("occupied")).pack(fill="x", pady=(4, 0))
        ttk.Button(recheck_row, text="Своб. с постройкой", command=lambda: self._start_recheck("free_with_cover")).pack(fill="x", pady=(4, 0))
        ttk.Button(recheck_row, text="Полный пересчёт", command=lambda: self._start_recheck("all")).pack(fill="x", pady=(4, 0))

        # --- live log ---
        ttk.Label(journal, text="ЖУРНАЛ СОБЫТИЙ", style="Section.TLabel").pack(fill="x", padx=14, pady=(14, 6))
        self.log_text = tk.Text(journal, bg=BG, fg=INK_DIM, insertbackground=INK,
                                 relief="flat", font=("Cascadia Mono", 9), wrap="word", padx=10, pady=10)
        self.log_text.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # --- owner search ---
        ttk.Label(filters, text="ПОИСК ВЛАДЕЛЬЦА", style="Section.TLabel").pack(fill="x", padx=14, pady=(14, 5))
        self.owner_search = ttk.Entry(filters)
        self.owner_search.pack(fill="x", padx=14)
        self.owner_search.bind("<Return>", self._on_owner_search)
        self.owner_results = tk.Listbox(filters, height=4, bg=BG, fg=INK_DIM, relief="flat",
                                         highlightthickness=0, font=("", 9))
        self.owner_results.pack(fill="x", padx=14, pady=(3, 8))
        self.owner_results.bind("<<ListboxSelect>>", self._on_owner_pick)

        # --- toggles ---
        ttk.Checkbutton(filters, text="Показывать ошибки проверки", variable=self.map_view.show_failed,
                         command=self.map_view.schedule_render).pack(fill="x", padx=14, pady=2)
        ttk.Checkbutton(filters, text="Подсвечивать территории", variable=self.map_view.show_territory,
                         command=self.map_view.schedule_render).pack(fill="x", padx=14, pady=2)
        ttk.Checkbutton(filters, text="Закрашивать пройденные при пересчёте", variable=self.map_view.show_recheck_progress,
                         command=self.map_view.schedule_render).pack(fill="x", padx=14, pady=2)

        ttk.Button(filters, text="Сбросить фильтры", command=self._reset_filters).pack(fill="x", padx=14, pady=(10, 4))
        ttk.Button(filters, text="Экспорт минералов · CSV", command=self._export_csv).pack(fill="x", padx=14, pady=(0, 10))

        # --- grouped legend ---
        ttk.Label(filters, text="ЛЕГЕНДА", style="Section.TLabel").pack(fill="x", padx=14, pady=(8, 3))
        legend_canvas = tk.Canvas(filters, bg=PANEL, highlightthickness=0)
        legend_scroll = ttk.Scrollbar(filters, orient="vertical", command=legend_canvas.yview)
        legend_body = ttk.Frame(legend_canvas)
        legend_body.bind("<Configure>", lambda _e: legend_canvas.configure(scrollregion=legend_canvas.bbox("all")))
        legend_window = legend_canvas.create_window((0, 0), window=legend_body, anchor="nw")
        legend_canvas.bind("<Configure>", lambda e: legend_canvas.itemconfigure(legend_window, width=e.width))
        legend_canvas.configure(yscrollcommand=legend_scroll.set)
        legend_canvas.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=(0, 10))
        legend_scroll.pack(side="right", fill="y", padx=(0, 8), pady=(0, 10))
        self.legend_groups = {}
        for name in ("Минералы", "Суша", "Вода"):
            grp = CollapsibleGroup(legend_body, name, self._on_legend_click)
            grp.pack(fill="x")
            self.legend_groups[name] = grp

        self._on_mode_change()

    def _on_mode_change(self):
        # `before=self.progress_label` держит видимый фрейм сразу после строки
        # статуса, а не даёт pack() каждый раз добавлять его в конец сайдбара
        # (ниже лога/кнопок) — из-за этого поля логина и казались пропавшими.
        if self.mode.get() == "local":
            self.remote_frame.pack_forget()
            self.local_frame.pack(fill="x", before=self.progress_label)
        else:
            self.local_frame.pack_forget()
            self.remote_frame.pack(fill="x", before=self.progress_label)

    # ---------- profiles ----------

    def _on_profile_selected(self, _e=None):
        data = profiles.load_profile(self.profile_combo.get())
        self.remote_host.delete(0, "end"); self.remote_host.insert(0, data.get("host", ""))
        self.remote_ssh_user.delete(0, "end"); self.remote_ssh_user.insert(0, data.get("username", "root"))
        self.remote_key_path.delete(0, "end"); self.remote_key_path.insert(0, data.get("key_path", ""))
        self.remote_username.delete(0, "end"); self.remote_username.insert(0, data.get("game_username", ""))

    def _new_profile(self):
        name = simpledialog.askstring("Новый профиль сервера", "Имя профиля:", parent=self.root)
        if not name:
            return
        profiles.save_profile(name, {"host": "", "username": "root", "key_path": "", "game_username": "", "port": 22})
        self.profile_combo.configure(values=profiles.list_profiles())
        self.profile_combo.set(name)

    def _save_current_profile(self):
        name = self.profile_combo.get()
        if not name:
            return
        profiles.save_profile(name, {
            "host": self.remote_host.get(), "username": self.remote_ssh_user.get(),
            "key_path": self.remote_key_path.get(), "game_username": self.remote_username.get(), "port": 22,
        })

    def _browse_key(self):
        path = filedialog.askopenfilename(title="SSH-ключ")
        if path:
            self.remote_key_path.delete(0, "end")
            self.remote_key_path.insert(0, path)

    # ---------- events / logging ----------

    def _log(self, text):
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        try:
            with LOG_FILE_PATH.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except OSError:
            pass  # тот же принцип, что и в остальном коде — лог на диск best-effort, не должен мешать работе

    def _set_progress(self, done, total, note=""):
        self.progress_bar["maximum"] = max(total, 1)
        self.progress_bar["value"] = done
        self.progress_var.set(f"{done} / {total}{note}")

    def _poll_events(self):
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == "call":
                    rest[0]()
        except queue.Empty:
            pass
        self.root.after(150, self._poll_events)

    def _safe_after(self, fn):
        self.events.put(("call", fn))

    # ---------- map refresh ----------

    def _refresh_map_from_disk(self, path, preserve_overlay=False):
        """Load and rasterise a large scan away from Tk's event loop.

        preserve_overlay: carry the "already visited this session" white
        shading (see FastMapView.mark_rechecked) across this reload instead
        of wiping it — pass True for the end-of-scan/recheck refresh, leave
        False for a genuinely fresh load (app startup, switching domain)."""
        self._map_load_generation += 1
        generation = self._map_load_generation
        path = Path(path)
        self.progress_var.set("Подготавливаю карту…")

        def prepare():
            try:
                state = cli.load_state(path)
                model = FastMapView.prepare_state(state) if state else None
            except Exception as exc:
                self._safe_after(lambda: self._log(f"Не удалось открыть карту: {exc}"))
                return
            if not model:
                return

            def apply():
                if generation != self._map_load_generation:
                    return
                self.current_state = state
                self.current_state_path = path
                self.current_map_model = model
                self.map_view.load_model(model, preserve_overlay=preserve_overlay)
                self._refresh_legend()
                self.progress_var.set(f"Карта готова · {len(model.points):,} клеток".replace(",", " "))

            self._safe_after(apply)

        threading.Thread(target=prepare, daemon=True, name="map-raster-loader").start()

    def _refresh_legend(self):
        if not self.current_map_model:
            return
        groups = {"Минералы": {}, "Суша": {}, "Вода": {}}
        for type_code, (color, name, count, mineral) in self.current_map_model.type_counts.items():
            groups[styles.legend_group(type_code)][type_code] = [color, name, count, mineral]
        for name, grp in groups.items():
            items = [(tc, c, n, cnt, m) for tc, (c, n, cnt, m) in grp.items()]
            items.sort(key=lambda it: -it[3])
            self.legend_groups[name].set_items(items)

    def _on_legend_click(self, type_code):
        self.map_view.set_type_filter(type_code)
        for grp in self.legend_groups.values():
            grp.highlight(self.map_view.selected_type)

    def _reset_filters(self):
        self.map_view.selected_type = None
        self.map_view.selected_owner = None
        self.map_view.render()
        for grp in self.legend_groups.values():
            grp.highlight(None)

    def _on_owner_search(self, _e=None):
        query = self.owner_search.get().strip().lower()
        self.owner_results.delete(0, "end")
        if not query or not self.current_map_model:
            return
        for name in sorted(self.current_map_model.owner_centers):
            if query in name.lower():
                self.owner_results.insert("end", name)

    def _on_owner_pick(self, _e=None):
        sel = self.owner_results.curselection()
        if not sel:
            return
        name = self.owner_results.get(sel[0])
        center = self.current_map_model.owner_centers.get(name) if self.current_map_model else None
        coords = center[:2] if center else None
        self.map_view.set_owner_filter(name, jump_coords=coords)

    def _export_csv(self):
        if not self.current_state:
            messagebox.showinfo("Экспорт", "Сначала загрузите карту.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["тип", "название", "качество%", "координаты", "ссылка", "статус", "владелец"])
            for hit in self.current_state.get("results", {}).values():
                if hit["type"] not in MINERAL_GROUND_TYPES:
                    continue
                link = f"https://www.landsoflords.com/map/{format_map_coords(hit['x'], hit['y'])}"
                w.writerow([hit["type"], hit.get("name", ""), hit.get("quality_pct", ""), f"{hit['x']},{hit['y']}",
                            link, hit.get("status", ""), hit.get("owner_org_name") or ""])
        messagebox.showinfo("Экспорт", f"Сохранено: {path}")

    # ================= LOCAL MODE =================

    def _on_local_username_changed(self, _e=None):
        username = self.local_username.get().strip()
        if not username or self.local_password.get():
            return
        saved = load_saved_password(username)
        if saved:
            self.local_password.delete(0, "end")
            self.local_password.insert(0, saved)
            self.remember_password_var.set(True)

    def _local_start(self):
        if self.local_busy:
            messagebox.showinfo("Уже работает", "Сначала дождитесь завершения текущей операции (скан или перепроверка).")
            return
        username = self.local_username.get().strip()
        password = self.local_password.get()
        proxy = _entry_real_value(self.local_proxy) or None
        if not username or not password:
            messagebox.showerror("Нет данных", "Введите логин и пароль.")
            return
        save_username(username)
        save_local_settings(proxy=proxy or "", share_progress=self.share_progress_var.get())
        if self.remember_password_var.get():
            save_password(username, password)
        else:
            forget_password(username)
        self.local_busy = True
        self.local_start_btn.config(state="disabled")
        self.local_stop_btn.config(state="normal")
        self.health_var.set("подключение…")
        threading.Thread(target=self._local_worker, args=(username, password, proxy), daemon=True).start()

    def _local_stop(self):
        if self.local_stop_event:
            self.local_stop_event.set()
        self.local_stop_btn.config(state="disabled")

    def _local_worker(self, username, password, proxy=None):
        # Thin wrapper: an uncaught exception anywhere in _local_worker_impl
        # (below) would otherwise kill this background thread silently — a
        # --windowed PyInstaller build has no console for the traceback to
        # land on, so the thread just vanishes with the status frozen on
        # whatever it last said. See _local_crash and the local_busy comment
        # in __init__ for the concrete failure this was built to catch.
        try:
            self._local_worker_impl(username, password, proxy)
        except Exception:
            tb = traceback.format_exc()
            self._safe_after(lambda: self._local_crash(tb))
        finally:
            self.local_busy = False

    def _local_worker_impl(self, username, password, proxy=None):
        client = LolClient("", "https://www.landsoflords.com", proxy=proxy)
        try:
            cli.login_with_timeout(client, username, password,
                                    log=lambda m: self._safe_after(lambda: self._log(m)))
        except (TimeoutError, ProtocolError, OSError) as e:
            self._safe_after(lambda: self._local_finish_error(str(e)))
            return
        self.local_client = client
        login_creds = (username, password)

        cx_raw, cy_raw = self.local_x.get().strip(), self.local_y.get().strip()
        if (not cx_raw or not cy_raw) and client.org_coords is None:
            # Seen live: an account with no owned settlement right now (or a
            # homepage layout _extract_org_coords() didn't match) leaves
            # org_coords None — client.org_coords[0] below would then raise
            # TypeError and silently kill this thread pre-fix (see
            # _local_crash/local_busy). continent_scan_cli.py's CLI path
            # already handles this with a clean exit; the GUI didn't.
            self._safe_after(lambda: self._local_finish_error(
                "Не удалось определить координаты домена автоматически (нет своего владения?). "
                "Укажите X/Y домена вручную в полях выше."
            ))
            return
        cx = int(cx_raw) if cx_raw else client.org_coords[0]
        cy = int(cy_raw) if cy_raw else client.org_coords[1]

        path = state_path_for(cx, cy)
        state = cli.load_state(path) or {"x": cx, "y": cy, "step": 1, "results": {}, "frontier": [], "failed": [], "fog": []}
        # frontier несёт глубину ухода в воду — (x, y, depth), см.
        # cli.WATER_EXPAND_DEPTH; failed-точки такой глубины не имели,
        # депту 0 для них — считаем их "как будто с суши".
        frontier_points = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in state.get("frontier", [])] \
            + [(p[0], p[1], 0) for p in state.get("failed", [])]
        state["failed"] = []
        if not frontier_points and not state["results"]:
            frontier_points = [(cx, cy, 0)]
        self._safe_after(lambda: self._log(
            f"[домен ({cx},{cy}), файл {path.name}: уже {len(state['results'])} точек, в очереди {len(frontier_points)}]"
        ))

        # Подтягиваем, что уже известно основному скану (см. build_known_cells.py),
        # чтобы не гонять запросы по территории, которую кто-то другой уже
        # прошёл — только "работает" (пустой набор), если файл не скачался.
        self._safe_after(lambda: self.health_var.set("проверяю уже известную территорию…"))
        self._safe_after(lambda: self._log("[запрашиваю список уже известной территории]"))
        try:
            known_cells = cli.call_with_timeout(fetch_known_cells, proxy, timeout=30)
        except TimeoutError:
            known_cells = set()
            self._safe_after(lambda: self._log("[не удалось скачать список известной территории: нет ответа за 30с — продолжаю без него]"))
        if known_cells:
            existing_keys = set(state["results"].keys()) | {f"{p[0]},{p[1]}" for p in frontier_points}
            boundary = known_cells_boundary(known_cells, near=(cx, cy))
            new_boundary = [(x, y, 0) for x, y in boundary if f"{x},{y}" not in existing_keys]
            frontier_points += new_boundary
            self._safe_after(lambda: self._log(
                f"[известная территория: {len(known_cells)} точек, добавлено {len(new_boundary)} новых на границе]"
            ))
        else:
            self._safe_after(lambda: self._log("[не удалось скачать список известной территории — продолжаю без него]"))

        from collections import deque
        frontier = deque(frontier_points)
        results = state["results"]
        # Туманные клетки (см. "fog" в cli.fetch_point) — не ретраим, скан
        # не может сам открыть туман, включаем в seen, чтобы обычное
        # соседнее обнаружение не подсовывало их заново.
        fog_points = [tuple(p) for p in state.get("fog", [])]
        fog = fog_points
        seen = set(results.keys()) | {f"{p[0]},{p[1]}" for p in frontier} | known_cells \
            | {f"{p[0]},{p[1]}" for p in fog_points}

        self._safe_after(lambda: self._refresh_map_from_disk(path))
        self._safe_after(lambda: self.health_var.set("работает"))
        self._safe_after(lambda: self._log("[скан запущен]"))

        share_progress = self.share_progress_var.get()
        submit_buffer, last_submit_time = [], time.time()
        session_id = str(uuid.uuid4())

        def do_submit(batch):
            ok, detail = submit_batch(batch, username, session_id, (cx, cy), proxy)
            if ok:
                self._safe_after(lambda: self._log(f"[отправлено на сервер: {len(batch)} точек]"))
            else:
                self._safe_after(lambda: self._log(f"[НЕ УДАЛОСЬ отправить {len(batch)} точек: {detail}]"))

        def flush_submit_buffer():
            if not submit_buffer:
                return
            batch = list(submit_buffer)
            submit_buffer.clear()
            threading.Thread(target=do_submit, args=(batch,), daemon=True).start()

        self.local_stop_event = threading.Event()
        stop_event = self.local_stop_event
        completed = 0
        pending = []
        consecutive_failures = 0
        last_log_time = [0.0]  # periodic "[просканировано N/M]" line — see below
        with ThreadPoolExecutor(max_workers=cli.CONCURRENCY) as pool:
            while frontier:
                if stop_event.is_set():
                    break
                chunk = [frontier.popleft() for _ in range(min(cli.CHUNK, len(frontier)))]
                futures = [pool.submit(cli.fetch_point, client, (p[0], p[1])) for p in chunk]
                visited_this_chunk = []
                for p, hit in zip(chunk, cli.results_or_timeout(futures, chunk)):
                    depth = p[2]
                    completed += 1
                    if hit.get("fog"):
                        # Не сеть/сессия виновата — клетка ещё не открыта в
                        # игре, ретраить бессмысленно (см. тот же случай в
                        # continent_scan_cli.py).
                        fog.append((hit["x"], hit["y"]))
                        continue
                    if hit.get("error"):
                        consecutive_failures += 1
                        continue
                    consecutive_failures = 0
                    key = f"{hit['x']},{hit['y']}"
                    results[key] = hit
                    pending.append(hit)
                    visited_this_chunk.append((hit["x"], hit["y"]))
                    if share_progress:
                        submit_buffer.append(hit)
                    # На суше глубина ухода в воду сбрасывается в 0, на воде
                    # растёт и ограничена cli.WATER_EXPAND_DEPTH — даёт
                    # полосу реально отсканированной воды у побережья, а не
                    # мгновенную остановку на первой же клетке.
                    if hit["type"] not in cli.CONTINENT_BOUNDARY_TYPES:
                        next_depth = 0
                        expand = True
                    else:
                        next_depth = depth + 1
                        expand = next_depth <= cli.WATER_EXPAND_DEPTH
                    if expand:
                        for nx, ny in cli.grid_neighbors(hit["x"], hit["y"], 1):
                            nkey = f"{nx},{ny}"
                            if nkey not in seen:
                                seen.add(nkey)
                                frontier.append((nx, ny, next_depth))
                    if len(pending) >= cli.SAVE_EVERY:
                        state["frontier"] = [list(p) for p in frontier]
                        state["fog"] = [list(p) for p in fog]
                        cli.flush_state(path, state, pending)
                        pending = []
                if visited_this_chunk:
                    # Same lightweight per-chunk shading as --recheck-all's
                    # on_progress (see mark_rechecked) — cheap putpixel calls
                    # against the already-rasterised map, not a reload, so it
                    # doesn't reintroduce the near-freeze the full redraw used
                    # to cause on a large account (see the comment below on
                    # why the preview itself isn't reloaded every flush here).
                    self._safe_after(lambda pts=visited_this_chunk: self.map_view.mark_rechecked(pts))
                if share_progress and submit_buffer and (
                    len(submit_buffer) >= SUBMIT_BATCH_SIZE
                    or time.time() - last_submit_time >= SUBMIT_INTERVAL_SECONDS
                ):
                    flush_submit_buffer()
                    last_submit_time = time.time()
                should_stop, consecutive_failures = cli.run_relogin_check(login_creds, client, consecutive_failures)
                if should_stop:
                    self._safe_after(lambda: self.health_var.set("сессия истекла"))
                    stop_event.set()
                    break
                elif consecutive_failures == 0:
                    self._safe_after(lambda: self.health_var.set("работает"))
                d, q = len(results), len(frontier)
                self._safe_after(lambda d=d, q=q: self._set_progress(d, d + q))
                # Прогресс-бар на вкладке "Скан" обновляется каждый чанк и
                # этого достаточно для него самого, но вкладка "Журнал" без
                # этой строки во время скана молчит целиком (только стартовые
                # сообщения) — раньше построчный лог сканирования был виден,
                # это его отсутствие и заметили.
                now = time.time()
                if now - last_log_time[0] >= 5:
                    last_log_time[0] = now
                    self._safe_after(lambda d=d, q=q: self._log(f"[просканировано {d}, в очереди {q}]"))
                # Preview intentionally NOT refreshed here anymore. It used to
                # re-read on every SAVE_EVERY flush — load_state() re-parses
                # the whole jsonl from scratch, and the redraw itself rebuilds
                # every canvas rectangle + the legend from all known points.
                # Both scale with total points already known, not with new
                # points this flush, so on an account with a large local
                # dataset (hundreds of thousands+) this got slower and more
                # frequent together as the file grew, and eventually looked
                # exactly like the app freezing shortly after a scan started.
                # The numeric progress line above is cheap and still live;
                # the map/legend only redraw once, at scan start and at
                # scan stop (see the two other _refresh_map_from_disk calls
                # in this method).
                time.sleep(cli.PAUSE_SECONDS)

        if share_progress:
            flush_submit_buffer()
        state["frontier"] = [list(p) for p in frontier]
        state["fog"] = [list(p) for p in fog]
        cli.flush_state(path, state, pending)
        self._safe_after(lambda p=path: self._refresh_map_from_disk(p, preserve_overlay=True))
        self._safe_after(lambda: self._local_finish(len(results), len(frontier)))

    def _local_finish(self, done, queued):
        self.local_start_btn.config(state="normal")
        self.local_stop_btn.config(state="disabled")
        self.health_var.set("остановлено")
        self._log(f"[скан остановлен: сохранено {done}, в очереди {queued}]")
        notify("Скан остановлен", f"Сохранено {done}, в очереди {queued}.")

    def _local_finish_error(self, msg):
        self.local_start_btn.config(state="normal")
        self.local_stop_btn.config(state="disabled")
        self.health_var.set("ошибка входа")
        messagebox.showerror("Не удалось войти", msg)

    def _local_crash(self, tb_text):
        self.local_start_btn.config(state="normal")
        self.local_stop_btn.config(state="disabled")
        self.health_var.set("ошибка")
        self._log(f"[КРИТИЧЕСКАЯ ОШИБКА, операция остановлена]\n{tb_text}")
        messagebox.showerror(
            "Непредвиденная ошибка",
            "Операция остановилась из-за непредвиденной ошибки. Подробности — во вкладке «Журнал».",
        )

    def _start_recheck(self, mode):
        if self.mode.get() == "local":
            if self.local_busy:
                messagebox.showinfo("Уже работает", "Сначала дождитесь завершения текущей операции (скан или перепроверка).")
                return
            username = self.local_username.get().strip()
            password = self.local_password.get()
            if not username or not password or not self.current_state_path:
                messagebox.showinfo("Нет данных", "Сначала запустите обычный скан хотя бы раз.")
                return
            proxy = _entry_real_value(self.local_proxy) or None
            self.local_busy = True
            threading.Thread(target=self._local_recheck_worker, args=(username, password, mode, proxy), daemon=True).start()
        else:
            if not self.remote or not self.remote.connected:
                messagebox.showinfo("Нет подключения", "Сначала подключитесь к серверу.")
                return
            if self.remote.is_busy():
                messagebox.showinfo("Уже работает", "На сервере уже что-то выполняется — сначала нажмите «Стоп».")
                return
            flag = {"all": "--recheck-all", "free": "--recheck-free", "occupied": "--recheck-occupied"}.get(mode, "--recheck-free-with-cover")
            self.remote.start_scan(self.remote_username.get().strip(), extra_args=flag)
            self.remote_password_row.pack(fill="x", padx=12, pady=(0, 6))
            self._log(f"[перепроверка запущена: {flag}, введите пароль]")

    def _local_recheck_worker(self, username, password, mode, proxy=None):
        # Same thin-wrapper reasoning as _local_worker/_local_worker_impl —
        # an uncaught exception here (e.g. a torn read of the state file
        # from a concurrent scan, now prevented by local_busy, but still a
        # backstop against whatever else could go wrong) must not vanish
        # silently in this --windowed build.
        try:
            self._local_recheck_worker_impl(username, password, mode, proxy)
        except Exception:
            tb = traceback.format_exc()
            self._safe_after(lambda: self._local_crash(tb))
        finally:
            self.local_busy = False

    def _local_recheck_worker_impl(self, username, password, mode, proxy=None):
        client = LolClient("", "https://www.landsoflords.com", proxy=proxy)
        try:
            cli.login_with_timeout(client, username, password,
                                    log=lambda m: self._safe_after(lambda: self._log(m)))
        except (TimeoutError, ProtocolError, OSError) as e:
            self._safe_after(lambda: messagebox.showerror("Не удалось войти", str(e)))
            return
        path = self.current_state_path
        state = cli.load_state(path)
        self.local_stop_event = threading.Event()
        self._safe_after(lambda: self._log(f"[перепроверка запущена: {mode}]"))
        last_log_time = [0.0]

        def on_progress(completed, total, changed, chunk):
            self._safe_after(lambda: self._set_progress(completed, total, f"  (изменено: {changed})"))
            self._safe_after(lambda pts=list(chunk): self.map_view.mark_rechecked(pts))
            now = time.time()
            if now - last_log_time[0] >= 5:
                last_log_time[0] = now
                self._safe_after(lambda: self._log(f"[перепроверено {completed}/{total}, изменений: {changed}]"))

        finished = cli.recheck_points(client, path, state, self.local_stop_event, (username, password), mode=mode, on_progress=on_progress)
        self._safe_after(lambda: self._refresh_map_from_disk(path, preserve_overlay=True))
        if finished:
            self._safe_after(lambda: self._log("[перепроверка завершена]"))
            self._safe_after(lambda: notify("Перепроверка", "Готово."))
        else:
            # Остановилась раньше конца — почти всегда сеть/сессия (см.
            # run_relogin_check: после нескольких неудачных попыток релогина
            # процесс тихо прекращает работу, но старый код здесь всегда
            # писал "завершена"/"Готово.", как будто дошёл до конца —
            # заметить недосчёт можно было только по логу, сверяя итоговое
            # "N/M" с N << M. Курсор резюме есть только у mode="all" —
            # повторный запуск для него продолжит с этого места, для
            # остальных режимов начнёт заново (но это дешёвые наборы).
            resume_note = " (продолжит с места остановки)" if mode == "all" else ""
            self._safe_after(lambda: self._log(f"[перепроверка остановлена раньше конца — вероятно, сеть/сессия; запустите ещё раз{resume_note}]"))
            self._safe_after(lambda: notify("Перепроверка", "Остановлена раньше конца (сеть/сессия) — запустите ещё раз."))

    # ================= REMOTE MODE =================

    def _remote_connect(self):
        self._save_current_profile()
        host = self.remote_host.get().strip()
        user = self.remote_ssh_user.get().strip() or "root"
        key = self.remote_key_path.get().strip() or None
        if not host:
            messagebox.showerror("Нет данных", "Укажите хост.")
            return
        self.health_var.set("подключение…")
        threading.Thread(target=self._remote_connect_worker, args=(host, user, key), daemon=True).start()

    def _remote_connect_worker(self, host, user, key):
        mgr = RemoteScanManager(host, username=user, key_path=key)
        try:
            mgr.connect()
            mgr.ensure_remote_setup()
        except RemoteError as e:
            self._safe_after(lambda: self._remote_connect_error(str(e)))
            return
        self.remote = mgr
        self._safe_after(self._remote_connect_ok)

    def _remote_connect_ok(self):
        self.health_var.set("подключено")
        self.remote_start_btn.config(state="normal")
        self._log("[подключено к серверу]")

    def _remote_connect_error(self, msg):
        self.health_var.set("ошибка подключения")
        messagebox.showerror("Не удалось подключиться", msg)

    def _remote_start(self):
        if not self.remote or not self.remote.connected:
            return
        username = self.remote_username.get().strip()
        if not username:
            messagebox.showerror("Нет данных", "Введите игровой логин.")
            return
        self.remote_start_btn.config(state="disabled")
        self.remote_stop_btn.config(state="normal")
        threading.Thread(target=self._remote_start_worker, args=(username,), daemon=True).start()

    def _remote_start_worker(self, username):
        try:
            # Намеренно Path(__file__).parent, не APP_DIR — в собранном exe
            # это указывает на распакованный бандл (_MEIPASS), где реально
            # лежат исходники .py, которые нужно закинуть на сервер. Рядом с
            # настоящим exe этих файлов нет — только в бандле.
            self.remote.deploy_files(Path(__file__).parent)
            self.remote.start_scan(username)
        except RemoteError as e:
            self._safe_after(lambda: messagebox.showerror("Ошибка", str(e)))
            return
        self._safe_after(lambda: self.remote_password_row.pack(fill="x", padx=12, pady=(0, 6)))
        self._safe_after(lambda: self._log("[скрипт запущен, введите пароль от игры и нажмите «Отправить»]"))
        self.remote_stop_event = threading.Event()
        threading.Thread(target=self._remote_poll_worker, args=(self.remote_stop_event,), daemon=True).start()

    def _remote_send_password(self):
        pw = self.remote_password_entry.get()
        if not pw or not self.remote:
            return
        self.remote.send_password(pw)
        self.remote_password_entry.delete(0, "end")
        self.remote_password_row.pack_forget()

    def _remote_stop(self):
        if self.remote:
            self.remote.send_interrupt()
        if self.remote_stop_event:
            self.remote_stop_event.set()
        self.remote_start_btn.config(state="normal")
        self.remote_stop_btn.config(state="disabled")

    def _remote_poll_worker(self, stop_event):
        last_log = ""
        while not stop_event.is_set():
            try:
                log = self.remote.capture_log(lines=20)
            except RemoteError:
                break
            if log != last_log:
                last_log = log
                self._safe_after(lambda t=log: self._update_log(t))
                if "failures in a row" in log and "Relogged in" not in log.rsplit("failures in a row", 1)[-1]:
                    self._safe_after(lambda: self.health_var.set("сессия истекла?"))
                elif "Relogged in" in log or "checked:" in log or "rechecked:" in log:
                    self._safe_after(lambda: self.health_var.set("работает"))
                if "Stopped." in log or "Recheck done" in log:
                    self._safe_after(lambda: notify("Скан на сервере остановлен", "Проверьте лог для деталей."))
            try:
                self.remote.rebuild_map()
                remote_name = self.remote.find_remote_state_filename()
                if remote_name:
                    local_path = PROFILE_DATA_DIR / remote_name
                    self.remote.pull_state_file(local_path, remote_name)
                    self._safe_after(lambda p=local_path: self._refresh_map_from_disk(p))
            except RemoteError:
                pass
            for _ in range(60):
                if stop_event.is_set():
                    return
                time.sleep(1)

    def _update_log(self, text):
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", text)
        self.log_text.see("end")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
