# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
VERSION = "SMART_REACCEL_MTF_BACKTEST_V2_1_UNBIASED"
RESULT_ROOT = Path("/data/results/smart_reaccel_mtf_v2_1")
DAILY_URL = "https://aikstockdata.com/data/public/daily/quotes_{date}.csv"

TARGET_DAYS = int(os.getenv("SMART_REACCEL_BT_DAYS", os.getenv("SMART_REACCEL_V21_DAYS", "10")))
MIN_CAP_100M = float(os.getenv("PULSE_MIN_MARKET_CAP_100M", "700"))
LS_T8412_INTERVAL = max(1.08, float(os.getenv("SMART_REACCEL_BT_LS_INTERVAL", os.getenv("SMART_REACCEL_V21_LS_INTERVAL", "1.12"))))
COST_BPS_TOTAL = max(0.0, float(os.getenv("SMART_REACCEL_V21_COST_BPS", "16")))
SIDE_SLIP = (COST_BPS_TOTAL / 2.0) / 10000.0
EXCLUDE = ("ETF", "ETN", "스팩", "SPAC", "리츠", "REIT", "인버스", "레버리지")


def _num(v, default=None):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip()) if v not in (None, "") else default
    except Exception:
        return default


def _ordinary(name):
    s = str(name or "").strip()
    u = s.upper().replace(" ", "")
    if not s or any(t.upper() in u for t in EXCLUDE):
        return False
    if re.search(r"(우|우B|우C|우선주)$", s):
        return False
    return True


def _safe(x):
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return None if not np.isfinite(x) else float(x)
    if isinstance(x, (pd.Timestamp, datetime, date)):
        return str(x)
    if isinstance(x, dict):
        return {str(k): _safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_safe(v) for v in x]
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    return x


