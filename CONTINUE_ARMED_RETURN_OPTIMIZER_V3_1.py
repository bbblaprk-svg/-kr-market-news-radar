#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CONTINUE_ARMED Return Optimizer V3
==================================
Purpose
-------
Optimize REALIZED RETURN after a stock has survived a strict CONTINUE_ARMED-style
structure. Upper-limit (+29.5%) prediction is diagnostic only, not the objective.

The script is designed to reuse the completed SMART_REACCEL_MTF_V2_1 Render run:
  /data/results/smart_reaccel_mtf_v2_1/<latest>/opportunities.csv
  /data/results/smart_reaccel_mtf_v2_1/bar_cache_1m/YYYY-MM-DD_CODE.csv.gz

If an upstream dataset already contains continue_armed labels, use --input-csv.
Otherwise the engine reconstructs STRICT_CONTINUE_ARMED from 1-minute cached bars.

Key principles
--------------
1) 'Dead-stock removal' remains upstream. Do not weaken the survival filter just
   to increase signal count.
2) +18/+20/+22/+25 are STATE/OBSERVATION thresholds, never automatic buys.
3) Entry is optimized around post-pullback reacceleration/retest, not threshold chase.
4) Objective priority: OOS expectancy -> PF -> drawdown -> median -> MAE.
5) Same-bar TP/SL ambiguity is always resolved conservatively as STOP first.
6) Transaction friction defaults to 16bp round-trip.

Outputs
-------
cohort_events.csv
trade_grid.csv
strategy_summary.csv
walkforward_summary.csv
best_strategies.csv
threshold_diagnostics.csv
report.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import time
from types import SimpleNamespace
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

VERSION = "CONTINUE_ARMED_RETURN_OPTIMIZER_V3_1"
DEFAULT_ROOT = Path("/data/results/smart_reaccel_mtf_v2_1")
COST_BPS_TOTAL = float(os.getenv("RETURN_OPT_COST_BPS", "16"))
SIDE_SLIP = (COST_BPS_TOTAL / 2.0) / 10000.0
THRESHOLDS = (18.0, 20.0, 22.0, 25.0, 29.5)


@dataclass(frozen=True)
class ArmedParams:
    impulse_min_pct: float = 10.0
    pullback_min_pp: float = 2.0
    pullback_max_pp: float = 8.0
    reclaim_gap_pp: float = 1.5
    high_reclaim_min: float = 0.97
    structural_fail_reclaim: float = 0.94
    vol_reaccel_min: float = 1.50
    compression_ratio_max: float = 0.70
    nochase_pct: float = 22.0


@dataclass(frozen=True)
class Strategy:
    entry_style: str
    stop_pct: float
    take_pct: float
    partial: bool = False
    trail: bool = False
    limit_extension: bool = False

    @property
    def key(self) -> str:
        if self.limit_extension:
            return f"{self.entry_style}_SL{self.stop_pct:g}_PARTIAL_LIMIT_EXT"
        flags = []
        if self.partial:
            flags.append("PARTIAL")
        if self.trail:
            flags.append("TRAIL")
        f = "_".join(flags) if flags else "FIXED"
        return f"{self.entry_style}_SL{self.stop_pct:g}_TP{self.take_pct:g}_{f}"


def num(v, default=np.nan):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def find_latest_result_dir(root: Path) -> Path:
    ds = []
    if root.exists():
        for p in root.iterdir():
            if p.is_dir() and re.fullmatch(r"\d{8}_\d{6}", p.name) and (p / "opportunities.csv").exists():
                ds.append(p)
    if not ds:
        raise FileNotFoundError(f"No SMART_REACCEL result dir with opportunities.csv under {root}")
    return sorted(ds)[-1]


def load_event_universe(root: Path) -> pd.DataFrame:
    latest = find_latest_result_dir(root)
    x = pd.read_csv(latest / "opportunities.csv")
    need = ["date", "code", "name", "prev_close", "day_peak_pct"]
    miss = [c for c in need if c not in x.columns]
    if miss:
        raise ValueError(f"opportunities.csv missing columns: {miss}")
    x["code"] = x["code"].astype(str).str.zfill(6)
    return x[need].drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)


