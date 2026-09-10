"""Headless continent-scan runner — the same wave/flood-fill logic as the
GUI's "Континент волной от домена", but with no Tkinter/Pillow/keyring
dependency, meant to run unattended on a remote server (systemd service,
tmux, nohup) instead of your own PC. Uses only lol_api.py + the standard
library — nothing to pip install.

Writes the exact same profile_data/continent_<x>_<y>_s<step>.json format
the desktop tools already read, so you can rsync/scp that file back and
keep using visualize_continent.py / continent_viewer.py /
build_map_artifact.py locally, unchanged.

Usage:
    python continent_scan_cli.py --phpsessid <cookie> [--step 1]
    python continent_scan_cli.py --username X --password Y [--step 1]

Credentials can also come from LOL_USERNAME / LOL_PASSWORD / LOL_PHPSESSID
environment variables instead of flags — do that (e.g. via a chmod-600
EnvironmentFile for a systemd service) if the box has other users on it,
since command-line arguments are visible to anyone via `ps aux`, but env
vars set this way generally aren't.

With no --x/--y, starts from your own domain's coordinates (discovered via
login/sync). Ctrl+C, or `kill`/`systemctl stop` (SIGTERM), stops cleanly —
finishes the in-flight batch, saves progress, then exits. Re-run afterwards
with the same X/Y/step (or nothing, if it's your own domain) to resume.
"""
import argparse
import getpass
import json
import os
import re
import signal
import sys
import threading
import time
from array import array
from collections import deque
from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from lol_api import LolClient, ProtocolError, WALL_TYPES

PROFILE_DATA_DIR = Path(__file__).parent / "profile_data"
CONTINENT_BOUNDARY_TYPES = {"swater"}  # open sea — the wave stops here; ice is crossable land within a continent, only water separates continents
# Раньше волна доходила ровно до первой клетки воды и там глушилась
# полностью — на карте вокруг берега была только однослойная кромка, а
# дальше сразу голый край канвы. Теперь волна вправе уйти вглубь воды ещё
# на столько клеток, сколько тут указано (не до бесконечности — открытый
# океан не сканируется целиком), давая настоящую полосу воды у побережья
# вместо тонкой каёмки. 2026-09-03, по просьбе пользователя.
WATER_EXPAND_DEPTH = 4
CONCURRENCY = 9
CHUNK = 10
PAUSE_SECONDS = 0.05
# Пробовали поднять до 15/15 (2026-08-20) — скорость не выросла (осталась
# ~2.0 точек/сек и с 9, и с 15 потоками), при этом среднее время ответа
# выросло пропорционально. Похоже на серверную блокировку PHP-сессии
# (PHPSESSID = файловая sess-блокировка по умолчанию в PHP) — запросы
# одной и той же сессии сервер обрабатывает строго по одному, так что
# клиентский параллелизм упирается в эту очередь, а не в сеть/CPU.
# Реальное ускорение возможно только через отдельную сессию/аккаунт
# (второй скан), не через рост CONCURRENCY этого клиента.
SAVE_EVERY = 250  # how often the wave/recheck loops flush pending points and
# a fresh frontier/failed snapshot — see append_results()/save_meta() below.
# Was raised from 25 back when every save did a full json.dumps() of the
# entire results dict (a real memory spike on a 1.9GB box past ~1M points);
# now that saves only append the points found since the last flush, the
# spike is gone and this number is mostly about not losing too much
# re-fetch work if the process dies mid-batch, not about save cost.
FAILURE_CIRCUIT_BREAKER = 20  # this many consecutive request failures likely means a dead session, not bad luck


