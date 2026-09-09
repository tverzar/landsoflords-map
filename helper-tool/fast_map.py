"""Fast raster map widget for the desktop scanner.

The legacy widget created one Tk canvas rectangle per map point on every
pan, zoom and mouse move.  This implementation keeps the data in a compact
model, renders the terrain into one Pillow image once, and presents a single
PhotoImage to Tk.  Hover lookup is O(1) by coordinate.
"""
from __future__ import annotations

import re
import tkinter as tk
import webbrowser
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageTk

from lol_api import MINERAL_GROUND_TYPES, format_map_coords
import styles


MAP_BG = "#0b0f14"
INK = "#eef2f5"
PANEL = "#141c26"


@dataclass(slots=True)
class MapPoint:
    x: int
    y: int
    type_code: str
    name: str
    quality: int
    status: str
    owner: str | None
    color: str


@dataclass(slots=True)
class MapModel:
    points: list[MapPoint]
    by_coord: dict[tuple[int, int], MapPoint]
    failed: list[tuple[int, int]]
    domain: tuple[int | None, int | None]
    bounds: tuple[int, int, int, int]
    base: Image.Image
    territory: Image.Image
    type_counts: dict[str, tuple[str, str, int, bool]]
    owner_centers: dict[str, tuple[float, float, int]]

    @classmethod
    def from_state(cls, state: dict) -> "MapModel":
        raw = state.get("results", {})
        points: list[MapPoint] = []
        by_coord: dict[tuple[int, int], MapPoint] = {}
        type_counts: dict[str, list] = {}
        owner_acc: dict[str, list[int]] = {}
        min_x = min_y = 2**31 - 1
        max_x = max_y = -(2**31)

        for hit in raw.values():
            x, y = int(hit["x"]), int(hit["y"])
            code = hit["type"]
            point = MapPoint(
                x=x, y=y, type_code=code,
                name=hit.get("name", code),
                quality=int(hit.get("quality_pct") or 0),
                status=hit.get("status", "free"),
                owner=hit.get("owner_org_name"),
                color=styles.marker_color(code),
            )
            points.append(point)
            by_coord[(x, y)] = point
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            row = type_counts.setdefault(code, [point.color, point.name, 0, code in MINERAL_GROUND_TYPES])
            row[2] += 1
            if point.owner:
                acc = owner_acc.setdefault(point.owner, [0, 0, 0])
                acc[0] += x; acc[1] += y; acc[2] += 1

        if not points:
            min_x = max_x = min_y = max_y = 0
        pad = 2
        min_x -= pad; min_y -= pad; max_x += pad; max_y += pad
        width, height = max_x - min_x + 1, max_y - min_y + 1
        # A malformed/cross-continent file must not allocate an unbounded image.
        if width * height > 50_000_000:
            raise ValueError(f"Карта слишком велика для превью: {width}×{height}")

        base = Image.new("RGB", (width, height), MAP_BG)
        territory = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        bp = base.load(); tp = territory.load()
        for p in points:
            ix, iy = p.x - min_x, p.y - min_y
            bp[ix, iy] = _hex_rgb(p.color)
            if p.status == "ours":
                tp[ix, iy] = (77, 196, 138, 170)
            elif p.status == "occupied":
                tp[ix, iy] = (225, 82, 121, 145)

        owner_centers = {name: (sx / n, sy / n, n) for name, (sx, sy, n) in owner_acc.items()}
        frozen_counts = {code: tuple(row) for code, row in type_counts.items()}
        return cls(
            points=points, by_coord=by_coord,
            failed=[tuple(p[:2]) for p in state.get("failed", [])],
            domain=(state.get("x"), state.get("y")),
            bounds=(min_x, min_y, max_x, max_y), base=base,
            territory=territory, type_counts=frozen_counts,
            owner_centers=owner_centers,
        )


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


