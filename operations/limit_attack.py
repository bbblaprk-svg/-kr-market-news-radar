"""Live CONTINUE_ARMED -> LIMIT_ATTACK tracker.

Additive, read-only consumer of already collected KIS market rows.
It never opens a broker session, never sends orders, and never changes the
native V1.7/RS/RE25/NXT lanes.

State flow:
OBSERVE -> IMPULSE -> PULLBACK_VALID -> RECOMPRESSION -> CONTINUE_ARMED
-> LIMIT_WATCH / LIMIT_ATTACK_ARMED -> LIMIT_ATTACK
Hard deterioration after setup -> FAILED_CONVERSION

+18/+20/+22/+25 are observation thresholds, never automatic buy signals.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "LIMIT_ATTACK_LIVE_V1_0"
KST = timezone(timedelta(hours=9))
THRESHOLDS = (18.0, 20.0, 22.0, 25.0)
ALIVE_STATES = ("CONTINUE_ARMED", "LIMIT_WATCH", "LIMIT_ATTACK_ARMED", "LIMIT_ATTACK")
STATE_RANK = {"LIMIT_ATTACK": 0, "LIMIT_ATTACK_ARMED": 1, "LIMIT_WATCH": 2, "CONTINUE_ARMED": 3}
MARKET_VETO = {"RISK_OFF_ACCEL", "PROGRAM_SELL_ACCEL", "BREADTH_COLLAPSE"}
NEWS_VETO = {"PRICED_IN", "SELL_THE_NEWS", "NEGATIVE_CONFIRMED", "OVERHANG", "POSITIVE_NEWS_FAILED"}


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _pick(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _dt(v):
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d.astimezone(KST) if d.tzinfo else None
    except (TypeError, ValueError):
        return None


def _stamp(d=None):
    return (d or datetime.now(KST)).isoformat(timespec="seconds")


def _fresh(at, current, seconds=180):
    d = _dt(at)
    return d is not None and -5 <= (current - d).total_seconds() <= seconds


def _quote_grade(row, current):
    at = _pick(row, "source_timestamp", "price_asof")
    if not _fresh(at, current):
        return "STALE"
    tq = str(_pick(row, "source_timestamp_quality", "timestamp_quality") or "")
    qq = str(_pick(row, "quality", "quote_quality") or "")
    if tq == "EXCHANGE_TIMESTAMP" and qq not in ("EXPIRED", "REST_STALE", "STALE"):
        return "EXCHANGE"
    if qq == "REST_CURRENT" and tq in ("REQUEST_TIMESTAMP", "REQUEST_TIME_ONLY", "REQUEST_TIME"):
        return "REST_CURRENT"
    return "REFERENCE"


def _truth(v):
    if v is True or str(v).upper() in ("PASS", "TRUE", "YES", "Y", "1"):
        return True
    if v is False or str(v).upper() in ("FAIL", "FALSE", "NO", "N", "0", "REJECT", "BLOCK"):
        return False
    return None


def _context_for(code, contexts):
    out = {}
    if not isinstance(contexts, dict):
        return out
    # Later sources only fill missing fields, so native/current fields remain authoritative.
    for name in ("alpha", "v17", "fullmarket"):
        src = contexts.get(name) or {}
        z = src.get(code) if isinstance(src, dict) else None
        if not isinstance(z, dict):
            continue
        for k, v in z.items():
            if k not in out and v is not None:
                out[k] = v
    return out


def _normalize(row, context, current):
    c = str(_pick(row, "code", "symbol") or "").strip().upper()
    if c.isdigit() and 0 < len(c) <= 6:
        c = c.zfill(6)
    price = _num(_pick(row, "price", "current_price"))
    change = _num(_pick(row, "change_rate", "change_pct"))
    volume = _num(row.get("volume"))
    turnover = _num(_pick(row, "turnover", "trading_value"))
    vwap = _num(_pick(row, "vwap"))
    if vwap is None:
        vwap = _num(_pick(context, "vwap"))
    execution = _num(_pick(row, "execution_strength"))
    if execution is None:
        execution = _num(_pick(context, "execution_strength"))
    session_high = _num(_pick(row, "high", "high_price", "session_high"))
    if session_high is None:
        session_high = price
    sector = _pick(row, "sector", "industry")
    if sector is None:
        sector = _pick(context, "sector", "ENERGY_CLUSTER", "industry")
    breadth = _num(_pick(context, "sector_breadth", "breadth"))
    sector_expansion = _truth(_pick(context, "sector_expansion"))
    market_state = _pick(row, "market_state") or _pick(context, "market_state")
    news_gate = _pick(context, "news_gate") or _pick(row, "news_gate")
    at = _pick(row, "source_timestamp", "price_asof")
    return {
        "code": c,
        "name": str(_pick(row, "name", "stock_name") or _pick(context, "name", "stock_name") or c),
        "market": "KRX",
        "price": price,
        "change_pct": change,
        "volume": volume,
        "turnover": turnover,
        "vwap": vwap,
        "execution_strength": execution,
        "session_high": session_high,
        "sector": str(sector) if sector not in (None, "") else "UNAVAILABLE",
        "sector_breadth": breadth,
        "sector_expansion": sector_expansion,
        "market_state": market_state,
        "news_gate": news_gate,
        "price_asof": at,
        "timestamp_quality": _pick(row, "source_timestamp_quality", "timestamp_quality"),
        "quote_quality": _pick(row, "quality", "quote_quality"),
        "quote_grade": _quote_grade(row, current),
        "market_cap_100m": _num(_pick(row, "market_cap_100m")),
        "rvol": _num(_pick(row, "rvol")) or _num(_pick(context, "rvol")),
        "rs5": _num(_pick(context, "rs5", "RS5")),
        "rs20": _num(_pick(context, "rs20", "RS20")),
    }


class LimitAttackTracker:
    """Small persistent same-day tracker. No broker/network access."""

    def __init__(self, path):
        self.path = Path(path)
        self.states = {}
        self.last_meta = {"status": "PENDING", "version": VERSION}
        self._load()

    def _load(self):
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if obj.get("version") == VERSION and isinstance(obj.get("states"), dict):
                self.states = obj["states"]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.states = {}

    def _save(self, current):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "version": VERSION,
                "saved_at": _stamp(current),
                "states": self.states,
            }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    @staticmethod
    def _day(current):
        return current.astimezone(KST).date().isoformat()

    @staticmethod
    def _threshold(change):
        if change is None:
            return None
        hit = [x for x in THRESHOLDS if change >= x]
        return max(hit) if hit else None

    def _new_state(self, snap, current):
        return {
            "limit_attack_version": VERSION,
            "limit_attack_trade_date": self._day(current),
            "limit_attack_state": "OBSERVE",
            "limit_attack_state_at": _stamp(current),
            "limit_attack_started_at": _stamp(current),
            "limit_attack_peak_price": snap.get("session_high") or snap.get("price"),
            "limit_attack_peak_change_pct": snap.get("change_pct"),
            "limit_attack_impulse_velocity": None,
            "limit_attack_compression_velocity": None,
            "limit_attack_pullback_confirm_count": 0,
            "limit_attack_continue_confirm_count": 0,
            "limit_attack_attack_confirm_count": 0,
            "limit_attack_vwap_fail_count": 0,
            "limit_attack_threshold": None,
            "limit_attack_threshold_times": {},
            "limit_attack_transition_count": 0,
            "limit_attack_prediction_only": True,
            "limit_attack_entry_decision": "WAIT",
            "limit_attack_data_status": "WARMING_INTRADAY_STATE",
            "limit_attack_reason": [],
            "limit_attack_prev_volume": snap.get("volume"),
            "limit_attack_prev_at": snap.get("price_asof"),
        }

    def _step(self, prev, snap, current, sector_sync_count=0):
        out = dict(prev or self._new_state(snap, current))
        state_before = str(out.get("limit_attack_state") or "OBSERVE")
        state = state_before
        price = snap.get("price")
        change = snap.get("change_pct")
        volume = snap.get("volume")
        at = snap.get("price_asof")
        grade = snap.get("quote_grade")
        reasons = []

        # Same-day only. The tracker is intentionally reset each KRX session.
        if out.get("limit_attack_trade_date") != self._day(current):
            out = self._new_state(snap, current)
            state_before = state = "OBSERVE"

        # Carry current market fields to the board even while state evidence warms.
        out.update({
            "code": snap.get("code"), "name": snap.get("name"), "market": "KRX", "lane": "LIMIT_ATTACK",
            "price": price, "change_pct": change, "volume": volume, "turnover": snap.get("turnover"),
            "price_asof": at, "timestamp_quality": snap.get("timestamp_quality"),
            "quote_quality": snap.get("quote_quality"), "runtime_quote_grade": grade,
            "vwap": snap.get("vwap"), "execution_strength": snap.get("execution_strength"),
            "rvol": snap.get("rvol"), "rs5": snap.get("rs5"), "rs20": snap.get("rs20"),
            "sector": snap.get("sector"), "sector_breadth": snap.get("sector_breadth"),
            "limit_attack_sector_sync_count": int(sector_sync_count or 0),
            "limit_attack_sector_expansion": snap.get("sector_expansion"),
            "limit_attack_market_state": snap.get("market_state"),
            "limit_attack_news_gate": snap.get("news_gate"),
            "market_cap_100m": snap.get("market_cap_100m"),
            "limit_attack_ask_absorption": "UNAVAILABLE",
            "decision": "PREDICTION_ONLY",
            "data_status": "READY" if grade in ("EXCHANGE", "REST_CURRENT") else "PENDING",
            "classification": "WATCH",
        })

        # No state promotion from stale/reference-only data.
        if price is None or change is None or volume is None or grade == "STALE":
            out["limit_attack_data_status"] = "PENDING_FRESH_QUOTE"
            out["limit_attack_reason"] = ["FRESH_PRICE_VOLUME_REQUIRED"]
            out["limit_attack_prev_volume"] = volume if volume is not None else out.get("limit_attack_prev_volume")
            out["limit_attack_prev_at"] = at or out.get("limit_attack_prev_at")
            return out, state_before != out.get("limit_attack_state")

        # Cumulative volume velocity between fresh snapshots.
        prev_vol = _num(out.get("limit_attack_prev_volume"))
        prev_at = _dt(out.get("limit_attack_prev_at"))
        cur_at = _dt(at)
        velocity = None
        if prev_vol is not None and prev_at and cur_at and cur_at > prev_at and volume >= prev_vol:
            sec = (cur_at - prev_at).total_seconds()
            if 5 <= sec <= 180:
                velocity = (volume - prev_vol) / sec * 60.0
        out["limit_attack_volume_velocity"] = velocity
        out["limit_attack_prev_volume"] = volume
        out["limit_attack_prev_at"] = at

        # Track peak only before a valid pullback freezes the reference.
        peak = _num(out.get("limit_attack_peak_price"))
        observed_high = max([x for x in (price, snap.get("session_high"), peak) if x is not None], default=price)
        if state in ("OBSERVE", "IMPULSE"):
            peak = observed_high
            out["limit_attack_peak_price"] = peak
            if peak and price:
                # This is only descriptive; threshold logic still uses live change_pct.
                out["limit_attack_peak_change_pct"] = max(
                    [x for x in (_num(out.get("limit_attack_peak_change_pct")), change) if x is not None],
                    default=change,
                )
        elif peak is None:
            peak = observed_high
            out["limit_attack_peak_price"] = peak

        pullback = (price / peak - 1.0) * 100.0 if price and peak else None
        high_reclaim = price / peak if price and peak else None
        vwap = snap.get("vwap")
        vwap_hold = (price >= vwap) if price is not None and vwap is not None else None
        out["limit_attack_pullback_pct"] = pullback
        out["limit_attack_high_reclaim"] = high_reclaim
        out["limit_attack_vwap_hold"] = vwap_hold

        impulse_v = _num(out.get("limit_attack_impulse_velocity"))
        if state in ("OBSERVE", "IMPULSE") and velocity is not None:
            impulse_v = max([x for x in (impulse_v, velocity) if x is not None], default=velocity)
            out["limit_attack_impulse_velocity"] = impulse_v

        compression_v = _num(out.get("limit_attack_compression_velocity"))
        contraction = (velocity / impulse_v) if velocity is not None and impulse_v not in (None, 0) else None
        out["limit_attack_contraction_ratio"] = contraction
        reaccel = (velocity / compression_v) if velocity is not None and compression_v not in (None, 0) else None
        out["limit_attack_reaccel_ratio"] = reaccel

        market_veto = snap.get("market_state") in MARKET_VETO
        news_veto = snap.get("news_gate") in NEWS_VETO
        out["limit_attack_market_veto"] = bool(market_veto)
        out["limit_attack_news_veto"] = bool(news_veto)

        # 1) Detect initial impulse.
        if state == "OBSERVE" and change >= 7.0:
            state = "IMPULSE"
            out["limit_attack_impulse_at"] = _stamp(current)
            out["limit_attack_data_status"] = "TRACKING"
            reasons.append("IMPULSE_GE_7")

        # 2) Healthy pullback from the running peak.
        if state == "IMPULSE":
            healthy_pullback = pullback is not None and -6.0 <= pullback <= -2.0
            if healthy_pullback:
                cnt = int(out.get("limit_attack_pullback_confirm_count") or 0) + 1
                out["limit_attack_pullback_confirm_count"] = cnt
                if cnt >= 1:
                    state = "PULLBACK_VALID"
                    out["limit_attack_pullback_at"] = _stamp(current)
                    reasons.append("HEALTHY_PULLBACK")
            elif pullback is not None and pullback < -8.0:
                state = "FAILED_CONVERSION"
                reasons.append("PULLBACK_TOO_DEEP")

        # 3) Volume contraction during/after pullback.
        if state == "PULLBACK_VALID":
            if contraction is not None and contraction <= 0.65:
                compression_v = velocity
                out["limit_attack_compression_velocity"] = compression_v
                out["limit_attack_reaccel_ratio"] = None
                state = "RECOMPRESSION"
                out["limit_attack_recompression_at"] = _stamp(current)
                reasons.append("VOLUME_CONTRACTION_LE_0P65")

        # Recalculate reacceleration after compression may have just been set.
        compression_v = _num(out.get("limit_attack_compression_velocity"))
        reaccel = (velocity / compression_v) if velocity is not None and compression_v not in (None, 0) else None
        out["limit_attack_reaccel_ratio"] = reaccel

        # 4) Reclaim + VWAP + reacceleration = CONTINUE_ARMED.
        if state == "RECOMPRESSION":
            structural = (
                high_reclaim is not None and high_reclaim >= 0.97
                and vwap_hold is True
                and reaccel is not None and reaccel >= 1.50
            )
            if structural:
                out["limit_attack_continue_confirm_count"] = int(out.get("limit_attack_continue_confirm_count") or 0) + 1
                state = "CONTINUE_ARMED"
                out["limit_attack_continue_armed_at"] = _stamp(current)
                reasons.extend(["HIGH_RECLAIM_GE_0P97", "VWAP_HOLD", "REACCEL_GE_1P50"])
            elif pullback is not None and pullback < -8.0:
                state = "FAILED_CONVERSION"
                reasons.append("RECOMPRESSION_FAILED_DEEP_PULLBACK")

        # 5) Death filter after CONTINUE_ARMED.
        if state in ALIVE_STATES:
            vf = int(out.get("limit_attack_vwap_fail_count") or 0)
            vf = vf + 1 if vwap_hold is False else 0
            out["limit_attack_vwap_fail_count"] = vf
            if (high_reclaim is not None and high_reclaim < 0.94) or vf >= 2:
                state = "FAILED_CONVERSION"
                reasons.append("POST_ARMED_STRUCTURE_BROKEN")

        # 6) Threshold head. These are prediction states, never automatic entries.
        if state in ALIVE_STATES:
            threshold = self._threshold(change)
            out["limit_attack_threshold"] = threshold
            times = dict(out.get("limit_attack_threshold_times") or {})
            if threshold is not None:
                for t in THRESHOLDS:
                    if t <= threshold and str(int(t)) not in times:
                        times[str(int(t))] = _stamp(current)
                out["limit_attack_threshold_times"] = times

                structural = (
                    high_reclaim is not None and high_reclaim >= 0.97
                    and vwap_hold is True
                    and reaccel is not None and reaccel >= 1.50
                )
                tape = (
                    (snap.get("execution_strength") is not None and snap["execution_strength"] >= 130.0)
                    or (price is not None and peak is not None and price >= peak)
                )
                contextual = (
                    sector_sync_count >= 2
                    or (snap.get("execution_strength") is not None and snap["execution_strength"] >= 150.0)
                    or (price is not None and peak is not None and price >= peak)
                )
                blocked = market_veto or news_veto
                if not structural or blocked:
                    state = "LIMIT_WATCH"
                    out["limit_attack_attack_confirm_count"] = 0
                    if blocked:
                        reasons.append("MARKET_OR_NEWS_VETO")
                    else:
                        reasons.append("THRESHOLD_REACHED_STRUCTURE_INCOMPLETE")
                elif tape:
                    cnt = int(out.get("limit_attack_attack_confirm_count") or 0) + 1
                    out["limit_attack_attack_confirm_count"] = cnt
                    state = "LIMIT_ATTACK" if cnt >= 2 and contextual and grade == "EXCHANGE" else "LIMIT_ATTACK_ARMED"
                    reasons.append("TAPE_CONFIRM")
                    if sector_sync_count >= 2:
                        reasons.append("SECTOR_SYNC_2PLUS")
                    if grade != "EXCHANGE":
                        reasons.append("EXCHANGE_AUTH_PENDING")
                else:
                    out["limit_attack_attack_confirm_count"] = 0
                    state = "LIMIT_ATTACK_ARMED"
                    reasons.append("STRUCTURE_READY_TAPE_PENDING")

        if state == "FAILED_CONVERSION":
            out["classification"] = "REJECT"
            out["decision"] = "FILTERED_OUT"
            out["limit_attack_entry_decision"] = "DO_NOT_ENTER"
            out["limit_attack_data_status"] = "READY"
        elif state == "LIMIT_ATTACK":
            # +25% and beyond is prediction-only: do not convert the upper-limit predictor into a chase entry.
            if (out.get("limit_attack_threshold") or 0) >= 25.0 or change > 22.0:
                out["limit_attack_entry_decision"] = "NO_CHASE_PREDICTION_ONLY"
            else:
                out["limit_attack_entry_decision"] = "ENTRY_REVIEW_NOT_AUTO_BUY"
            out["limit_attack_data_status"] = "READY"
            out["classification"] = "ARMED"
        elif state == "LIMIT_ATTACK_ARMED":
            out["limit_attack_entry_decision"] = "WAIT_TAPE_CONFIRM"
            out["limit_attack_data_status"] = "READY" if grade == "EXCHANGE" else "PENDING_EXCHANGE_AUTH"
            out["classification"] = "ARMED"
        elif state == "LIMIT_WATCH":
            out["limit_attack_entry_decision"] = "WAIT"
            out["limit_attack_data_status"] = "READY"
            out["classification"] = "WATCH"
        elif state == "CONTINUE_ARMED":
            out["limit_attack_entry_decision"] = "WAIT_THRESHOLD"
            out["limit_attack_data_status"] = "READY"
            out["classification"] = "ARMED"
        else:
            out["limit_attack_entry_decision"] = "WAIT"
            out["classification"] = "WATCH"

        if state != state_before:
            out["limit_attack_transition_count"] = int(out.get("limit_attack_transition_count") or 0) + 1
            out["limit_attack_state_at"] = _stamp(current)
        out["limit_attack_state"] = state
        out["limit_attack_reason"] = reasons or list(out.get("limit_attack_reason") or [])[-6:]
        out["selection_path"] = (
            ["FULL_KRX_STRONG", "CONTINUE_ARMED", state]
            if state in ALIVE_STATES else ["FULL_KRX_STRONG", state]
        )
        out["entry_phase"] = out.get("limit_attack_entry_decision")
        out["expansion_phase"] = (
            "UPPER_LIMIT_ATTACK_PREDICTION" if state == "LIMIT_ATTACK"
            else "UPPER_LIMIT_ATTACK_ARMED" if state == "LIMIT_ATTACK_ARMED"
            else "CONTINUATION_SURVIVOR" if state in ("CONTINUE_ARMED", "LIMIT_WATCH")
            else "FILTERED" if state == "FAILED_CONVERSION" else "WARMING"
        )
        out["promotion_conditions"] = [
            "CONTINUE_ARMED first",
            "threshold +18/+20/+22/+25 is observation only",
            "HIGH_RECLAIM >= 0.97",
            "VWAP_HOLD = PASS",
            "REACCEL >= 1.50",
            "tape confirm: execution_strength >= 130 or prior-peak breakout",
            "LIMIT_ATTACK needs 2 confirmations and exchange-timestamp authority",
        ]
        out["invalidation_conditions"] = [
            "post-armed HIGH_RECLAIM < 0.94",
            "VWAP loss on 2 consecutive refreshes",
            "market/news veto blocks LIMIT_ATTACK promotion",
            "+25% and >+22% are NO_CHASE prediction-only",
        ]
        out["gates"] = {
            "HIGH_RECLAIM_97": "PASS" if high_reclaim is not None and high_reclaim >= 0.97 else "PENDING" if high_reclaim is None else "FAIL",
            "VWAP_HOLD": "PASS" if vwap_hold is True else "FAIL" if vwap_hold is False else "PENDING",
            "REACCEL_1P50": "PASS" if reaccel is not None and reaccel >= 1.50 else "PENDING" if reaccel is None else "FAIL",
            "SECTOR_SYNC_2": "PASS" if sector_sync_count >= 2 else "PENDING",
            "EXECUTION_130": "PASS" if snap.get("execution_strength") is not None and snap["execution_strength"] >= 130 else "PENDING" if snap.get("execution_strength") is None else "FAIL",
            "EXCHANGE_AUTH": "PASS" if grade == "EXCHANGE" else "PENDING",
        }
        return out, state != state_before

    def refresh(self, rows, current=None, contexts=None):
        current = current or datetime.now(KST)
        day = self._day(current)
        normalized = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            c = str(_pick(row, "code", "symbol") or "").strip().upper()
            if c.isdigit() and 0 < len(c) <= 6:
                c = c.zfill(6)
            if not c:
                continue
            snap = _normalize(row, _context_for(c, contexts), current)
            normalized[c] = snap

        # Same-sector synchronized strong peers, computed from the same live KRX snapshot.
        group_strong = {}
        for c, s in normalized.items():
            group = s.get("sector")
            if group in (None, "", "UNAVAILABLE"):
                continue
            strong = s.get("change_pct") is not None and s["change_pct"] >= 5.0
            if strong and s.get("price") is not None and s.get("vwap") is not None:
                strong = s["price"] >= s["vwap"]
            if strong:
                group_strong.setdefault(group, set()).add(c)

        # Drop prior sessions. Keep only current-day state.
        self.states = {
            c: s for c, s in self.states.items()
            if isinstance(s, dict) and s.get("limit_attack_trade_date") == day
        }

        # New tracking starts at +7%. Existing same-day symbols keep being followed even after a pullback.
        track_codes = set(self.states)
        track_codes.update(c for c, s in normalized.items() if s.get("change_pct") is not None and s["change_pct"] >= 7.0)

        changed = 0
        for c in sorted(track_codes):
            snap = normalized.get(c)
            if not snap:
                continue
            group = snap.get("sector")
            peer_count = max(0, len(group_strong.get(group, set()) - {c})) if group not in (None, "", "UNAVAILABLE") else 0
            updated, did_change = self._step(self.states.get(c), snap, current, peer_count)
            self.states[c] = updated
            changed += int(did_change)

        # Bound persistent state; largest current movers are more useful if an abnormal day explodes.
        if len(self.states) > 300:
            keep = sorted(
                self.states.items(),
                key=lambda kv: (_num(kv[1].get("change_pct")) or -999),
                reverse=True,
            )[:300]
            self.states = dict(keep)

        persisted = self._save(current)
        counts = {}
        for s in self.states.values():
            st = s.get("limit_attack_state")
            counts[st] = counts.get(st, 0) + 1
        self.last_meta = {
            "status": "READY",
            "version": VERSION,
            "updated_at": _stamp(current),
            "tracked": len(self.states),
            "alive": sum(counts.get(x, 0) for x in ALIVE_STATES),
            "limit_attack": counts.get("LIMIT_ATTACK", 0),
            "limit_attack_armed": counts.get("LIMIT_ATTACK_ARMED", 0),
            "continue_armed": counts.get("CONTINUE_ARMED", 0),
            "failed_conversion": counts.get("FAILED_CONVERSION", 0),
            "state_counts": counts,
            "transitions_this_refresh": changed,
            "persistence": "READY" if persisted else "PENDING",
            "source": "KIS_EXISTING_LIVE_ROWS_READONLY",
        }
        return dict(self.last_meta)

    def board(self, market="KRX", limit=80, offset=0):
        if market != "KRX":
            return {"market": market, "status": "NOT_APPLICABLE", "total": 0, "rows": [], "offset": offset, "limit": limit}
        rows = [dict(s) for s in self.states.values() if s.get("limit_attack_state") in ALIVE_STATES]
        rows.sort(key=lambda r: (
            STATE_RANK.get(r.get("limit_attack_state"), 9),
            -(_num(r.get("change_pct")) or -999),
            r.get("code") or "",
        ))
        total = len(rows)
        return {
            "market": market,
            "status": self.last_meta.get("status", "PENDING"),
            "total": total,
            "rows": rows[offset:offset + limit],
            "offset": offset,
            "limit": limit,
            "meta": dict(self.last_meta),
        }