class CompactResults(MutableMapping):
    """Memory-compact stand-in for a plain {"x,y": {...}} results dict.

    Measured with tracemalloc against a real production snapshot
    (2026-08-26, ~1.64M points, after the near-OOM episodes that followed
    the 2026-08-25 save-format fix): a plain dict per point costs ~272
    bytes of pure structural overhead before any string data is even
    counted — ~1.18GB just in dict scaffolding at that point count, the
    single biggest remaining consumer of the scan process's resident
    memory on the 1.9GB VPS. This continent only has ~40 terrain types, 3
    statuses, and a few hundred distinct owner names, so those string
    values repeat massively — storing them once each (interned) and
    keeping per-point data as parallel typed arrays (coordinates and
    quality as small ints, type/status/owner as small integer indices)
    measured ~217MB for the same point count instead — an ~82% cut, most
    of it (~950MB on this dataset) freed up as headroom on the server.

    2026-08-31: added a `domain` block (name/acres/population/
    center_distance/protected, present on tiles inside any domain's zone —
    own or foreign) as parallel columns, same interned-index pattern as
    owner_org_name. Measured with tracemalloc against the real ~986K-point
    helper ledger (helper_contributions.json) with synthetic-but-realistic
    domain data on ~68% of points (every occupied tile plus a third of
    free tiles, assigned to real owner names): ~17.7 bytes/point on
    average, extrapolating to ~43MB at the ~2.56M-point scale the server
    was at that day — negligible against the ~950MB the original
    columnar migration freed up.

    Implements the same Mapping interface every caller in this codebase
    already uses against a plain results dict — results[key], .get(),
    `key in results`, .keys()/.values()/.items(), len(), and dict(results)
    for the rare full-materialization call sites (flush_state's
    first-ever-flush migration path, assemble_primary.py) — via
    MutableMapping, so nothing outside this class needs to know the
    storage isn't a plain dict.

    Deletion isn't supported (nothing in this codebase ever deletes a
    result — checked before writing this) — supporting it would mean
    shifting every backing array on every delete, not worth it for a code
    path that's never exercised."""

    __slots__ = (
        "_key_to_row", "_row_keys", "_xs", "_ys", "_quality", "_type_idx",
        "_status_idx", "_owner_idx", "_types", "_type_to_idx",
        "_statuses", "_status_to_idx", "_owners", "_owner_to_idx",
        "_domain_idx", "_domain_acres", "_domain_population",
        "_domain_center_distance", "_domain_protected",
        "_domains", "_domain_to_idx",
        "_wall_idx", "_walls", "_wall_to_idx",
    )

    def __init__(self):
        self._key_to_row = {}
        self._row_keys = []  # index -> key, preserves insertion order like a real dict
        self._xs = array("i")
        self._ys = array("i")
        self._quality = array("B")   # 0-100 fits a byte
        self._type_idx = array("H")  # unsigned short — room for thousands of terrain types
        self._status_idx = array("B")
        self._owner_idx = array("i")  # signed — -1 is the sentinel for owner_org_name=None
        self._types = []
        self._type_to_idx = {}
        self._statuses = []
        self._status_to_idx = {}
        self._owners = []
        self._owner_to_idx = {None: -1}
        # domain block (name/acres/population/distance-from-center/protected) —
        # only present on tiles inside *some* domain's zone (own or foreign),
        # same interned-index pattern as owner_org_name above. -1 in
        # _domain_idx means "no domain block on this tile at all" (most of
        # the continent — open sea/wilderness far from any settlement); the
        # other four domain_* arrays are meaningless at that row and just
        # hold whatever was last written (0), same convention as owner_idx.
        self._domain_idx = array("i")
        self._domain_acres = array("i")
        self._domain_population = array("i")
        self._domain_center_distance = array("i")  # -1 sentinel for "distance unknown", 0 is a legitimate real distance
        self._domain_protected = array("B")
        self._domains = []
        self._domain_to_idx = {None: -1}
        # `wall` (2026-09-05) — fortification building type (WALL_TYPES in
        # lol_api.py) on tiles that have one, same interned-index pattern.
        # A signed byte is plenty (~20 known types, sentinel -1 for "no
        # fortification here" — the vast majority of tiles).
        self._wall_idx = array("b")
        self._walls = []
        self._wall_to_idx = {None: -1}

    @staticmethod
    def _intern(mapping, lst, value):
        i = mapping.get(value)
        if i is None:
            i = len(lst)
            mapping[value] = i
            lst.append(value)
        return i

    def __setitem__(self, key, value):
        t = self._intern(self._type_to_idx, self._types, value["type"])
        s = self._intern(self._status_to_idx, self._statuses, value["status"])
        o = self._intern(self._owner_to_idx, self._owners, value.get("owner_org_name"))
        domain = value.get("domain")
        if domain is None:
            d, d_acres, d_pop, d_dist, d_prot = -1, 0, 0, 0, 0
        else:
            d = self._intern(self._domain_to_idx, self._domains, domain["name"])
            d_acres = domain["acres"]
            d_pop = domain["population"]
            cd = domain.get("center_distance")
            d_dist = cd if cd is not None else -1
            d_prot = 1 if domain.get("protected") else 0
        w = self._intern(self._wall_to_idx, self._walls, value.get("wall"))
        row = self._key_to_row.get(key)
        if row is None:
            row = len(self._row_keys)
            self._key_to_row[key] = row
            self._row_keys.append(key)
            self._xs.append(value["x"])
            self._ys.append(value["y"])
            self._quality.append(int(value["quality_pct"]))
            self._type_idx.append(t)
            self._status_idx.append(s)
            self._owner_idx.append(o)
            self._domain_idx.append(d)
            self._domain_acres.append(d_acres)
            self._domain_population.append(d_pop)
            self._domain_center_distance.append(d_dist)
            self._domain_protected.append(d_prot)
            self._wall_idx.append(w)
        else:  # update in place — e.g. --recheck-free re-fetching an already-known point
            self._xs[row] = value["x"]
            self._ys[row] = value["y"]
            self._quality[row] = int(value["quality_pct"])
            self._type_idx[row] = t
            self._status_idx[row] = s
            self._owner_idx[row] = o
            self._domain_idx[row] = d
            self._domain_acres[row] = d_acres
            self._domain_population[row] = d_pop
            self._domain_center_distance[row] = d_dist
            self._domain_protected[row] = d_prot
            self._wall_idx[row] = w

    def __getitem__(self, key):
        row = self._key_to_row[key]  # raises KeyError naturally, same as a plain dict
        owner_i = self._owner_idx[row]
        domain_i = self._domain_idx[row]
        if domain_i == -1:
            domain = None
        else:
            dist = self._domain_center_distance[row]
            domain = {
                "name": self._domains[domain_i],
                "acres": self._domain_acres[row],
                "population": self._domain_population[row],
                "center_distance": dist if dist != -1 else None,
                "protected": bool(self._domain_protected[row]),
            }
        wall_i = self._wall_idx[row]
        return {
            "type": self._types[self._type_idx[row]],
            "quality_pct": self._quality[row],
            "status": self._statuses[self._status_idx[row]],
            "owner_org_name": self._owners[owner_i] if owner_i != -1 else None,
            "x": self._xs[row], "y": self._ys[row],
            "domain": domain,
            "wall": self._walls[wall_i] if wall_i != -1 else None,
        }

    def __delitem__(self, key):
        raise NotImplementedError("CompactResults не поддерживает удаление — в кодовой базе оно нигде не используется")

    def __iter__(self):
        return iter(self._row_keys)

    def __len__(self):
        return len(self._row_keys)

    def __contains__(self, key):
        return key in self._key_to_row

    def __repr__(self):
        return f"<CompactResults: {len(self._row_keys)} точек>"


