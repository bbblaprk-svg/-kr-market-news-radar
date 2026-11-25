"""Independent US pre-market -> Korea NXT after-market synchronization observer.

Purpose
-------
Measure whether US pre-market price discovery leads Korean NXT after-market
prices by minutes. This module is observational: it never places orders and it
never reuses NXT_POST_ACCEL decisions. It only reads the existing NXT authority
rows and persists its own bounded research ledger.

Key safety rule: a single arbitrary US mover does not activate a Korean theme.
Activation needs either an explicit direct anchor mapping or >=2 US movers in
the same transferable industry theme.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))
VERSION = "US_PRE_NXT_SYNC_V1_0"

# Direct anchors are intentionally sparse. These are cross-market transmission
# anchors, not general theme recommendations.
DIRECT_MAP = {
    "MU": ["000660", "005930", "042700", "240810", "104830"],
    "NVDA": ["000660", "042700", "007660", "353200", "356860"],
    "AMD": ["000660", "005930", "353200", "356860"],
    "LRCX": ["240810", "036930", "084370", "319660"],
    "AMAT": ["240810", "036930", "084370", "319660"],
    "ASML": ["240810", "036930", "084370"],
    "TSM": ["000660", "005930", "042700"],
    "AVGO": ["007660", "353200", "356860", "218410"],
    "MRVL": ["007660", "353200", "356860", "138080"],
    "CRDO": ["007660", "353200", "356860", "138080"],
    "LITE": ["010170", "115440", "056360", "138080"],
    "CIEN": ["010170", "115440", "056360", "138080"],
    "ANET": ["010170", "115440", "056360", "007660"],
    "PANW": ["053800", "263860", "150900", "411080"],
    "CRWD": ["053800", "263860", "150900", "411080"],
    "ZS": ["053800", "263860", "150900", "430690"],
    "OKLO": ["034020", "052690", "083650", "032820"],
    "SMR": ["034020", "052690", "083650", "105840"],
    "CCJ": ["034020", "052690", "083650", "105840"],
    "LEU": ["034020", "052690", "083650", "105840"],
    "FSLR": ["009830", "010060", "322000", "011930"],
    "ENPH": ["009830", "010060", "322000", "011930"],
    "SEDG": ["009830", "010060", "322000", "011930"],
    "BE": ["336260", "382900", "288620", "267260"],
    "PLUG": ["336260", "382900", "288620"],
    "FCEL": ["336260", "382900", "288620"],
    "GEV": ["267260", "298040", "010120", "062040"],
    "ETN": ["267260", "298040", "010120", "062040"],
    "VRT": ["083450", "053080", "396470", "267260"],
    "RKLB": ["099320", "189300", "361390", "272210"],
    "ASTS": ["099320", "189300", "361390", "272210"],
    "LUNR": ["099320", "189300", "361390", "272210"],
    "LMT": ["012450", "079550", "064350", "047810"],
    "NOC": ["012450", "079550", "272210", "214430"],
    "RTX": ["012450", "079550", "272210", "214430"],
    "TSLA": ["373220", "006400", "247540", "066970", "005380"],
    "RIVN": ["373220", "006400", "247540", "066970"],
}

# Cluster members come from the user's 2026-11-25 Korean synchronization map.
THEME_KR = {
    "SEMICONDUCTOR": ["042700", "089030", "031980", "039030", "240810", "036930", "084370", "319660", "357780", "005290", "014680", "104830", "058470", "095340", "131290", "353200", "222800", "356860", "007810"],
    "OPTICAL_NETWORK": ["010170", "115440", "056360", "138080", "218410", "050890", "073490"],
    "CYBERSECURITY": ["053800", "263860", "042510", "067920", "150900", "430690", "411080", "434480", "356680", "115500", "046970"],
    "POWER_GRID": ["267260", "298040", "010120", "062040", "001440", "000500", "103590"],
    "DATA_CENTER_COOLING": ["083450", "053080", "396470"],
    "NUCLEAR": ["034020", "052690", "083650", "032820", "105840"],
    "SOLAR": ["009830", "010060", "322000", "011930"],
    "HYDROGEN_FUELCELL": ["336260", "382900", "288620"],
    "BATTERY_EV": ["373220", "006400", "096770", "247540", "066970", "003670", "005070", "137400", "222080", "372170", "217820"],
    "ROBOTICS": ["277810", "108490", "454910", "466100", "058610", "160190", "389500", "437730", "090360", "388720", "117730", "319400"],
    "DEFENSE": ["012450", "079550", "064350", "047810", "272210", "214430", "448710"],
    "SPACE": ["099320", "189300", "361390", "272210"],
    "COPPER": ["103140", "025820", "012800"],
    "STEEL": ["005490", "004020", "460860"],
    "SHIPBUILDING": ["329180", "042660", "010140", "009540", "082740", "071970", "077970", "033500", "017960", "075580", "014620", "023160", "013030"],
}

THEME_RULES = [
    ("SEMICONDUCTOR", ("semiconductor", "chip", "integrated circuit", "memory", "fabless", "wafer", "microprocessor")),
    ("OPTICAL_NETWORK", ("optical", "fiber optic", "networking", "communications equipment", "data communications")),
    ("CYBERSECURITY", ("cybersecurity", "security software", "network security", "identity security")),
    ("POWER_GRID", ("electrical equipment", "power equipment", "transformer", "grid", "power generation equipment")),
    ("DATA_CENTER_COOLING", ("data center", "thermal management", "cooling")),
    ("NUCLEAR", ("nuclear", "uranium")),
    ("SOLAR", ("solar", "photovoltaic")),
    ("HYDROGEN_FUELCELL", ("fuel cell", "hydrogen")),
    ("BATTERY_EV", ("electric vehicle", "battery", "lithium")),
    ("ROBOTICS", ("robotics", "industrial automation")),
    ("DEFENSE", ("defense", "military", "weapons")),
    ("SPACE", ("space", "satellite")),
    ("COPPER", ("copper",)),
    ("STEEL", ("steel",)),
    ("SHIPBUILDING", ("shipbuilding", "marine transportation equipment")),
]

THRESHOLDS = (0.5, 1.0, 1.5, 2.0, 3.0)
DOWNSIDE = (-0.5, -1.0)


def now_kst() -> datetime:
    return datetime.now(KST)


def stamp(t: datetime | None = None) -> str:
    return (t or now_kst()).isoformat(timespec="seconds")


def num(v):
    if isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", "").replace("%", "").replace("$", "").strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_human_number(s):
    if s is None:
        return None
    raw = str(s).replace("$", "").replace(",", "").strip().upper()
    m = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*([KMBT]?)", raw)
    if not m:
        return num(raw)
    x = float(m.group(1))
    return x * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(2)]


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.cell = []
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.in_tr = True
            self.row = []
        elif self.in_tr and tag in ("td", "th"):
            self.in_cell = True
            self.cell = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.in_tr and tag in ("td", "th") and self.in_cell:
            text = " ".join(" ".join(self.cell).split())
            self.row.append(html.unescape(text))
            self.in_cell = False
        elif tag == "tr" and self.in_tr:
            if self.row:
                self.rows.append(self.row)
            self.in_tr = False


def _default_http(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; US_PRE_NXT_SYNC/1.0; research observer)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


class USPremarketSource:
    def __init__(self, http_get=None):
        self.http_get = http_get or _default_http
        self.json_url = os.getenv("US_PRE_JSON_URL", "").strip()
        self.html_url = os.getenv("US_PRE_HTML_URL", "https://chartexchange.com/screener/pre-market-gainers/").strip()

    def fetch(self):
        if self.json_url:
            raw = self.http_get(self.json_url)
            obj = json.loads(raw)
            rows = obj.get("rows", obj if isinstance(obj, list) else [])
            return self._normalize(rows), "US_PRE_JSON_URL"
        raw = self.http_get(self.html_url)
        return self._parse_html(raw), "CHARTEXCHANGE_PREMARKET_GAINERS"

    def _normalize(self, rows):
        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            ticker = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
            pct = num(r.get("pre_pct") if r.get("pre_pct") is not None else r.get("premarket_pct"))
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker) or pct is None:
                continue
            out.append({
                "ticker": ticker,
                "name": str(r.get("name") or ticker),
                "pre_price": num(r.get("pre_price") or r.get("price")),
                "pre_pct": pct,
                "pre_volume": parse_human_number(r.get("pre_volume") or r.get("volume")),
                "market_cap": parse_human_number(r.get("market_cap")),
                "industry": str(r.get("industry") or ""),
                "exchange": str(r.get("exchange") or ""),
                "equity_type": str(r.get("equity_type") or ""),
            })
        return out

    def _parse_html(self, raw):
        p = _TableParser()
        p.feed(raw)
        rows = p.rows
        header = None
        out = []
        for row in rows:
            low = [x.lower() for x in row]
            if header is None and any("symbol" == x or "symbol" in x for x in low) and any("pre-market" in x or "premarket" in x for x in low):
                header = low
                continue
            if not header or len(row) < 5:
                continue
            # ChartExchange expected: #, Symbol, Price, Pre-market %, MarketCap,
            # Volume, Name, ..., Exchange, EquityType, Industry
            ticker = row[1].upper().strip() if len(row) > 1 else ""
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
                continue
            pct = num(row[3] if len(row) > 3 else None)
            if pct is None:
                continue
            out.append({
                "ticker": ticker,
                "name": row[6] if len(row) > 6 else ticker,
                "pre_price": num(row[2] if len(row) > 2 else None),
                "pre_pct": pct,
                "market_cap": parse_human_number(row[4] if len(row) > 4 else None),
                "pre_volume": parse_human_number(row[5] if len(row) > 5 else None),
                "exchange": row[-3] if len(row) >= 3 else "",
                "equity_type": row[-2] if len(row) >= 2 else "",
                "industry": row[-1] if row else "",
            })
        return out


def theme_for(row):
    ticker = str(row.get("ticker") or "").upper()
    if ticker in DIRECT_MAP:
        # Direct ticker may still receive its industry theme, but direct mapping
        # remains the activation authority.
        direct = True
    else:
        direct = False
    text = (str(row.get("industry") or "") + " " + str(row.get("name") or "")).lower()
    for theme, keys in THEME_RULES:
        if any(k in text for k in keys):
            return theme, direct
    return None, direct


@dataclass
class Config:
    min_pct: float = float(os.getenv("US_PRE_MIN_PCT", "5"))
    direct_min_pct: float = float(os.getenv("US_PRE_DIRECT_MIN_PCT", "7"))
    sector_count: int = int(os.getenv("US_PRE_SECTOR_CONFIRM_COUNT", "2"))
    sector_mean_pct: float = float(os.getenv("US_PRE_SECTOR_MEAN_PCT", "5"))
    min_market_cap: float = float(os.getenv("US_PRE_MIN_MCAP_USD", "200000000"))
    min_volume: float = float(os.getenv("US_PRE_MIN_VOLUME", "50000"))
    top_n: int = int(os.getenv("US_PRE_TOP_N", "30"))
    poll_seconds: int = int(os.getenv("US_PRE_POLL_SECONDS", "60"))
    event_minutes: int = int(os.getenv("US_PRE_EVENT_MINUTES", "90"))
    retention_days: int = int(os.getenv("US_PRE_RETENTION_DAYS", "120"))
    kr_no_chase_pct: float = float(os.getenv("US_PRE_KR_NO_CHASE_PCT", "8"))
    lag_gap_pct: float = float(os.getenv("US_PRE_LAG_GAP_PCT", "2"))


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS source_snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, ticker TEXT, theme TEXT,
              pre_pct REAL, pre_price REAL, pre_volume REAL, market_cap REAL,
              name TEXT, industry TEXT, source TEXT);
            CREATE INDEX IF NOT EXISTS idx_uspre_snap_at ON source_snapshots(at);
            CREATE INDEX IF NOT EXISTS idx_uspre_snap_theme ON source_snapshots(theme,at);
            CREATE TABLE IF NOT EXISTS events(
              event_id TEXT PRIMARY KEY, trade_date TEXT, theme TEXT, trigger_at TEXT,
              trigger_pct REAL, mean_pct REAL, confirm_count INTEGER, mapping_mode TEXT,
              source TEXT, status TEXT, last_at TEXT, payload TEXT);
            CREATE INDEX IF NOT EXISTS idx_uspre_event_day ON events(trade_date,trigger_at);
            CREATE TABLE IF NOT EXISTS tracks(
              event_id TEXT, code TEXT, name TEXT, baseline_at TEXT, baseline_price REAL,
              baseline_change_pct REAL, last_at TEXT, last_price REAL, last_ret REAL,
              max_ret REAL, min_ret REAL, state TEXT, hit_json TEXT, payload TEXT,
              PRIMARY KEY(event_id,code));
            CREATE INDEX IF NOT EXISTS idx_uspre_tracks_state ON tracks(state,last_at);
            """)

    def connect(self):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def cleanup(self, current, days):
        cutoff = stamp(current - timedelta(days=max(30, days)))
        with self.lock, self.connect() as c:
            c.execute("DELETE FROM source_snapshots WHERE at<?", (cutoff,))
            c.execute("DELETE FROM tracks WHERE event_id IN (SELECT event_id FROM events WHERE last_at<?)", (cutoff,))
            c.execute("DELETE FROM events WHERE last_at<?", (cutoff,))