def load_cached_1m(root: Path, date: str, code: str) -> pd.DataFrame:
    p = root / "bar_cache_1m" / f"{date}_{str(code).zfill(6)}.csv.gz"
    if not p.exists():
        return pd.DataFrame()
    x = pd.read_csv(p)
    if "datetime" not in x.columns:
        return pd.DataFrame()
    x["datetime"] = pd.to_datetime(x["datetime"], utc=True, errors="coerce").dt.tz_convert("Asia/Seoul")
    for c in ["open", "high", "low", "close", "volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.dropna(subset=["datetime", "open", "high", "low", "close", "volume"]).sort_values("datetime").reset_index(drop=True)


def resample(df: pd.DataFrame, mins: int) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    x = df.set_index("datetime").sort_index()
    y = (x.resample(f"{mins}min", origin="start_day", label="right", closed="right")
           .agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"})
           .dropna())
    y = y[(y.index.strftime("%H%M") >= "0900") & (y.index.strftime("%H%M") <= "1530")]
    return y.reset_index()


def features(df: pd.DataFrame, prev_close: float) -> pd.DataFrame:
    x = df.copy()
    if x.empty:
        return x
    for c in ["open","high","low","close"]:
        x[f"{c}_pct"] = (x[c] / prev_close - 1.0) * 100.0
    tp = (x.high + x.low + x.close) / 3.0
    x["vwap"] = (tp * x.volume).cumsum() / x.volume.cumsum().replace(0, np.nan)
    x["vwap_hold"] = x.close >= x.vwap
    x["session_high"] = x.high.cummax()
    x["session_high_pct"] = x.high_pct.cummax()
    x["high_reclaim"] = x.close / x.session_high.replace(0, np.nan)
    x["pre_high"] = x.high.cummax().shift(1)
    x["breakout_accept"] = x.pre_high.notna() & (x.close >= x.pre_high)
    x["vol_prev3"] = x.volume.shift(1).rolling(3, min_periods=2).mean()
    x["vol_reaccel"] = x.volume / x.vol_prev3.replace(0, np.nan)
    x["vol_med5"] = x.volume.rolling(5, min_periods=3).median()
    x["ma5"] = x.close.rolling(5).mean()
    x["ma20"] = x.close.rolling(20).mean()
    x["s5"] = x.ma5.diff()
    x["s20"] = x.ma20.diff()
    below = (~x.vwap_hold).astype(int)
    x["two_below_vwap"] = below.rolling(2, min_periods=2).sum().eq(2)
    x["prior2_high"] = x.high.shift(1).rolling(2, min_periods=2).max()
    x["prior2_low"] = x.low.shift(1).rolling(2, min_periods=2).min()
    return x


def reconstruct_armed(f: pd.DataFrame, p: ArmedParams):
    """Strict offline reconstruction of the prior CONTINUE_ARMED concept.

    It requires: impulse -> controlled pullback -> volume compression ->
    reclaim near prior high + VWAP hold + volume reacceleration.
    """
    if f.empty:
        return None
    impulse_hits = np.flatnonzero((f.high_pct >= p.impulse_min_pct).to_numpy())
    if len(impulse_hits) == 0:
        return None
    impulse_i = int(impulse_hits[0])
    peak_pct = -1e9
    peak_i = None
    impulse_vol = None
    pull_i = None

    for i in range(impulse_i, len(f)):
        r = f.iloc[i]
        if r.high_pct > peak_pct:
            peak_pct = float(r.high_pct)
            peak_i = i
            # Local impulse volume benchmark around the new peak.
            lo = max(0, i - 2)
            impulse_vol = float(f.iloc[lo:i+1].volume.mean())
        dd_pp = peak_pct - float(r.low_pct)
        if peak_i is not None and i > peak_i and p.pullback_min_pp <= dd_pp <= p.pullback_max_pp:
            if not bool(r.two_below_vwap) and float(r.high_reclaim) >= p.structural_fail_reclaim:
                comp = float(r.volume / impulse_vol) if impulse_vol and impulse_vol > 0 else np.nan
                if pd.notna(comp) and comp <= p.compression_ratio_max:
                    pull_i = i
                    break
        if dd_pp > p.pullback_max_pp:
            return None
    if pull_i is None:
        return None

    reclaim_target = max(p.impulse_min_pct, peak_pct - p.reclaim_gap_pp)
    for i in range(pull_i + 1, len(f)):
        r = f.iloc[i]
        if bool(r.two_below_vwap) or float(r.high_reclaim) < p.structural_fail_reclaim:
            return None
        structural = (
            float(r.close_pct) >= reclaim_target
            and bool(r.vwap_hold)
            and float(r.high_reclaim) >= p.high_reclaim_min
        )
        vol_ok = pd.notna(r.vol_reaccel) and float(r.vol_reaccel) >= p.vol_reaccel_min
        if structural and (vol_ok or bool(r.breakout_accept)):
            return {
                "armed_i": i,
                "impulse_i": impulse_i,
                "pull_i": pull_i,
                "peak_i": peak_i,
                "peak_pct": peak_pct,
                "pullback_depth_pp": peak_pct - float(f.iloc[pull_i].low_pct),
                "armed_close_pct": float(r.close_pct),
                "armed_time": r.datetime,
                "high_reclaim": float(r.high_reclaim),
                "vol_reaccel": None if pd.isna(r.vol_reaccel) else float(r.vol_reaccel),
            }
    return None


def entry_index(f: pd.DataFrame, armed_i: int, style: str, nochase_pct: float):
    """Return completed-bar signal index; execution happens at next bar open."""
    if armed_i is None or armed_i >= len(f) - 1:
        return None
    if float(f.iloc[armed_i].close_pct) > nochase_pct:
        return None

    # 1) Immediate next-bar entry after strict CONTINUE_ARMED.
    if style == "ARMED_NEXT":
        return armed_i

    # 2) Wait for a controlled VWAP/reclaim retest within 30 minutes (10 x 3m bars).
    if style == "VWAP_RETEST":
        end = min(len(f) - 1, armed_i + 10)
        ref = float(f.iloc[armed_i].close)
        for i in range(armed_i + 1, end + 1):
            r = f.iloc[i]
            if float(r.close_pct) > nochase_pct:
                return None
            near = float(r.low) <= max(float(r.vwap), ref * 0.995) * 1.006
            reclaim = float(r.close) >= float(r.vwap) and float(r.close) >= ref * 0.995
            candle = float(r.close) >= float(r.open)
            if near and reclaim and candle and not bool(r.two_below_vwap):
                return i
        return None

    # 3) Micro BOS then retest: strongest continuation-style entry.
    if style == "MICRO_BOS_RETEST":
        bos_i = None
        end = min(len(f) - 2, armed_i + 10)
        for i in range(armed_i, end + 1):
            r = f.iloc[i]
            if pd.notna(r.prior2_high):
                vol_ok = pd.notna(r.vol_reaccel) and float(r.vol_reaccel) >= 1.25
                if float(r.close) > float(r.prior2_high) and bool(r.vwap_hold) and vol_ok:
                    bos_i = i
                    break
        if bos_i is None:
            return None
        bos_level = float(f.iloc[bos_i].prior2_high)
        for i in range(bos_i + 1, min(len(f) - 1, bos_i + 5) + 1):
            r = f.iloc[i]
            if float(r.close_pct) > nochase_pct:
                return None
            touched = float(r.low) <= bos_level * 1.006
            held = float(r.close) >= bos_level * 0.997 and float(r.close) >= float(r.vwap)
            if touched and held and not bool(r.two_below_vwap):
                return i
        return None

    raise ValueError(f"Unknown entry style: {style}")


def structural_stop_pct(f: pd.DataFrame, signal_i: int, raw_entry: float, cap: float) -> float:
    lo = max(0, signal_i - 4)
    swing = float(f.iloc[lo:signal_i+1].low.min())
    risk = max(0.8, (1.0 - swing / raw_entry) * 100.0 + 0.25)
    return min(cap, max(1.2, risk))


def simulate_trade(f: pd.DataFrame, sig_i: int, strategy: Strategy):
    """Simulate one completed-bar signal.

    LIMIT_EXTENSION is intentionally different from a fixed take-profit:
    - bank 50% at +1R,
    - keep the runner while structure survives,
    - after +18/+20/+22/+25 day-change thresholds, progressively tighten the
      structural trail,
    - never open a new position because a threshold was crossed.
    """
    if sig_i is None or sig_i + 1 >= len(f):
        return None
    raw_entry = float(f.iloc[sig_i + 1].open)
    entry = raw_entry * (1.0 + SIDE_SLIP)
    stop_pct = structural_stop_pct(f, sig_i, raw_entry, strategy.stop_pct)
    stop_px = entry * (1.0 - stop_pct / 100.0)
    tp_px = entry * (1.0 + strategy.take_pct / 100.0) if strategy.take_pct > 0 else np.inf
    sig_time = f.iloc[sig_i].datetime
    ent_time = f.iloc[sig_i + 1].datetime

    mfe = -999.0
    mae = 999.0
    realized_cash = 0.0
    remaining = 1.0
    partial_done = False
    moved_be = False
    limit_hit = False
    max_extension_stage = 0.0
    exit_px = float(f.iloc[-1].close) * (1.0 - SIDE_SLIP)
    exit_time = f.iloc[-1].datetime
    result = "EOD"

    # Partial plan: 50% at 1R; runner is managed by either fixed TP/trail
    # or the threshold-aware LIMIT_EXTENSION head.
    one_r_px = entry * (1.0 + stop_pct / 100.0)
    reclaim_floor_by_stage = {18.0: 0.960, 20.0: 0.965, 22.0: 0.970, 25.0: 0.975}

    for j in range(sig_i + 1, len(f)):
        r = f.iloc[j]
        hi = float(r.high)
        lo = float(r.low)
        cl = float(r.close)
        mfe = max(mfe, (hi / entry - 1.0) * 100.0)
        mae = min(mae, (lo / entry - 1.0) * 100.0)

        hit_s = lo <= stop_px
        hit_t = (not strategy.limit_extension) and hi >= tp_px
        hit_partial = strategy.partial and (not partial_done) and hi >= one_r_px

        # Conservative intrabar ordering. If both adverse and favorable levels
        # are touched in the same bar, assume stop first.
        if hit_s and (hit_t or hit_partial):
            realized_cash += remaining * (stop_px * (1.0 - SIDE_SLIP))
            remaining = 0.0
            exit_px = stop_px * (1.0 - SIDE_SLIP)
            exit_time = r.datetime
            result = "SL_AMBIG"
            break
        if hit_s:
            realized_cash += remaining * (stop_px * (1.0 - SIDE_SLIP))
            remaining = 0.0
            exit_px = stop_px * (1.0 - SIDE_SLIP)
            exit_time = r.datetime
            result = "SL" if not partial_done else "PARTIAL_THEN_SL"
            break

        if hit_partial:
            px = one_r_px * (1.0 - SIDE_SLIP)
            realized_cash += 0.50 * px
            remaining -= 0.50
            partial_done = True
            # Banked half permits protecting the runner around breakeven.
            stop_px = max(stop_px, entry * 1.0005)
            moved_be = True

        if hit_t:
            px = tp_px * (1.0 - SIDE_SLIP)
            realized_cash += remaining * px
            remaining = 0.0
            exit_px = px
            exit_time = r.datetime
            result = "TP" if not partial_done else "PARTIAL_TP"
            break

        # Ordinary trailing mode.
        if strategy.trail and (not strategy.limit_extension) and j >= sig_i + 3:
            if mfe >= stop_pct or partial_done:
                trail_level = max(
                    float(r.vwap),
                    float(f.iloc[max(sig_i + 1, j - 2):j + 1].low.min())
                )
                stop_px = max(stop_px, trail_level * 0.997)
                moved_be = moved_be or stop_px >= entry

        # LIMIT EXTENSION: thresholds are observation states only.
        # A threshold crossed on this completed bar can tighten the stop for
        # subsequent bars; it never creates a new entry.
        if strategy.limit_extension:
            hpct = float(r.high_pct)
            crossed = [th for th in (18.0, 20.0, 22.0, 25.0, 29.5) if hpct >= th]
            if crossed:
                max_extension_stage = max(max_extension_stage, max(crossed))
                limit_hit = limit_hit or max_extension_stage >= 29.5

            if partial_done or max_extension_stage >= 18.0:
                recent_low = float(f.iloc[max(sig_i + 1, j - 2):j + 1].low.min())
                trail_level = max(float(r.vwap), recent_low)
                # Tighten more after +22/+25 without turning those levels into
                # automatic sell prices.
                pad = 0.997
                if max_extension_stage >= 22.0:
                    pad = 0.998
                if max_extension_stage >= 25.0:
                    pad = 0.999
                stop_px = max(stop_px, trail_level * pad)
                moved_be = moved_be or stop_px >= entry

            # Threshold-aware structural giveback veto. This uses the completed
            # bar only, so no future data is used.
            active_stages = [th for th in reclaim_floor_by_stage if max_extension_stage >= th]
            if active_stages:
                st = max(active_stages)
                reclaim_floor = reclaim_floor_by_stage[st]
                if float(r.high_reclaim) < reclaim_floor and cl < float(r.vwap):
                    px = cl * (1.0 - SIDE_SLIP)
                    realized_cash += remaining * px
                    remaining = 0.0
                    exit_px = px
                    exit_time = r.datetime
                    result = f"LIMIT_EXT_STRUCT_EXIT_{int(st)}"
                    break

        # Universal structural death veto.
        if j >= sig_i + 3 and bool(r.two_below_vwap) and pd.notna(r.s5) and float(r.s5) < 0:
            px = cl * (1.0 - SIDE_SLIP)
            realized_cash += remaining * px
            remaining = 0.0
            exit_px = px
            exit_time = r.datetime
            result = "STRUCT_EXIT"
            break

    if remaining > 0:
        realized_cash += remaining * exit_px
        remaining = 0.0

    ret_pct = (realized_cash / entry - 1.0) * 100.0
    r_mult = ret_pct / stop_pct if stop_pct > 0 else np.nan
    capture = ret_pct / mfe if mfe > 0 and ret_pct > 0 else 0.0
    return {
        "signal_time": str(sig_time),
        "entry_time": str(ent_time),
        "raw_entry": raw_entry,
        "entry": entry,
        "stop_pct_used": stop_pct,
        "take_pct": strategy.take_pct,
        "exit_time": str(exit_time),
        "exit": exit_px,
        "result": result,
        "return_pct": ret_pct,
        "R": r_mult,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "capture_ratio": capture,
        "partial_done": partial_done,
        "moved_be": moved_be,
        "limit_extension": strategy.limit_extension,
        "max_extension_stage": max_extension_stage,
        "limit_hit": limit_hit,
        "cost_bps_total": COST_BPS_TOTAL,
    }

def threshold_diag(f: pd.DataFrame, start_i: int, prev_close: float):
    after = f.iloc[start_i:].copy()
    out = {}
    for th in THRESHOLDS:
        hit = np.flatnonzero((after.high_pct >= th).to_numpy())
        out[f"hit_{str(th).replace('.', '_')}"] = bool(len(hit))
        if len(hit):
            out[f"time_{str(th).replace('.', '_')}"] = str(after.iloc[int(hit[0])].datetime)
    return out


def max_drawdown_pct(returns_pct: Iterable[float]) -> float:
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in returns_pct:
        eq *= 1.0 + float(r) / 100.0
        peak = max(peak, eq)
        mdd = min(mdd, (eq / peak - 1.0) * 100.0)
    return float(mdd)


def summarize(g: pd.DataFrame):
    if g.empty:
        return {"n":0}
    rets = g.return_pct.to_numpy(float)
    rs = g.R.to_numpy(float)
    pos = rets[rets > 0].sum()
    neg = -rets[rets < 0].sum()
    return {
        "n": int(len(g)),
        "win_rate_pct": float((rets > 0).mean() * 100.0),
        "avg_return_pct": float(rets.mean()),
        "median_return_pct": float(np.median(rets)),
        "p10_return_pct": float(np.quantile(rets, 0.10)),
        "profit_factor": float(pos / neg) if neg > 0 else None,
        "avg_R": float(np.nanmean(rs)),
        "median_R": float(np.nanmedian(rs)),
        "avg_mfe_pct": float(g.mfe_pct.mean()),
        "avg_mae_pct": float(g.mae_pct.mean()),
        "median_mae_pct": float(g.mae_pct.median()),
        "capture_ratio_avg": float(g.capture_ratio.mean()),
        "stop_rate_pct": float(g.result.astype(str).str.contains("SL").mean() * 100.0),
        "max_drawdown_pct": max_drawdown_pct(g.sort_values(["date","entry_time"]).return_pct.tolist()),
    }


def chronological_folds(keys: pd.DataFrame, n_folds: int = 4):
    dates = sorted(keys.date.astype(str).unique())
    if len(dates) < 4:
        return []
    chunks = np.array_split(np.array(dates, dtype=object), min(n_folds, len(dates)))
    folds = []
    for k in range(1, len(chunks)):
        train_dates = np.concatenate(chunks[:k]).tolist()
        test_dates = chunks[k].tolist()
        folds.append((train_dates, test_dates))
    return folds


def conservative_score(s: dict) -> float:
    n = s.get("n", 0) or 0
    avg = s.get("avg_return_pct")
    pf = s.get("profit_factor")
    med = s.get("median_return_pct")
    mdd = s.get("max_drawdown_pct")
    mae = s.get("avg_mae_pct")
    if n < 5 or avg is None or pf is None:
        return -999.0
    # Shrink small samples toward zero, penalize PF<1 and deep drawdown/MAE.
    shrink = n / (n + 20.0)
    sc = avg * shrink
    sc += 0.20 * (pf - 1.0)
    sc += 0.08 * min(0.0, med or 0.0)
    sc += 0.015 * (mdd or 0.0)
    sc += 0.02 * (mae or 0.0)
    return float(sc)


def strategy_grid():
    styles = ["ARMED_NEXT", "VWAP_RETEST", "MICRO_BOS_RETEST"]
    stops = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0]
    takes = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
    for st in styles:
        for sl in stops:
            for tp in takes:
                if tp < sl * 0.8:
                    continue
                yield Strategy(st, sl, tp, partial=False, trail=False, limit_extension=False)
                if tp >= sl * 1.5:
                    yield Strategy(st, sl, tp, partial=True, trail=True, limit_extension=False)
            # One non-duplicated threshold-aware runner per stop/entry style.
            yield Strategy(st, sl, 0.0, partial=True, trail=True, limit_extension=True)

