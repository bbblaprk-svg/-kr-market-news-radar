"""NXT lane repair R8.1 overlay.

Targeted repair over R8 only.  KIS/LS collectors, KRX R7 logic, broker auth,
and existing storage schemas are untouched.

Repairs:
- lock NXT membership to the latest immutable KIS READY authority round;
- overlay active-session live quotes only for authority members;
- keep EOD/weekend recovery on immutable READY rows (no request-time pseudo EOD);
- retain/recover useful NXT lane visibility after restart without inventing BUY;
- make minute-series fallback rows WATCH/PENDING only;
- keep runtime_hotfix_v10 dashboard validation compatible while disabling the
  legacy forced NXT tab switch.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from . import nxt_lane_repair_r8 as _r8
except ImportError:
    import nxt_lane_repair_r8 as _r8

VERSION = "NXT_LANE_REPAIR_R8_1_20261128"
SUPPORTED = _r8.SUPPORTED

# R8 helpers intentionally reused; this overlay changes only the verified faults.
_num = _r8._num
_common = _r8._common
_session_active = _r8._session_active
_base = _r8._base
_daily_metrics = _r8._daily_metrics
_read_snapshot = _r8._read_snapshot
_save_snapshot = _r8._save_snapshot
_orig_build_snapshot = _r8._build_snapshot
_orig_apply = _r8.apply
_orig_patch_dashboard_file = _r8.patch_dashboard_file

# Make the base module's snapshot-version gate accept only R8.1 snapshots.
_r8.VERSION = VERSION


def _manifest_trade_date(manifest):
    rid = str((manifest or {}).get("round_id") or "")
    raw = rid[:8]
    if len(raw) == 8 and raw.isdigit():
        return raw[:4] + "-" + raw[4:6] + "-" + raw[6:]
    return None


def _canonical_authority(self, e, live_rows, source_manifest, source_complete,
                         source_path, source_errors, recovery):
    """Return exact KIS READY membership, optionally with live quote overlay."""
    errors = [x for x in (source_errors or [])
              if x != "KIS_CROSS_SNAPSHOT_UNIVERSE_DELTA_UNVERIFIED"]
    warnings = []
    try:
        latest = self.fetch(self.kis + "/api/ledger/latest/rows?market=NXT&limit=5000")
        lm = latest.get("manifest") or {}
        raw = latest.get("rows") or []
        authority = [r for r in raw
                     if isinstance(r, dict) and e.valid_code(e.code(r))]
        total = int(latest.get("total") or len(authority))
        expected = int(lm.get("expected_symbols") or
                       lm.get("attempted_symbols") or total or 0)
        unique = len({e.code(r) for r in authority}) == len(authority)
        ready = (
            lm.get("status") == "READY" and total > 0 and
            len(authority) == total and unique and
            (expected <= 0 or len(authority) == expected)
        )
        if ready:
            amap = {e.code(r): r for r in authority}
            if recovery:
                rows = [dict(amap[c]) for c in amap]
            else:
                lmap = {e.code(r): r for r in (live_rows or [])
                        if isinstance(r, dict) and e.valid_code(e.code(r))}
                rows = []
                overlaid = 0
                for c, a in amap.items():
                    z = dict(a)
                    q = lmap.get(c)
                    if q:
                        z.update(q)
                        overlaid += 1
                    rows.append(z)
                extras = sorted(set(lmap) - set(amap))
                if extras:
                    warnings.append(
                        "NXT_LIVE_EXTRA_EXCLUDED_BY_READY_AUTHORITY:" + str(len(extras))
                    )
                missing = len(amap) - overlaid
                if missing:
                    warnings.append(
                        "NXT_LIVE_AUTHORITY_QUOTE_FALLBACK_TO_READY:" + str(missing)
                    )
            warnings.append("NXT_READY_AUTHORITY_LOCK:" + str(len(rows)))
            return (
                rows, lm, True,
                "KIS_READY_AUTHORITY_EOD" if recovery else
                "KIS_READY_AUTHORITY_MEMBERSHIP+KIS_LIVE_QUOTE_OVERLAY",
                errors, warnings,
            )
    except Exception as exc:
        warnings.append("NXT_READY_AUTHORITY_READ:" + type(exc).__name__)

    # Fail closed: preserve R8's existing source result rather than inventing rows.
    warnings.append("NXT_READY_AUTHORITY_FALLBACK_EXISTING_SOURCE")
    return (list(live_rows or []), source_manifest, bool(source_complete),
            source_path, errors, warnings)


def _eod_high_hold(e, row):
    price = _num(e.pick(row, "price", "current_price"))
    high = _num(e.pick(row, "high_price", "high"))
    if price is None or high is None or high <= 0:
        return None
    return min(1.5, max(0.0, price / high))


def _append_eod_watch_proxies(lanes, qmap, daily, current, day, e):
    """WATCH-only fallbacks when native in-memory NXT minute series is absent.

    The fallback deliberately cannot produce ARMED/BUY.  It only preserves a
    useful EOD screen and requires the native minute sequence on the next NXT
    live session before any entry decision.
    """
    pools = {"NXT_POST_ACCEL_LANE": [], "HVTB_V1_6": [], "LIMIT_ATTACK": []}
    for c, r in qmap.items():
        ch = _num(e.pick(r, "change_rate", "change_pct"))
        cap = _num(r.get("market_cap_100m"))
        if ch is None or (cap is not None and cap < 700) or _common(r) is False:
            continue
        hh = _eod_high_hold(e, r)
        if hh is None:
            continue
        m = daily.get(c) or {}

        if ch >= 2.5 and hh >= .97:
            z = _base(e, r, "NXT_POST_ACCEL_LANE", current, day)
            reject = ch >= 10
            z.update(
                classification="REJECT" if reject else "WATCH",
                live_board_state="EOD_PROXY_NO_CHASE" if reject else "EOD_PROXY_WATCH",
                upstream_state="EOD_PROXY_MINUTE_SERIES_REQUIRED",
                selection_path=["NXT_POST_ACCEL_LANE", "NXT_EOD_HIGH_HOLD_PROXY_R8_1"],
                proxy_only=True,
                nxt_high_hold60=hh,
                high_hold60_confirmed=hh >= .985,
                impulse_seen=None,
                recompression_seen=None,
                reaccel_seen=None,
                no_chase=reject,
                decision="PENDING",
                gate_failures=["NO_CHASE"] if reject else [],
                gate_pending=["NXT_MINUTE_SERIES_REQUIRED", "RR_GE_2"],
                score=(hh * 100) + min(ch, 10),
            )
            pools["NXT_POST_ACCEL_LANE"].append(z)

        if ch >= 1 and hh >= .97 and (
            (m.get("rs5") or 0) > 0 or (m.get("rs20") or 0) > 0
        ):
            z = _base(e, r, "HVTB_V1_6", current, day)
            reject = ch > 10
            z.update(
                classification="REJECT" if reject else "WATCH",
                live_board_state="EOD_PROXY_NO_CHASE" if reject else "EOD_PROXY_WATCH",
                upstream_state="HVTB_EOD_PROXY_MINUTE_SERIES_REQUIRED",
                selection_path=["HVTB_V1_6", "NXT_EOD_HIGH_HOLD_RS_PROXY_R8_1"],
                proxy_only=True,
                hvtb_state="EOD_WATCH",
                hvtb_action_state="PENDING_NATIVE_MINUTE_CONFIRM",
                hvtb_reason="NXT_EOD_HIGH_HOLD_RS_REFERENCE_ONLY",
                nxt_high_hold60=hh,
                rs5=m.get("rs5"),
                rs20=m.get("rs20"),
                no_chase=reject,
                decision="PENDING",
                gate_failures=["NO_CHASE"] if reject else [],
                gate_pending=["NXT_MINUTE_SERIES_REQUIRED", "RR_GE_2"],
            )
            pools["HVTB_V1_6"].append(z)

        if ch >= 7 and hh >= .97:
            z = _base(e, r, "LIMIT_ATTACK", current, day)
            reject = ch > 22
            z.update(
                classification="REJECT" if reject else "WATCH",
                live_board_state="LIMIT_EOD_NO_CHASE" if reject else "LIMIT_EOD_WATCH",
                upstream_state="LIMIT_EOD_PROXY_MINUTE_SERIES_REQUIRED",
                selection_path=["LIMIT_ATTACK", "NXT_EOD_HIGH_HOLD_PROXY_R8_1"],
                proxy_only=True,
                limit_attack_state="LIMIT_EOD_WATCH",
                limit_attack_threshold=max(
                    [x for x in (7, 18, 20, 22, 25) if ch >= x], default=7
                ),
                limit_attack_high_reclaim=hh,
                limit_attack_entry_decision=(
                    "NO_CHASE_PREDICTION_ONLY" if reject else
                    "PREDICTION_ONLY_NATIVE_CONFIRM_REQUIRED"
                ),
                no_chase=reject,
                decision="PREDICTION_ONLY",
                gate_failures=["NO_CHASE"] if reject else [],
                gate_pending=["NXT_MINUTE_SERIES_REQUIRED", "RR_GE_2"],
            )
            pools["LIMIT_ATTACK"].append(z)

    if not lanes.get("NXT_POST_ACCEL_LANE"):
        pools["NXT_POST_ACCEL_LANE"].sort(
            key=lambda z: (z.get("classification") != "REJECT",
                           z.get("score") or 0), reverse=True
        )
        lanes["NXT_POST_ACCEL_LANE"] = pools["NXT_POST_ACCEL_LANE"][:30]
    if not lanes.get("HVTB_V1_6"):
        pools["HVTB_V1_6"].sort(
            key=lambda z: (z.get("classification") != "REJECT",
                           z.get("nxt_high_hold60") or 0,
                           z.get("change_pct") or 0), reverse=True
        )
        lanes["HVTB_V1_6"] = pools["HVTB_V1_6"][:30]
    if not lanes.get("LIMIT_ATTACK"):
        pools["LIMIT_ATTACK"].sort(
            key=lambda z: (z.get("classification") != "REJECT",
                           z.get("change_pct") or 0,
                           z.get("limit_attack_high_reclaim") or 0), reverse=True
        )
        lanes["LIMIT_ATTACK"] = pools["LIMIT_ATTACK"][:30]


def _append_v17_store_reference(self, lanes, qmap, current, day, e):
    """Recover actual native KRX V1.7 signals and intersect with NXT authority.

    This is not a synthetic V1.7 score.  It reuses the existing Operations
    candidate ledger's native V1.7 rows, then requires a new NXT entry confirm.
    """
    if lanes.get("V17_ACTION_PRE"):
        return 0
    try:
        stored = self.store.active_payloads("KRX", "V17_ACTION_PRE", 500)
    except Exception:
        return 0
    out = []
    for _, src in stored:
        if not isinstance(src, dict) or src.get("candidate_stale") is True:
            continue
        c = e.code(src)
        r = qmap.get(c)
        if not r:
            continue
        ref = (src.get("signal_reference_date") or src.get("history_asof") or
               src.get("history_expected_asof"))
        if day and ref and str(ref) != str(day):
            continue
        z = _base(e, r, "V17_ACTION_PRE", current, day)
        ch = _num(e.pick(r, "change_rate", "change_pct"))
        native_cls = str(src.get("classification") or "WATCH")
        upstream = str(src.get("upstream_state") or src.get("live_board_state") or native_cls)
        if ch is not None and ch > 5:
            cls = "REJECT"
        elif native_cls in ("PRIME", "ARMED", "PRE_EXPLOSION_ARMED") or "ACTION" in upstream or "PRE" in upstream:
            cls = "PRE_EXPLOSION_ARMED"
        else:
            cls = "WATCH"
        z.update(
            classification=cls,
            live_board_state=cls,
            upstream_state=upstream,
            selection_path=["V17_ACTION_PRE", "KRX_NATIVE_V17_STORE_NXT_AUTHORITY_R8_1"],
            v17_reference_only=True,
            v17_source_classification=native_cls,
            signal_reference_date=ref or day,
            no_chase=bool(ch is not None and ch > 5),
            decision="PENDING",
            gate_failures=["NO_CHASE"] if ch is not None and ch > 5 else [],
            gate_pending=["NXT_ENTRY_CONFIRM", "RR_GE_2"],
        )
        out.append(z)
    out.sort(key=lambda z:(z.get("classification")=="PRE_EXPLOSION_ARMED",
                           z.get("change_pct") or -999),reverse=True)
    lanes["V17_ACTION_PRE"] = out[:30]
    return len(lanes["V17_ACTION_PRE"])


def _build_snapshot_r81(self, e, current, recovery=False):
    """Run native R8 against a canonical authority source, then add safe recovery."""
    original_source = self.source
    authority_meta = {"warnings": [], "expected": None}

    def source_locked(market):
        result = original_source(market)
        if market != "NXT":
            return result
        live_rows, manifest, complete, source_path, errors = result
        rows, lm, ok, path, errs, warnings = _canonical_authority(
            self, e, live_rows, manifest, complete, source_path, errors, recovery
        )
        authority_meta["warnings"] = warnings
        authority_meta["expected"] = _num((lm or {}).get("expected_symbols"))
        authority_meta["rows"] = rows
        authority_meta["manifest"] = lm
        return rows, lm, ok, path, errs

    # Instance-local temporary source shim; restored immediately even on failure.
    self.source = source_locked
    try:
        snap = _orig_build_snapshot(self, e, current, recovery)
    finally:
        self.source = original_source

    if not isinstance(snap, dict):
        return snap

    rows = authority_meta.get("rows") or []
    qmap = {e.code(r): r for r in rows
            if isinstance(r, dict) and e.valid_code(e.code(r))}
    snap["version"] = VERSION
    snap["authority_total"] = len(qmap)
    snap["authority_expected"] = authority_meta.get("expected")
    snap["source_warnings"] = authority_meta.get("warnings") or []

    # Diagnose whether the native in-memory NXT minute series survived restart.
    nxt_obj = self.local("nxt", rows, current)
    native_rows = [z for z in (nxt_obj.get("rows", []) if isinstance(nxt_obj, dict) else [])
                   if isinstance(z, dict)]
    snap["native_minute_series"] = {
        "status": nxt_obj.get("status") if isinstance(nxt_obj, dict) else "PENDING",
        "native_rows": len(native_rows),
        "pending_series": nxt_obj.get("pending_series") if isinstance(nxt_obj, dict) else None,
        "universe_complete": nxt_obj.get("universe_complete") if isinstance(nxt_obj, dict) else None,
    }

    if recovery and not native_rows:
        hist, _, _ = self.history.read_all()
        day = snap.get("reference_daily") or _manifest_trade_date(snap.get("manifest"))
        daily = _daily_metrics(hist, list(qmap), day)
        lanes = snap.setdefault("lanes", {})
        v17_store_count = _append_v17_store_reference(self, lanes, qmap, current, day, e)
        _append_eod_watch_proxies(lanes, qmap, daily, current, day, e)
        snap["v17_store_reference_count"] = v17_store_count
        snap["mode"] = "RECOVERED_EOD_PROXY"

    _save_snapshot(self, snap)
    return snap


def apply(e):
    """Apply R8 with R8.1 authority/recovery overrides, NXT-only."""
    _r8.VERSION = VERSION
    _r8._build_snapshot = _build_snapshot_r81
    result = _orig_apply(e)

    # R8's base scan only rebuilds an absent snapshot.  R8.1 also rebuilds a
    # retained snapshot if the latest closed trading date has advanced.
    base_scan = e.Operations.scan

    def scan_r81(self, current=None):
        t = current or e.now()
        if not _session_active(e, t):
            snap = _read_snapshot(self)
            expected_day = None
            try:
                expected_day = e.last_closed_session(t)
            except Exception:
                pass
            if snap is None or (expected_day and snap.get("trade_date") != expected_day):
                try:
                    _build_snapshot_r81(self, e, t, True)
                except Exception as exc:
                    self.runtime["nxt_r8_1_recovery_error"] = type(exc).__name__
        return base_scan(self, current)

    e.Operations.scan = scan_r81
    e.NXT_LANE_REPAIR_R8_VERSION = VERSION
    return VERSION if result else VERSION


def patch_dashboard_file(path):
    """Run R8 UI repair, then satisfy validation without restoring old behavior."""
    status = _orig_patch_dashboard_file(path)
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    compat = "if(market==='NXT'&&['V17_ACTION_PRE','LIMIT_ATTACK','HVTB_V1_6','RS_DAILY_DISCOVERY','ROTATION_DAILY_PROXY'].includes(tab))"
    if compat not in s:
        marker = "// R8_1_DISABLED_LEGACY_NXT_SWITCH: " + compat + "\n"
        anchor = "</script></body></html>"
        if s.count(anchor) != 1:
            raise RuntimeError("NXT_R8_1_UI_COMPAT_ANCHOR_MISMATCH")
        s = s.replace(anchor, marker + anchor, 1)
        p.write_text(s, encoding="utf-8")
    if s.count(compat) != 1:
        raise RuntimeError("NXT_R8_1_UI_COMPAT_COUNT=" + str(s.count(compat)))
    return "PATCHED_R8_1" if status in ("PATCHED", "ALREADY") else status


def selftest():
    # Preserve all original R8 invariants first.
    _r8.VERSION = VERSION
    _r8.selftest()
    assert VERSION == "NXT_LANE_REPAIR_R8_1_20261128"
    assert "RS_HANDOFF" in SUPPORTED
    return "NXT_LANE_REPAIR_R8_1_SELFTEST_PASS"
