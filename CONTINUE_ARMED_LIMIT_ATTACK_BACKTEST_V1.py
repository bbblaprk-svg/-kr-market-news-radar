#!/usr/bin/env python3
"""
CONTINUE_ARMED -> LIMIT_ATTACK threshold backtest
Version: V1.0
Purpose:
  Keep the upstream CONTINUE_ARMED logic unchanged.
  Only among bars that have already survived CONTINUE_ARMED, measure what happens
  when the stock first reaches +18/+20/+22/+25% intraday.

Required input CSV columns:
  code, datetime, prev_close, open, high, low, close, volume, continue_armed

Recommended optional columns:
  name, cluster_id, market_pct, execution_strength, ask_absorption,
  sector_sync_count, limit_up_price

Important:
  - This engine DOES NOT redefine CONTINUE_ARMED.
  - It never treats +18/+20/+22/+25% as an automatic buy signal.
  - It records WATCH / ATTACK_ARMED / ATTACK / FAILED_CONVERSION separately.
  - Cross-threshold detection uses intraday HIGH, while confirmation uses CLOSE/VWAP.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import math
import pandas as pd
import numpy as np


THRESHOLDS = (18.0, 20.0, 22.0, 25.0)


@dataclass
class Params:
    high_reclaim_min: float = 0.97
    vol_reaccel_min: float = 1.50
    sector_sync_min: int = 2
    peer_min_pct: float = 5.0
    execution_strength_min: float = 130.0
    fail_high_reclaim: float = 0.94
    limit_fallback_pct: float = 29.5
    fade_drop_pp: float = 8.0
    strong_close_gap_from_high_pp: float = 3.0


def to_bool(v):
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if pd.isna(v):
        return False
    s = str(v).strip().lower()
    return s in {"1","true","t","yes","y","pass","armed","continue_armed"}


def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    req = {"code","datetime","prev_close","open","high","low","close","volume","continue_armed"}
    missing = req - set(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")

    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"])
    x["date"] = x["datetime"].dt.date.astype(str)
    x["code"] = x["code"].astype(str).str.zfill(6)
    x["continue_armed"] = x["continue_armed"].map(to_bool)

    for c in ["prev_close","open","high","low","close","volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna(subset=["prev_close","open","high","low","close","volume"])
    x = x[x["prev_close"] > 0].sort_values(["code","date","datetime"]).reset_index(drop=True)

    # Intraday percent changes from previous close.
    for px in ["open","high","low","close"]:
        x[f"{px}_pct"] = (x[px] / x["prev_close"] - 1.0) * 100.0

    # Leakage-safe cumulative VWAP using typical price.
    tp = (x["high"] + x["low"] + x["close"]) / 3.0
    x["_pv"] = tp * x["volume"]
    g = x.groupby(["code","date"], sort=False)
    x["vwap"] = g["_pv"].cumsum() / g["volume"].cumsum().replace(0, np.nan)
    x["vwap_hold"] = x["close"] >= x["vwap"]

    # Session high before current bar and including current bar.
    x["pre_session_high"] = g["high"].cummax().shift(1)
    x["session_high"] = g["high"].cummax()
    x["high_reclaim"] = x["close"] / x["session_high"].replace(0, np.nan)

    # Reacceleration vs previous 3 bars (no current-bar leakage).
    x["_vol_prev3"] = g["volume"].transform(lambda s: s.shift(1).rolling(3, min_periods=2).mean())
    x["vol_reaccel"] = x["volume"] / x["_vol_prev3"].replace(0, np.nan)

    # Two consecutive closes under VWAP = structural warning, not automatic outcome.
    x["_below_vwap"] = (~x["vwap_hold"]).astype(int)
    x["two_bars_below_vwap"] = g["_below_vwap"].transform(
        lambda s: s.rolling(2, min_periods=2).sum().eq(2)
    )

    # Breakout acceptance: close at/above the high that existed before this bar.
    x["breakout_accept"] = (
        x["pre_session_high"].notna() & (x["close"] >= x["pre_session_high"])
    )

    # Optional cluster co-movement. If an upstream sector_sync_count exists, preserve it.
    if "sector_sync_count" in x.columns:
        x["sector_sync_count"] = pd.to_numeric(x["sector_sync_count"], errors="coerce").fillna(0).astype(int)
    elif "cluster_id" in x.columns:
        # Count OTHER stocks in same cluster/timestamp that are >= peer_min_pct and above VWAP.
        strong = (x["close_pct"] >= p.peer_min_pct) & x["vwap_hold"]
        temp = x[["datetime","cluster_id","code"]].copy()
        temp["_strong"] = strong.astype(int)
        cluster_count = temp.groupby(["datetime","cluster_id"])["_strong"].transform("sum")
        x["sector_sync_count"] = (cluster_count - strong.astype(int)).clip(lower=0).astype(int)
    else:
        x["sector_sync_count"] = 0

    if "market_pct" in x.columns:
        x["market_pct"] = pd.to_numeric(x["market_pct"], errors="coerce").fillna(0.0)
        x["rs_spread_pp"] = x["close_pct"] - x["market_pct"]
    else:
        x["rs_spread_pp"] = np.nan

    if "execution_strength" in x.columns:
        x["execution_strength"] = pd.to_numeric(x["execution_strength"], errors="coerce")
    else:
        x["execution_strength"] = np.nan

    if "ask_absorption" in x.columns:
        x["ask_absorption"] = x["ask_absorption"].map(to_bool)
    else:
        x["ask_absorption"] = False

    if "limit_up_price" in x.columns:
        x["limit_up_price"] = pd.to_numeric(x["limit_up_price"], errors="coerce")
    else:
        x["limit_up_price"] = np.nan

    return x


def classify_cross(row: pd.Series, p: Params) -> tuple[str, list[str], list[str]]:
    hard_pass = []
    hard_fail = []

    checks = {
        "VWAP_HOLD": bool(row["vwap_hold"]),
        "HIGH_RECLAIM_97": bool(row["high_reclaim"] >= p.high_reclaim_min),
        "VOL_REACCEL_1P5": bool(pd.notna(row["vol_reaccel"]) and row["vol_reaccel"] >= p.vol_reaccel_min),
        "SECTOR_SYNC_2": bool(row["sector_sync_count"] >= p.sector_sync_min),
    }
    for k, ok in checks.items():
        (hard_pass if ok else hard_fail).append(k)

    death_veto = (
        bool(row["two_bars_below_vwap"])
        or bool(row["high_reclaim"] < p.fail_high_reclaim)
    )
    if death_veto:
        return "FAILED_CONVERSION", hard_pass, hard_fail

    if hard_fail:
        return "LIMIT_WATCH", hard_pass, hard_fail

    # Final tape confirmation is deliberately separate from the structural ARM state.
    tape_confirm = (
        bool(row["breakout_accept"])
        or bool(row["ask_absorption"])
        or bool(pd.notna(row["execution_strength"]) and row["execution_strength"] >= p.execution_strength_min)
    )
    if tape_confirm:
        return "LIMIT_ATTACK", hard_pass, hard_fail
    return "LIMIT_ATTACK_ARMED", hard_pass, hard_fail


def outcome_from_cross(day: pd.DataFrame, cross_idx: int, threshold: float, p: Params) -> dict:
    after = day.loc[cross_idx:].copy()
    cross = day.loc[cross_idx]
    entry = float(cross["close"])

    if pd.notna(cross.get("limit_up_price", np.nan)):
        limit_hit = bool((after["high"] >= float(cross["limit_up_price"])).any())
    else:
        limit_hit = bool((after["high_pct"] >= p.limit_fallback_pct).any())

    final = after.iloc[-1]
    final_close_pct = float(final["close_pct"])
    day_high_pct_after = float(after["high_pct"].max())
    day_low_pct_after = float(after["low_pct"].min())

    mfe = float((after["high"].max() / entry - 1.0) * 100.0) if entry > 0 else np.nan
    mae = float((after["low"].min() / entry - 1.0) * 100.0) if entry > 0 else np.nan

    # Objective, threshold-relative labels. These are transparent and configurable.
    fade = final_close_pct <= (threshold - p.fade_drop_pp)
    strong_close = (
        final_close_pct >= threshold
        and (day_high_pct_after - final_close_pct) <= p.strong_close_gap_from_high_pp
    )

    return {
        "limit_hit": limit_hit,
        "strong_close": bool(strong_close),
        "fade": bool(fade),
        "final_close_pct": final_close_pct,
        "day_high_pct_after": day_high_pct_after,
        "day_low_pct_after": day_low_pct_after,
        "mfe_after_cross_pct": mfe,
        "mae_after_cross_pct": mae,
    }


def transition_summary(ev: pd.DataFrame) -> pd.DataFrame:
    """
    Nested-threshold conditional transitions.
    Denominator is the number of unique code-date events that reached the prior stage.
    This avoids reading the raw +25% hit rate as if it were directly comparable to +18%.
    """
    if ev.empty:
        return pd.DataFrame()

    key = ["code", "date"]
    reached = {}
    for thr in THRESHOLDS:
        reached[thr] = set(map(tuple, ev.loc[ev["threshold"] == thr, key].drop_duplicates().to_numpy()))

    # A day is counted as limit-hit if any threshold record for that code-date says limit_hit.
    limit_set = set(map(tuple, ev.loc[ev["limit_hit"], key].drop_duplicates().to_numpy()))

    rows = []
    stages = list(THRESHOLDS)
    for a, b in zip(stages[:-1], stages[1:]):
        den = reached[a]
        num = reached[b]
        rows.append({
            "from_stage": f"+{int(a)}%",
            "to_stage": f"+{int(b)}%",
            "from_events": len(den),
            "to_events": len(num),
            "conditional_transition_rate": (len(num) / len(den)) if den else np.nan,
            "stalled_before_next": len(den - num),
            "stalled_rate": (len(den - num) / len(den)) if den else np.nan,
        })

    den = reached[25.0]
    rows.append({
        "from_stage": "+25%",
        "to_stage": "LIMIT",
        "from_events": len(den),
        "to_events": len(den & limit_set),
        "conditional_transition_rate": (len(den & limit_set) / len(den)) if den else np.nan,
        "stalled_before_next": len(den - limit_set),
        "stalled_rate": (len(den - limit_set) / len(den)) if den else np.nan,
    })
    return pd.DataFrame(rows)


def run_backtest(df: pd.DataFrame, p: Params):
    x = prepare(df, p)
    events = []

    for (code, date), day in x.groupby(["code","date"], sort=False):
        day = day.copy()
        armed_rows = day.index[day["continue_armed"]]
        if len(armed_rows) == 0:
            continue
        armed_idx = armed_rows[0]
        armed_time = day.loc[armed_idx, "datetime"]

        # Only evaluate threshold crossings AFTER the stock has survived CONTINUE_ARMED.
        eligible = day.loc[armed_idx:].copy()

        for thr in THRESHOLDS:
            hit = eligible.index[eligible["high_pct"] >= thr]
            if len(hit) == 0:
                continue
            idx = hit[0]
            row = day.loc[idx]
            state, passes, fails = classify_cross(row, p)
            out = outcome_from_cross(day, idx, thr, p)

            events.append({
                "code": code,
                "name": row.get("name", ""),
                "date": date,
                "continue_armed_time": armed_time,
                "threshold": thr,
                "cross_time": row["datetime"],
                "cross_close_pct": float(row["close_pct"]),
                "cross_high_pct": float(row["high_pct"]),
                "vwap_hold": bool(row["vwap_hold"]),
                "high_reclaim": float(row["high_reclaim"]),
                "vol_reaccel": None if pd.isna(row["vol_reaccel"]) else float(row["vol_reaccel"]),
                "sector_sync_count": int(row["sector_sync_count"]),
                "rs_spread_pp": None if pd.isna(row["rs_spread_pp"]) else float(row["rs_spread_pp"]),
                "execution_strength": None if pd.isna(row["execution_strength"]) else float(row["execution_strength"]),
                "ask_absorption": bool(row["ask_absorption"]),
                "breakout_accept": bool(row["breakout_accept"]),
                "state": state,
                "pass_reasons": "|".join(passes),
                "fail_reasons": "|".join(fails),
                **out,
            })

    ev = pd.DataFrame(events)
    if ev.empty:
        return ev, pd.DataFrame()

    def summarize(g):
        n = len(g)
        return pd.Series({
            "events": n,
            "limit_hits": int(g["limit_hit"].sum()),
            "limit_hit_rate": g["limit_hit"].mean(),
            "strong_close_rate": g["strong_close"].mean(),
            "fade_rate": g["fade"].mean(),
            "avg_mfe_after_cross_pct": g["mfe_after_cross_pct"].mean(),
            "avg_mae_after_cross_pct": g["mae_after_cross_pct"].mean(),
        })

    summary = (
        ev.groupby(["threshold","state"], dropna=False)
          .apply(summarize, include_groups=False)
          .reset_index()
          .sort_values(["threshold","state"])
    )
    return ev, summary


def self_test():
    # Tiny synthetic smoke test: validates state transitions only, not market performance.
    t = pd.date_range("2026-01-02 09:00", periods=8, freq="5min")
    base = pd.DataFrame({
        "code":["000001"]*8,
        "name":["SELFTEST"]*8,
        "datetime":t,
        "prev_close":[100]*8,
        "open":[108,112,116,117,119,121,124,127],
        "high":[110,114,118,120,122,124,127,130],
        "low":[107,111,115,116,118,120,123,126],
        "close":[109,113,117,119,121,123,126,129],
        "volume":[100,120,140,80,70,160,220,300],
        "continue_armed":[False,False,False,True,True,True,True,True],
        "sector_sync_count":[0,0,1,2,2,2,3,3],
        "execution_strength":[100,105,110,120,135,145,155,170],
        "ask_absorption":[False,False,False,False,False,True,True,True],
    })
    ev, sm = run_backtest(base, Params())
    tr = transition_summary(ev)
    assert not ev.empty
    assert not tr.empty
    assert set(ev["threshold"]).issubset(set(THRESHOLDS))
    return {
        "events": len(ev),
        "states": ev["state"].value_counts().to_dict(),
        "transitions": tr.to_dict(orient="records"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv", nargs="?")
    ap.add_argument("--outdir", default="limit_attack_output")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2))
        return

    if not args.input_csv:
        raise SystemExit("input_csv required (or use --self-test)")

    p = Params()
    df = pd.read_csv(args.input_csv)
    ev, sm = run_backtest(df, p)
    tr = transition_summary(ev)

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    ev.to_csv(out / "limit_attack_events.csv", index=False, encoding="utf-8-sig")
    sm.to_csv(out / "limit_attack_summary.csv", index=False, encoding="utf-8-sig")
    tr.to_csv(out / "threshold_transitions.csv", index=False, encoding="utf-8-sig")
    (out / "params.json").write_text(json.dumps(asdict(p), ensure_ascii=False, indent=2), encoding="utf-8")

    print(sm.to_string(index=False) if not sm.empty else "NO ELIGIBLE EVENTS")
    if not tr.empty:
        print("\nTRANSITIONS")
        print(tr.to_string(index=False))
    print(f"\nWrote: {out.resolve()}")


if __name__ == "__main__":
    main()