def build_cohort(root: Path, event_df: pd.DataFrame, p: ArmedParams):
    rows = []
    frames = {}
    for k, e in event_df.iterrows():
        one = load_cached_1m(root, e.date, e.code)
        if one.empty:
            continue
        f3 = features(resample(one, 3), float(e.prev_close))
        ar = reconstruct_armed(f3, p)
        if ar is None:
            continue
        key = (str(e.date), str(e.code).zfill(6))
        frames[key] = f3
        rows.append({
            "date": str(e.date), "code": str(e.code).zfill(6), "name": e.name,
            "prev_close": float(e.prev_close), "day_peak_pct": float(e.day_peak_pct),
            **ar,
            **threshold_diag(f3, ar["armed_i"], float(e.prev_close)),
        })
    return pd.DataFrame(rows), frames


def load_labeled_input(path: Path, p: ArmedParams):
    """Optional path for a native upstream file already containing continue_armed labels."""
    x = pd.read_csv(path)
    req = {"code","datetime","prev_close","open","high","low","close","volume","continue_armed"}
    miss = req - set(x.columns)
    if miss:
        raise ValueError(f"input csv missing: {sorted(miss)}")
    x["code"] = x.code.astype(str).str.zfill(6)
    x["datetime"] = pd.to_datetime(x.datetime)
    x["date"] = x.datetime.dt.date.astype(str)
    cohort = []
    frames = {}
    for (code,date), d in x.groupby(["code","date"], sort=False):
        f3 = features(d[["datetime","open","high","low","close","volume"]].reset_index(drop=True), float(d.prev_close.iloc[0]))
        a = d.continue_armed.astype(str).str.lower().isin(["1","true","pass","armed","continue_armed"])
        idxs = np.flatnonzero(a.to_numpy())
        if len(idxs) == 0:
            continue
        i = int(idxs[0])
        key = (str(date),str(code).zfill(6))
        frames[key] = f3
        cohort.append({
            "date":str(date),"code":str(code).zfill(6),"name":d.get("name",pd.Series([code])).iloc[0],
            "prev_close":float(d.prev_close.iloc[0]),"day_peak_pct":float(f3.high_pct.max()),
            "armed_i":i,"armed_time":f3.iloc[i].datetime,"armed_close_pct":float(f3.iloc[i].close_pct),
            "high_reclaim":float(f3.iloc[i].high_reclaim),"vol_reaccel":num(f3.iloc[i].vol_reaccel),
            "pullback_depth_pp":np.nan,
            **threshold_diag(f3,i,float(d.prev_close.iloc[0])),
        })
    return pd.DataFrame(cohort), frames