class FastMapView(tk.Frame):
    """Single-image, debounced pan/zoom map with constant-time hover."""

    def __init__(self, parent, on_coord_search=None, **kwargs):
        super().__init__(parent, bg=MAP_BG, **kwargs)
        self.on_coord_search = on_coord_search
        self.model: MapModel | None = None
        self.selected_type: str | None = None
        self.selected_owner: str | None = None
        self.show_failed = tk.BooleanVar(value=True)
        self.show_territory = tk.BooleanVar(value=True)
        self.show_recheck_progress = tk.BooleanVar(value=True)
        self.recheck_overlay: Image.Image | None = None
        # Independent from model.bounds on purpose — mark_rechecked() is
        # used during an active wave scan too, which by design doesn't
        # reload/resize the base model mid-scan (see the comment in
        # map_app.py's _local_worker_impl on why), yet a wave scan is
        # exactly the case that discovers cells OUTSIDE whatever bounds the
        # model had at scan start. Grows on demand instead.
        self.overlay_bounds: tuple[int, int, int, int] | None = None
        self.scale = 1.0  # screen pixels per world cell
        self.off_x = self.off_y = 0.0
        self._drag: tuple[int, int] | None = None
        self._render_job = None
        self._photo = None
        self._tooltip = None
        self._build()

    @staticmethod
    def prepare_state(state: dict) -> MapModel:
        return MapModel.from_state(state)

    def _build(self):
        toolbar = tk.Frame(self, bg="#111923", padx=10, pady=8)
        toolbar.pack(fill="x")
        self._toolbar_button(toolbar, "+", lambda: self.zoom_by(1.35), width=3).pack(side="left")
        self._toolbar_button(toolbar, "−", lambda: self.zoom_by(1 / 1.35), width=3).pack(side="left", padx=5)
        self._toolbar_button(toolbar, "Вся карта", self.fit_to_view).pack(side="left")
        self.coord_entry = tk.Entry(
            toolbar, bg="#0b1118", fg=INK, insertbackground=INK,
            relief="flat", font=("Segoe UI", 10), width=20,
        )
        self.coord_entry.pack(side="left", padx=(14, 5), ipady=5)
        self.coord_entry.insert(0, "10957E28048N")
        self._toolbar_button(toolbar, "Перейти", self._jump_from_entry).pack(side="left")
        self.zoom_label = tk.Label(toolbar, text="", bg="#111923", fg="#8190a3", font=("Segoe UI", 9))
        self.zoom_label.pack(side="right")

        self.canvas = tk.Canvas(self, bg=MAP_BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.schedule_render())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Motion>", self._on_hover)

    @staticmethod
    def _toolbar_button(parent, text, command, width=None):
        return tk.Button(
            parent, text=text, command=command, width=width,
            bg="#202c3a", fg=INK, activebackground="#2c3b4d",
            activeforeground=INK, relief="flat", bd=0,
            font=("Segoe UI", 9, "bold"), padx=10, pady=5, cursor="hand2",
        )

    def load_model(self, model: MapModel, preserve_overlay: bool = False):
        """preserve_overlay=True carries the "already visited this session"
        shading across a reload instead of wiping it — used for the
        end-of-operation refresh after a scan/recheck pass (so the
        just-finished progress stays visible), not for a genuinely fresh
        load (app startup, switching domain) where a blank overlay is
        correct. The reload's bounds can be LARGER than before (a wave
        scan discovering brand new territory grows the known bounding box)
        — reposition the old pixels into a correctly-sized/offset canvas
        rather than just keeping the old image, or already-marked cells
        would drift relative to the newly (re)computed base image."""
        old_overlay, old_bounds = self.recheck_overlay, self.overlay_bounds
        self.model = model
        self.selected_type = self.selected_owner = None
        if preserve_overlay and old_overlay is not None and old_bounds is not None:
            self.recheck_overlay = old_overlay
            self.overlay_bounds = old_bounds
        else:
            self.recheck_overlay = None
            self.overlay_bounds = None
        self.fit_to_view()

    @staticmethod
    def _repositioned_overlay(old_overlay, old_bounds, new_bounds):
        if old_bounds == new_bounds:
            return old_overlay
        new_w = new_bounds[2] - new_bounds[0] + 1
        new_h = new_bounds[3] - new_bounds[1] + 1
        canvas = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
        # No mask arg: paste() with a mask alpha-blends source over dest
        # (squares the alpha, muddies white toward grey) — passing none
        # does a verbatim RGBA pixel copy instead, which is what relocating
        # already-composited pixels onto a bigger blank canvas needs.
        canvas.paste(old_overlay, (old_bounds[0] - new_bounds[0], old_bounds[1] - new_bounds[1]))
        return canvas

    def mark_rechecked(self, coords):
        """Shade already-rechecked cells bright white — lets --recheck-all's
        slow multi-day pass (and, now, a plain wave scan too) be watched
        visually as a spreading "done" wash instead of only a bare
        progress-bar percentage. White reads clearly against every
        terrain/territory color underneath, unlike a dark tint which blends
        into water/rock. Cheap: one putpixel per coord (CHUNK-sized
        batches, ~10 points).

        Deliberately keyed to its own overlay_bounds, not model.bounds: a
        wave scan doesn't reload/resize the base model mid-scan (see the
        comment in map_app.py's _local_worker_impl on why — it used to
        look like a freeze on a large account) yet is exactly the case
        that discovers cells OUTSIDE whatever bounds the model had at scan
        start. So the overlay grows its own canvas on demand instead of
        silently dropping out-of-bounds marks."""
        if not coords:
            return
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        if self.recheck_overlay is None:
            # Seed from the current model's own bounds when one is loaded,
            # not just the first coord's tiny bbox — --recheck-all's coords
            # are always within model.bounds by definition (it only ever
            # revisits already-known cells), so this sizes the canvas once,
            # correctly, up front instead of growing it by a few pixels on
            # nearly every chunk as scattered dict-order coords trickle in
            # (each growth step is a full image copy — cheap once, not
            # thousands of times over a multi-million-cell pass). A wave
            # scan's genuinely-new, out-of-bounds coords still grow it
            # further via the branch below, just far less often.
            if self.model:
                mb = self.model.bounds
                self.overlay_bounds = (min(mb[0], *xs), min(mb[1], *ys), max(mb[2], *xs), max(mb[3], *ys))
            else:
                self.overlay_bounds = (min(xs), min(ys), max(xs), max(ys))
            w = self.overlay_bounds[2] - self.overlay_bounds[0] + 1
            h = self.overlay_bounds[3] - self.overlay_bounds[1] + 1
            self.recheck_overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        else:
            min_x, min_y, max_x, max_y = self.overlay_bounds
            needed = (min(min_x, *xs), min(min_y, *ys), max(max_x, *xs), max(max_y, *ys))
            if needed != self.overlay_bounds:
                self.recheck_overlay = self._repositioned_overlay(self.recheck_overlay, self.overlay_bounds, needed)
                self.overlay_bounds = needed
        min_x, min_y, _, _ = self.overlay_bounds
        px = self.recheck_overlay.load()
        for wx, wy in coords:
            px[wx - min_x, wy - min_y] = (255, 255, 255, 165)
        self.schedule_render()

    def load_state(self, state: dict):
        self.load_model(self.prepare_state(state))

    def fit_to_view(self):
        if not self.model:
            return
        w, h = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        min_x, min_y, max_x, max_y = self.model.bounds
        world_w, world_h = max_x - min_x + 1, max_y - min_y + 1
        self.scale = max(0.03, min(w / world_w, h / world_h, 18) * 0.94)
        self.off_x = (w - world_w * self.scale) / 2
        self.off_y = (h - world_h * self.scale) / 2
        self.schedule_render()

    def zoom_by(self, factor: float, anchor=None):
        if not self.model:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        ax, ay = anchor or (w / 2, h / 2)
        wx, wy = self._screen_to_world(ax, ay)
        self.scale = max(0.03, min(48.0, self.scale * factor))
        min_x, min_y, _, _ = self.model.bounds
        self.off_x = ax - (wx - min_x) * self.scale
        self.off_y = ay - (wy - min_y) * self.scale
        self.schedule_render()

    def jump_to(self, wx: float, wy: float):
        if not self.model:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.scale = max(self.scale, 10.0)
        min_x, min_y, _, _ = self.model.bounds
        self.off_x = w / 2 - (wx - min_x) * self.scale
        self.off_y = h / 2 - (wy - min_y) * self.scale
        self.schedule_render()

    def set_type_filter(self, type_code):
        self.selected_type = None if self.selected_type == type_code else type_code
        self.selected_owner = None
        self.schedule_render()

    def set_owner_filter(self, owner_name, jump_coords=None):
        self.selected_owner = None if self.selected_owner == owner_name else owner_name
        self.selected_type = None
        if self.selected_owner and jump_coords:
            self.jump_to(*jump_coords)
        else:
            self.schedule_render()

    def render(self):
        self._render_job = None
        model = self.model
        if not model:
            return
        w, h = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        inv = 1.0 / self.scale
        image = model.base.transform(
            (w, h), Image.Transform.AFFINE,
            (inv, 0, -self.off_x * inv, 0, inv, -self.off_y * inv),
            resample=Image.Resampling.NEAREST, fillcolor=MAP_BG,
        ).convert("RGBA")

        if self.show_territory.get():
            overlay = model.territory.transform(
                (w, h), Image.Transform.AFFINE,
                (inv, 0, -self.off_x * inv, 0, inv, -self.off_y * inv),
                resample=Image.Resampling.NEAREST, fillcolor=(0, 0, 0, 0),
            )
            image.alpha_composite(overlay)

        if self.recheck_overlay is not None and self.show_recheck_progress.get():
            # overlay_bounds is independent of model.bounds (see
            # mark_rechecked) — shift the transform's constant term by the
            # offset between the two origins, or the shading would drift
            # relative to the base map whenever they differ.
            model_min_x, model_min_y, _, _ = model.bounds
            ov_min_x, ov_min_y, _, _ = self.overlay_bounds
            dx0, dy0 = model_min_x - ov_min_x, model_min_y - ov_min_y
            # Zoomed out beyond 1 world-unit-per-pixel, a plain NEAREST
            # affine sample point-samples exactly one source pixel per screen
            # pixel — with only a sparse trickle of marked cells against
            # millions, the odds any given sample lands on one are near
            # zero, so the wash flickered in and out of existence instead of
            # reading as progress. Box-reduce the overlay first so
            # downsampling averages neighboring pixels instead of picking
            # one, then undo the resulting alpha dilution (reduce() box-
            # averages RGBA correctly — white stays white — but a single
            # marked cell in an f*f block still comes out at ~1/(f*f) of its
            # alpha, i.e. invisible against colorful terrain; any cell
            # marked in a block should read as "this block is visited," not
            # "this block is 1/16 visited").
            f = max(1, round(1 / self.scale)) if self.scale < 1 else 1
            src = self.recheck_overlay.reduce(f) if f > 1 else self.recheck_overlay
            if f > 1:
                boost = f * f
                r, g, b, av = src.split()
                av = av.point(lambda v: min(255, v * boost))
                src = Image.merge("RGBA", (r, g, b, av))
            a = inv / f
            overlay = src.transform(
                (w, h), Image.Transform.AFFINE,
                (a, 0, (-self.off_x * inv + dx0) / f, 0, a, (-self.off_y * inv + dy0) / f),
                resample=Image.Resampling.NEAREST, fillcolor=(0, 0, 0, 0),
            )
            image.alpha_composite(overlay)

        selected_type, selected_owner = self.selected_type, self.selected_owner
        if selected_type or selected_owner:
            veil = Image.new("RGBA", image.size, (5, 8, 12, 190))
            image.alpha_composite(veil)
            draw = ImageDraw.Draw(image)
            x0, y0, x1, y1 = self._visible_world_bounds(w, h)
            cell = max(1, round(self.scale))
            for wy in range(y0, y1 + 1):
                for wx in range(x0, x1 + 1):
                    p = model.by_coord.get((wx, wy))
                    if p and (p.type_code == selected_type or (selected_owner and p.owner == selected_owner)):
                        sx, sy = self._world_to_screen(wx, wy)
                        draw.rectangle((sx, sy, sx + cell, sy + cell), fill=p.color)

        draw = ImageDraw.Draw(image)
        if self.show_failed.get() and self.scale >= 1:
            for wx, wy in model.failed:
                sx, sy = self._world_to_screen(wx, wy)
                if -4 <= sx <= w + 4 and -4 <= sy <= h + 4:
                    draw.rectangle((sx, sy, sx + max(2, self.scale), sy + max(2, self.scale)), fill="#ff4858")
        if model.domain[0] is not None:
            dx, dy = self._world_to_screen(*model.domain)
            draw.ellipse((dx - 7, dy - 7, dx + 7, dy + 7), outline=INK, width=2)

        self._photo = ImageTk.PhotoImage(image)
        self.canvas.delete("map")
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw", tags="map")
        self.canvas.tag_lower("map")
        self.zoom_label.configure(text=f"{self.scale:.1f} px/клетку")

    def schedule_render(self):
        if self._render_job is None:
            self._render_job = self.after(16, self.render)

    def _visible_world_bounds(self, width, height):
        a = self._screen_to_world(0, 0); b = self._screen_to_world(width, height)
        return int(a[0]) - 1, int(a[1]) - 1, int(b[0]) + 1, int(b[1]) + 1

    def _world_to_screen(self, wx, wy):
        min_x, min_y, _, _ = self.model.bounds
        return (wx - min_x) * self.scale + self.off_x, (wy - min_y) * self.scale + self.off_y

    def _screen_to_world(self, sx, sy):
        min_x, min_y, _, _ = self.model.bounds
        return (sx - self.off_x) / self.scale + min_x, (sy - self.off_y) / self.scale + min_y

    def _jump_from_entry(self):
        m = re.search(r"(\d+)\s*([EWew])\s*(\d+)\s*([NSns])", self.coord_entry.get())
        if m:
            x = int(m[1]) * (1 if m[2].upper() == "E" else -1)
            y = int(m[3]) * (-1 if m[4].upper() == "N" else 1)
            self.jump_to(x, y)

    def _on_press(self, event):
        self._drag = (event.x, event.y)

    def _on_drag(self, event):
        if not self._drag:
            return
        x, y = self._drag
        self.off_x += event.x - x; self.off_y += event.y - y
        self._drag = (event.x, event.y)
        self.schedule_render()

    def _on_release(self, _event):
        self._drag = None

    def _on_wheel(self, event):
        self.zoom_by(1.18 if event.delta > 0 else 1 / 1.18, (event.x, event.y))

    def _on_double_click(self, event):
        wx, wy = self._screen_to_world(event.x, event.y)
        webbrowser.open(f"https://www.landsoflords.com/map/{format_map_coords(round(wx), round(wy))}")

    def _on_hover(self, event):
        if not self.model:
            return
        wx, wy = self._screen_to_world(event.x, event.y)
        p = self.model.by_coord.get((round(wx), round(wy)))
        if self._tooltip:
            self.canvas.delete(self._tooltip)
            self._tooltip = None
        if not p:
            return
        status = {"ours": "наш домен", "occupied": f"занято: {p.owner or '?'}"}.get(p.status, "свободно")
        text = f"{p.name} · {p.quality}%\n{status}"
        self._tooltip = self.canvas.create_text(
            event.x + 14, event.y + 12, text=text, anchor="nw", fill=INK,
            font=("Segoe UI", 9), tags="overlay",
        )
