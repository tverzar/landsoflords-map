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
import signal
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lol_api import LolClient, ProtocolError

PROFILE_DATA_DIR = Path(__file__).parent / "profile_data"
CONTINENT_BOUNDARY_TYPES = {"swater", "ice"}  # open sea / ice — the wave stops here, everything else is crossable
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
SAVE_EVERY = 250  # was 25 — full-state json.dumps() every save is a memory
# spike on top of the resident dict (~150MB+ raw JSON on a full pass); with
# swap now in place this is belt-and-braces, but less frequent saves still
# means less peak memory churn on a 1.9GB box. Trade-off: more re-fetched
# points lost if the process dies mid-batch.
FAILURE_CIRCUIT_BREAKER = 20  # this many consecutive request failures likely means a dead session, not bad luck


def continent_state_path(cx, cy, step):
    PROFILE_DATA_DIR.mkdir(exist_ok=True)
    return PROFILE_DATA_DIR / f"continent_{cx}_{cy}_s{step}.json"


def load_state(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)  # atomic-ish on the same filesystem


def grid_neighbors(x, y, step):
    for dx in (-step, 0, step):
        for dy in (-step, 0, step):
            if dx or dy:
                yield x + dx, y + dy


def fetch_point(client, point):
    wx, wy = point
    try:
        info = client.fetch_tile_info(wx, wy)
    except (ProtocolError, OSError) as e:
        return {"error": str(e), "x": wx, "y": wy}
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
    # building_name/road/ground_skills/cover_skills/domain "про запас",
    # но ими никто не пользовался, а на ~700к точек это давало заметный
    # лишний расход памяти (и один из факторов участившихся OOM).
    return {
        "type": ground["type"], "quality_pct": ground["quality_pct"],
        "status": status, "owner_org_name": info.get("owner_org_name"),
        "x": wx, "y": wy,
    }


def run_relogin_check(login_creds, client, consecutive_failures):
    """Returns (should_stop, new_consecutive_failures). Shared by the wave loop and the recheck pass."""
    if consecutive_failures < FAILURE_CIRCUIT_BREAKER:
        return False, consecutive_failures
    if login_creds:
        print(f"\n{FAILURE_CIRCUIT_BREAKER} failures in a row — session probably died. Trying to relogin...")
        try:
            client.login(*login_creds)
            client.sync()
            print("Relogged in, continuing.")
            return False, 0
        except (ProtocolError, OSError) as e:
            print(f"Relogin failed ({e}) — stopping, progress saved.")
            return True, consecutive_failures
    print(
        f"\n{FAILURE_CIRCUIT_BREAKER} failures in a row (session expired?) — stopping instead of "
        "burning through the rest of the queue for nothing. Progress saved — restart with a fresh "
        "--phpsessid (or use --username/--password for automatic relogin)."
    )
    return True, consecutive_failures