def run(args):
    p = ArmedParams()
    root = Path(args.root)
    if args.input_csv:
        cohort, frames = load_labeled_input(Path(args.input_csv), p)
        source = f"native labels: {args.input_csv}"
    else:
        ev = load_event_universe(root)
        cohort, frames = build_cohort(root, ev, p)
        source = f"reconstructed from latest SMART_REACCEL cache under {root}"

    if cohort.empty:
        raise RuntimeError("NO_CONTINUE_ARMED_COHORT")

    trades = []
    entries_cache = {}
    for _, e in cohort.iterrows():
        key = (str(e.date), str(e.code).zfill(6))
        f = frames[key]
        for style in ["ARMED_NEXT","VWAP_RETEST","MICRO_BOS_RETEST"]:
            sig_i = entry_index(f, int(e.armed_i), style, p.nochase_pct)
            entries_cache[(key,style)] = sig_i
        for s in strategy_grid():
            sig_i = entries_cache[(key,s.entry_style)]
            tr = simulate_trade(f, sig_i, s)
            if tr is None:
                continue
            trades.append({
                "date":e.date,"code":e.code,"name":e.name,
                "day_peak_pct":e.day_peak_pct,"armed_close_pct":e.armed_close_pct,
                "strategy":s.key,"entry_style":s.entry_style,"stop_cap_pct":s.stop_pct,
                "take_pct_cfg":s.take_pct,"partial":s.partial,"trail":s.trail,"limit_extension_cfg":s.limit_extension,
                **tr,
            })

    t = pd.DataFrame(trades)
    if t.empty:
        raise RuntimeError("NO_TRADES_AFTER_ENTRY_RULES")

    summaries = []
    for strategy, g in t.groupby("strategy", sort=False):
        base = summarize(g)
        summaries.append({"strategy":strategy, **base, "score":conservative_score(base)})
    sm = pd.DataFrame(summaries)

    # Chronological walk-forward. Each test block is never used to choose itself.
    wf_rows = []
    folds = chronological_folds(cohort[["date","code"]], n_folds=4)
    for fold_id, (train_dates, test_dates) in enumerate(folds, 1):
        trn = t[t.date.astype(str).isin(train_dates)]
        tst = t[t.date.astype(str).isin(test_dates)]
        cand = []
        for strategy, g in trn.groupby("strategy"):
            s = summarize(g)
            cand.append((conservative_score(s), strategy, s))
        cand.sort(reverse=True, key=lambda z:z[0])
        for rank, (_, strategy, train_s) in enumerate(cand[:5], 1):
            test_g = tst[tst.strategy == strategy]
            test_s = summarize(test_g)
            wf_rows.append({
                "fold":fold_id,"rank_in_train":rank,"strategy":strategy,
                "train_dates":f"{min(train_dates)}..{max(train_dates)}",
                "test_dates":f"{min(test_dates)}..{max(test_dates)}",
                **{f"train_{k}":v for k,v in train_s.items()},
                **{f"test_{k}":v for k,v in test_s.items()},
                "test_score":conservative_score(test_s),
            })
    wf = pd.DataFrame(wf_rows)

    # Strategy robustness: only strategies that were selected by TRAIN data get
    # credit for their subsequent TEST block. This prevents test-block leakage.
    robust = []
    for _, r in sm.iterrows():
        z = wf[wf.strategy == r.strategy] if not wf.empty else pd.DataFrame()
        valid = z[z.test_n.fillna(0) > 0].copy() if not z.empty else pd.DataFrame()
        oos_scores = valid.test_score.replace(-999.0, np.nan).dropna().tolist() if not valid.empty else []
        oos_avg = float(np.mean(oos_scores)) if oos_scores else np.nan
        oos_worst = float(np.min(oos_scores)) if oos_scores else np.nan
        oos_n_total = int(valid.test_n.fillna(0).sum()) if not valid.empty else 0
        if not valid.empty and oos_n_total > 0:
            oos_avg_return = float(
                np.average(valid.test_avg_return_pct, weights=valid.test_n)
            )
            oos_pf_median = float(valid.test_profit_factor.dropna().median()) if valid.test_profit_factor.notna().any() else np.nan
            oos_median_return = float(
                np.average(valid.test_median_return_pct, weights=valid.test_n)
            )
            oos_worst_avg_return = float(valid.test_avg_return_pct.min())
        else:
            oos_avg_return = np.nan
            oos_pf_median = np.nan
            oos_median_return = np.nan
            oos_worst_avg_return = np.nan

        # Deployment gate is intentionally hard. A full-sample winner is not
        # deployable if it cannot remain positive out-of-sample.
        deployable = bool(
            len(oos_scores) >= 2
            and oos_n_total >= 15
            and pd.notna(oos_avg_return) and oos_avg_return > 0
            and pd.notna(oos_pf_median) and oos_pf_median > 1.0
            and pd.notna(oos_worst_avg_return) and oos_worst_avg_return > -0.35
        )
        robust_score = float(
            r.score
            + (0.90 * oos_avg if pd.notna(oos_avg) else -0.35)
            + (0.10 * max(-1.0, min(1.0, oos_avg_return)) if pd.notna(oos_avg_return) else -0.10)
        )
        robust.append({
            **r.to_dict(),
            "oos_appearances":len(oos_scores),
            "oos_n_total":oos_n_total,
            "oos_score_avg":oos_avg,
            "oos_score_worst":oos_worst,
            "oos_avg_return_pct":oos_avg_return,
            "oos_median_return_pct":oos_median_return,
            "oos_pf_median":oos_pf_median,
            "oos_worst_avg_return_pct":oos_worst_avg_return,
            "deployable":deployable,
            "robust_score":robust_score,
        })
    best = pd.DataFrame(robust).sort_values(
        ["deployable","robust_score","avg_return_pct","profit_factor","median_return_pct"],
        ascending=[False,False,False,False,False]
    )
    deployable_best = best[best.deployable].copy()
    deployment_status = "READY_FOR_PAPER_TEST" if not deployable_best.empty else "NO_DEPLOY_OOS_GATE"

    # Threshold transition diagnostics for the cohort only.
    diag = []
    prev_set = set(map(tuple, cohort[["date","code"]].to_numpy()))
    prev_name = "CONTINUE_ARMED"
    for th in THRESHOLDS:
        col = f"hit_{str(th).replace('.', '_')}"
        cur_set = set(map(tuple, cohort.loc[cohort[col].astype(bool), ["date","code"]].to_numpy()))
        den = len(prev_set)
        num = len(prev_set & cur_set)
        diag.append({"from_stage":prev_name,"to_stage":f"+{th}%","from_events":den,"to_events":num,
                     "transition_rate_pct":100.0*num/den if den else np.nan})
        prev_set = prev_set & cur_set
        prev_name = f"+{th}%"
    diag = pd.DataFrame(diag)

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    cohort.to_csv(out/"cohort_events.csv",index=False,encoding="utf-8-sig")
    t.to_csv(out/"trade_grid.csv",index=False,encoding="utf-8-sig")
    sm.sort_values("score",ascending=False).to_csv(out/"strategy_summary.csv",index=False,encoding="utf-8-sig")
    wf.to_csv(out/"walkforward_summary.csv",index=False,encoding="utf-8-sig")
    best.head(50).to_csv(out/"best_strategies.csv",index=False,encoding="utf-8-sig")
    diag.to_csv(out/"threshold_diagnostics.csv",index=False,encoding="utf-8-sig")

    report = {
        "version":VERSION,"source":source,"cohort_n":int(len(cohort)),"trade_rows":int(len(t)),
        "cost_bps_total":COST_BPS_TOTAL,"armed_params":asdict(p),
        "objective":"realized-return-first; death-filter preserved; upper-limit conversion diagnostic/runner-extension only",
        "deployment_status":deployment_status,
        "cohort_integrity_notice":("Native upstream CONTINUE_ARMED labels used." if args.input_csv else "Reconstructed cohort from cached bars; do NOT call this the exact historical 41-event ledger unless native labels are supplied."),
        "best":best.head(15).replace({np.nan:None}).to_dict(orient="records"),
        "deployable_best":deployable_best.head(10).replace({np.nan:None}).to_dict(orient="records"),
        "threshold_diagnostics":diag.replace({np.nan:None}).to_dict(orient="records"),
    }
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")

    print(f"{VERSION}_READY")
    print(json.dumps({"outdir":str(out),"cohort_n":len(cohort),"trade_rows":len(t),
                      "best":report["best"][:5],"thresholds":report["threshold_diagnostics"]},
                     ensure_ascii=False,default=str))