class LS:
    """Read-only LS client.

    t1102 current quote is used only to resolve the user's 700억 hard market-cap gate.
    t8412 supplies historical KRX one-minute bars used by the backtest.
    """

    def __init__(self):
        self.key = os.getenv("LS_APP_KEY", "").strip()
        self.secret = os.getenv("LS_APP_SECRET", "").strip()
        self.base = os.getenv("LS_BASE_URL", "https://openapi.ls-sec.co.kr:8080").rstrip("/")
        self.s = requests.Session()
        self.token = None
        self.exp = 0.0
        self.last_by_tr: dict[str, float] = {}
        self.lock = threading.RLock()

    @property
    def configured(self):
        return bool(self.key and self.secret)

    def _interval(self, tr):
        # t1102 is allowed at a much higher rate than chart TRs.
        if tr == "t1102":
            return 0.13
        if tr == "t8412":
            return LS_T8412_INTERVAL
        return 1.08

    def wait(self, tr):
        with self.lock:
            interval = self._interval(tr)
            last = self.last_by_tr.get(tr, 0.0)
            gap = time.monotonic() - last
            if gap < interval:
                time.sleep(interval - gap)
            self.last_by_tr[tr] = time.monotonic()

    def auth(self):
        if self.token and time.monotonic() < self.exp - 60:
            return
        if not self.configured:
            raise RuntimeError("LS_NOT_CONFIGURED")
        last = ""
        for u in [self.base + "/oauth2/token", self.base + "/oauth2/tokenP"]:
            try:
                self.wait("oauth")
                r = self.s.post(
                    u,
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type": "client_credentials",
                        "appkey": self.key,
                        "appsecretkey": self.secret,
                        "scope": "oob",
                    },
                    timeout=20,
                )
                if r.status_code != 200:
                    last = f"HTTP{r.status_code}:{r.text[:120]}"
                    continue
                j = r.json()
                self.token = j.get("access_token")
                if self.token:
                    self.exp = time.monotonic() + max(600, int(j.get("expires_in") or 86400))
                    return
            except Exception as e:
                last = str(e)
        raise RuntimeError("LS_TOKEN:" + last)

    def post(self, path, tr, body, cont="N", key=""):
        self.auth()
        last = ""
        for attempt in range(6):
            self.wait(tr)
            h = {
                "content-type": "application/json; charset=UTF-8",
                "authorization": f"Bearer {self.token}",
                "tr_cd": tr,
                "tr_cont": cont,
                "tr_cont_key": key,
            }
            mac = os.getenv("LS_MAC_ADDRESS", "").strip()
            if mac:
                h["mac_address"] = mac
            try:
                r = self.s.post(self.base + path, headers=h, json=body, timeout=25)
                txt = r.text[:300]
                if r.status_code == 200:
                    j = r.json()
                    cd = str(j.get("rsp_cd") or "")
                    if cd in ("", "00000", "0000", "0") or (j.get(f"{tr}OutBlock1") or []):
                        return j, {k.lower(): v for k, v in r.headers.items()}
                    last = f"{cd}:{j.get('rsp_msg')}"
                    if cd != "IGW00201":
                        raise RuntimeError(f"LS {tr} {last}")
                else:
                    last = f"HTTP{r.status_code}:{txt}"
                if ("IGW00201" in txt or "호출 거래건수를 초과" in txt or r.status_code >= 500) and attempt < 5:
                    time.sleep(min(12.0, 1.8 * (2 ** attempt)))
                    continue
                raise RuntimeError(f"LS {tr} {last}")
            except requests.RequestException as e:
                last = f"{type(e).__name__}:{e}"
                if attempt < 5:
                    time.sleep(min(12.0, 1.8 * (2 ** attempt)))
                    continue
                raise
        raise RuntimeError(f"LS_{tr}_RETRY_EXHAUSTED:{last}")

    def market_cap_100m(self, code):
        body = {"t1102InBlock": {"shcode": str(code).zfill(6), "exchgubun": "K"}}
        j, _ = self.post("/stock/market-data", "t1102", body)
        z = j.get("t1102OutBlock") or {}
        cap = _num(z.get("total"))
        if cap is None or cap <= 0:
            raise RuntimeError(f"LS_T1102_CAP_EMPTY:{code}")
        return float(cap)

    def minute1_day(self, code, ds):
        cache_dir = RESULT_ROOT / "bar_cache_1m"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cp = cache_dir / f"{ds}_{str(code).zfill(6)}.csv.gz"
        if cp.exists():
            try:
                x = pd.read_csv(cp)
                x["datetime"] = pd.to_datetime(x["datetime"], utc=True).dt.tz_convert("Asia/Seoul")
                return x.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
            except Exception:
                try:
                    cp.unlink()
                except Exception:
                    pass

        body = {
            "t8412InBlock": {
                "shcode": str(code).zfill(6),
                "ncnt": 1,
                "qrycnt": 500,
                "nday": "1",
                "sdate": ds.replace("-", ""),
                "stime": "",
                "edate": ds.replace("-", ""),
                "etime": "",
                "cts_date": "",
                "cts_time": "",
                "comp_yn": "N",
            }
        }
        j, _ = self.post("/stock/chart", "t8412", body)
        out = []
        for z in j.get("t8412OutBlock1") or []:
            try:
                d = str(z.get("date") or "")
                t = "".join(c for c in str(z.get("time") or "") if c.isdigit()).zfill(6)[-6:]
                dt = pd.Timestamp(
                    f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}",
                    tz="Asia/Seoul",
                )
                if dt.strftime("%H%M") < "0900" or dt.strftime("%H%M") > "1530":
                    continue
                op, hi, lo, cl = [float(z.get(k) or 0) for k in ("open", "high", "low", "close")]
                vol = float(z.get("jdiff_vol") or z.get("volume") or 0)
                if min(op, hi, lo, cl) <= 0:
                    continue
                out.append(
                    {
                        "datetime": dt,
                        "open": op,
                        "high": hi,
                        "low": lo,
                        "close": cl,
                        "volume": max(0.0, vol),
                    }
                )
            except Exception:
                continue
        if not out:
            return pd.DataFrame()
        df = pd.DataFrame(out).sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
        try:
            df.to_csv(cp, index=False, compression="gzip")
        except Exception:
            pass
        return df