def continent_state_path(cx, cy, step):
    PROFILE_DATA_DIR.mkdir(exist_ok=True)
    return PROFILE_DATA_DIR / f"continent_{cx}_{cy}_s{step}.json"


def results_log_path(path):
    return path.with_suffix(".jsonl")


def meta_path(path):
    return path.with_suffix(".meta.json")


def append_results(path, records):
    """Appends already-fetched points to the on-disk log — O(len(records)),
    never touches or re-serializes anything already on disk. This is the
    fix for the old save_state(): that rewrote the ENTIRE results dict as
    one JSON blob on every save, which past ~1M points meant holding both
    the live dict AND a 150MB+ serialized copy in memory at once — the
    single biggest cause of the near-OOM episodes on the 1.9GB scan VPS
    (2026-08-25). Duplicate appends (recheck_points() re-fetching an
    already-known point) are fine: load_state() below does last-line-wins
    reconstruction, same semantics as the old results[key] = hit dict
    update it replaces."""
    if not records:
        return
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    with results_log_path(path).open("a", encoding="utf-8") as f:
        f.write(text)


def save_meta(path, meta):
    """Small file (x/y/step/frontier/failed/recheck cursor) — still cheap to
    fully rewrite every save, unlike the (now potentially multi-million-row)
    results log."""
    mp = meta_path(path)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    tmp.replace(mp)  # atomic-ish on the same filesystem