def recheck_points(client, path, state, stop_event, login_creds, mode):
    """One-off correction/backfill pass over already-saved points — doesn't
    touch the frontier/failed queues, not part of the wave expansion.

    mode="free_with_cover": only status=free points with a cover feature —
    under the old owner-detection logic those could be someone else's
    territory that we simply couldn't see (see the `bldicons` fallback in
    lol_api.parse_tile_page). This set shrinks on its own each run (fixed
    points stop matching the filter), so no separate resume cursor needed.

    mode="all": every saved point, to backfill the newer fields (road,
    ground_skills, domain) that older scans didn't capture. Resumable via a
    saved cursor — this can be 100k+ points and take many hours."""
    results = state["results"]
    if mode == "free_with_cover":
        targets = [
            tuple(int(n) for n in key.split(","))
            for key, hit in results.items()
            if hit.get("status") == "free" and hit.get("cover_name")
        ]
        cursor_key = None
        label = "already-saved points (free, with a cover feature)"
    else:
        targets = [tuple(int(n) for n in key.split(",")) for key in results.keys()]
        cursor_key = "recheck_all_cursor"
        label = "already-saved points (full backfill)"

    total = len(targets)
    start_at = state.get(cursor_key, 0) if cursor_key else 0
    if start_at:
        print(f"Resuming: skipping {start_at}/{total} already done in this pass.")
    targets = targets[start_at:]
    print(f"Rechecking {total} {label} ({len(targets)} left to go)...")

    completed = start_at
    changed = 0
    since_save = 0
    consecutive_failures = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for i in range(0, len(targets), CHUNK):
            if stop_event.is_set():
                break
            chunk = targets[i:i + CHUNK]
            for hit in [f.result() for f in [pool.submit(fetch_point, client, p) for p in chunk]]:
                completed += 1
                if hit.get("error"):
                    consecutive_failures += 1
                    continue
                consecutive_failures = 0
                key = f"{hit['x']},{hit['y']}"
                if hit.get("status") != results.get(key, {}).get("status", hit.get("status")):
                    changed += 1
                results[key] = hit
                since_save += 1
                if cursor_key:
                    state[cursor_key] = completed
                if since_save >= SAVE_EVERY:
                    save_state(path, state)
                    since_save = 0

            should_stop, consecutive_failures = run_relogin_check(login_creds, client, consecutive_failures)
            if should_stop:
                stop_event.set()
                break

            elapsed = time.time() - t0
            rate = (completed - start_at) / elapsed if elapsed > 0 else 0
            print(f"\rrechecked: {completed}/{total}  changed: {changed}  ({rate:.1f} points/sec)   ", end="", flush=True)
            time.sleep(PAUSE_SECONDS)

    if cursor_key and completed >= total:
        state.pop(cursor_key, None)

    save_state(path, state)
    print(f"\nRecheck done. Checked {completed}/{total}, {changed} changed.")


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
        "--recheck-all", action="store_true",
        help="полный пересчёт вообще всех уже сохранённых точек — добьёт новые поля (road/ground_skills/domain/"
             "cover_type), которых не было в более старых сканах. Резюмируемо (сохраняет позицию), но на "
             "большом наборе может идти много часов.",
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
    else:
        password = password or getpass.getpass("Password: ")
        login_creds = (username, password)
        client = LolClient("", args.base_url)
        try:
            client.login(*login_creds)
        except (ProtocolError, OSError) as e:
            sys.exit(f"Login failed ({e}) — check the password and try again.")

    try:
        client.sync()
    except (ProtocolError, OSError) as e:
        sys.exit(f"Could not sync ({e}) — check the password/session and try again.")
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

    frontier_points = [tuple(p) for p in state.get("frontier", [])] + [tuple(p) for p in state.get("failed", [])]
    state["failed"] = []
    if not frontier_points and not state["results"]:
        frontier_points = [(cx, cy)]
    frontier = deque(frontier_points)
    results = state["results"]
    seen = set(results.keys()) | {f"{p[0]},{p[1]}" for p in frontier}

    print(f"Already saved: {len(results)}  Queued: {len(frontier)}")

    completed = 0
    since_save = 0
    failed = []
    consecutive_failures = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        while frontier:
            if stop_event.is_set():
                break
            chunk = [frontier.popleft() for _ in range(min(CHUNK, len(frontier)))]
            for hit in [f.result() for f in [pool.submit(fetch_point, client, p) for p in chunk]]:
                completed += 1
                if hit.get("error"):
                    failed.append((hit["x"], hit["y"]))
                    consecutive_failures += 1
                    continue
                consecutive_failures = 0
                key = f"{hit['x']},{hit['y']}"
                results[key] = hit
                since_save += 1
                if hit["type"] not in CONTINENT_BOUNDARY_TYPES:
                    for nx, ny in grid_neighbors(hit["x"], hit["y"], step):
                        nkey = f"{nx},{ny}"
                        if nkey not in seen:
                            seen.add(nkey)
                            frontier.append((nx, ny))
                if since_save >= SAVE_EVERY:
                    state["frontier"] = [list(p) for p in frontier]
                    state["failed"] = [list(p) for p in failed]
                    save_state(path, state)
                    since_save = 0

            should_stop, consecutive_failures = run_relogin_check(login_creds, client, consecutive_failures)
            if should_stop:
                stop_event.set()
                break

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            print(f"\rchecked: {len(results)}  queued: {len(frontier)}  ({rate:.1f} points/sec)   ", end="", flush=True)
            time.sleep(PAUSE_SECONDS)

    state["frontier"] = [list(p) for p in frontier]
    state["failed"] = [list(p) for p in failed]
    save_state(path, state)
    print(f"\nStopped. Total saved: {len(results)}, still queued: {len(frontier)}.")
    print("Run again with the same --x/--y/--step to continue from here.")


if __name__ == "__main__":
    main()
