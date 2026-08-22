"""Client for landsoflords.com's /ajax/query endpoint.

The game has no public JSON API — everything goes through one endpoint that
exchanges a lightly-obfuscated (not real crypto) pipe-delimited protocol,
reverse-engineered from the game's own client JS (main.<build>.js):

  - Auth is a plain PHP session cookie (PHPSESSID), same as browsing the
    site normally.
  - Every page embeds a few JS globals needed to talk to the API:
      Wmbjmcw          per-page-load obfuscation key ("token" below)
      Qavtioyb         server unix timestamp at page render
      Fizyjbbcjiml     own figure/character id
  - Requests: POST /ajax/query, body token=<key>&ts=<serverUnixSeconds>&q=<blob>
    where <blob> is base64(cipher(...)) — see encode_query().
  - Responses: base64(cipher(...)) too — see decode_response(). The
    plaintext is '|'-delimited fields, format specific to each query
    script (unit, map, org, tactic, ...), '$'/'§'/',' used as sub-separators
    in places. There is no schema doc; field order was recovered by reading
    the client's own parser functions (e.g. the unit/moreData constructors).
"""
from __future__ import annotations

import base64
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class ProtocolError(Exception):
    """Raised when the server rejects a query or the page no longer has a
    valid session (logged out, cookie expired)."""


_SENTINELS = {"!TOKEN", "!EXPIRED", "!TIMEOUT"}


def _derive_key(token: str, ts: int, build_stride: int) -> str:
    kstring = f"{token}{ts}"
    klen = len(kstring)
    return "".join(kstring[(ts + i * build_stride) % klen] for i in range(20))