def flush_state(path, state, pending):
    """Call at every SAVE_EVERY tick and once more at loop exit — appends
    whatever's accumulated in `pending` (points found/changed since the
    last flush) and snapshots the small meta fields.

    The very first flush of a process's lifetime (jsonl not created yet,
    whether that's because we're migrating an old-format state file — a
    single big JSON blob, from before this change — or just starting a
    brand-new scan) has to write ALL of state["results"], not just
    `pending`: on a legacy-format state, everything loaded from the old
    file is sitting in state["results"] but has never been appended to
    the jsonl log, and `pending` only holds points found since THIS
    process started — appending just `pending` would silently drop every
    already-known point. (Caught by a migration test before this ever
    touched real data — 5000 pre-existing points vanished on first
    reload.) Once the jsonl exists, subsequent flushes append only
    `pending` as normal — the rest is already durable on disk.

    Also renames aside any legacy single-file state as a frozen backup
    once the jsonl exists, so it stops looking like the live file —
    load_state() only reads it on that very first call, before this."""
    jp = results_log_path(path)
    if jp.exists():
        append_results(path, pending)
    else:
        append_results(path, list(state["results"].values()))
    if path.exists():
        path.replace(path.with_suffix(".json.pre-jsonl-bak"))
    meta = {
        "x": state["x"], "y": state["y"], "step": state["step"],
        "frontier": state.get("frontier", []), "failed": state.get("failed", []),
        "fog": state.get("fog", []),
    }
    if "recheck_all_cursor" in state:
        # Present-but-None is recheck_points()'s own explicit "pass
        # finished, clear the resume point" — write that through as-is.
        meta["recheck_all_cursor"] = state["recheck_all_cursor"]
    else:
        # Absent means THIS caller (e.g. the plain wave scan, which
        # shares this same meta.json and calls flush_state() on its own
        # unrelated schedule) has no opinion on the field at all — carry
        # forward whatever a concurrent/prior --recheck-all pass already
        # saved instead of silently erasing its resume point just because
        # this particular flush's state dict never tracked it. Bug found
        # 2026-09-09: a crash mid-recheck-all followed by the app coming
        # back up in plain-scan mode left recheck_all_cursor permanently
        # gone, forcing an hours-long backfill pass to restart from zero.
        try:
            existing = json.loads(meta_path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if "recheck_all_cursor" in existing:
            meta["recheck_all_cursor"] = existing["recheck_all_cursor"]
    save_meta(path, meta)


def load_state(path):
    """Reads the incremental jsonl+meta format if present (see
    append_results/save_meta above); otherwise falls back to the old
    single-JSON-blob format for a one-time transparent migration — the
    next flush_state() call rewrites it as jsonl and retires the old file
    (see flush_state). Either way, callers get the same
    {"x","y","step","results","frontier","failed"} shape as before."""
    jp = results_log_path(path)
    if jp.exists():
        results = CompactResults()
        bad_lines = 0
        with jp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1  # a torn last line from a crash mid-write — skip, not fatal
                    continue
                results[f"{rec['x']},{rec['y']}"] = rec
        if bad_lines:
            print(f"Warning: skipped {bad_lines} corrupt line(s) in {jp.name} (likely a torn write from a crash).")
        try:
            meta = json.loads(meta_path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        x, y, step = meta.get("x"), meta.get("y"), meta.get("step")
        if x is None or y is None or step is None:
            m = re.match(r"continent_(-?\d+)_(-?\d+)_s(\d+)", path.stem)
            if m:
                x, y, step = int(m.group(1)), int(m.group(2)), int(m.group(3))
        state = {
            "x": x, "y": y, "step": step, "results": results,
            "frontier": meta.get("frontier", []), "failed": meta.get("failed", []),
            "fog": meta.get("fog", []),
        }
        if "recheck_all_cursor" in meta:
            state["recheck_all_cursor"] = meta["recheck_all_cursor"]
        return state
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def grid_neighbors(x, y, step):
    for dx in (-step, 0, step):
        for dy in (-step, 0, step):
            if dx or dy:
                yield x + dx, y + dy


def fetch_point(client, point, capture_building=False):
    wx, wy = point
    try:
        info = client.fetch_tile_info(wx, wy)
    except (ProtocolError, OSError) as e:
        return {"error": str(e), "x": wx, "y": wy}
    # "Туман" — клетка ещё не открыта этим аккаунтом в игре, а не сбой сети:
    # страница отдаётся нормально, просто без ground-блока. Раньше это било
    # в тот же "no ground data", неотличимый от реальной ошибки сессии — см.
    # комментарий у _FOG_RE в lol_api.py про застревание в релогин-цикле.
    if info.get("fog"):
        return {"fog": True, "x": wx, "y": wy}
    ground = info.get("ground")
    if not ground:
        return {"error": "no ground data", "x": wx, "y": wy}
    owner_id = info.get("owner_org_id")
    if owner_id is None:
        status = "free"
    elif owner_id == client.org_id:
        status = "ours"
    else:
        status = "occupied"
    # Только поля, которые реально читают build_map_artifact.py и
    # build_minerals_dashboard.py — раньше сохраняли ещё name/cover_*/
    # building_name/road/ground_skills/cover_skills "про запас", но ими
    # никто не пользовался, а на ~700к точек это давало заметный лишний
    # расход памяти (и один из факторов участившихся OOM). `domain`
    # возвращён (2026-08-31) — площадь/население/расстояние до центра
    # домена нужны, чтобы прикидывать границы территории без полного
    # скана; замерено отдельно перед включением, см. CompactResults.
    result = {
        "type": ground["type"], "quality_pct": ground["quality_pct"],
        "status": status, "owner_org_name": info.get("owner_org_name"),
        "domain": info.get("domain"),
        "x": wx, "y": wy,
    }
    # `wall` (2026-09-05) — только для клеток со стенами/воротами/башнями/
    # бонами (см. WALL_TYPES): подавляющее большинство клеток вообще не
    # имеют cover-постройки такого типа (или не имеют cover вовсе), так что
    # это поле почти всегда отсутствует — тот же принцип экономии памяти,
    # что и выше, просто применённый к новому полю, а не отказ от него.
    cover = info.get("cover")
    if cover and cover.get("type") in WALL_TYPES:
        result["wall"] = cover["type"]
    elif cover and capture_building:
        # Same `cover` slot as `wall` above (parse_tile_page() doesn't
        # distinguish a constructed building from natural cover like
        # forest/orchard — both come back under /help/bld?type=..., see
        # its docstring), just not a fortification. Default-off and
        # helper-only (2026-09-10, user request: "будем хранить их
        # отдельно, потом решу что с ними делать") — this is exactly the
        # kind of per-tile extra the 2026-08-31 comment above warns cost
        # noticeable memory at ~700k points and got stripped from the
        # server's own scan; only map_app.py's local calls opt in.
        result["building"] = {
            "type": cover.get("type"), "name": cover.get("name"),
            "quality_pct": cover.get("quality_pct"),
        }
    return result


FUTURE_TIMEOUT = 30  # a bit more slack than fetch_point's own 20s socket timeout
# Relogin (run_relogin_check) gets its own, more patient budget: login()+
# sync() each already retry internally (lol_api._with_retries, up to 3 tries
# with backoff sleeps), so a single attempt legitimately costs more than one
# fetch_point() call — and unlike a single stuck fetch (bounded, retried next
# chunk automatically), a failed relogin stops the ENTIRE unattended process.
# Seen live (2026-09-06, server, --recheck-occupied): a lone 30s-capped
# attempt gave up on what turned out to be a transient network blip,
# aborting a multi-hour unattended pass over a few recoverable seconds.
RELOGIN_TIMEOUT = 60
RELOGIN_ATTEMPTS = 3

def results_or_timeout(futures, points):
    """[hit, ...] for a whole submitted chunk, each future.result() capped so the
    chunk as a whole can't wait past FUTURE_TIMEOUT — NOT FUTURE_TIMEOUT per future
    (an earlier version called future.result(timeout=FUTURE_TIMEOUT) once per future
    in a plain list comprehension, i.e. sequentially — with CHUNK=10 all genuinely
    stuck at once, that's up to 10x FUTURE_TIMEOUT for one chunk, ~5 minutes of
    looking exactly as hung as no timeout at all before this was caught live).

    Seen live: a chunk of requests through a helper's proxy that never raised (no
    exception, no timeout from the 20s socket timeout in lol_api.py — zero CPU,
    zero open TCP connections the whole time, window still responsive) but also
    never returned. concurrent.futures.wait() blocks on the whole set at once, so
    the wall-clock cost here is bounded by FUTURE_TIMEOUT regardless of how many
    futures in the chunk are stuck."""
    done, not_done = wait(futures, timeout=FUTURE_TIMEOUT)
    results = []
    for f, p in zip(futures, points):
        if f in done:
            results.append(f.result())
        else:
            results.append({"error": f"timed out after {FUTURE_TIMEOUT}s waiting for a response", "x": p[0], "y": p[1]})
    return results


def call_with_timeout(fn, *args, timeout=None, **kwargs):
    """Runs fn(*args, **kwargs) on a throwaway worker thread with a hard wall-clock
    ceiling, raising TimeoutError on expiry instead of blocking forever. General-
    purpose version of the wait()-based pattern below (results_or_timeout,
    login_with_timeout) — any single blocking network call (fetch_known_cells()
    in map_app.py, say) can hang the exact same way behind a flaky proxy: no
    exception, no CPU, no open connection, just gone. Whatever raised inside
    fn propagates unchanged once it actually returns/raises.

    pool.shutdown(wait=False) on every exit — the default wait=True would block
    right back on the same hung thread this is trying to time out on."""
    timeout = timeout or FUTURE_TIMEOUT
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn, *args, **kwargs)
    done, _not_done = wait([future], timeout=timeout)
    if not done:
        pool.shutdown(wait=False)
        raise TimeoutError(f"call timed out after {timeout}s")
    try:
        return future.result()
    finally:
        pool.shutdown(wait=False)


def _proxy_display(proxy):
    """host:port only — never log the embedded user:pass from an http://user:pass@host:port proxy URL."""
    if not proxy:
        return "без прокси"
    m = re.search(r"@([^/]+)$", proxy)
    return f"через прокси {m.group(1)}" if m else "через прокси"


def _login_and_sync(client, username, password):
    client.login(username, password)
    client.sync()


def login_with_timeout(client, username, password, timeout=None, log=print):
    """login()+sync() run directly on the calling thread have no bound but
    lol_api.py's own 20s _urlopen(timeout=...) — which, live, did not reliably
    fire behind a helper's HTTP proxy (a request that never raised an exception
    and never returned either — zero CPU, zero open TCP connections the whole
    time, so not merely slow). Seen at BOTH the very first login (status frozen
    on "подключение…" indefinitely) and at mid-run relogin after a burst of
    failures — same fix at every call site: bound the wait with
    concurrent.futures.wait(), independent of whatever the underlying socket
    layer does or doesn't do.

    `log` receives short progress lines around the attempt — a caller with no
    UI can pass print (the default); map_app.py pipes it into its own log
    widget so a stuck attempt reads as "still trying" instead of a frozen
    status label with zero explanation. Raises TimeoutError on expiry, or
    whatever login()/sync() itself raised (typically ProtocolError/OSError)."""
    timeout = timeout or FUTURE_TIMEOUT
    log(f"[вход: логин {username}, {_proxy_display(client.proxy)}]")
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_login_and_sync, client, username, password)
    # shutdown(wait=False) on every exit below — the default wait=True would
    # block right back on the same hung thread we're trying to time out on.
    done, _not_done = wait([future], timeout=timeout)
    if not done:
        pool.shutdown(wait=False)
        log(f"[вход не удался: нет ответа за {timeout}с — прокси/сеть не отвечают или зависли]")
        raise TimeoutError(f"login timed out after {timeout}s (proxy/network hang, no response at all)")
    try:
        future.result()
    except (ProtocolError, OSError) as e:
        pool.shutdown(wait=False)
        log(f"[вход не удался: {e}]")
        raise
    pool.shutdown(wait=False)
    log("[вход выполнен]")