class SyncSidecar:
    def __init__(self, ops, source=None, config=None, path=None):
        self.ops = ops
        self.source = source or USPremarketSource()
        self.cfg = config or Config()
        root = path or os.getenv("US_PRE_NXT_DB", "/data/us_pre_nxt_sync/us_pre_nxt_sync.sqlite3")
        try:
            self.ledger = Ledger(root)
        except OSError:
            self.ledger = Ledger("/tmp/us_pre_nxt_sync.sqlite3")
        self.stop = threading.Event()
        self.thread = None
        self.last = {"status": "PENDING", "version": VERSION, "reason": "NOT_RUN"}
        self._last_raw_store = None
        self._last_cleanup = None

    def in_window(self, current):
        if current.weekday() >= 5:
            return False
        hm = current.hour * 60 + current.minute
        return 18 * 60 <= hm < 20 * 60

    def _eligible_us(self, rows):
        out = []
        for r in rows:
            pct = num(r.get("pre_pct")); cap = num(r.get("market_cap")); vol = num(r.get("pre_volume"))
            typ = str(r.get("equity_type") or "").lower()
            if pct is None or pct < self.cfg.min_pct:
                continue
            if cap is not None and cap < self.cfg.min_market_cap:
                continue
            if vol is not None and vol < self.cfg.min_volume:
                continue
            if any(x in typ for x in ("etf", "warrant", "preferred", "unit")):
                continue
            theme, direct = theme_for(r)
            z = dict(r, theme=theme, direct=direct)
            out.append(z)
        out.sort(key=lambda x: num(x.get("pre_pct")) or -999, reverse=True)
        return out[:max(5, self.cfg.top_n)]

    def _themes(self, eligible):
        grouped = {}
        for r in eligible:
            theme = r.get("theme")
            if theme:
                grouped.setdefault(theme, []).append(r)
        active = []
        for theme, xs in grouped.items():
            pcts = [num(x.get("pre_pct")) for x in xs if num(x.get("pre_pct")) is not None]
            direct_x = [x for x in xs if x.get("direct") and (num(x.get("pre_pct")) or 0) >= self.cfg.direct_min_pct]
            sector_ok = len(xs) >= self.cfg.sector_count and (sum(pcts) / len(pcts) if pcts else -999) >= self.cfg.sector_mean_pct
            if not direct_x and not sector_ok:
                continue
            codes = []
            mode = "DIRECT" if direct_x else "SECTOR_CONFIRM"
            for x in direct_x:
                codes.extend(DIRECT_MAP.get(x["ticker"], []))
            if sector_ok:
                codes.extend(THEME_KR.get(theme, []))
                if direct_x:
                    mode = "DIRECT+SECTOR_CONFIRM"
            # Preserve configured priority and remove duplicates.
            codes = list(dict.fromkeys(codes))
            active.append({
                "theme": theme,
                "rows": xs,
                "codes": codes,
                "mapping_mode": mode,
                "max_pct": max(pcts) if pcts else None,
                "mean_pct": sum(pcts) / len(pcts) if pcts else None,
                "confirm_count": len(xs),
            })
        active.sort(key=lambda x: ((x["mapping_mode"] != "SECTOR_CONFIRM"), x["max_pct"] or -999), reverse=True)
        return active

    def _event_id(self, current, theme):
        return current.date().isoformat() + ":" + theme

    def _create_or_refresh_event(self, current, t, source_name, qmap):
        event_id = self._event_id(current, t["theme"])
        with self.ledger.lock, self.ledger.connect() as c:
            row = c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            payload = {
                "us_rows": t["rows"][:10],
                "mapped_codes": t["codes"],
                "policy": {"sector_confirm_count": self.cfg.sector_count, "kr_no_chase_pct": self.cfg.kr_no_chase_pct, "lag_gap_pct": self.cfg.lag_gap_pct},
            }
            if row is None:
                c.execute("INSERT INTO events(event_id,trade_date,theme,trigger_at,trigger_pct,mean_pct,confirm_count,mapping_mode,source,status,last_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (event_id, current.date().isoformat(), t["theme"], stamp(current), t["max_pct"], t["mean_pct"], t["confirm_count"], t["mapping_mode"], source_name, "ACTIVE", stamp(current), json.dumps(payload, ensure_ascii=False)))
                for code in t["codes"]:
                    q = qmap.get(code)
                    if not q:
                        continue
                    price = num(q.get("price")); ch = num(q.get("change_pct") if q.get("change_pct") is not None else q.get("change_rate"))
                    if not price or price <= 0:
                        continue
                    state = "PRICED_IN" if ch is not None and ch >= self.cfg.kr_no_chase_pct else "SYNC_LAG"
                    c.execute("INSERT OR IGNORE INTO tracks(event_id,code,name,baseline_at,baseline_price,baseline_change_pct,last_at,last_price,last_ret,max_ret,min_ret,state,hit_json,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (event_id, code, q.get("name") or code, stamp(current), price, ch, stamp(current), price, 0.0, 0.0, 0.0, state, "{}", json.dumps({"mapping_mode": t["mapping_mode"]}, ensure_ascii=False)))
            else:
                c.execute("UPDATE events SET trigger_pct=?,mean_pct=?,confirm_count=?,mapping_mode=?,source=?,last_at=?,payload=? WHERE event_id=?",
                          (t["max_pct"], t["mean_pct"], t["confirm_count"], t["mapping_mode"], source_name, stamp(current), json.dumps(payload, ensure_ascii=False), event_id))
        return event_id

    def _update_tracks(self, current, active_by_theme, qmap):
        with self.ledger.lock, self.ledger.connect() as c:
            events = c.execute("SELECT * FROM events WHERE trade_date=? AND status='ACTIVE'", (current.date().isoformat(),)).fetchall()
            for ev in events:
                trigger = datetime.fromisoformat(ev["trigger_at"])
                if trigger.tzinfo is None:
                    trigger = trigger.replace(tzinfo=KST)
                expired = (current - trigger).total_seconds() > self.cfg.event_minutes * 60 or current.hour >= 20
                t = active_by_theme.get(ev["theme"])
                if expired:
                    c.execute("UPDATE events SET status='CLOSED',last_at=? WHERE event_id=?", (stamp(current), ev["event_id"]))
                tracks = c.execute("SELECT * FROM tracks WHERE event_id=?", (ev["event_id"],)).fetchall()
                for tr in tracks:
                    q = qmap.get(tr["code"])
                    if not q:
                        continue
                    price = num(q.get("price"))
                    if not price or not tr["baseline_price"]:
                        continue
                    ret = (price / tr["baseline_price"] - 1.0) * 100.0
                    mx = max(num(tr["max_ret"]) or 0.0, ret)
                    mn = min(num(tr["min_ret"]) or 0.0, ret)
                    hits = json.loads(tr["hit_json"] or "{}")
                    for th in THRESHOLDS:
                        key = "up_" + str(th).replace(".", "_")
                        if key not in hits and ret >= th:
                            hits[key] = stamp(current)
                    for th in DOWNSIDE:
                        key = "down_" + str(abs(th)).replace(".", "_")
                        if key not in hits and ret <= th:
                            hits[key] = stamp(current)
                    ch = num(q.get("change_pct") if q.get("change_pct") is not None else q.get("change_rate"))
                    state = tr["state"]
                    up05 = hits.get("up_0_5"); down05 = hits.get("down_0_5")
                    up10 = hits.get("up_1_0")
                    if tr["baseline_change_pct"] is not None and tr["baseline_change_pct"] >= self.cfg.kr_no_chase_pct:
                        state = "PRICED_IN"
                    elif up10 and (not down05 or up10 < down05):
                        state = "SYNC_CONFIRMED"
                    elif down05 and (not up05 or down05 < up05):
                        state = "DECOUPLED"
                    elif expired and not up05:
                        state = "NO_RESPONSE"
                    elif t:
                        # A still-strong US theme with a Korea stock that has not
                        # yet moved 0.5% is the actual lag window under study.
                        state = "SYNC_LAG" if ret < 0.5 else "EARLY_RESPONSE"
                    payload = json.loads(tr["payload"] or "{}")
                    if t:
                        payload.update(us_theme_pct=t.get("max_pct"), us_theme_mean_pct=t.get("mean_pct"), us_confirm_count=t.get("confirm_count"), mapping_mode=t.get("mapping_mode"))
                    c.execute("UPDATE tracks SET last_at=?,last_price=?,last_ret=?,max_ret=?,min_ret=?,state=?,hit_json=?,payload=? WHERE event_id=? AND code=?",
                              (stamp(current), price, ret, mx, mn, state, json.dumps(hits, ensure_ascii=False), json.dumps(payload, ensure_ascii=False), ev["event_id"], tr["code"]))

    def cycle(self, current=None):
        current = current or now_kst()
        if not self.in_window(current):
            self.last = {"status": "IDLE", "version": VERSION, "reason": "OUTSIDE_18_20_KST", "at": stamp(current)}
            return self.last
        try:
            raw, source_name = self.source.fetch()
            eligible = self._eligible_us(raw)
            themes = self._themes(eligible)
            nxt_rows = self.ops.live_rows_only("NXT")
            qmap = {str(r.get("code") or r.get("symbol") or ""): r for r in nxt_rows if isinstance(r, dict)}
            # Persist the top raw evidence at 5-minute cadence, not every poll.
            bucket = current.replace(second=0, microsecond=0, minute=(current.minute // 5) * 5)
            if self._last_raw_store != bucket:
                with self.ledger.lock, self.ledger.connect() as c:
                    for r in eligible:
                        c.execute("INSERT INTO source_snapshots(at,ticker,theme,pre_pct,pre_price,pre_volume,market_cap,name,industry,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  (stamp(current), r["ticker"], r.get("theme"), r.get("pre_pct"), r.get("pre_price"), r.get("pre_volume"), r.get("market_cap"), r.get("name"), r.get("industry"), source_name))
                self._last_raw_store = bucket
            for t in themes:
                if t["codes"]:
                    self._create_or_refresh_event(current, t, source_name, qmap)
            active_by_theme = {t["theme"]: t for t in themes}
            self._update_tracks(current, active_by_theme, qmap)
            if self._last_cleanup is None or (current - self._last_cleanup).total_seconds() >= 3600:
                self.ledger.cleanup(current, self.cfg.retention_days)
                self._last_cleanup = current
            self.last = {
                "status": "READY", "version": VERSION, "at": stamp(current), "source": source_name,
                "us_rows": len(raw), "eligible_top": len(eligible), "active_themes": len(themes),
                "themes": [{k:v for k,v in t.items() if k != "rows"} for t in themes],
                "nxt_rows": len(nxt_rows),
            }
        except Exception as exc:
            self.last = {"status": "PENDING", "version": VERSION, "at": stamp(current), "error": type(exc).__name__, "detail": str(exc)[:180]}
        return self.last

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def loop():
            self.stop.wait(3)
            while not self.stop.is_set():
                current = now_kst()
                try:
                    self.cycle(current)
                except Exception as exc:
                    self.last = {"status":"PENDING","version":VERSION,"at":stamp(current),"error":type(exc).__name__}
                wait = self.cfg.poll_seconds if self.in_window(current) else 300
                self.stop.wait(max(30, wait))
        self.thread = threading.Thread(target=loop, daemon=True, name="us-pre-nxt-sync")
        self.thread.start()

    def api(self, raw_path):
        u = urlparse(raw_path); q = parse_qs(u.query)
        parts = [x for x in u.path.split("/") if x]
        kind = parts[-1] if parts else "status"
        try:
            limit = max(1, min(500, int((q.get("limit") or ["100"])[0])))
        except ValueError:
            return 400, {"error":"invalid limit"}
        if kind in ("us-pre-nxt-sync", "status"):
            return 200, dict(self.last, policy={
                "window_kst":"18:00-20:00", "min_pct":self.cfg.min_pct,
                "direct_min_pct":self.cfg.direct_min_pct, "sector_confirm_count":self.cfg.sector_count,
                "sector_mean_pct":self.cfg.sector_mean_pct, "kr_no_chase_pct":self.cfg.kr_no_chase_pct,
                "lag_gap_pct":self.cfg.lag_gap_pct, "thresholds":THRESHOLDS,
            })
        with self.ledger.lock, self.ledger.connect() as c:
            if kind == "events":
                rows = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY trigger_at DESC LIMIT ?", (limit,)).fetchall()]
                for r in rows:
                    r["payload"] = json.loads(r["payload"] or "{}")
                return 200, {"status":"READY","rows":rows,"version":VERSION}
            if kind == "board":
                rows = [dict(r) for r in c.execute("SELECT t.*,e.theme,e.trigger_at,e.trigger_pct,e.mean_pct,e.confirm_count,e.mapping_mode,e.status event_status FROM tracks t JOIN events e USING(event_id) ORDER BY e.trigger_at DESC,t.max_ret DESC LIMIT ?", (limit,)).fetchall()]
                for r in rows:
                    r["hit_json"] = json.loads(r["hit_json"] or "{}")
                    r["payload"] = json.loads(r["payload"] or "{}")
                return 200, {"status":"READY","rows":rows,"version":VERSION}
            if kind == "summary":
                states = {r[0]:r[1] for r in c.execute("SELECT state,COUNT(*) FROM tracks GROUP BY state").fetchall()}
                n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                return 200, {"status":"READY","events":n,"states":states,"version":VERSION}
        return 404, {"error":"not found"}


def install(engine_module):
    """Attach the sidecar without changing native lanes or broker collectors."""
    e = engine_module
    original_start = e.Operations.start
    original_api = e.Operations.api

    def ensure(self):
        side = getattr(self, "_us_pre_nxt_sync", None)
        if side is None:
            side = SyncSidecar(self)
            self._us_pre_nxt_sync = side
        return side

    def start(self):
        result = original_start(self)
        try:
            ensure(self).start()
        except Exception as exc:
            self._us_pre_nxt_sync_error = type(exc).__name__
        return result

    def api(self, raw_path):
        path = urlparse(raw_path).path.rstrip("/")
        if path.startswith("/api/operations/us-pre-nxt-sync"):
            try:
                return ensure(self).api(raw_path)
            except Exception as exc:
                return 503, {"status":"PENDING","version":VERSION,"error":type(exc).__name__}
        status, payload = original_api(self, raw_path)
        if status == 200 and isinstance(payload, dict) and path.endswith("/status"):
            payload = dict(payload)
            side = getattr(self, "_us_pre_nxt_sync", None)
            payload["us_pre_nxt_sync"] = dict(side.last) if side else {"status":"PENDING","version":VERSION,"reason":"NOT_STARTED"}
        return status, payload

    e.Operations.start = start
    e.Operations.api = api
    e.US_PRE_NXT_SYNC_VERSION = VERSION
    return {"ok":True,"version":VERSION,"direct_anchors":len(DIRECT_MAP),"themes":len(THEME_KR)}


__all__ = ["install", "SyncSidecar", "USPremarketSource", "theme_for", "VERSION"]