def encode_query(token: str, server_ts: int, script: str, args: dict | None = None) -> str:
    q = ""
    for key, value in (args or {}).items():
        q += f"{key}:{urllib.parse.quote(str(value), safe='')},"
    q += f"query:{script}"

    ts_short = (server_ts // 9) % 9999
    key = _derive_key(token, ts_short, build_stride=7)

    chars = [f"{ts_short:04d}"]
    for i, ch in enumerate(q):
        c = ord(ch)
        kc = ord(key[(i * 7) % len(key)])
        chars.append(chr((c + kc) % 256))
    raw_bytes = "".join(chars).encode("latin-1")
    return base64.b64encode(raw_bytes).decode("ascii")


def decode_response(token: str, blob: str) -> str:
    blob = blob.strip()
    if not blob or blob in _SENTINELS or blob == "!ERROR":
        return ""
    raw = base64.b64decode(blob).decode("latin-1")
    ts_short = int(raw[:4])
    key = _derive_key(token, ts_short, build_stride=3)
    body = raw[4:]
    chars = []
    for i, ch in enumerate(body):
        c = ord(ch)
        kc = ord(key[(i * 7) % len(key)])
        chars.append(chr((c - kc) % 256))
    return urllib.parse.unquote("".join(chars), errors="replace")


# The game's JS is put through a build-time obfuscator that renames every
# identifier — and does so on a *very* short cycle (the build number in
# /js/main.<build>.js bumped mid-session while this was being written), so
# matching a specific variable name is a dead end. Instead these values are
# picked out structurally, by shape, straight from the page HTML:
#   - the obfuscation token is always a bare 20-char alphanumeric string
#     literal (verified against 3 independent page loads / build numbers);
#   - the server clock is the only bare ~10-digit ("1xxxxxxxxx", i.e.
#     unix-seconds-shaped) integer literal on the page;
#   - the account/session id ("fid") and the character's own unit id aren't
#     obfuscated at all — they're plain `data-fid="…"` / `data-unit-id="…"`
#     attributes in the #user menu at the top of the (home page only —
#     other pages list many units and the first data-unit-id won't be ours).
_TOKEN_RE = re.compile(r"='([A-Za-z0-9]{20})'")
_SERVER_TS_RE = re.compile(r"=(1\d{9})\b")
_FIGURE_ID_RE = re.compile(r'data-fid="(\d+)"')
_OWN_UNIT_ID_RE = re.compile(r'data-unit-id="(\d+)"')
# own domain/org id + its map coords, from the "locate on map" button next to
# the org's own link in the #user menu — plain, unobfuscated HTML.
_ORG_ID_RE = re.compile(r'href="/arm/org/(\d+)"')
_ORG_COORDS_RE = re.compile(r"lol\.go\('/map\?x=(-?\d+)&y=(-?\d+)'\)")
# management list pages (/mgmt/<org>/units|buildings|resources) embed their
# roster/list as one more cipher-encoded blob, decoded client-side on load —
# same cipher, just delivered inline instead of via /ajax/query.
_MGMT_BLOB_RE = re.compile(r'<div id="mgmt"[^>]*>(.*?)</div>', re.S)


def _extract_token(html: str) -> str:
    matches = _TOKEN_RE.findall(html)
    if len(matches) != 1:
        raise ProtocolError(
            f"expected exactly one 20-char token literal on the page, found {len(matches)} "
            "— the site's page layout may have changed"
        )
    return matches[0]


def _extract_server_ts(html: str) -> int:
    matches = _SERVER_TS_RE.findall(html)
    if len(matches) != 1:
        raise ProtocolError(
            f"expected exactly one server-timestamp literal on the page, found {len(matches)} "
            "— the site's page layout may have changed"
        )
    return int(matches[0])


def _extract_figure_id(html: str) -> int | None:
    m = _FIGURE_ID_RE.search(html)
    return int(m.group(1)) if m else None


def _extract_own_unit_id(html: str) -> int | None:
    m = _OWN_UNIT_ID_RE.search(html)
    return int(m.group(1)) if m else None


def _extract_org_id(html: str) -> int | None:
    m = _ORG_ID_RE.search(html)
    return int(m.group(1)) if m else None


def _extract_org_coords(html: str) -> tuple[int, int] | None:
    m = _ORG_COORDS_RE.search(html)
    return (int(m.group(1)), int(m.group(2))) if m else None


class LolClient:
    def __init__(self, phpsessid: str, base_url: str = "https://www.landsoflords.com", proxy: str | None = None):
        """proxy: "http://host:port", "http://user:pass@host:port", or a
        SOCKS URL if the socks handler is registered externally (stdlib
        urllib has no built-in SOCKS support) — passed straight through to
        urllib.request.ProxyHandler, which reads scheme://host:port itself.
        None means no per-client proxy (module-level urlopen() would still
        pick up HTTP_PROXY/HTTPS_PROXY env vars by default, but a client
        built with its own opener here does NOT fall back to those —
        explicit proxy in, explicit proxy used, nothing implicit)."""
        self.base_url = base_url.rstrip("/")
        self.phpsessid = phpsessid
        self.token: str | None = None
        self.figure_id: int | None = None  # account/session id ("fid")
        self.unit_id: int | None = None  # own character's unit id, used for 'unit' queries
        self.org_id: int | None = None  # own domain/settlement org id
        self.org_coords: tuple[int, int] | None = None  # own domain's map location
        self._server_ts_at_sync: int | None = None
        self._local_ts_at_sync: float | None = None
        self.proxy = proxy
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            self._opener = urllib.request.build_opener(handler)
        else:
            self._opener = urllib.request.build_opener()

    def _urlopen(self, req, timeout=20):
        return self._opener.open(req, timeout=timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Cookie": f"PHPSESSID={self.phpsessid}",
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ru",  # matches this app's UI language
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _with_retries(fn, tries: int = 3):
        last_error = None
        for attempt in range(tries):
            try:
                return fn()
            except urllib.error.HTTPError as e:
                # HTTPError carries the response's still-open socket (so
                # callers can read the error body) — urlopen() raising it
                # skips the `with` block entirely, so nothing else ever
                # closes it. Left open, that's a file-descriptor leak; under
                # sustained load (many 429s / expired-session errors in a
                # row) it eventually exhausts the process's FD limit and
                # every request start failing with "Too many open files".
                e.close()
                last_error = e
                if e.code == 429 and attempt < tries - 1:
                    # the server is rate-limiting us — respect Retry-After if it
                    # sent one, otherwise back off harder than a normal retry
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 5 * (attempt + 1)
                    time.sleep(min(delay, 30))
                elif e.code != 429 and attempt < tries - 1:
                    time.sleep(0.5 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = e
                if attempt < tries - 1:
                    time.sleep(0.5 * (attempt + 1))
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
            raise ProtocolError(
                "сервер игры ограничил частоту запросов (429 Too Many Requests) — "
                "подождите немного и попробуйте снова со сканом поменьше/пореже"
            )
        raise last_error

    def _get(self, path: str) -> str:
        def do():
            req = urllib.request.Request(self.base_url + path, headers=self._headers())
            with self._urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        return self._with_retries(do)

    def _post(self, path: str, data: dict) -> str:
        def do():
            body = urllib.parse.urlencode(data).encode("ascii")
            headers = self._headers({
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            })
            req = urllib.request.Request(self.base_url + path, data=body, headers=headers)
            with self._urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        return self._with_retries(do)

    def _get_set_cookie_session(self, path: str, data: bytes | None = None) -> str | None:
        def do():
            headers = self._headers({"Content-Type": "application/x-www-form-urlencoded"}) if data else self._headers()
            req = urllib.request.Request(self.base_url + path, data=data, headers=headers)
            with self._urlopen(req, timeout=20) as resp:
                for cookie in resp.headers.get_all("Set-Cookie") or []:
                    m = re.match(r"PHPSESSID=([^;]+)", cookie)
                    if m:
                        return m.group(1)
            return None
        return self._with_retries(do)

    def login(self, username: str, password: str) -> None:
        """POST to /login with a username+password to obtain a fresh
        PHPSESSID, replacing self.phpsessid on success. The form (GET
        /login) has no CAPTCHA as of writing, just name/password/login
        fields — if the site ever adds one this will start failing with a
        clear "still not logged in" error from sync() below rather than
        silently doing nothing.

        A plain PHP session has to exist *before* the credentials are
        posted — GET /login itself sets one via Set-Cookie, and the site
        only accepts the login if that same session cookie comes back on
        the POST (standard session-fixation protection). Skipping this
        GET first makes the POST silently fail even with correct
        credentials."""
        pre_session = self._get_set_cookie_session("/login")
        if pre_session:
            self.phpsessid = pre_session

        body = urllib.parse.urlencode({"name": username, "password": password, "login": "Войти"}).encode("utf-8")
        session_id = self._get_set_cookie_session("/login", data=body)
        if session_id:
            self.phpsessid = session_id
        self.sync()  # raises ProtocolError if this still isn't an authenticated session

    def sync(self) -> None:
        """(Re)load the home page to pick up the current obfuscation key &
        server clock. Needed once at startup and again whenever the server
        rejects a query (!TOKEN / !EXPIRED / !TIMEOUT — the key rotates with
        each page render). Always fetches '/' specifically — it's the only
        page where the first data-unit-id on the page is guaranteed to be
        our own (other pages list many units)."""
        html = self._get("/")
        if "id=\"user\"" not in html:
            raise ProtocolError(
                "not logged in — PHPSESSID is missing/expired, or the username/password "
                "were rejected (log in again — either via login() or in the browser, "
                "copying a fresh cookie value)"
            )
        self.token = _extract_token(html)
        figure_id = _extract_figure_id(html)
        if figure_id is not None:
            self.figure_id = figure_id
        unit_id = _extract_own_unit_id(html)
        if unit_id is not None:
            self.unit_id = unit_id
        org_id = _extract_org_id(html)
        if org_id is not None:
            self.org_id = org_id
        org_coords = _extract_org_coords(html)
        if org_coords is not None:
            self.org_coords = org_coords
        self._server_ts_at_sync = _extract_server_ts(html)
        self._local_ts_at_sync = time.monotonic()

    def _current_server_ts(self) -> int:
        assert self._server_ts_at_sync is not None and self._local_ts_at_sync is not None
        elapsed = time.monotonic() - self._local_ts_at_sync
        return int(self._server_ts_at_sync + elapsed)

    def query(self, script: str, args: dict | None = None, *, _retried: bool = False) -> str:
        if self.token is None:
            self.sync()
        server_ts = self._current_server_ts()
        q = encode_query(self.token, server_ts, script, args)
        raw = self._post("/ajax/query", {"token": self.token, "ts": server_ts, "q": q})
        stripped = raw.strip()
        if stripped in _SENTINELS:
            if _retried:
                raise ProtocolError(f"server keeps rejecting the session ({stripped})")
            self.sync()
            return self.query(script, args, _retried=True)
        if stripped == "!ERROR":
            raise ProtocolError(f"server returned !ERROR for query {script!r} {args!r}")
        return decode_response(self.token, raw)

    def fetch_mgmt_list(self, org_id: int, section: str) -> str:
        """GET /mgmt/<org_id>/<section> (section: 'units'|'buildings'|
        'resources') and decode its embedded list. Each of these pages has
        its own inline-script token (independent of self.token / sync()),
        so it's extracted fresh from this page's own HTML."""
        html = self._get(f"/mgmt/{org_id}/{section}")
        m = _MGMT_BLOB_RE.search(html)
        if not m:
            raise ProtocolError(f"no unit/building/resource list found on /mgmt/{org_id}/{section}")
        token = _extract_token(html)
        return decode_response(token, m.group(1))

    def fetch_tile_bytes(self, tile: dict, zoom: int, group: int) -> bytes:
        def do():
            req = urllib.request.Request(tile_url(self.base_url, zoom, group, tile), headers=self._headers())
            with self._urlopen(req, timeout=20) as resp:
                return resp.read()
        return self._with_retries(do)

    def fetch_tile_info(self, x: int, y: int) -> dict:
        """GET /map/<coords> — a plain server-rendered page (no cipher
        involved at all), the same 'explore here' screen the game shows
        when you visit a spot on the map. Its right column has up to two
        sections: what's growing on the plot right now (forest/orchard —
        absent if something's been built there) and the underlying
        ground/terrain type (always present). See parse_tile_page()."""
        html = self._get(f"/map/{format_map_coords(x, y)}")
        return parse_tile_page(html)


# ---------------------------------------------------------------------------
# Parsers for known response shapes, field order taken directly from the
# game client's own JS constructors (Vxrpyqwglra / .moreData / lol.Weather in
# main.<build>.js) rather than guessed.

def _split(raw: str, sep: str = "|") -> list[str]:
    return raw.split(sep)


def parse_unit_short(raw: str) -> dict:
    """query('unit', {id: ...}) — brief form used on the map/lists."""
    f = _split(raw)
    n = 0

    def nxt():
        nonlocal n
        v = f[n] if n < len(f) else ""
        n += 1
        return v

    d: dict = {"id": int(nxt() or 0)}
    if len(f) > 1:
        d.update(type=nxt(), typeName=nxt(), nature=nxt(), name=nxt(), icon=nxt())
    if len(f) > 5:
        d.update(
            quality=float(nxt() or 0), state=float(nxt() or 0), stunned=int(nxt() or 0),
            happiness=float(nxt() or 0), tendency=float(nxt() or 0), age=int(nxt() or 0),
            level=int(nxt() or 0), fid=int(nxt() or 0), blazon=nxt(),
            canControl=bool(int(nxt() or 0)),
        )
        d["canManage"] = d["canControl"]
        d.update(
            canDelete=bool(int(nxt() or 0)), compatibility=float(nxt() or 0),
            isOnline=bool(int(nxt() or 0)), isSelected=bool(int(nxt() or 0)),
            isCaptain=bool(int(nxt() or 0)), isTroop=bool(int(nxt() or 0)),
            isAllowed=bool(int(nxt() or 0)), isMagical=bool(int(nxt() or 0)),
            isRelic=bool(int(nxt() or 0)), isOnStrike=bool(int(nxt() or 0)),
            isForRent=bool(int(nxt() or 0)), isHireable=bool(int(nxt() or 0)),
            isMortal=bool(int(nxt() or 0)), hasAccess=bool(int(nxt() or 0)),
            slot=int(nxt() or 0), actId=int(nxt() or 0), isMoving=bool(int(nxt() or 0)),
        )
        d["coords"] = {"x": int(nxt() or 0), "y": int(nxt() or 0)}
        d.update(distance=int(nxt() or 0), money=float(nxt() or 0), rank=int(nxt() or 0))
        d["owner"] = {"id": int(nxt() or 0), "blazon": nxt(), "name": nxt()}
    return d


def _csv_items(raw: str, sep: str = ":") -> list[list[str]]:
    return [item.split(sep) for item in raw.split(",") if item]


def parse_unit_more(raw: str) -> dict:
    """query('unit', {id: ..., more: None}) — extended sheet shown in the
    unit info popup: attributes, skills, consumption, equipment, resources."""
    f = _split(raw)
    n = 0

    def nxt():
        nonlocal n
        v = f[n] if n < len(f) else ""
        n += 1
        return v

    d: dict = {"id": int(nxt() or 0), "fullName": nxt(), "legend": nxt()}
    langs = nxt()
    d["langs"] = langs.split(",") if langs else []
    company = nxt()
    d["company"] = []
    for item in _csv_items(company):
        if len(item) >= 4:
            d["company"].append({"id": int(item[0] or 0), "icon": item[1], "name": item[2], "typeName": item[3]})
    d["healingCost"] = int(nxt() or 0)
    d["xpProgress"] = float(nxt() or 0)
    d["stats"] = {
        "st": int(nxt() or 0), "ag": int(nxt() or 0), "co": int(nxt() or 0),
        "in": int(nxt() or 0), "wi": int(nxt() or 0), "ch": int(nxt() or 0),
    }
    d["carriedWeight"] = int(nxt() or 0)
    d["carriageCapacity"] = int(nxt() or 0)
    d["pace"] = int(nxt() or 0)
    d["remoteness"] = int(nxt() or 0)
    d["def"] = {
        "melee": int(nxt() or 0), "polearm": int(nxt() or 0),
        "ranged": int(nxt() or 0), "explode": int(nxt() or 0),
    }
    equipment = nxt()
    d["equipment"] = [
        {"type": i[0], "name": i[1], "quantity": int(i[2]), "quality": float(i[3])}
        for i in _csv_items(equipment) if len(i) >= 4
    ]
    skills = nxt()
    d["skills"] = [{"type": i[0], "name": i[1], "bonus": int(i[2])} for i in _csv_items(skills) if len(i) >= 3]
    auto = nxt()
    d["auto"] = [{"name": i[0], "subtitle": i[1]} for i in _csv_items(auto) if len(i) >= 2]
    consumption = nxt()
    d["consumption"] = [
        {"type": i[0], "name": i[1], "quality": i[2]} for i in _csv_items(consumption) if len(i) >= 3
    ]
    # `resources` (lol.Res) has a long, conditionally-shaped field list on the
    # client (store/carrier/vendor/market blocks only appear when relevant) —
    # only the fixed leading fields are pulled out here; anything malformed
    # is skipped rather than raising, since we don't have a full schema for it.
    resources_raw = nxt()
    resources = []
    for item in resources_raw.split(","):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 10:
            continue
        try:
            resources.append({
                "type": parts[0], "genId": int(parts[1] or 0), "name": parts[2],
                "quality": float(parts[8] or 0), "quantity": float(parts[9] or 0),
            })
        except ValueError:
            continue
    d["resources"] = resources
    return d


def parse_weather(raw: str) -> dict:
    symbol, _, temp = raw.partition(":")
    return {"symbol": symbol, "temp": int(temp or 0)}


def split_items(raw: str, sep: str = "$") -> list[str]:
    return [item for item in raw.split(sep) if item]


def _field(f: list[str], i: int, cast=str, default=None):
    if i >= len(f) or f[i] == "":
        return default
    try:
        return cast(f[i])
    except ValueError:
        return default


def parse_building(raw: str) -> dict:
    """One entry of /mgmt/<org>/buildings — field order from lol.Building."""
    f = _split(raw)
    return {
        "id": _field(f, 0, int, 0), "type": _field(f, 1, str, ""),
        "level": _field(f, 2, int, 0), "status": _field(f, 3, str, ""),
        "name": _field(f, 4, str, ""), "quality": _field(f, 5, float, 0.0),
        "state": _field(f, 6, float, 0.0),
        "coords": {"x": _field(f, 7, int, 0), "y": _field(f, 8, int, 0)},
        "distance": _field(f, 9, int, 0), "isWild": bool(_field(f, 10, int, 0)),
        "compatibility": _field(f, 11, float, 0.0),
        "owner": {"id": _field(f, 12, int, 0), "blazon": _field(f, 13, str, ""), "name": _field(f, 14, str, "")},
    }


def parse_resource_stock(raw: str) -> dict:
    """One entry of /mgmt/<org>/resources — only the fixed leading fields of
    lol.Res are parsed (enough for a stock overview); the tail is a long,
    conditionally-shaped block (store/carrier/vendor/price/market) that
    isn't needed here and isn't parsed."""
    f = _split(raw)
    return {
        "type": _field(f, 0, str, ""), "genId": _field(f, 1, int, 0), "name": _field(f, 2, str, ""),
        "isPersistent": bool(_field(f, 3, int, 0)), "isRelic": bool(_field(f, 4, int, 0)),
        "isLocked": bool(_field(f, 5, int, 0)), "canDelete": bool(_field(f, 6, int, 0)),
        "unit": _field(f, 7, str, ""), "quality": _field(f, 8, float, 0.0),
        "quantity": _field(f, 9, float, 0.0), "inside": _field(f, 10, float, 0.0),
        "missingQuantity": _field(f, 11, float, 0.0), "reservedQuantity": _field(f, 12, float, 0.0),
        "restored": _field(f, 13, int, 0),
        "coords": {"x": _field(f, 14, int, 0), "y": _field(f, 15, int, 0)},
    }


def parse_map_info(raw: str) -> dict:
    f = _split(raw)
    return {
        "x": _field(f, 0, int, 0), "y": _field(f, 1, int, 0),
        "w": _field(f, 2, int, 0), "h": _field(f, 3, int, 0),
        "zoom": _field(f, 4, int, 1), "mode": _field(f, 5, str, ""),
        "group": _field(f, 6, int, 0),
        "tx0": _field(f, 7, int, 0), "ty0": _field(f, 8, int, 0),
        "tw": _field(f, 9, int, 0), "th": _field(f, 10, int, 0),
        "altx": _field(f, 11, int, 0),
    }


def parse_map_tiles(raw: str) -> list[dict]:
    tiles = []
    for item in split_items(raw):
        f = item.split("|")
        if len(f) < 5:
            continue
        tiles.append({
            "tag": f[0], "x": int(f[1]), "y": int(f[2]),
            "version": int(f[3]), "released": bool(int(f[4])),
        })
    return tiles


def parse_map_response(raw: str) -> dict:
    """query('map', {nav:'', x, y, w, h, zoom, mode}) — '§'-delimited
    sections, order from lol.Map.updateWithData. Only the sections needed
    for a minimap (weather/info/tiles) are parsed here; the rest (regions,
    domains, wonders, orgs, units, routes, tactics, planning, ...) are
    ignored for now."""
    sections = raw.split("§")
    sections += [""] * max(0, 4 - len(sections))
    return {
        "weather": parse_weather(sections[0]) if sections[0] else None,
        "info": parse_map_info(sections[2]) if sections[2] else None,
        "tiles": parse_map_tiles(sections[3]) if sections[3] else [],
    }


def tile_url(base_url: str, zoom: int, group: int, tile: dict) -> str:
    suffix = "" if tile["released"] else ".unreleased"
    return f"{base_url}/img/tile/{tile['tag']}.{zoom}.{group}.{tile['version']}{suffix}.jpg"


def format_map_coords(x: int, y: int) -> str:
    """/map/<coords> URL format, e.g. (10961, -28052) -> '10961E28052N'.
    Sign convention confirmed against the in-game calendar/coordinate
    display: negative y shows as N(orth), positive x as E(ast)."""
    ew = "E" if x >= 0 else "W"
    ns = "N" if y <= 0 else "S"
    return f"{abs(x)}{ew}{abs(y)}{ns}"


# A /map/<coords> tile page is plain server-rendered HTML — no cipher
# involved at all, unlike everything else in this file. Its right column
# has up to two `<h2>` sections in a row: a natural-cover one linking to
# /help/bld?type=... (forest/orchard/etc — missing if something's been
# built on the plot) and a ground/terrain one linking to /help/gnd?type=...
# (always present). Parsed per-section (split on <h2>) rather than with one
# combined regex, so a heading elsewhere on the page can't get matched up
# with the wrong link/quality further down.
_TILE_HEADING_RE = re.compile(r"<h2>([^<]+?)(?:\s*<small>\(([^)]*)\)</small>)?</h2>")
_TILE_TYPE_RE = re.compile(r"lol\.go\('/help/(bld|gnd)\?type=(\w+)'\)")
_TILE_QUALITY_RE = re.compile(r'title="([^"]*\((\d+)%\))"')
# Which domain (if any) claims this spot — a `<div class="card[ store]"
# data-id="<id>">...<i class="name">Name</i>` block ("Tabletland / 4326
# acres / ..." + the domain's mini management card), present in the tile
# page's right column whenever the tile falls inside *some* domain's zone
# (ours or foreign — confirmed absent for open sea/wilderness far from any
# settlement). First version tried `<a href="/arm/org/<id>">Name</a>`, but
# that link's content is an `<img>`, not text, so it never actually
# matched anything — caught live against a real tile (the domain's own
# center, which should obviously show itself as the owner).
#
# The class was originally assumed to be `card store` only for your OWN
# territory (confirmed on 2600+ foreign built-up tiles, zero `card store`
# blocks) — true as far as it went, but "store" turned out to just mean
# "this card has management buttons enabled", not "this is the only owner
# card that ever renders". A tile with no building at all — a copper deposit
# under natural tree cover, inside a *foreign* domain's agricultural zone —
# still renders the same `<i class="name">` card, just as plain `class="card"`
# (no "store"). Caught live at 11251E27725N: domain block showed "Linum-Et-Sal"
# with zone distance 29, `owner_org_id` came back None under the old regex,
# yet the user confirmed in-game that a rendered herald/name card there does
# mean the plot is claimed. " store" is now optional in the match.
_TILE_OWNER_RE = re.compile(r'<div class="card(?: store)?" data-id="(\d+)">.*?<i class="name">([^<]+)</i>', re.S)
# Fallback for tiles that have a building/street/crop feature (`cover` set)
# but, for whatever reason, no full domain card above — the smaller "who
# built this" icon block: `<div class="bldicons"><a href="/arm/org/<id>"
# class="button"><img ... title="<Name>"/></a>`. Tied to the cover feature
# rather than the plot itself, so still absent on genuinely untouched
# wilderness with no card either.
_TILE_BLDICONS_OWNER_RE = re.compile(r'<div class="bldicons">.*?<a href="/arm/org/(\d+)" class="button"><img[^>]*title="([^"]+)"', re.S)
# Extra per-tile info worth capturing once rather than re-fetching the same
# page again later for it (all fixed/slow-changing properties of the spot,
# unlike weather or the region-wide "generation" counter, which are both
# skipped — they're the same for the whole area and change constantly,
# so storing them per-tile would be stale the moment it's saved).
_TILE_ROAD_RE = re.compile(r"icon/road\.png")
_TILE_SKILL_RE = re.compile(r'/help/skill\?type=(\w+)">[^<]+</a>\s*<span class="bonus">(-?[\d.]+)</span>')
# Cover-only stats — condition (wear) and structural resistance, both
# absent on ground (only built/grown things degrade or resist destruction).
# Note: `_TILE_SKILL_RE` above matches the raw pre-render HTML, where bonus
# spans are plain `class="bonus"` with full float precision (e.g.
# "23.444643661409") — the colored, rounded "+23" seen in a browser is a
# client-side JS reformat applied *after* this page loads, never present in
# what our scanner actually fetches. Skill bonuses apply to whichever slot
# (ground or cover) they're printed under — e.g. a tree cover's own
# woodcutting/hunting bonuses — not just ground, which the code used to
# assume and silently drop the cover ones.
_TILE_STATE_RE = re.compile(r'<div class="state[^"]*"[^>]*title="[^"]*\((\d+)%\)"')
_TILE_RESIST_RE = re.compile(r'title="Сопротивление \(к разрушению\)"[^>]*>[^\d]*(\d+)')
# The domain-info block — present only when the tile falls inside *some*
# domain's territory (ours or foreign), between the weather <h2> and the
# region <h2>. Unlike the owner name (which is per-org), this is the
# *domain's* own name/size — e.g. a village and its parish can be two
# different orgs over the same domain, both pointing at one "Mearm".
_TILE_DOMAIN_RE = re.compile(
    r'<h2>([^<]+)</h2>\s*<p>([\d.,]+)\s*акров.*?<br>(\d+)\s*жителей', re.S,
)
_TILE_DOMAIN_DIST_RE = re.compile(r'Расстояние от центра домена"[^>]*>&thinsp;(\d+)')
# A second icon — `icon/protected.png` title="Защищённая область" — sometimes
# follows the zone distance/label in the same short <p>, e.g. tiles right
# around a settlement's core. Bounded to just after the distance match so it
# can't accidentally pick up an unrelated "protected" icon further down the
# page.
_TILE_PROTECTED_RE = re.compile(r"icon/protected\.png")


def parse_tile_page(html: str) -> dict:
    result: dict = {
        "cover": None, "ground": None, "owner_org_id": None, "owner_org_name": None,
        "road": False, "ground_skills": None, "cover_skills": None,
        "cover_state_pct": None, "cover_resist": None, "domain": None,
    }
    col_match = re.search(r'<div class="column" id="right">(.*?)<div id="footer"', html, re.S)
    region = col_match.group(1) if col_match else html
    owner_m = _TILE_OWNER_RE.search(region)
    if not owner_m:
        owner_m = _TILE_BLDICONS_OWNER_RE.search(region)
    if owner_m:
        result["owner_org_id"] = int(owner_m.group(1))
        result["owner_org_name"] = owner_m.group(2).strip()
    for chunk in re.split(r"(?=<h2>)", region):
        heading = _TILE_HEADING_RE.match(chunk)
        type_m = _TILE_TYPE_RE.search(chunk)
        quality_m = _TILE_QUALITY_RE.search(chunk)
        if not (heading and type_m and quality_m):
            continue
        name, subtitle = heading.groups()
        kind, ground_type = type_m.groups()
        entry = {
            "name": name.strip(), "type": ground_type,
            "quality_label": quality_m.group(1), "quality_pct": int(quality_m.group(2)),
        }
        if subtitle:
            entry["subtitle"] = subtitle
        slot = "cover" if kind == "bld" else "ground"
        if result[slot] is None:
            result[slot] = entry
            skills = {m.group(1): round(float(m.group(2)), 1) for m in _TILE_SKILL_RE.finditer(chunk)}
            if slot == "ground":
                result["road"] = bool(_TILE_ROAD_RE.search(chunk))
                if skills:
                    result["ground_skills"] = skills
            else:
                if skills:
                    result["cover_skills"] = skills
                state_m = _TILE_STATE_RE.search(chunk)
                if state_m:
                    result["cover_state_pct"] = int(state_m.group(1))
                resist_m = _TILE_RESIST_RE.search(chunk)
                if resist_m:
                    result["cover_resist"] = int(resist_m.group(1))

    domain_m = _TILE_DOMAIN_RE.search(region)
    if domain_m:
        dist_m = _TILE_DOMAIN_DIST_RE.search(region, domain_m.end())
        protected = bool(dist_m and _TILE_PROTECTED_RE.search(region, dist_m.end(), dist_m.end() + 150))
        result["domain"] = {
            "name": domain_m.group(1).strip(),
            "acres": int(domain_m.group(2).replace(".", "").replace(",", "")),
            "population": int(domain_m.group(3)),
            "center_distance": int(dist_m.group(1)) if dist_m else None,
            "protected": protected,
        }
    return result


# Every ground ("gnd") type the game's own encyclopedia lists, across its 8
# geology sub-categories (/help/gnd/<category>, all publicly readable — no
# login needed): arid, deposit (minerals), flat, mountain, rocky, volcanic,
# water, wet. Several type codes are cross-listed under more than one
# category on the site (e.g. "volcano" under both mountain and volcanic,
# "salt" under both arid and deposit) — each is assigned here to a single
# category by priority (deposit first, since that's the actionable one,
# then water/wet/volcanic/mountain/rocky/arid/flat), used for map-marker
# styling. `name` is the encyclopedia's category-page label, for reference/
# legends — prefer the live per-tile `name` from parse_tile_page() when
# displaying an actual scanned hit, since wording can differ slightly
# (e.g. "Луга" here vs "Луг" on an actual meadow tile).
GND_TYPES = {
    # deposit (mineral) — highest priority
    "iron": {"name": "Месторождение железа", "category": "deposit"},
    "tin": {"name": "Месторождение олова", "category": "deposit"},
    "copper": {"name": "Месторождение меди", "category": "deposit"},
    "coal": {"name": "Месторождение угля", "category": "deposit"},
    "salt": {"name": "Солончак", "category": "deposit"},
    "lead": {"name": "Месторождение свинца", "category": "deposit"},
    "silver": {"name": "Месторождение серебра", "category": "deposit"},
    "gold": {"name": "Месторождение золота", "category": "deposit"},
    "emerald": {"name": "Месторождение изумрудов", "category": "deposit"},
    "ruby": {"name": "Месторождение рубинов", "category": "deposit"},
    "diamond": {"name": "Месторождение алмазов", "category": "deposit"},
    "saphir": {"name": "Месторождение сапфиров", "category": "deposit"},
    "sulfur": {"name": "Месторождение серы", "category": "deposit"},
    # water
    "ice": {"name": "Лёд", "category": "water"},
    "swater": {"name": "Море", "category": "water"},
    "bank": {"name": "Берег", "category": "water"},
    "canal": {"name": "Канал", "category": "water"},
    "dike": {"name": "Плотина", "category": "water"},
    "fwater": {"name": "Река", "category": "water"},
    "pond": {"name": "Пруд", "category": "water"},
    "shore": {"name": "Побережье", "category": "water"},
    # wet
    "tundra": {"name": "Тундра", "category": "wet"},
    "marsh": {"name": "Болото", "category": "wet"},
    "smarsh": {"name": "Марши", "category": "wet"},
    "silt": {"name": "Ил", "category": "wet"},
    # volcanic
    "basalt": {"name": "Базальтовая порода", "category": "volcanic"},
    "volcano": {"name": "Вулкан", "category": "volcanic"},
    # mountain
    "limeston": {"name": "Известняковая порода", "category": "mountain"},
    "granite": {"name": "Гранитная порода", "category": "mountain"},
    "sandston": {"name": "Песчаная порода", "category": "mountain"},
    "marble": {"name": "Мраморная порода", "category": "mountain"},
    "guano": {"name": "Месторождение гуано", "category": "mountain"},
    # rocky
    "limey": {"name": "Известняковое плато", "category": "rocky"},
    "shrub": {"name": "Кустарниковая степь", "category": "rocky"},
    "schist": {"name": "Сланец", "category": "rocky"},
    "slate": {"name": "Шифер", "category": "rocky"},
    "plateau": {"name": "Плато", "category": "rocky"},
    # arid
    "dune": {"name": "Дюны", "category": "arid"},
    "brush": {"name": "Кустарниковые заросли", "category": "arid"},
    # flat
    "meadow": {"name": "Луга", "category": "flat"},
    "plain": {"name": "Равнина", "category": "flat"},
    "clay": {"name": "Глинистая почва", "category": "flat"},
    "chalk": {"name": "Меловая почва", "category": "flat"},
}

MINERAL_GROUND_TYPES = {t for t, info in GND_TYPES.items() if info["category"] == "deposit"}