def self_test():
    rng = pd.date_range("2026-11-25 09:00", periods=80, freq="3min", tz="Asia/Seoul")
    # Synthetic: impulse -> pullback/compression -> reclaim -> continuation.
    close = np.array([100.0]*80)
    path = [101,103,106,109,112,114,113,111.5,112.5,114,115,116,118,119,120,121]
    close[:len(path)] = path
    close[len(path):] = np.linspace(121,126,80-len(path))
    op = np.r_[100,close[:-1]]
    hi = np.maximum(op,close) * 1.003
    lo = np.minimum(op,close) * 0.997
    vol = np.array([120,150,220,300,450,500,180,150,170,260,360,420,500,520,530,540] + [200]*(80-len(path)),float)
    f = pd.DataFrame({"datetime":rng,"open":op,"high":hi,"low":lo,"close":close,"volume":vol})
    ff = features(f,100.0)
    p = ArmedParams(compression_ratio_max=0.75)
    a = reconstruct_armed(ff,p)
    assert a is not None, "armed reconstruction failed"
    for style in ["ARMED_NEXT","VWAP_RETEST","MICRO_BOS_RETEST"]:
        i = entry_index(ff,a["armed_i"],style,p.nochase_pct)
        if i is not None:
            s = Strategy(style,3.0,5.0,partial=True,trail=True)
            tr = simulate_trade(ff,i,s)
            assert tr is not None
    print(json.dumps({"self_test":"PASS","armed":a},ensure_ascii=False,default=str,indent=2))



