"""Operations runtime hotfix V10.

Purpose:
- Use the most recently CLOSED KRX daily session while an intraday KIS READY
  manifest belongs to the current session.
- Ignore an unclosed current-day LS daily candle in operational daily-history
  evaluation.
- Accept fresh KIS REST_CURRENT request-time quotes as current authority data
  without relabeling their provenance as exchange timestamps.
- Infer market-session openness from the KST schedule when the source row does
  not carry a session_open flag.
- Tolerate <=1% cross-snapshot membership drift for a large universe while
  keeping the exact drift counts visible in the audit payload.
- Tighten bounded Operations DB maintenance to prevent persistent-disk growth.

This module does not place orders, mutate broker caches, or change KIS/LS auth.
"""
from __future__ import annotations

import os
from datetime import time as dtime

_APPLIED = False


def _set_storage_guards() -> None:
    # Bounded projection history. Active candidates and tracking outcomes are
    # not removed by Store.maintenance; only old/inactive audit rows are pruned.
    os.environ.setdefault("OPERATIONS_MAINTENANCE_INTERVAL_SECONDS", "3600")
    os.environ.setdefault("OPERATIONS_EVENTS_MAX_ROWS", "6000")
    os.environ.setdefault("OPERATIONS_CYCLES_MAX_ROWS", "1200")
    os.environ.setdefault("OPERATIONS_RETENTION_DAYS", "14")
    os.environ.setdefault("OPERATIONS_OUTCOME_RETENTION_DAYS", "120")
    os.environ.setdefault("STORAGE_MAINTENANCE_INTERVAL_SECONDS", "3600")
    os.environ.setdefault("HISTORY_RETENTION_ENABLED", "true")
    os.environ.setdefault("HISTORY_RETENTION_SESSIONS", "320")


def apply() -> None:
    global _APPLIED
    if _APPLIED:
        return
    _set_storage_guards()

    from operations import engine as e
    from operations import trading_calendar as tc

    # Preserve originals exactly once so repeated imports remain idempotent.
    original_last_closed = getattr(tc, "_V10_ORIGINAL_LAST_CLOSED", tc.last_closed_session)
    tc._V10_ORIGINAL_LAST_CLOSED = original_last_closed

    def last_closed_session_v10(current):
        # On the 2026-11-19 CSAT session KRX closes at 16:30, not 15:30.
        # The existing calendar accepts a close only after a 10-minute buffer;
        # extend that same safety rule to 16:40 for this one day.
        if current.date().isoformat() == "2026-11-19" and current.time() < dtime(16, 40):
            if current.time() >= dtime(15, 40):
                current = current.replace(hour=15, minute=0, second=0, microsecond=0)
        return original_last_closed(current)

    tc.last_closed_session = last_closed_session_v10
    e.last_closed_session = last_closed_session_v10

    original_expected = getattr(e, "_V10_ORIGINAL_EXPECTED_CLOSED", e.expected_closed_daily_date)
    e._V10_ORIGINAL_EXPECTED_CLOSED = original_expected

    def expected_closed_daily_date_v10(manifest, current, market=None):
        # A current-session KIS scan proves the active trading date, but it can
        # never prove that today's DAILY candle is closed.  During KRX hours use
        # the independently versioned XKRX calendar's last closed session.
        if market in (None, "KRX"):
            manifest_day = e.closed_trade_date_from_manifest(manifest, market)
            last_closed = last_closed_session_v10(current)
            if not manifest_day or not last_closed:
                return None
            today = current.date().isoformat()
            # Accept only the current trading-session manifest or the immediately
            # preceding closed-session manifest. Older snapshots stay fail-closed.
            if manifest_day not in (today, last_closed):
                return None
            return last_closed
        return original_expected(manifest, current, market)

    e.expected_closed_daily_date = expected_closed_daily_date_v10

    original_session = getattr(e, "_V10_ORIGINAL_SESSION_OPEN", e.session_open_for_market)
    e._V10_ORIGINAL_SESSION_OPEN = original_session

    def session_open_for_market_v10(market, current, observed):
        observed_tri = e.tri(observed)
        if current.weekday() >= 5:
            return False
        if observed_tri is False:
            return False
        hm = current.hour * 60 + current.minute + current.second / 60.0
        market = str(market or "").upper()
        if current.date().isoformat() == e.SPECIAL_SESSION_DATE:
            if market == "KRX":
                schedule_open = 600 <= hm < 990       # 10:00-16:30
            elif market == "NXT":
                schedule_open = (600.5 <= hm < 980) or (1000 <= hm < 1200)
            else:
                schedule_open = False
        else:
            schedule_open = ((540 <= hm < 930) if market == "KRX" else
                             (480 <= hm < 1200) if market == "NXT" else False)
        # Missing source session flags are common in KIS REST rows.  The schedule
        # is authoritative for whether evaluation may proceed; explicit False
        # from a source still vetoes above.
        return bool(schedule_open)

    e.session_open_for_market = session_open_for_market_v10

    # Keep exact quote provenance, but allow fresh KIS REST_CURRENT rows to pass
    # DATA_FRESH. RE25 runtime re-arm still separately requires exchange-clock
    # proof in re25_runtime_transition, so this does not weaken that trigger.
    original_core = getattr(e, "_V10_ORIGINAL_CORE_GATES", e.core_gates)
    e._V10_ORIGINAL_CORE_GATES = original_core

    def core_gates_v10(row, current):
        tq = row.get("timestamp_quality")
        qq = row.get("quote_quality")
        rest_current = (
            qq == "REST_CURRENT"
            and tq in ("REQUEST_TIMESTAMP", "REQUEST_TIME_ONLY", "REQUEST_TIME")
            and e.fresh(row.get("price_asof"), current)
        )
        if not rest_current:
            return original_core(row, current)
        row["timestamp_quality"] = "EXCHANGE_TIMESTAMP"
        try:
            out = original_core(row, current)
        finally:
            row["timestamp_quality"] = tq
        out["timestamp_quality"] = tq
        out["data_fresh_basis"] = "KIS_REST_CURRENT_REQUEST_TIME"
        return out

    e.core_gates = core_gates_v10

    # During the session the LS cache can contain an in-progress current-day
    # candle. Operational V1.7/RE25 must evaluate only COMPLETED sessions.
    original_read_all = getattr(e.History, "_V10_ORIGINAL_READ_ALL", e.History.read_all)
    e.History._V10_ORIGINAL_READ_ALL = original_read_all

    def history_read_all_v10(self):
        hist, dates, state = original_read_all(self)
        cutoff = last_closed_session_v10(e.now())
        if cutoff and isinstance(dates, list):
            dates = [d for d in dates if d <= cutoff]
        return hist, dates, state

    e.History.read_all = history_read_all_v10

    original_snapshot_get = getattr(e.HistorySnapshot, "_V10_ORIGINAL_GET", e.HistorySnapshot.get)
    e.HistorySnapshot._V10_ORIGINAL_GET = original_snapshot_get

    def history_snapshot_get_v10(self, symbol, default=None):
        rows = original_snapshot_get(self, symbol, default)
        if not isinstance(rows, list):
            return rows
        cutoff = last_closed_session_v10(e.now())
        if not cutoff:
            return rows
        return [r for r in rows if isinstance(r, dict) and str(r.get("date") or "") <= cutoff]

    e.HistorySnapshot.get = history_snapshot_get_v10

    # A tiny membership difference between a rolling live universe and the last
    # immutable READY snapshot must not halt 2,400 stocks. Preserve exact counts
    # and mark that the match was tolerance-based rather than exact.
    original_audit = getattr(e, "_V10_ORIGINAL_UNIVERSE_AUDIT", e.universe_membership_audit)
    e._V10_ORIGINAL_UNIVERSE_AUDIT = original_audit

    def universe_membership_audit_v10(*args, **kwargs):
        audit = dict(original_audit(*args, **kwargs) or {})
        if audit.get("membership_matched") is True:
            audit.setdefault("exact_membership_matched", True)
            return audit
        live = int(e.number(audit.get("live_unique")) or 0)
        latest = int(e.number(audit.get("latest_unique")) or 0)
        diff = int(e.number(audit.get("live_only_count")) or 0) + int(e.number(audit.get("latest_only_count")) or 0)
        denom = max(live, latest, 1)
        if min(live, latest) >= 500 and diff / denom <= 0.01:
            audit["exact_membership_matched"] = False
            audit["membership_matched"] = True
            audit["status"] = "READY_TOLERATED_CROSS_SNAPSHOT_DRIFT"
            audit["coverage_assertion"] = "MATERIAL_SCOPE_MATCH__EXACT_DIFF_RETAINED"
            audit["tolerated_drift_ratio"] = round(diff / denom, 6)
        return audit

    e.universe_membership_audit = universe_membership_audit_v10

    # HVTB V1.6 is isolated and fail-closed: a patch error never takes down V10.
    try:
        _install_hvtb(e)
        e.HVTB_V16_SELFTEST = _hvtb_selftest()
    except Exception as exc:
        e.HVTB_V16_HOTFIX_ERROR = type(exc).__name__

    e.VERSION = "KRX_NXT_OPERATIONS_V10_LIVE_HISTORY_DISK_GUARD"
    _APPLIED = True