def load_cap_cache():
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    p = RESULT_ROOT / "cap_cache_t1102.json"
    if not p.exists():
        return {}, p
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        return {str(k).zfill(6): float(v) for k, v in j.items() if _num(v) is not None}, p
    except Exception:
        return {}, p


def daily_rows(ds):
    r = requests.get(
        DAILY_URL.format(date=ds.strftime("%Y%m%d")),
        timeout=20,
        headers={"User-Agent": "smart-reaccel-bt/2.1"},
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig", errors="replace"))))


def build_events(ls):
    """Complete 10-trading-day +5% event universe.

    No outcome-prioritized truncation is allowed. Market-cap unknown is not treated as PASS.
    """
    raw = []
    audits = []
    d = datetime.now(KST).date()
    got = 0
    scanned = 0
    while got < TARGET_DAYS and scanned < 65:
        scanned += 1
        if d.weekday() < 5:
            try:
                rows = daily_rows(d)
                if rows:
                    got += 1
                    n = 0
                    for r in rows:
                        code = str(r.get("종목코드") or r.get("srtnCd") or "").strip().zfill(6)
                        name = str(r.get("종목명") or r.get("itmsNm") or "").strip()
                        market = str(r.get("mrktCtg") or "").strip().upper()
                        if not re.fullmatch(r"\d{6}", code) or market not in {"KOSPI", "KOSDAQ"} or not _ordinary(name):
                            continue
                        close = _num(r.get("clpr"))
                        vs = _num(r.get("vs"))
                        high = _num(r.get("hipr"))
                        if close is None or vs is None or high is None:
                            continue
                        prev = close - vs
                        if prev <= 0:
                            continue
                        peak = (high / prev - 1) * 100
                        if peak >= 5.0:
                            raw.append(
                                {
                                    "date": d.isoformat(),
                                    "code": code,
                                    "name": name,
                                    "market": market,
                                    "prev_close": prev,
                                    "day_peak_pct": peak,
                                }
                            )
                            n += 1
                    audits.append({"date": d.isoformat(), "rows": len(rows), "raw_5pct_events": n})
            except Exception as e:
                audits.append({"date": d.isoformat(), "error": f"{type(e).__name__}:{e}"})
        d -= timedelta(days=1)

    # Deduplicate exact stock-day events.
    raw = list({(e["date"], e["code"]): e for e in raw}.values())
    raw = sorted(raw, key=lambda x: (x["date"], x["code"]))

    cap_cache, cap_path = load_cap_cache()
    codes = sorted({e["code"] for e in raw})
    cap_errors = {}
    for i, code in enumerate(codes, 1):
        if code in cap_cache and cap_cache[code] > 0:
            continue
        try:
            cap_cache[code] = ls.market_cap_100m(code)
        except Exception as exc:
            cap_errors[code] = f"{type(exc).__name__}:{exc}"
        if i == 1 or i % 50 == 0:
            print(f"SMART_REACCEL_CAP_PROGRESS {i}/{len(codes)} known={len(cap_cache)} errors={len(cap_errors)}", flush=True)

    try:
        cap_path.write_text(json.dumps(cap_cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    eligible = []
    below = 0
    missing = 0
    for e in raw:
        cap = cap_cache.get(e["code"])
        if cap is None or cap <= 0:
            missing += 1
            continue
        if cap < MIN_CAP_100M:
            below += 1
            continue
        eligible.append({**e, "cap_100m": cap, "cap_source": "LS_T1102_CURRENT"})

    cap_audit = {
        "raw_5pct_events": len(raw),
        "unique_codes": len(codes),
        "cap_resolved_codes": len([c for c in codes if cap_cache.get(c, 0) > 0]),
        "cap_error_codes": len(cap_errors),
        "cap_missing_events": missing,
        "cap_below_700_events": below,
        "eligible_events": len(eligible),
        "cap_coverage_pct": round(
            100.0 * len([c for c in codes if cap_cache.get(c, 0) > 0]) / max(1, len(codes)), 2
        ),
        "cap_rule": f"current LS t1102 market cap >= {MIN_CAP_100M:.0f}억원; unknown is excluded, never PASS",
        "cap_errors_sample": dict(list(cap_errors.items())[:50]),
    }
    return eligible, audits, cap_audit


def resample(df, mins):
    if df.empty:
        return df
    x = df.set_index("datetime").sort_index()
    y = (
        x.resample(
            f"{mins}min",
            origin="start_day",
            offset="0min",
            label="right",
            closed="right",
        )
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    return y[
        (y.index.strftime("%H%M") >= "0900") & (y.index.strftime("%H%M") <= "1530")
    ].reset_index()


def features(df, prev):
    x = df.copy()
    c = x.close
    v = x.volume
    x["ma5"] = c.rolling(5).mean()
    x["ma20"] = c.rolling(20).mean()
    x["s5"] = x.ma5.diff()
    x["s20"] = x.ma20.diff()
    x["gap"] = (x.ma20 - x.ma5).abs() / x.ma20
    tp = (x.high + x.low + x.close) / 3
    x["vwap"] = (tp * v).cumsum() / v.replace(0, np.nan).cumsum()
    x["gain"] = (x.close / prev - 1) * 100
    x["max_gain"] = (x.high.cummax() / prev - 1) * 100
    x["dhi"] = x.high.cummax()
    x["dlo"] = x.low.cummin()
    rng = (x.dhi - x.dlo).replace(0, np.nan)
    x["pres"] = (x.close - x.dlo) / rng
    x["vol_med5"] = v.rolling(5).median()
    x["vol_mean3"] = v.rolling(3).mean()
    x["vol_prev3"] = v.shift(3).rolling(3).mean()
    x["ma_turn"] = (x.s5 > 0) & (
        (x.s5.shift(1) <= 0) | (x.s5.shift(2) <= 0) | (x.s5.shift(3) <= 0)
    )
    x["gap_comp"] = (x.gap < x.gap.shift(1)) & (x.gap.shift(1) <= x.gap.shift(2))
    x["golden"] = x.ma5 > x.ma20
    x["vol_reaccel"] = (v > x.vol_med5 * 1.15) & (x.close > x.close.shift(1))
    x["sell_exhaust"] = (x.vol_mean3 <= x.vol_prev3 * 0.92) | (
        (x.close > x.open) & (v > v.shift(1) * 1.05)
    )
    x["vwap_ok"] = (x.close >= x.vwap * 0.997) | (
        (x.close.shift(1) < x.vwap.shift(1)) & (x.close >= x.vwap)
    )
    x["early"] = (x.gain >= 5) & (x.gain <= 12) & (x.max_gain <= 18)

    # Two allowed pullback paths, both requiring a real pullback/recompression.
    x["normal_pullback"] = (x.pres >= 0.50) & (x.pres <= 0.70)
    x["shallow_continue"] = (x.pres >= 0.75) & (x.pres <= 0.95)
    x["hold"] = (x.normal_pullback | x.shallow_continue) & ((x.dhi / x.close - 1) >= 0.005)
    return x


def map5(v5, ts):
    if v5.empty:
        return None
    z = v5[v5.datetime <= ts]
    return None if z.empty else z.iloc[-1]


def signal_index(df, variant, v5=None):
    for i, r in df.iterrows():
        if i < 20:
            continue
        base = bool(r.early and r.hold and r.sell_exhaust and r.vol_reaccel and r.vwap_ok)
        if not base:
            continue
        if variant in ("B", "C", "D", "E") and not bool(r.ma_turn):
            continue
        if variant in ("C", "E") and not bool(r.gap_comp or r.golden):
            continue
        if variant == "D" and not bool(r.golden):
            continue
        if variant == "E" and v5 is not None:
            q = map5(v5, r.datetime)
            if q is not None and pd.notna(q.ma20):
                if bool(q.s5 < 0 and q.s20 < 0 and q.close < q.vwap):
                    continue
        return i
    return None


def first_cross_index(df, prev_close, threshold_pct):
    a = np.flatnonzero(
        (((df["high"] / prev_close) - 1.0) * 100.0 >= threshold_pct).fillna(False).to_numpy()
    )
    return int(a[0]) if len(a) else None


def outcome(df, i, tp):
    if i is None or i + 1 >= len(df):
        return None

    raw_entry = float(df.iloc[i + 1].open)
    ent = raw_entry * (1.0 + SIDE_SLIP)
    sig_t = str(df.iloc[i].datetime)
    ent_t = str(df.iloc[i + 1].datetime)

    stop_trigger = ent * 0.94
    target_trigger = ent * (1.0 + tp)

    mfe = -999.0
    mae = 999.0
    exit_px = float(df.iloc[-1].close) * (1.0 - SIDE_SLIP)
    exit_t = str(df.iloc[-1].datetime)
    result = "EOD"

    for j in range(i + 1, len(df)):
        r = df.iloc[j]
        mfe = max(mfe, (float(r.high) / ent - 1) * 100)
        mae = min(mae, (float(r.low) / ent - 1) * 100)
        hit_s = float(r.low) <= stop_trigger
        hit_t = float(r.high) >= target_trigger

        # Same-bar target/stop ambiguity is treated conservatively as stop first.
        if hit_s and hit_t:
            result = "SL_AMBIG"
            exit_px = stop_trigger * (1.0 - SIDE_SLIP)
            exit_t = str(r.datetime)
            break
        if hit_s:
            result = "SL"
            exit_px = stop_trigger * (1.0 - SIDE_SLIP)
            exit_t = str(r.datetime)
            break
        if hit_t:
            result = "TP"
            exit_px = target_trigger * (1.0 - SIDE_SLIP)
            exit_t = str(r.datetime)
            break

        # Structure exit: two bars of MA5 deterioration + price below VWAP.
        if j >= i + 3:
            r0 = df.iloc[j - 1]
            if r.s5 < 0 and r0.s5 < 0 and r.close < r.vwap:
                result = "STRUCT_EXIT"
                exit_px = float(r.close) * (1.0 - SIDE_SLIP)
                exit_t = str(r.datetime)
                break

    ret = (exit_px / ent - 1) * 100
    return {
        "signal_time": sig_t,
        "entry_time": ent_t,
        "raw_entry": raw_entry,
        "entry": ent,
        "exit_time": exit_t,
        "exit": exit_px,
        "result": result,
        "return_pct": ret,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "cost_bps_total": COST_BPS_TOTAL,
    }


def summarize_trades(rows):
    if not rows:
        return {"n": 0}
    n = len(rows)
    wins = sum(r["result"] == "TP" for r in rows)
    positive = sum(r["return_pct"] > 0 for r in rows)
    sl = sum(r["result"] in ("SL", "SL_AMBIG") for r in rows)
    rets = np.array([r["return_pct"] for r in rows], float)
    pos = rets[rets > 0].sum()
    neg = -rets[rets < 0].sum()
    return {
        "n": n,
        "tp_rate_pct": round(wins / n * 100, 2),
        "positive_trade_rate_pct": round(positive / n * 100, 2),
        "stop_rate_pct": round(sl / n * 100, 2),
        "avg_return_pct": round(float(rets.mean()), 3),
        "median_return_pct": round(float(np.median(rets)), 3),
        "profit_factor": round(float(pos / neg), 3) if neg > 0 else None,
        "avg_mfe_pct": round(float(np.mean([r["mfe_pct"] for r in rows])), 3),
        "avg_mae_pct": round(float(np.mean([r["mae_pct"] for r in rows])), 3),
        "expectancy_R": round(float(rets.mean() / 6.0), 3),
    }


def summarize_opportunities(rows):
    if not rows:
        return {"event_n": 0}
    n = len(rows)
    sig = sum(bool(r["signal"]) for r in rows)
    doomed = [r for r in rows if r["day_peak_pct"] < 12.0]
    out = {
        "event_n": n,
        "signal_n": sig,
        "signal_rate_pct": round(sig / n * 100, 2),
        "doomed_n_peak_lt12": len(doomed),
        "doomed_filter_rate_pct": (
            round(100.0 * sum(not r["signal"] for r in doomed) / len(doomed), 2) if doomed else None
        ),
    }
    for th in (18, 20, 22, 25, 29.5):
        winners = [r for r in rows if r["day_peak_pct"] >= th]
        kept = [r for r in winners if r.get(f"signal_before_{str(th).replace('.', '_')}")]
        out[f"winner_{str(th).replace('.', '_')}_n"] = len(winners)
        out[f"recall_before_{str(th).replace('.', '_')}_pct"] = (
            round(100.0 * len(kept) / len(winners), 2) if winners else None
        )
    return out


class Runner:
    def __init__(self):
        self.ls = LS()

    def run(self):
        t0 = datetime.now(KST)
        print("SMART_REACCEL_V21_START " + t0.isoformat(), flush=True)

        events, daily_audit, cap_audit = build_events(self.ls)
        print(
            "SMART_REACCEL_V21_UNIVERSE "
            + json.dumps(
                {
                    "days": TARGET_DAYS,
                    "eligible_events": len(events),
                    "cap_audit": cap_audit,
                    "ls": self.ls.configured,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not events:
            raise RuntimeError("EVENT_UNIVERSE_EMPTY_AFTER_CAP_GATE")
        if cap_audit["cap_coverage_pct"] < 90.0:
            raise RuntimeError(
                f"CAP_COVERAGE_TOO_LOW:{cap_audit['cap_coverage_pct']}% -- do not calculate win rate"
            )

        variants = ["A", "B", "C", "D", "E"]
        frames = [2, 3, 5]
        result_rows = []
        opp_rows = []
        errors = []
        bar_event_keys = set()

        for k, e in enumerate(events, 1):
            if k == 1 or k % 20 == 0:
                print(
                    f'SMART_REACCEL_V21_PROGRESS {k}/{len(events)} {e["date"]} {e["name"]}({e["code"]})',
                    flush=True,
                )
            try:
                one = self.ls.minute1_day(e["code"], e["date"])
            except Exception as ex:
                errors.append({**e, "error": f"{type(ex).__name__}:{ex}"})
                continue
            if one.empty:
                errors.append({**e, "error": "EMPTY_1M"})
                continue

            bar_event_keys.add((e["date"], e["code"]))
            v5 = features(resample(one, 5), e["prev_close"])

            for m in frames:
                f = features(resample(one, m), e["prev_close"])
                for v in variants:
                    idx = signal_index(f, v, v5)
                    opp = {
                        **e,
                        "frame_min": m,
                        "variant": v,
                        "signal": idx is not None,
                        "signal_time": None if idx is None else str(f.iloc[idx].datetime),
                    }
                    for th in (18, 20, 22, 25, 29.5):
                        ci = first_cross_index(f, e["prev_close"], th)
                        key = f"signal_before_{str(th).replace('.', '_')}"
                        # Conservative: signal must occur on a strictly earlier completed bar.
                        opp[key] = bool(idx is not None and ci is not None and idx < ci)
                    opp_rows.append(opp)

                    if idx is None:
                        continue
                    for tp in (0.12, 0.15):
                        o = outcome(f, idx, tp)
                        if o:
                            result_rows.append(
                                {
                                    **e,
                                    "frame_min": m,
                                    "variant": v,
                                    "tp": int(tp * 100),
                                    **o,
                                }
                            )

        summaries = []
        for m in frames:
            for v in variants:
                oo = [r for r in opp_rows if r["frame_min"] == m and r["variant"] == v]
                om = summarize_opportunities(oo)
                for tp in (12, 15):
                    rr = [
                        r
                        for r in result_rows
                        if r["frame_min"] == m and r["variant"] == v and r["tp"] == tp
                    ]
                    summaries.append(
                        {
                            "frame_min": m,
                            "variant": v,
                            "tp": tp,
                            **summarize_trades(rr),
                            **om,
                        }
                    )

        eligible = [
            s
            for s in summaries
            if s.get("n", 0) >= 20
            and s.get("expectancy_R") is not None
            and s.get("profit_factor") is not None
        ]
        # Model selection is lexicographic, not a live trading score:
        # expectancy -> PF -> doomed-stock removal -> +20/+25 recall -> lower stop rate.
        eligible.sort(
            key=lambda s: (
                s.get("expectancy_R", -999),
                s.get("profit_factor", -1),
                s.get("doomed_filter_rate_pct") if s.get("doomed_filter_rate_pct") is not None else -1,
                s.get("recall_before_20_pct") if s.get("recall_before_20_pct") is not None else -1,
                s.get("recall_before_25_pct") if s.get("recall_before_25_pct") is not None else -1,
                -(s.get("stop_rate_pct") or 100),
            ),
            reverse=True,
        )

        report = {
            "version": VERSION,
            "started_at": t0.isoformat(),
            "finished_at": datetime.now(KST).isoformat(),
            "target_days": TARGET_DAYS,
            "event_count": len(events),
            "bar_event_count": len(bar_event_keys),
            "error_events": len(errors),
            "cost_bps_total": COST_BPS_TOTAL,
            "cap_audit": cap_audit,
            "variant_def": {
                "A": "price hold + supply exhaustion + volume reaccel + VWAP",
                "B": "A + 3/2/5m MA5 turn",
                "C": "B + MA5/20 gap compression OR already golden",
                "D": "B + MA5>MA20 golden cross required",
                "E": "C + 5m hard downtrend veto",
            },
            "universe_policy": "ALL +5% stock-day events in the 10 trading days after strict current-cap >=700억 gate; no outcome-prioritized truncation",
            "execution_policy": "next-bar open, 16bp round-trip execution friction, same-bar TP/SL ambiguity counted as SL",
            "summaries": summaries,
            "best": eligible[:10],
            "daily_audit": daily_audit,
            "errors": errors[:200],
        }

        RESULT_ROOT.mkdir(parents=True, exist_ok=True)
        rid = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
        out = RESULT_ROOT / rid
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result_rows).to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(opp_rows).to_csv(out / "opportunities.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(summaries).to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
        (out / "report.json").write_text(
            json.dumps(_safe(report), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        marker = RESULT_ROOT / f"AUTORUN_{VERSION}_{TARGET_DAYS}D_DONE.json"
        marker.write_text(
            json.dumps({"output_dir": str(out), "report": _safe(report)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            "SMART_REACCEL_V21_READY "
            + json.dumps(
                _safe(
                    {
                        "output_dir": str(out),
                        "event_count": len(events),
                        "bar_event_count": len(bar_event_keys),
                        "error_events": len(errors),
                        "cap_audit": cap_audit,
                        "best": eligible[:10],
                        "summaries": summaries,
                    }
                ),
                ensure_ascii=False,
            ),
            flush=True,
        )


def _autorun():
    if str(os.getenv("SMART_REACCEL_BT_AUTORUN", os.getenv("SMART_REACCEL_V21_AUTORUN", "true"))).lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    marker = RESULT_ROOT / f"AUTORUN_{VERSION}_{TARGET_DAYS}D_DONE.json"
    if marker.exists():
        try:
            j = json.loads(marker.read_text(encoding="utf-8"))
            print("SMART_REACCEL_V21_AUTORUN_SKIP " + json.dumps(_safe(j), ensure_ascii=False), flush=True)
        except Exception:
            print("SMART_REACCEL_V21_AUTORUN_SKIP marker_exists", flush=True)
        return

    def job():
        try:
            time.sleep(20)
            Runner().run()
        except Exception as e:
            print(
                "SMART_REACCEL_V21_ERROR "
                + f"{type(e).__name__}:{e}\n{traceback.format_exc()[-5000:]}",
                flush=True,
            )

    threading.Thread(target=job, daemon=True, name="smart-reaccel-v21-bt").start()


_autorun()
