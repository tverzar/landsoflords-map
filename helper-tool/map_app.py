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
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import uuid
import webbrowser
import winsound
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import ttk, messagebox, filedialog, simpledialog

from lol_api import LolClient, ProtocolError, format_map_coords, MINERAL_GROUND_TYPES
import continent_scan_cli as cli
import profiles
from remote import RemoteScanManager, RemoteError
import styles

try:
    import keyring
    import keyring.errors
except ImportError:
    keyring = None

PROFILE_DATA_DIR = Path(__file__).parent / "profile_data"
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
SUBMIT_BATCH_SIZE = 150  # точек — что раньше наступит, то и шлём
SUBMIT_INTERVAL_SECONDS = 120

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
LOCAL_LOGIN_PATH = Path(__file__).parent / "local_login.json"


def _load_local_settings():
    try:
        return json.loads(LOCAL_LOGIN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_saved_username():
    return _load_local_settings().get("username") or ""


def load_saved_proxy():
    return _load_local_settings().get("proxy") or ""


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
    style.configure("TButton", background=BG, foreground=INK, bordercolor=BORDER, padding=6)
    style.map("TButton", background=[("active", BORDER)])
    style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_INK)
    style.map("Accent.TButton", background=[("active", "#d99a4e")])
    style.configure("TEntry", fieldbackground=BG, foreground=INK, bordercolor=BORDER, insertcolor=INK)
    style.configure("TCombobox", fieldbackground=BG, foreground=INK, background=BG)
    style.configure("TNotebook", background=PANEL, bordercolor=BORDER)
    style.configure("TNotebook.Tab", background=BG, foreground=INK_DIM, padding=(10, 5))
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