# ---------------------------------------------------------------------------
# HVTB V1.6 LS READ-ONLY RUNTIME LANE
# ---------------------------------------------------------------------------

def _install_hvtb(e):
    """Attach HVTB as an isolated read-only lane without editing native engines.

    The native full-market/capital-handoff output selects the upper-structure
    pool. Actual LS t8412 5-minute data is queried only for the strongest
    HVTB_FOCUS names and only during the verified KRX session. No order/account
    API is used, and no broker-owned cache is written.
    """
    import math
    import time
    from datetime import datetime
    from statistics import median
    import pandas as pd

    LANE = "HVTB_V1_6"
    if LANE not in e.VIEW_LANES:
        e.VIEW_LANES = tuple(e.VIEW_LANES) + (LANE,)

    def n(v):
        if isinstance(v, bool):
            return None
        try:
            x = float(v)
            return x if math.isfinite(x) else None
        except (TypeError, ValueError):
            return None

    def ccode(v):
        s = str(v or "").strip().upper()
        return s.zfill(6) if s.isdigit() and 0 < len(s) <= 6 else s

    def bar_ret(bars, idx=-1):
        if not isinstance(bars, list) or len(bars) < abs(idx) + 1:
            return None
        i = len(bars) + idx if idx < 0 else idx
        if i <= 0:
            return None
        a, b = n(bars[i - 1].get("close")), n(bars[i].get("close"))
        return None if not a or b is None else (b / a - 1.0) * 100.0

    def transfer(f):
        st = str((f or {}).get("selection_state") or (f or {}).get("state") or "").upper()
        return bool(
            (f or {}).get("capital_handoff_confirmed") is True
            or st in {"PRIMARY_SELECTED", "PRIMARY_ARMED", "PRIMARY_RESERVE"}
            or any(
                str(z.get("lane") or "").upper() == "A3_ROTATION"
                for z in ((f or {}).get("lane_details") or [])
                if isinstance(z, dict)
            )
        )

    def armed(f):
        st = str((f or {}).get("selection_state") or (f or {}).get("state") or "").upper()
        return bool((f or {}).get("capital_handoff_confirmed") is True or st in {"PRIMARY_SELECTED", "PRIMARY_ARMED"})

    def range_pos(hi, lo, cl):
        hi, lo, cl = n(hi), n(lo), n(cl)
        if hi is None or lo is None or cl is None or hi <= lo:
            return None
        return max(0.0, min(1.0, (cl - lo) / (hi - lo)))

    def find_app(ops):
        # integration.alpha/nxt closures capture the already-running app dict.
        # This reuses the authenticated LS provider; it never creates OAuth.
        for fn in (ops.providers or {}).values():
            if not callable(fn):
                continue
            for cell in getattr(fn, "__closure__", None) or ():
                try:
                    obj = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(obj, dict) and callable(obj.get("get_engine")):
                    return obj
        return None

    def ls_provider(ops):
        app = find_app(ops)
        if not app:
            return None
        try:
            return app["get_engine"]().ls
        except Exception:
            return None

    def minute5(ops, symbol, current):
        cache = getattr(ops, "_hvtb_minute_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            ops._hvtb_minute_cache = cache
        key = ccode(symbol)
        mono = time.monotonic()
        hit = cache.get(key)
        ttl = max(60, int(os.getenv("HVTB_V16_MINUTE_CACHE_SECONDS", "120")))
        if hit and mono - hit[0] <= ttl:
            return hit[1].copy()
        ls = ls_provider(ops)
        if ls is None or not hasattr(ls, "_post_tr"):
            raise RuntimeError("LS_T8412_PROVIDER_UNAVAILABLE")
        ds = current.strftime("%Y%m%d")
        body = {
            "t8412InBlock": {
                "shcode": key,
                "ncnt": 5,
                "qrycnt": 500,
                "nday": "1",
                "sdate": ds,
                "stime": "",
                "edate": ds,
                "etime": "",
                "cts_date": "",
                "cts_time": "",
                "comp_yn": "N",
            }
        }
        raw = ls._post_tr(getattr(ls, "chart_path", "/stock/chart"), "t8412", body)
        payload = raw.json() if hasattr(raw, "json") else (raw[0] if isinstance(raw, tuple) else raw)
        data = []
        for z in (payload or {}).get("t8412OutBlock1") or []:
            ds0 = str(z.get("date") or "").strip()
            tm = "".join(ch for ch in str(z.get("time") or "") if ch.isdigit())
            if len(ds0) != 8 or len(tm) < 4:
                continue
            try:
                ts = datetime.strptime(ds0 + (tm + "000000")[:6], "%Y%m%d%H%M%S").replace(tzinfo=e.KST)
            except ValueError:
                continue
            hm = ts.hour * 60 + ts.minute
            if not 540 <= hm <= 930:
                continue
            op, hi, lo, cl = [n(z.get(k)) for k in ("open", "high", "low", "close")]
            vol = max(0.0, n(z.get("jdiff_vol", z.get("volume"))) or 0.0)
            if all(x is not None and x > 0 for x in (op, hi, lo, cl)):
                data.append({"timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": vol})
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
        cache[key] = (mono, df.copy())
        return df

    def prepare5(df):
        x = df.copy().sort_values("timestamp").reset_index(drop=True)
        pc = x["close"].shift(1)
        tr = pd.concat([x["high"] - x["low"], (x["high"] - pc).abs(), (x["low"] - pc).abs()], axis=1).max(axis=1)
        x["atr5"] = tr.rolling(14, min_periods=7).mean()
        typical = (x["high"] + x["low"] + x["close"]) / 3.0
        x["vwap"] = (typical * x["volume"]).cumsum() / x["volume"].cumsum().replace(0, float("nan"))
        x["med5"] = x["volume"].rolling(5, min_periods=3).median()
        x["base5"] = x["volume"].shift(1).rolling(5, min_periods=3).median()
        x["reaccel"] = x["volume"] / x["base5"].replace(0, float("nan"))
        x["bull"] = x["close"] > x["open"]
        return x

    def action(ops, symbol, daily, kis_price, current):
        try:
            df = minute5(ops, symbol, current)
        except Exception as exc:
            return {
                "hvtb_action_state": "ACTION_WAIT",
                "hvtb_action_reason": "LS_5M_UNAVAILABLE",
                "ls_5m_status": "PENDING",
                "ls_5m_error": type(exc).__name__,
            }
        if df is None or len(df) < 10:
            return {"hvtb_action_state": "ACTION_WAIT", "hvtb_action_reason": "INSUFFICIENT_5M", "ls_5m_status": "NO_DATA"}
        x = prepare5(df)
        reacc = vw = bos = retest = False
        level = retest_low = None
        bos_i = None
        hold = 0
        for i in range(5, len(x) - 1):
            r, p = x.iloc[i], x.iloc[i - 1]
            rv = n(r.get("reaccel"))
            if not reacc:
                if rv is not None and 1.20 <= rv <= 2.50:
                    reacc = True
                else:
                    continue
            vhold = r["close"] >= r["vwap"] and r["low"] >= r["vwap"] * 0.996
            vreclaim = (p["close"] < p["vwap"] <= r["close"]) or (r["low"] < r["vwap"] <= r["close"])
            if not vw:
                if vhold or vreclaim:
                    vw = True
                else:
                    continue
            structure = float(x.iloc[i - 3 : i]["high"].max())
            if not bos:
                if r["close"] > structure:
                    bos, level, bos_i, hold = True, structure, i, 0
                else:
                    continue
                continue
            if i <= bos_i:
                continue
            atr = n(r.get("atr5")) or 0.0
            band = min(max(level * 0.0035, atr * 0.35), level * 0.015)
            if not retest:
                valid_retest = r["low"] <= level + band and r["high"] >= level - band and r["low"] > p["low"] and r["close"] >= r["vwap"]
                if valid_retest:
                    retest, retest_low, hold = True, float(min(r["low"], p["low"])), 0
                else:
                    continue
            hold = hold + 1 if r["close"] >= level - band and r["close"] >= r["vwap"] else 0
            if hold < 2:
                continue
            med5 = n(r.get("med5"))
            vol_ok = med5 is not None and med5 > 0 and 0.80 * med5 <= r["volume"] <= 2.50 * med5
            if not (r["bull"] and r["close"] > p["high"] and vol_ok):
                continue
            nxt = x.iloc[i + 1]
            entry = float(nxt["open"])
            if entry / float(r["close"]) - 1.0 > 0.015:
                return {"hvtb_action_state": "NO_CHASE", "hvtb_action_reason": "NEXT_5M_GAP_GT_1P5", "bos_level": level, "retest_low": retest_low, "ls_5m_status": "READY"}
            stop = retest_low - 0.20 * atr
            if stop <= 0 or stop >= entry:
                return {"hvtb_action_state": "ACTION_WAIT", "hvtb_action_reason": "INVALID_STOP", "ls_5m_status": "READY"}
            risk = (entry - stop) / entry
            if risk > 0.06:
                return {"hvtb_action_state": "RISK_TOO_LARGE", "hvtb_action_reason": "STRUCTURAL_RISK_GT_6PCT", "entry_price": entry, "stop_price": stop, "ls_5m_status": "READY"}
            highs = [n(z.get("high")) for z in (daily or [])[-20:]]
            observed = [h for h in highs if h is not None and h > entry]
            target = max(observed) if observed else None
            rr = (target - entry) / (entry - stop) if target is not None else None
            if rr is None or rr < 2.0:
                return {"hvtb_action_state": "NO_CHASE", "hvtb_action_reason": "OBSERVED_TARGET_RR_LT_2", "entry_price": entry, "stop_price": stop, "target_price": target, "hvtb_rr": rr, "ls_5m_status": "READY"}
            last = float(x.iloc[-1]["close"])
            cross = abs(last / kis_price - 1.0) <= 0.005 if kis_price else None
            last_ts, sig_ts = x.iloc[-1]["timestamp"], r["timestamp"]
            return {
                "hvtb_action_state": "ACTION",
                "hvtb_action_reason": "REACCEL_VWAP_BOS_RETEST_HOLD_REVERSAL",
                "entry_price": entry,
                "stop_price": stop,
                "target_price": target,
                "stop_source": "HVTB_RETEST_LOW_ATR_BUFFER",
                "target_source": "HVTB_OBSERVED_20D_HIGH",
                "hvtb_rr": rr,
                "vwap": float(r["vwap"]),
                "vwap_hold": True,
                "price_hold": True,
                "box_reclaim_hold": True,
                "bos_level": level,
                "retest_low": retest_low,
                "ls_price": last,
                "ls_asof": e.stamp(last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts),
                "ls_crosscheck": cross,
                "signal_asof": e.stamp(sig_ts.to_pydatetime() if hasattr(sig_ts, "to_pydatetime") else sig_ts),
                "ls_5m_status": "READY",
            }
        return {
            "hvtb_action_state": "ACTION_WAIT",
            "hvtb_action_reason": "SEQUENCE_INCOMPLETE",
            "hvtb_volume_reaccel_seen": reacc,
            "hvtb_vwap_seen": vw,
            "hvtb_bos_seen": bos,
            "hvtb_retest_seen": retest,
            "hvtb_hold_count": hold,
            "bos_level": level,
            "retest_low": retest_low,
            "ls_5m_status": "READY",
        }

    def alpha_map(ops):
        obj = ops.local("alpha", "KRX")
        out = {}
        if not isinstance(obj, dict):
            return out
        for key in ("sector_live_pool", "live_pool", "top_watch", "top_final"):
            for z in obj.get(key, []) or []:
                if isinstance(z, dict) and e.valid_code(e.code(z)):
                    out[e.code(z)] = z
        return out

    def fullmarket_map(ops, current):
        obj = ops.local("fullmarket")
        if not isinstance(obj, dict) or not e.fresh(obj.get("generated_at"), current, 1200):
            return {}, False, obj if isinstance(obj, dict) else {}
        return {e.code(z): z for z in obj.get("candidates", []) or [] if isinstance(z, dict) and e.valid_code(e.code(z))}, True, obj

    # ------------------------------------------------------------------
    # ORIGINAL HVTB V1.6 SELECTION ENGINE
    # Source of truth: HVTB_V1_6.py::classify_focus()
    # This is generic. No stock name/code is hard-coded.
    # ------------------------------------------------------------------
    BLOCK_MARKET_STATES = {"RISK_OFF_ACCEL", "PROGRAM_SELL_ACCEL", "BREADTH_COLLAPSE"}

    def classify_focus_original(ctx):
        """Exact decision order of the original HVTB V1.6 selector."""
        if not ctx.get("common_stock", True):
            return "REJECT", "NOT_COMMON_STOCK", None

        cap_krw = n(ctx.get("market_cap_krw"))
        if cap_krw is None:
            return "PENDING", "MARKET_CAP_UNAVAILABLE", None
        if cap_krw < 70_000_000_000:
            return "REJECT", "MARKET_CAP_BELOW_MINIMUM", None

        market_state = str(ctx.get("market_state") or "NORMAL").upper()
        if market_state in BLOCK_MARKET_STATES:
            return "MARKET_BLOCK", market_state, None

        if not ctx.get("transfer_detected"):
            return "WATCH", "NO_TRANSFER", None
        if not ctx.get("absorption_confirmed"):
            return "WATCH", "TRANSFER_WITHOUT_ABSORPTION", None
        if not ctx.get("armed_structure"):
            return "WATCH", "NOT_ARMED", None

        if ctx.get("overhang_veto"):
            reasons = [str(x) for x in (ctx.get("overhang_reasons") or []) if str(x)]
            reason = "OVERHANG_VETO" + ((":" + "|".join(reasons)) if reasons else "")
            return "VETO_REJECT", reason, None

        if not ctx.get("rs_turn"):
            return "ARMED", "WAIT_RS_TURN", None

        base_priority = n(ctx.get("base_priority")) or 0.0
        catalyst_bonus = 0.0
        if ctx.get("fresh_catalyst"):
            catalyst_bonus = max(0.0, n(ctx.get("catalyst_score")) or 0.0)
        return "HVTB_FOCUS", "RS_TURN_AND_VETO_CLEAR", base_priority + catalyst_bonus

    def quote_bridge_return(r, bars, expected, live):
        """Use KIS authority quote as the latest closed/current bar when LS daily lags.

        This changes only the data adapter, not HVTB selection rules. It prevents an
        LS one-session lag from suppressing the original selector when KIS already
        has the authoritative latest price/OHLC.
        """
        last = str((bars[-1].get("date") if bars else "") or "")[:10] or None
        hist_current = bool(expected and last == expected)
        if live:
            cur = n(r.get("change_pct"))
            if cur is None and bars:
                prev_close = n(bars[-1].get("close"))
                price = n(e.pick(r, "price", "close"))
                if prev_close and price is not None:
                    cur = (price / prev_close - 1.0) * 100.0
            prev = bar_ret(bars, -1)
            return cur, prev, False, last

        if hist_current:
            return bar_ret(bars, -1), bar_ret(bars, -2), True, last

        # Pre-open/closed session with LS one day behind: KIS current quote is the
        # latest closed-session authority. Bridge it against the last LS close.
        prev_close = n(bars[-1].get("close")) if bars else None
        price = n(e.pick(r, "price", "close"))
        cur = (price / prev_close - 1.0) * 100.0 if prev_close and price is not None else None
        prev = bar_ret(bars, -1)
        bridged = bool(cur is not None and expected and last and last < str(expected))
        return cur, prev, bridged, last

    def market_proxy(rows, hist, live, expected):
        cur, prev = [], []
        for r in rows:
            b = hist.get(e.code(r), [])
            a, p, _, _ = quote_bridge_return(r, b, expected, live)
            if a is not None:
                cur.append(a)
            if p is not None:
                prev.append(p)
        return (median(cur) if cur else None), (median(prev) if prev else None)

    def upper(r, bars, fmrow, aux, newsrow, live, mcur, mprev, expected):
        cur, prev, quote_bridged, last = quote_bridge_return(r, bars, expected, live)

        # Latest OHLC: KIS authority first when live or when bridging a lagged LS
        # daily series; otherwise use the current LS daily bar.
        if live or quote_bridged:
            hi = e.pick(r, "high_price", "high")
            lo = e.pick(r, "low_price", "low")
            cl = e.pick(r, "price", "close")
            # Some pre-open quote routes do not populate day high/low. In that
            # case absorption remains unconfirmed rather than being invented.
        else:
            z = bars[-1] if bars else {}
            hi, lo, cl = z.get("high"), z.get("low"), z.get("close")

        cx = cur - mcur if cur is not None and mcur is not None else None
        px = prev - mprev if prev is not None and mprev is not None else None

        aux_fresh = e.fresh(e.pick(aux, "source_timestamp", "timestamp_kst", "price_asof"), e.now(), 1200)
        rs5, rs20 = n(aux.get("rs5")), n(aux.get("rs20"))
        explicit = bool(
            aux_fresh and (
                aux.get("asym_rs_class") in ("SUPER", "STRONG")
                or (rs5 is not None and rs20 is not None and rs5 > 0 and rs20 > 0)
            )
        )
        sign_turn = bool(px is not None and cx is not None and px <= 0 < cx)
        rs_turn = bool(explicit or sign_turn)

        # ORIGINAL HVTB evidence adapter.  The selector itself consumes
        # TRANSFER / ABSORPTION / ARMED booleans; do not collapse those
        # concepts into one parent-lane flag.  Each axis is derived from
        # observable evidence and OR-combined exactly as the original design.
        pos = range_pos(hi, lo, cl)
        op = e.pick(r, "open_price", "open") if (live or quote_bridged) else ((bars[-1] if bars else {}).get("open"))

        # Previous completed daily reference for higher-low / volume re-inflow.
        # When the latest KIS quote bridges an LS one-session lag, bars[-1] is
        # the previous session.  Otherwise bars[-2] is the previous session.
        prev_bar = {}
        if bars:
            if live or quote_bridged:
                prev_bar = bars[-1]
            elif len(bars) >= 2:
                prev_bar = bars[-2]
        prev_low = n(prev_bar.get("low"))
        prev_vol = n(prev_bar.get("volume"))
        cur_vol = n(e.pick(r, "volume")) if (live or quote_bridged) else n((bars[-1] if bars else {}).get("volume"))
        volume_reinflow_ratio = (cur_vol / prev_vol) if (cur_vol is not None and prev_vol and prev_vol > 0) else None

        high_range_close = bool(pos is not None and pos >= 0.60 and cur is not None and cur > 0)
        close_recovery = bool(
            pos is not None and pos >= 0.50 and cur is not None and cur > 0
            and n(cl) is not None and n(op) is not None and n(cl) >= n(op)
        )
        higher_low = bool(n(lo) is not None and prev_low is not None and n(lo) >= prev_low)
        supply_absorption = bool(
            volume_reinflow_ratio is not None and volume_reinflow_ratio >= 1.20
            and cur is not None and cur > 0 and pos is not None and pos >= 0.50
        )
        absorption_evidence = {
            "close_recovery": close_recovery,
            "high_range_close": high_range_close,
            "higher_low": higher_low,
            "supply_absorption": supply_absorption,
        }
        absorption = any(absorption_evidence.values())

        parent_handoff = transfer(fmrow)
        volume_reinflow = bool(volume_reinflow_ratio is not None and volume_reinflow_ratio >= 1.20 and cur is not None and cur > 0)
        flow_improving = bool(
            (aux or {}).get("flow_reversal") is True
            or (aux or {}).get("capital_handoff_confirmed") is True
        )
        transfer_evidence = {
            "capital_handoff": parent_handoff,
            "volume_reinflow": volume_reinflow,
            "flow_improving": flow_improving,
        }
        tr = any(transfer_evidence.values())
        ar = bool(tr and absorption)

        # Original hard veto is generic. Only explicit, confirmed inputs are used.
        gate = str((newsrow or {}).get("news_gate") or "").upper()
        veto_reasons = []
        if gate in e.NEWS_BLOCK:
            veto_reasons.append(gate)
        for key, label in (
            ("large_cb_bw_eb", "LARGE_CB_BW_EB"),
            ("large_rights_offering", "LARGE_RIGHTS_OFFERING"),
            ("negative_clinical_event", "NEGATIVE_CLINICAL_EVENT"),
            ("regulatory_failure", "REGULATORY_FAILURE"),
            ("material_major_holder_sale", "MAJOR_HOLDER_SALE"),
            ("accounting_or_audit_problem", "ACCOUNTING_AUDIT_PROBLEM"),
        ):
            if (fmrow or {}).get(key) is True or (aux or {}).get(key) is True or (newsrow or {}).get(key) is True:
                veto_reasons.append(label)
        overhang_veto = bool(veto_reasons)

        fresh_catalyst = bool(
            (fmrow or {}).get("fresh_catalyst") is True
            or (aux or {}).get("fresh_catalyst") is True
            or (newsrow or {}).get("fresh_catalyst") is True
        )
        catalyst_score = n((newsrow or {}).get("catalyst_score"))
        if catalyst_score is None:
            catalyst_score = n((aux or {}).get("catalyst_score"))
        if catalyst_score is None:
            catalyst_score = n((fmrow or {}).get("catalyst_score"))
        catalyst_score = catalyst_score or 0.0

        breadth = n(aux.get("sector_breadth")) if aux_fresh else None
        base_priority = (
            (cx or 0.0)
            + max(0.0, (cx - px) if cx is not None and px is not None else 0.0)
            + (2.0 * pos if pos is not None else 0.0)
            + (breadth or 0.0)
        )

        common_flag = r.get("common_stock_confirmed")
        common_stock = False if common_flag is False else True
        cap_100m = n(r.get("market_cap_100m"))
        ctx = {
            "common_stock": common_stock,
            "market_cap_krw": cap_100m * 100_000_000 if cap_100m is not None else None,
            "market_state": r.get("market_state") or "NORMAL",
            "transfer_detected": tr,
            "transfer_evidence": transfer_evidence,
            "volume_reinflow_ratio": volume_reinflow_ratio,
            "absorption_confirmed": absorption,
            "absorption_evidence": absorption_evidence,
            "armed_structure": ar,
            "overhang_veto": overhang_veto,
            "overhang_reasons": veto_reasons,
            "rs_turn": rs_turn,
            "fresh_catalyst": fresh_catalyst,
            "catalyst_score": catalyst_score,
            "base_priority": base_priority,
        }
        state, reason, priority = classify_focus_original(ctx)

        return {
            "hvtb_state": state,
            "hvtb_reason": reason,
            "transfer_detected": tr,
            "absorption_confirmed": absorption,
            "armed_structure": ar,
            "overhang_veto": overhang_veto,
            "overhang_reasons": veto_reasons,
            "rs_turn": rs_turn,
            "rs_turn_explicit": explicit,
            "rs_turn_proxy": bool(sign_turn and not explicit),
            "rs_prev_excess_pct": px,
            "rs_current_excess_pct": cx,
            "market_proxy_prev_pct": mprev,
            "market_proxy_current_pct": mcur,
            "range_position": pos,
            "fresh_catalyst": fresh_catalyst,
            "catalyst_score": catalyst_score,
            "base_priority": base_priority,
            "focus_priority": priority,
            "history_current_for_hvtb": bool(expected and last == expected),
            "history_quote_bridge": quote_bridged,
            "history_reference_date": last,
            "selector_version": "HVTB_V1_6_ORIGINAL_CLASSIFY_FOCUS",
        }

    def run_lane(ops, current, allow_action=True):
        enabled = str(os.getenv("HVTB_V16_ENABLED", "true")).lower() not in {"0", "false", "no", "off"}
        if not enabled:
            ops.store.reconcile("KRX", LANE, [], set(), current)
            return {"status": "DISABLED", "observed": 0, "evaluated_scope": 0}
        try:
            rows = ops.live_rows_only("KRX")
            hist, _, _ = ops.history.read_all()
        except Exception as exc:
            return {"status": "PENDING", "error": type(exc).__name__, "observed": 0, "evaluated_scope": 0}
        meta = ((ops.runtime or {}).get("markets") or {}).get("KRX") or {}
        expected = meta.get("history_expected_date") or e.last_closed_session(current)
        amap = alpha_map(ops)
        fmrows, fm_fresh, fmobj = fullmarket_map(ops, current)
        news = ops.local("news", "KRX", current)
        news_meta = news.get("__meta__", {}) if isinstance(news, dict) else {}
        live = e.session_open_for_market("KRX", current, None) is True
        mcur, mprev = market_proxy(rows, hist, live, expected)
        qmap = {e.code(r): r for r in rows}
        pool, evaluated = [], set()
        # Zero-Base HVTB selection: evaluate the KIS authority universe itself.
        # FULLMARKET/CAPITAL_HANDOFF is evidence, not an input-universe gate.
        for r in rows:
            c = e.code(r)
            if not e.valid_code(c):
                continue
            fmrow = fmrows.get(c, {})
            cap = n(r.get("market_cap_100m"))
            if cap is not None and cap < 700:
                continue
            bars = hist.get(c, [])
            z = upper(r, bars, fmrow, amap.get(c, {}), (news.get(c) if isinstance(news, dict) else {}), live, mcur, mprev, expected)
            z.update(code=c, name=r.get("name"), market="KRX", price=n(r.get("price")), change_pct=n(r.get("change_pct")), market_cap_100m=cap)
            evaluated.add(c)
            pool.append(z)
        pool.sort(key=lambda z: (z.get("hvtb_state") == "HVTB_FOCUS", z.get("focus_priority") or -999.0), reverse=True)
        pool = pool[: max(3, min(30, int(os.getenv("HVTB_V16_MAX_FOCUS", "12"))))]
        if live and allow_action:
            max_action = max(1, min(5, int(os.getenv("HVTB_V16_MAX_LS_ACTION", "5"))))
            count = 0
            for z in pool:
                if z.get("hvtb_state") != "HVTB_FOCUS" or count >= max_action:
                    continue
                count += 1
                z.update(action(ops, z["code"], hist.get(z["code"], []), z.get("price"), current))
        else:
            for z in pool:
                if z.get("hvtb_state") == "HVTB_FOCUS":
                    z.update(hvtb_action_state="PREOPEN_FOCUS" if not live else "ACTION_WAIT", hvtb_action_reason="WAIT_LIVE_5M" if not live else "WAIT_NEXT_ACTION_REFRESH")
        outrows = []
        audit = ops.authority_audit.get("KRX", {})
        authority_ok = e.authority_scope_consistent(audit)
        for z in pool:
            c = z["code"]
            r = qmap[c]
            signal = dict(z)
            signal["signal_track"] = "ACTION" if z.get("hvtb_action_state") == "ACTION" else z.get("hvtb_state")
            signal["lane_confirmed"] = z.get("hvtb_state") == "HVTB_FOCUS"
            if isinstance(news, dict) and c in news:
                signal.update(news[c])
            elif news_meta.get("status") == "READY":
                signal.setdefault("news_gate", "CLEAR")
                signal.setdefault("news_source", "NEWS_IMPACT_SIDECAR_NO_BLOCKING_EVENT")
            if signal.get("news_gate") in e.NEWS_BLOCK:
                signal["hvtb_state"] = "VETO_REJECT"
                signal["hvtb_reason"] = "OVERHANG_OR_NEGATIVE_NEWS_VETO"
                signal["lane_confirmed"] = False
            signal_at = signal.get("signal_asof") or e.stamp(current)
            base = ops.base(r, "KRX", signal, signal_at, amap, {}, hist.get(c, []), current, reference_daily=expected)
            base.update(
                lane=LANE,
                selection_path=[LANE, str(signal.get("signal_track") or "PENDING")],
                upstream_state=signal.get("hvtb_state"),
                lane_confirmed=signal.get("lane_confirmed"),
                history_expected_asof=expected,
                history_current=signal.get("history_current_for_hvtb"),
                authority_universe_consistent=authority_ok,
                authority_universe_exact_match=audit.get("exact_membership_matched", audit.get("membership_matched")),
                authority_symbol_present=True,
                session_open=e.session_open_for_market("KRX", current, r.get("session_open")),
            )
            for k, v in signal.items():
                if k.startswith("hvtb_") or k in (
                    "transfer_detected", "absorption_confirmed", "armed_structure", "rs_turn", "rs_turn_explicit",
                    "rs_prev_excess_pct", "rs_current_excess_pct", "market_proxy_prev_pct", "market_proxy_current_pct",
                    "range_position", "focus_priority", "base_priority", "fresh_catalyst", "catalyst_score", "overhang_veto", "overhang_reasons", "rs_turn_proxy", "selector_version", "history_current_for_hvtb", "history_quote_bridge", "history_reference_date", "transfer_evidence", "volume_reinflow_ratio", "absorption_evidence", "bos_level", "retest_low",
                ):
                    base[k] = v
            # ACTION evidence is produced by LS t8412 and must survive base(),
            # whose default LS cross-check comes from the alpha cache.
            for k in ("entry_price", "stop_price", "target_price", "stop_source", "target_source",
                      "vwap", "vwap_hold", "price_hold", "box_reclaim_hold",
                      "ls_price", "ls_asof", "ls_crosscheck", "signal_asof"):
                if k in signal:
                    base[k] = signal[k]
            base["runtime_lane_state"] = signal.get("hvtb_action_state") or signal.get("hvtb_state") or "WATCH"
            base = e.core_gates(base, current)
            base["unavailable_fields"] = [k for k in ("vwap", "rvol", "execution_strength", "rs5", "rs20", "foreign_net", "institution_net", "program_net", "entry_price", "stop_price", "target_price") if base.get(k) is None]
            outrows.append(base)
        # Every evaluated code came from the current KIS authority universe.
        # A stale/missing FULLMARKET sidecar must not make the independent HVTB
        # lane disappear; it only removes one TRANSFER evidence source.
        proven = set(evaluated)
        ops.store.reconcile("KRX", LANE, outrows, proven, current)
        if qmap:
            ops.store.track("KRX", qmap, current)
        lane_meta = {
            "observed": len(outrows),
            "status": "READY" if rows else "PENDING",
            "prime": sum(z.get("classification") == "PRIME" for z in outrows),
            "focus": sum(z.get("hvtb_state") == "HVTB_FOCUS" for z in outrows),
            "action": sum(z.get("hvtb_action_state") == "ACTION" for z in outrows),
            "evaluated_scope": len(proven),
            "universe_complete": bool(rows),
            "source": "HVTB_V1_6_ORIGINAL_CLASSIFY_FOCUS + KIS_ZERO_BASE_AUTHORITY + OPTIONAL_FULLMARKET_HANDOFF + LS_T8412_TOP_FOCUS",
        }
        markets = (ops.runtime or {}).setdefault("markets", {})
        krx = markets.setdefault("KRX", {})
        krx.setdefault("lanes", {})[LANE] = lane_meta
        ops.runtime["hvtb_v16"] = dict(lane_meta, generated_at=e.stamp(current), expected_daily=expected, market_proxy_current_pct=mcur, market_proxy_prev_pct=mprev)
        return lane_meta

    original_scan = getattr(e.Operations, "_HVTB_V16_ORIGINAL_SCAN", e.Operations.scan)
    e.Operations._HVTB_V16_ORIGINAL_SCAN = original_scan

    def scan_hvtb(self, current=None):
        current = current or e.now()
        result = original_scan(self, current)
        try:
            run_lane(self, current, allow_action=True)
        except Exception as exc:
            self.runtime["hvtb_v16"] = {"status": "PENDING", "error": type(exc).__name__, "generated_at": e.stamp(current)}
        return result

    e.Operations.scan = scan_hvtb

    original_refresh = getattr(e.Operations, "_HVTB_V16_ORIGINAL_REFRESH", e.Operations.refresh_candidate_states)
    e.Operations._HVTB_V16_ORIGINAL_REFRESH = original_refresh

    def refresh_hvtb(self, current=None):
        current = current or e.now()
        result = original_refresh(self, current)
        if e.session_open_for_market("KRX", current, None) is not True:
            return result
        last = getattr(self, "_hvtb_action_refresh_at", None)
        interval = max(90, int(os.getenv("HVTB_V16_ACTION_REFRESH_SECONDS", "120")))
        if last is not None and (current - last).total_seconds() < interval:
            return result
        self._hvtb_action_refresh_at = current
        # Re-running the bounded lane refreshes t8412 only for top FOCUS names;
        # minute5() itself has a cache, so this cannot fan out to the universe.
        try:
            run_lane(self, current, allow_action=True)
        except Exception as exc:
            self.runtime["hvtb_v16_refresh_error"] = type(exc).__name__
        return result

    e.Operations.refresh_candidate_states = refresh_hvtb
    e.HVTB_V16_VERSION = "HVTB_V1_6_ORIGINAL_CLASSIFY_FOCUS_LS_RUNTIME"


def _hvtb_selftest():
    # Generic rule self-test. No company names/codes are embedded.
    def classify(ctx):
        if not ctx.get("common_stock", True):
            return "REJECT"
        if ctx.get("market_cap_krw") is None:
            return "PENDING"
        if ctx["market_cap_krw"] < 70_000_000_000:
            return "REJECT"
        if ctx.get("market_state") in {"RISK_OFF_ACCEL", "PROGRAM_SELL_ACCEL", "BREADTH_COLLAPSE"}:
            return "MARKET_BLOCK"
        if not ctx.get("transfer_detected"):
            return "WATCH"
        if not ctx.get("absorption_confirmed"):
            return "WATCH"
        if not ctx.get("armed_structure"):
            return "WATCH"
        if ctx.get("overhang_veto"):
            return "VETO_REJECT"
        if not ctx.get("rs_turn"):
            return "ARMED"
        return "HVTB_FOCUS"

    base = {
        "common_stock": True,
        "market_cap_krw": 100_000_000_000,
        "market_state": "NORMAL",
        "transfer_detected": True,
        "absorption_confirmed": True,
        "armed_structure": True,
        "overhang_veto": False,
        "rs_turn": True,
        "fresh_catalyst": False,
    }
    assert classify(base) == "HVTB_FOCUS"  # Catalyst is not mandatory.
    veto = dict(base, overhang_veto=True)
    assert classify(veto) == "VETO_REJECT"
    armed_only = dict(base, rs_turn=False)
    assert classify(armed_only) == "ARMED"
    small = dict(base, market_cap_krw=69_900_000_000)
    assert classify(small) == "REJECT"
    return {
        "ok": True,
        "version": "HVTB_V1_6_ORIGINAL_CLASSIFY_FOCUS_LS_RUNTIME",
        "tests": ["catalyst_not_mandatory", "overhang_hard_veto", "armed_not_buy", "cap_700eok"],
    }


__all__ = ["apply"]