def _autorun():
    """Optional same-service one-shot autorun.

    Read-only consumer of the SMART_REACCEL persistent result/cache. It never
    calls broker APIs and never changes upstream files or live lane state.
    """
    if str(os.getenv("RETURN_OPT_AUTORUN", "false")).strip().lower() not in {"1","true","yes","on"}:
        return
    outdir = Path(os.getenv(
        "RETURN_OPT_OUTDIR",
        "/data/results/continue_armed_return_optimizer_v3_1"
    ))
    marker = outdir / f"AUTORUN_{VERSION}_DONE.json"

    def job():
        wait_s = max(5, int(os.getenv("RETURN_OPT_WAIT_SECONDS", "30")))
        max_checks = max(1, int(os.getenv("RETURN_OPT_MAX_CHECKS", "120")))
        root = Path(os.getenv("RETURN_OPT_ROOT", str(DEFAULT_ROOT)))
        last_error = None
        for _ in range(max_checks):
            try:
                # Require a completed upstream result directory and its cache.
                latest = find_latest_result_dir(root)
                if marker.exists():
                    try:
                        prior = json.loads(marker.read_text(encoding="utf-8"))
                        if str(prior.get("source_result_dir")) == str(latest):
                            print("RETURN_OPT_AUTORUN_SKIP " + json.dumps(prior, ensure_ascii=False), flush=True)
                            return
                    except Exception:
                        pass
                if not (root / "bar_cache_1m").exists():
                    raise FileNotFoundError(f"bar cache missing under {root}")
                args = SimpleNamespace(
                    root=str(root),
                    input_csv=None,
                    outdir=str(outdir),
                )
                run(args)
                outdir.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({
                    "version": VERSION,
                    "source_result_dir": str(latest),
                    "completed_at": pd.Timestamp.now(tz="Asia/Seoul").isoformat(),
                    "status": "DONE",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"RETURN_OPT_AUTORUN_DONE {marker}", flush=True)
                return
            except Exception as e:
                last_error = f"{type(e).__name__}:{e}"
                # The upstream backtest may still be building its 1m cache.
                time.sleep(wait_s)
        print(f"RETURN_OPT_AUTORUN_ERROR {last_error}", flush=True)

    threading.Thread(
        target=job,
        daemon=True,
        name="continue-armed-return-opt-v31",
    ).start()



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root",default=str(DEFAULT_ROOT))
    ap.add_argument("--input-csv",default=None,help="optional native continue_armed labeled CSV")
    ap.add_argument("--outdir",default="/data/results/continue_armed_return_optimizer_v3")
    ap.add_argument("--self-test",action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        run(args)


if __name__ != "__main__":
    _autorun()

if __name__ == "__main__":
    main()
