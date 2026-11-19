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

    e.VERSION = "KRX_NXT_OPERATIONS_V10_LIVE_HISTORY_DISK_GUARD"
    _APPLIED = True


__all__ = ["apply"]