def fetch_known_cells():
    """Множество "x,y" уже известных основному скану координат — см.
    KNOWN_CELLS_URL. Пустое множество при любой сетевой/форматной ошибке
    (нет файла, сервер недоступен и т.п.) — вызывающий код тогда просто
    работает как раньше, без подсказки."""
    req = urllib.request.Request(KNOWN_CELLS_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = gzip.decompress(resp.read())
        flat = json.loads(raw)
        return {f"{flat[i]},{flat[i + 1]}" for i in range(0, len(flat), 2)}
    except (urllib.error.URLError, OSError, ValueError, gzip.BadGzipFile):
        return set()


def submit_batch(points, submitter, session_id, domain):
    """Шлёт один батч точек на приёмный воркер — вызывается из фонового
    потока (см. _flush_submit_buffer в App), никогда из потока самого
    скана, чтобы сетевой сбой/задержка тут не тормозили сам скан. Молча
    проглатывает любые ошибки — присылка это best-effort дополнение к
    основному скану, а не то, от чего он должен зависеть."""
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
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except (urllib.error.URLError, OSError):
        pass


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
        self.remote = None
        self.remote_stop_event = None
        self.remote_awaiting_password = False
        self.current_state_path = None
        self.current_state = None

        self._build()
        root.after(150, self._poll_events)

    # ---------- layout ----------

    def _build(self):
        paned = tk.PanedWindow(self.root, orient="horizontal", bg=BG, sashwidth=4, bd=0)
        paned.pack(fill="both", expand=True)

        sidebar_outer = ttk.Frame(paned, width=300)
        paned.add(sidebar_outer, minsize=280)
        canvas_scroll = tk.Canvas(sidebar_outer, bg=PANEL, highlightthickness=0, width=300)
        vbar = ttk.Scrollbar(sidebar_outer, orient="vertical", command=canvas_scroll.yview)
        self.sidebar = ttk.Frame(canvas_scroll)
        self.sidebar.bind("<Configure>", lambda e: canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all")))
        canvas_scroll.create_window((0, 0), window=self.sidebar, anchor="nw", width=282)
        canvas_scroll.configure(yscrollcommand=vbar.set)
        canvas_scroll.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.map_view = MapView(paned)
        paned.add(self.map_view, minsize=400)

        self._build_sidebar()

    def _build_sidebar(self):
        s = self.sidebar
        pad = dict(padx=12, pady=(8, 2))

        mode_row = ttk.Frame(s)
        mode_row.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Radiobutton(mode_row, text="Локально", value="local", variable=self.mode,
                         command=self._on_mode_change).pack(side="left", expand=True, fill="x")
        ttk.Radiobutton(mode_row, text="Сервер", value="remote", variable=self.mode,
                         command=self._on_mode_change).pack(side="left", expand=True, fill="x")

        self.health_var = tk.StringVar(value="")
        ttk.Label(s, textvariable=self.health_var, style="Dim.TLabel").pack(fill="x", padx=12)

        # --- local frame ---
        self.local_frame = ttk.Frame(s)
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
        self.remember_password_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.local_frame, text="Запомнить пароль (в системном хранилище)",
            variable=self.remember_password_var,
        ).pack(fill="x", padx=12, pady=(2, 0))
        self.share_progress_var = tk.BooleanVar(value=False)
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
        self.remote_frame = ttk.Frame(s)
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
        self.progress_label = ttk.Label(s, textvariable=self.progress_var, style="Dim.TLabel")
        self.progress_label.pack(fill="x", padx=12, pady=(6, 2))
        self.progress_bar = ttk.Progressbar(s, mode="determinate", maximum=1)
        self.progress_bar.pack(fill="x", padx=12)

        recheck_row = ttk.Frame(s)
        recheck_row.pack(fill="x", padx=12, pady=8)
        ttk.Button(recheck_row, text="Своб. с постройкой", command=lambda: self._start_recheck("free_with_cover")).pack(fill="x")
        ttk.Button(recheck_row, text="Полный пересчёт", command=lambda: self._start_recheck("all")).pack(fill="x", pady=(4, 0))

        # --- live log ---
        ttk.Label(s, text="Лог", style="Dim.TLabel").pack(fill="x", padx=12, pady=(4, 2))
        self.log_text = tk.Text(s, height=8, bg=BG, fg=INK_DIM, insertbackground=INK,
                                 relief="flat", font=("Consolas", 8), wrap="word")
        self.log_text.pack(fill="x", padx=12)

        # --- owner search ---
        ttk.Label(s, text="Поиск владельца", style="Dim.TLabel").pack(fill="x", padx=12, pady=(8, 2))
        self.owner_search = ttk.Entry(s)
        self.owner_search.pack(fill="x", padx=12)
        self.owner_search.bind("<Return>", self._on_owner_search)
        self.owner_results = tk.Listbox(s, height=3, bg=BG, fg=INK_DIM, relief="flat",
                                         highlightthickness=0, font=("", 9))
        self.owner_results.pack(fill="x", padx=12, pady=(2, 4))
        self.owner_results.bind("<<ListboxSelect>>", self._on_owner_pick)

        # --- toggles ---
        ttk.Checkbutton(s, text="Не удалось проверить", variable=self.map_view.show_failed,
                         command=self.map_view.render).pack(fill="x", padx=12, pady=2)
        ttk.Checkbutton(s, text="Территория (своя/чужая)", variable=self.map_view.show_territory,
                         command=self.map_view.render).pack(fill="x", padx=12, pady=2)

        ttk.Button(s, text="Экспорт минералов (CSV)", command=self._export_csv).pack(fill="x", padx=12, pady=(8, 4))
        ttk.Button(s, text="Показать всё", command=self._reset_filters).pack(fill="x", padx=12, pady=(0, 8))

        # --- grouped legend ---
        ttk.Label(s, text="Легенда", style="Dim.TLabel").pack(fill="x", padx=12, pady=(4, 2))
        self.legend_groups = {}
        for name in ("Минералы", "Суша", "Вода"):
            grp = CollapsibleGroup(s, name, self._on_legend_click)
            grp.pack(fill="x", padx=12)
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

    def _refresh_map_from_disk(self, path):
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.current_state = state
        self.current_state_path = path
        self.map_view.load_state(state)
        self._refresh_legend()

    def _refresh_legend(self):
        if not self.current_state:
            return
        pts = load_points(self.current_state)
        groups = {"Минералы": {}, "Суша": {}, "Вода": {}}
        for p in pts:
            g = groups[p["group"]]
            if p["type"] not in g:
                g[p["type"]] = [p["color"], p["name"], 0, p["mineral"]]
            g[p["type"]][2] += 1
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
        if not query or not self.current_state:
            return
        seen = set()
        for hit in self.current_state.get("results", {}).values():
            name = hit.get("owner_org_name")
            if name and query in name.lower() and name not in seen:
                seen.add(name)
                self.owner_results.insert("end", name)

    def _on_owner_pick(self, _e=None):
        sel = self.owner_results.curselection()
        if not sel:
            return
        name = self.owner_results.get(sel[0])
        coords = None
        xs, ys, n = 0, 0, 0
        for hit in self.current_state.get("results", {}).values():
            if hit.get("owner_org_name") == name:
                xs += hit["x"]; ys += hit["y"]; n += 1
        if n:
            coords = (xs / n, ys / n)
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
        username = self.local_username.get().strip()
        password = self.local_password.get()
        proxy = _entry_real_value(self.local_proxy) or None
        if not username or not password:
            messagebox.showerror("Нет данных", "Введите логин и пароль.")
            return
        save_username(username)
        save_local_settings(proxy=proxy or "")
        if self.remember_password_var.get():
            save_password(username, password)
        else:
            forget_password(username)
        self.local_start_btn.config(state="disabled")
        self.local_stop_btn.config(state="normal")
        self.health_var.set("подключение…")
        threading.Thread(target=self._local_worker, args=(username, password, proxy), daemon=True).start()

    def _local_stop(self):
        if self.local_stop_event:
            self.local_stop_event.set()
        self.local_stop_btn.config(state="disabled")

    def _local_worker(self, username, password, proxy=None):
        try:
            client = LolClient("", "https://www.landsoflords.com", proxy=proxy)
            client.login(username, password)
            client.sync()
        except (ProtocolError, OSError) as e:
            self._safe_after(lambda: self._local_finish_error(str(e)))
            return
        self.local_client = client
        login_creds = (username, password)

        cx_raw, cy_raw = self.local_x.get().strip(), self.local_y.get().strip()
        cx = int(cx_raw) if cx_raw else client.org_coords[0]
        cy = int(cy_raw) if cy_raw else client.org_coords[1]

        path = state_path_for(cx, cy)
        state = cli.load_state(path) or {"x": cx, "y": cy, "step": 1, "results": {}, "frontier": [], "failed": []}
        frontier_points = [tuple(p) for p in state.get("frontier", [])] + [tuple(p) for p in state.get("failed", [])]
        state["failed"] = []
        if not frontier_points and not state["results"]:
            frontier_points = [(cx, cy)]

        # Подтягиваем, что уже известно основному скану (см. build_known_cells.py),
        # чтобы не гонять запросы по территории, которую кто-то другой уже
        # прошёл — только "работает" (пустой набор), если файл не скачался.
        self._safe_after(lambda: self.health_var.set("проверяю уже известную территорию…"))
        known_cells = fetch_known_cells()
        if known_cells:
            existing_keys = set(state["results"].keys()) | {f"{p[0]},{p[1]}" for p in frontier_points}
            boundary = known_cells_boundary(known_cells, near=(cx, cy))
            frontier_points += [(x, y) for x, y in boundary if f"{x},{y}" not in existing_keys]

        from collections import deque
        frontier = deque(frontier_points)
        results = state["results"]
        seen = set(results.keys()) | {f"{p[0]},{p[1]}" for p in frontier} | known_cells

        self._safe_after(lambda: self._refresh_map_from_disk(path))
        self._safe_after(lambda: self.health_var.set("работает"))

        share_progress = self.share_progress_var.get()
        submit_buffer, last_submit_time = [], time.time()
        session_id = str(uuid.uuid4())

        def flush_submit_buffer():
            if not submit_buffer:
                return
            batch = list(submit_buffer)
            submit_buffer.clear()
            threading.Thread(
                target=submit_batch, args=(batch, username, session_id, (cx, cy)), daemon=True,
            ).start()

        self.local_stop_event = threading.Event()
        stop_event = self.local_stop_event
        completed = since_save = consecutive_failures = 0
        with ThreadPoolExecutor(max_workers=cli.CONCURRENCY) as pool:
            while frontier:
                if stop_event.is_set():
                    break
                chunk = [frontier.popleft() for _ in range(min(cli.CHUNK, len(frontier)))]
                for hit in [f.result() for f in [pool.submit(cli.fetch_point, client, p) for p in chunk]]:
                    completed += 1
                    if hit.get("error"):
                        consecutive_failures += 1
                        continue
                    consecutive_failures = 0
                    key = f"{hit['x']},{hit['y']}"
                    results[key] = hit
                    since_save += 1
                    if share_progress:
                        submit_buffer.append(hit)
                    if hit["type"] not in cli.CONTINENT_BOUNDARY_TYPES:
                        for nx, ny in cli.grid_neighbors(hit["x"], hit["y"], 1):
                            nkey = f"{nx},{ny}"
                            if nkey not in seen:
                                seen.add(nkey)
                                frontier.append((nx, ny))
                    if since_save >= cli.SAVE_EVERY:
                        state["frontier"] = [list(p) for p in frontier]
                        cli.save_state(path, state)
                        since_save = 0
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
                self._safe_after(lambda p=path: self._refresh_map_from_disk(p))
                time.sleep(cli.PAUSE_SECONDS)

        if share_progress:
            flush_submit_buffer()
        state["frontier"] = [list(p) for p in frontier]
        cli.save_state(path, state)
        self._safe_after(lambda: self._local_finish(len(results), len(frontier)))

    def _local_finish(self, done, queued):
        self.local_start_btn.config(state="normal")
        self.local_stop_btn.config(state="disabled")
        self.health_var.set("остановлено")
        notify("Скан остановлен", f"Сохранено {done}, в очереди {queued}.")

    def _local_finish_error(self, msg):
        self.local_start_btn.config(state="normal")
        self.local_stop_btn.config(state="disabled")
        self.health_var.set("ошибка входа")
        messagebox.showerror("Не удалось войти", msg)

    def _start_recheck(self, mode):
        if self.mode.get() == "local":
            username = self.local_username.get().strip()
            password = self.local_password.get()
            if not username or not password or not self.current_state_path:
                messagebox.showinfo("Нет данных", "Сначала запустите обычный скан хотя бы раз.")
                return
            proxy = _entry_real_value(self.local_proxy) or None
            threading.Thread(target=self._local_recheck_worker, args=(username, password, mode, proxy), daemon=True).start()
        else:
            if not self.remote or not self.remote.connected:
                messagebox.showinfo("Нет подключения", "Сначала подключитесь к серверу.")
                return
            if self.remote.is_busy():
                messagebox.showinfo("Уже работает", "На сервере уже что-то выполняется — сначала нажмите «Стоп».")
                return
            flag = "--recheck-all" if mode == "all" else "--recheck-free-with-cover"
            self.remote.start_scan(self.remote_username.get().strip(), extra_args=flag)
            self.remote_password_row.pack(fill="x", padx=12, pady=(0, 6))
            self._log(f"[перепроверка запущена: {flag}, введите пароль]")

    def _local_recheck_worker(self, username, password, mode, proxy=None):
        try:
            client = LolClient("", "https://www.landsoflords.com", proxy=proxy)
            client.login(username, password)
            client.sync()
        except (ProtocolError, OSError) as e:
            self._safe_after(lambda: messagebox.showerror("Не удалось войти", str(e)))
            return
        path = self.current_state_path
        state = cli.load_state(path)
        self.local_stop_event = threading.Event()
        cli.recheck_points(client, path, state, self.local_stop_event, (username, password), mode=mode)
        self._safe_after(lambda: self._refresh_map_from_disk(path))
        self._safe_after(lambda: notify("Перепроверка", "Готово."))

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