def run_relogin_check(login_creds, client, consecutive_failures):
    """Returns (should_stop, new_consecutive_failures). Shared by the wave loop and the recheck pass."""
    if consecutive_failures < FAILURE_CIRCUIT_BREAKER:
        return False, consecutive_failures
    if login_creds:
        print(f"\n{FAILURE_CIRCUIT_BREAKER} failures in a row — session probably died. Trying to relogin...")
        for attempt in range(1, RELOGIN_ATTEMPTS + 1):
            try:
                login_with_timeout(client, *login_creds, timeout=RELOGIN_TIMEOUT)
            except (TimeoutError, ProtocolError, OSError) as e:
                if attempt < RELOGIN_ATTEMPTS:
                    print(f"Relogin attempt {attempt}/{RELOGIN_ATTEMPTS} failed ({e}) — retrying...")
                    time.sleep(5)
                    continue
                print(f"Relogin failed after {RELOGIN_ATTEMPTS} attempts ({e}) — stopping, progress saved.")
                return True, consecutive_failures
            print("Relogged in, continuing.")
            return False, 0
    print(
        f"\n{FAILURE_CIRCUIT_BREAKER} failures in a row (session expired?) — stopping instead of "
        "burning through the rest of the queue for nothing. Progress saved — restart with a fresh "
        "--phpsessid (or use --username/--password for automatic relogin)."
    )
    return True, consecutive_failures


def recheck_points(client, path, state, stop_event, login_creds, mode, on_progress=None, capture_building=False):
    """One-off correction/backfill pass over already-saved points — doesn't
    touch the frontier/failed queues, not part of the wave expansion.

    mode="free_with_cover": only status=free points with a cover feature —
    under the old owner-detection logic those could be someone else's
    territory that we simply couldn't see (see the `bldicons` fallback in
    lol_api.parse_tile_page). This set shrinks on its own each run (fixed
    points stop matching the filter), so no separate resume cursor needed.

    mode="free": every currently status=free point — catches free land that
    got claimed by a player since it was last scanned. Cheaper than a full
    rescan (which re-walks the whole frontier/flood-fill from scratch): this
    only re-fetches coordinates already known to us and currently marked
    free, typically a small fraction of the full continent. Meant to be run
    periodically (e.g. daily cron) alongside the main wave scan, not as a
    one-off. No resume cursor — this set shrinks as free land gets claimed,
    same reasoning as free_with_cover.

    mode="all": every saved point, to backfill the newer fields (road,
    ground_skills, domain) that older scans didn't capture. Resumable via a
    saved cursor — this can be 100k+ points and take many hours.

    on_progress(completed, total, changed), if given, is called once per
    chunk — lets a GUI caller (map_app.py) show live progress instead of
    the bare print()s here, which a windowed .exe has nowhere to display
    (looked exactly like the app had hung on a run through the full
    free-cell set — hours with a silent, motionless window)."""
    results = state["results"]
    if mode == "free_with_cover":
        targets = [
            tuple(int(n) for n in key.split(","))
            for key, hit in results.items()
            if hit.get("status") == "free" and hit.get("cover_name")
        ]
        cursor_key = None
        label = "already-saved points (free, with a cover feature)"
    elif mode == "free":
        targets = [
            tuple(int(n) for n in key.split(","))
            for key, hit in results.items()
            if hit.get("status") == "free"
        ]
        cursor_key = None
        label = "already-saved points (currently free — checking for new claims)"
    elif mode == "occupied":
        # Владения — не только новых захватов, ещё и backfill/обновление
        # domain (name/acres/population/center_distance) на уже занятых
        # клетках: это поле добавили в fetch_point() позже, чем отсканили
        # большую часть карты, а домен может расти со временем и на давно
        # занятой территории. --recheck-free его не трогает вообще (там
        # только status=free), а полный --recheck-all вдвое дороже —
        # заново гоняет и свободные клетки тоже.
        targets = [
            tuple(int(n) for n in key.split(","))
            for key, hit in results.items()
            if hit.get("status") in ("occupied", "ours")
        ]
        cursor_key = None
        label = "already-saved points (occupied — refreshing owner/domain data)"
    else:
        targets = [tuple(int(n) for n in key.split(",")) for key in results.keys()]
        # Sorted by distance from the domain, nearest first (2026-09-10,
        # user request) — plain dict order reflects ancient history (mostly
        # whichever account's flood-fill happened to discover a cell
        # first, on a merged continent frequently the *other* account's
        # home base), not anything relevant to whoever's running this
        # pass. Found live: the account's own domain sat at position
        # 1,151,401 of 2,929,557 in dict order — this pass wouldn't have
        # reached it for a very long time. `recheck_all_cursor` is a
        # positional index into `targets`, so changing the order makes any
        # existing saved cursor point at the wrong cells — callers must
        # reset it to 0 the one time this ordering changes for a given
        # state file (see project memory for the 2026-09-10 live reset).
        cx, cy = state.get("x"), state.get("y")
        if cx is not None and cy is not None:
            targets.sort(key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        cursor_key = "recheck_all_cursor"
        label = "already-saved points (full backfill, nearest-to-domain first)"

    total = len(targets)
    # `or 0` rather than `.get(cursor_key, 0)`: an explicit None (the
    # finish-marker below) must resume from scratch exactly like a
    # genuinely absent key, not crash on the first `completed += 1`.
    start_at = (state.get(cursor_key) or 0) if cursor_key else 0
    if start_at:
        print(f"Resuming: skipping {start_at}/{total} already done in this pass.")
    targets = targets[start_at:]
    print(f"Rechecking {total} {label} ({len(targets)} left to go)...")

    completed = start_at
    changed = 0
    pending = []
    consecutive_failures = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for i in range(0, len(targets), CHUNK):
            if stop_event.is_set():
                break
            chunk = targets[i:i + CHUNK]
            futures = [pool.submit(fetch_point, client, p, capture_building) for p in chunk]
            for hit in results_or_timeout(futures, chunk):
                completed += 1
                if hit.get("error"):
                    consecutive_failures += 1
                    continue
                consecutive_failures = 0
                if hit.get("fog"):
                    # Туман — клетка временно не видна этому аккаунту, не
                    # "точка исчезла". Без этой проверки bare {"fog","x","y"}
                    # затирал уже известные type/status/owner в results[key]
                    # и засчитывался как "changed", хотя ничего не менялось.
                    continue
                key = f"{hit['x']},{hit['y']}"
                if hit.get("status") != results.get(key, {}).get("status", hit.get("status")):
                    changed += 1
                results[key] = hit
                pending.append(hit)
                if cursor_key:
                    state[cursor_key] = completed
                if len(pending) >= SAVE_EVERY:
                    flush_state(path, state, pending)
                    pending = []

            should_stop, consecutive_failures = run_relogin_check(login_creds, client, consecutive_failures)
            if should_stop:
                stop_event.set()
                break

            elapsed = time.time() - t0
            rate = (completed - start_at) / elapsed if elapsed > 0 else 0
            print(f"\rrechecked: {completed}/{total}  changed: {changed}  ({rate:.1f} points/sec)   ", end="", flush=True)
            if on_progress:
                # `chunk` — just-processed (x, y) targets, so a GUI caller
                # can shade them on the map as "already covered this pass"
                # without needing a separate coordinate log.
                on_progress(completed, total, changed, chunk)
            time.sleep(PAUSE_SECONDS)

    finished = completed >= total
    if cursor_key and finished:
        # Explicit None, not pop(): flush_state() needs to tell "the pass
        # finished, clear the resume point" apart from "this caller (e.g.
        # the plain wave scan, sharing the same meta.json) never had an
        # opinion on this field" — an absent key means the latter and now
        # preserves whatever's already on disk instead of erasing it.
        state[cursor_key] = None

    flush_state(path, state, pending)
    print(f"\nRecheck done. Checked {completed}/{total}, {changed} changed.")
    return finished


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://www.landsoflords.com")
    parser.add_argument("--phpsessid", default=None, help="session cookie, if you already have one")
    parser.add_argument("--username", default=None, help="game login, if PHPSESSID isn't given")
    parser.add_argument("--password", default=None, help="password (omit to be prompted, hidden, not in shell history)")
    parser.add_argument("--x", type=int, default=None, help="start X (default: your domain's coords)")
    parser.add_argument("--y", type=int, default=None, help="start Y (default: your domain's coords)")
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument(
        "--recheck-free-with-cover", action="store_true",
        help="одноразовый проход по уже сохранённым точкам со статусом free, у которых есть постройка/улица/поле — "
             "под старой логикой определения хозяина такие могли быть чьей-то территорией, которую не было видно",
    )
    parser.add_argument(
        "--recheck-free", action="store_true",
        help="проход по всем уже сохранённым точкам со статусом free — ловит клетки, которые кто-то занял с "
             "момента последнего скана. Дешевле полного пересканирования (не идёт волной заново, только по уже "
             "известным свободным координатам) — предназначено для периодического запуска (например, раз в "
             "сутки по cron) параллельно с основным сканом.",
    )
    parser.add_argument(
        "--recheck-all", action="store_true",
        help="полный пересчёт вообще всех уже сохранённых точек — добьёт новые поля (road/ground_skills/domain/"
             "cover_type), которых не было в более старых сканах. Резюмируемо (сохраняет позицию), но на "
             "большом наборе может идти много часов.",
    )
    parser.add_argument(
        "--recheck-occupied", action="store_true",
        help="проход по всем уже сохранённым точкам со статусом occupied/ours — обновляет владельца и domain "
             "(имя/акры/население/дистанция до центра) на уже занятой территории. Дешевле --recheck-all (не "
             "трогает свободные клетки) и в отличие от --recheck-free реально обновляет domain, который на "
             "занятых клетках status=free-проход не видит вообще.",
    )
    args = parser.parse_args()

    phpsessid = args.phpsessid or os.environ.get("LOL_PHPSESSID")
    username = args.username or os.environ.get("LOL_USERNAME")
    password = args.password or os.environ.get("LOL_PASSWORD")

    if not phpsessid and not username:
        sys.exit("Need either PHPSESSID or username+password (flags --phpsessid/--username/--password, or env vars LOL_PHPSESSID/LOL_USERNAME/LOL_PASSWORD).")

    login_creds = None  # (username, password), kept only for auto-relogin if the session dies mid-run
    if phpsessid:
        client = LolClient(phpsessid, args.base_url)
        try:
            client.sync()
        except (ProtocolError, OSError) as e:
            sys.exit(f"Could not sync ({e}) — check the password/session and try again.")
    else:
        password = password or getpass.getpass("Password: ")
        login_creds = (username, password)
        client = LolClient("", args.base_url)
        try:
            login_with_timeout(client, *login_creds)
        except TimeoutError as e:
            sys.exit(f"Login timed out ({e}) — check the network and try again.")
        except (ProtocolError, OSError) as e:
            sys.exit(f"Login failed ({e}) — check the password and try again.")
    print(f"org_id={client.org_id}  org_coords={client.org_coords}")

    if args.x is not None and args.y is not None:
        cx, cy = args.x, args.y
    else:
        cx, cy = client.org_coords or (None, None)
    if cx is None or cy is None:
        sys.exit("Could not determine a start point (no org_coords, and --x/--y not given).")

    step = args.step
    path = continent_state_path(cx, cy, step)
    state = load_state(path)
    if state is None:
        state = {"x": cx, "y": cy, "step": step, "results": {}, "frontier": [], "failed": []}

    print(f"State file: {path}")

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        print(f"\nSignal {signum} — finishing the current batch and saving...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.recheck_all:
        recheck_points(client, path, state, stop_event, login_creds, mode="all")
        return
    if args.recheck_free_with_cover:
        recheck_points(client, path, state, stop_event, login_creds, mode="free_with_cover")
        return
    if args.recheck_free:
        recheck_points(client, path, state, stop_event, login_creds, mode="free")
        return
    if args.recheck_occupied:
        recheck_points(client, path, state, stop_event, login_creds, mode="occupied")
        return

    # Точки во frontier несут глубину ухода в воду (см. WATER_EXPAND_DEPTH) —
    # (x, y, depth). failed-точки и самая первая точка старта такой глубины
    # никогда не имели (2-элементные), депту 0 для них — не баг, а
    # осознанный выбор: считаем их "как будто с суши", так что скан вправе
    # снова уйти на всю глубину в воду оттуда, если понадобится.
    frontier_points = [(p[0], p[1], p[2] if len(p) > 2 else 0) for p in state.get("frontier", [])] \
        + [(p[0], p[1], 0) for p in state.get("failed", [])]
    state["failed"] = []
    if not frontier_points and not state["results"]:
        frontier_points = [(cx, cy, 0)]
    frontier = deque(frontier_points)
    results = state["results"]
    # Туманные клетки (см. "fog" в fetch_point) держим отдельно от failed —
    # ретраить их бессмысленно, скан не может сам открыть туман. Включаем их
    # в seen, чтобы обычное соседнее обнаружение не подсовывало их во
    # frontier заново на каждый перезапуск.
    fog_points = [tuple(p) for p in state.get("fog", [])]
    seen = set(results.keys()) | {f"{p[0]},{p[1]}" for p in frontier} | {f"{p[0]},{p[1]}" for p in fog_points}

    print(f"Already saved: {len(results)}  Queued: {len(frontier)}  Fogged: {len(fog_points)}")

    completed = 0
    pending = []
    failed = []
    fog = fog_points
    consecutive_failures = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        while frontier:
            if stop_event.is_set():
                break
            chunk = [frontier.popleft() for _ in range(min(CHUNK, len(frontier)))]
            futures = [pool.submit(fetch_point, client, (p[0], p[1])) for p in chunk]
            for p, hit in zip(chunk, results_or_timeout(futures, chunk)):
                depth = p[2]
                completed += 1
                if hit.get("fog"):
                    # Не сеть виновата и не сессия — просто ещё не открыто в
                    # игре. Не считаем провалом (не трогаем
                    # consecutive_failures — иначе пачка туманных клеток
                    # подряд ложно триггерит "сессия умерла" и уходит в
                    # бесконечный релогин, который тут ничего не чинит) и не
                    # ретраим (fog, не failed).
                    fog.append((hit["x"], hit["y"]))
                    continue
                if hit.get("error"):
                    failed.append((hit["x"], hit["y"]))
                    consecutive_failures += 1
                    continue
                consecutive_failures = 0
                key = f"{hit['x']},{hit['y']}"
                results[key] = hit
                pending.append(hit)
                # На суше глубина сбрасывается в 0 (пришли на твёрдую землю —
                # снова можно уйти на WATER_EXPAND_DEPTH клеток в любую
                # воду); на воде глубина растёт, и расширяемся, только пока
                # не упёрлись в потолок — так вокруг всего побережья
                # получается полоса реально отсканированной воды, а не
                # мгновенная остановка на первой же клетке (что раньше
                # оставляло только однослойную кромку и голый край канвы на
                # карте без него).
                if hit["type"] not in CONTINENT_BOUNDARY_TYPES:
                    next_depth = 0
                    expand = True
                else:
                    next_depth = depth + 1
                    expand = next_depth <= WATER_EXPAND_DEPTH
                if expand:
                    for nx, ny in grid_neighbors(hit["x"], hit["y"], step):
                        nkey = f"{nx},{ny}"
                        if nkey not in seen:
                            seen.add(nkey)
                            frontier.append((nx, ny, next_depth))
                if len(pending) >= SAVE_EVERY:
                    state["frontier"] = [list(p) for p in frontier]
                    state["failed"] = [list(p) for p in failed]
                    state["fog"] = [list(p) for p in fog]
                    flush_state(path, state, pending)
                    pending = []

            should_stop, consecutive_failures = run_relogin_check(login_creds, client, consecutive_failures)
            if should_stop:
                stop_event.set()
                break

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            print(f"\rchecked: {len(results)}  queued: {len(frontier)}  fogged: {len(fog)}  ({rate:.1f} points/sec)   ", end="", flush=True)
            time.sleep(PAUSE_SECONDS)

    state["frontier"] = [list(p) for p in frontier]
    state["failed"] = [list(p) for p in failed]
    state["fog"] = [list(p) for p in fog]
    flush_state(path, state, pending)
    print(f"\nStopped. Total saved: {len(results)}, still queued: {len(frontier)}.")
    print("Run again with the same --x/--y/--step to continue from here.")


if __name__ == "__main__":
    main()
