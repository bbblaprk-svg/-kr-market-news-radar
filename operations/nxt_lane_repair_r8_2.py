"""NXT lane repair R8.2 safety overlay.

Adds one fail-closed trading safety layer over R8.1 only.
KIS/LS collectors, KRX R7, broker authentication, data schemas, and the R8.1
authority/EOD recovery logic are untouched.

Policy:
- 20/30-bar native structures may be visible as WATCH;
- ARMED/ACTION/PRIME is forbidden until the native NXT provider itself reports
  gate_status=PASS with >=60 bars, >=60 minutes span and >=60 recent bars;
- RR must be measured from native observed entry/stop/target and be >=2.0;
- NO_CHASE must pass;
- missing RR is PENDING, never assumed;
- EOD/weekend proxy rows remain WATCH/PENDING only.
"""
from __future__ import annotations

try:
    from . import nxt_lane_repair_r8_1 as _r81
except ImportError:
    import nxt_lane_repair_r8_1 as _r81

VERSION = "NXT_LANE_REPAIR_R8_2_20261128"
SUPPORTED = _r81.SUPPORTED

_num = _r81._num
_session_active = _r81._session_active
_read_snapshot = _r81._read_snapshot
_save_snapshot = _r81._save_snapshot
_orig_build_r81 = _r81._build_snapshot_r81
_orig_apply_r81 = _r81.apply

PROMOTION_CLASSES = {"ARMED", "ACTION", "PRIME", "PRE_EXPLOSION_ARMED"}
NATIVE_ENTRY_LANES = ("NXT_POST_ACCEL_LANE", "HVTB_V1_6", "LIMIT_ATTACK")


def _rr_from_feature(f):
    entry = _num((f or {}).get("entry_price"))
    stop = _num((f or {}).get("stop_price"))
    target = _num((f or {}).get("target_price"))
    if entry is None or stop is None or target is None:
        return None
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _native_60m_pass(f):
    f = f or {}
    bars = _num(f.get("nxt_bar_count"))
    span = _num(f.get("nxt_span_minutes"))
    recent = _num(f.get("nxt_recent60_count"))
    return bool(
        f.get("gate_status") == "PASS"
        and bars is not None and bars >= 60
        and span is not None and span >= 60
        and recent is not None and recent >= 60
    )


def _merge_unique(existing, additions):
    out = []
    for x in list(existing or []) + list(additions or []):
        if x and x not in out:
            out.append(x)
    return out


def _enforce_row(row, feature):
    """Apply the R8.2 promotion gate to one native-session lane row."""
    z = dict(row or {})
    f = feature or {}

    # EOD/weekend proxy rows are already fail-closed by R8.1.
    if z.get("proxy_only") is True:
        z["native_entry_gate_enforced"] = True
        z["native_entry_gate_status"] = "PROXY_NOT_ELIGIBLE"
        return z

    native_pass = _native_60m_pass(f)
    rr = _rr_from_feature(f)
    rr_pass = rr is not None and rr >= 2.0
    no_chase_fail = z.get("no_chase") is True

    bars = _num(f.get("nxt_bar_count"))
    span = _num(f.get("nxt_span_minutes"))
    recent = _num(f.get("nxt_recent60_count"))

    z["native_entry_gate_enforced"] = True
    z["native_provider_gate_status"] = f.get("gate_status") or "PENDING"
    z["native_bar_count"] = bars
    z["native_span_minutes"] = span
    z["native_recent60_count"] = recent
    z["native_rr"] = rr
    z["native_rr_source"] = (
        "NXT_OBSERVED_ENTRY_STOP_TARGET" if rr is not None else
        "PENDING_NATIVE_TARGET_OR_STOP"
    )

    gates = dict(z.get("gates") or {})
    gates["NXT_NATIVE_60M_PASS"] = "PASS" if native_pass else "PENDING"
    gates["RR_GE_2"] = "PASS" if rr_pass else ("PENDING" if rr is None else "FAIL")
    gates["NO_CHASE"] = "FAIL" if no_chase_fail else "PASS"
    z["gates"] = gates

    pending = list(z.get("gate_pending") or [])
    failures = list(z.get("gate_failures") or [])

    if not native_pass:
        pending = _merge_unique(pending, ["NXT_NATIVE_60M_PASS"])
    else:
        pending = [x for x in pending if x not in ("NXT_NATIVE_60M_PASS", "NXT_FULL_SEQUENCE")]

    if rr is None:
        pending = _merge_unique(pending, ["RR_GE_2"])
        failures = [x for x in failures if x != "RR_GE_2"]
    elif not rr_pass:
        pending = [x for x in pending if x != "RR_GE_2"]
        failures = _merge_unique(failures, ["RR_GE_2"])
    else:
        pending = [x for x in pending if x != "RR_GE_2"]
        failures = [x for x in failures if x != "RR_GE_2"]

    if no_chase_fail:
        failures = _merge_unique(failures, ["NO_CHASE"])
    else:
        failures = [x for x in failures if x != "NO_CHASE"]

    z["gate_pending"] = pending
    z["gate_failures"] = failures

    promotion_allowed = native_pass and rr_pass and not no_chase_fail
    z["native_entry_gate_status"] = "PASS" if promotion_allowed else "BLOCKED"

    cls = str(z.get("classification") or "WATCH")
    lane = z.get("lane")

    if no_chase_fail:
        z["classification"] = "REJECT"
        z["live_board_state"] = "NO_CHASE"
        z["decision"] = "PREDICTION_ONLY" if lane == "LIMIT_ATTACK" else "PENDING"
    elif cls in PROMOTION_CLASSES and not promotion_allowed:
        z["classification"] = "WATCH"
        z["live_board_state"] = "NATIVE_60M_RR_WAIT"
        z["decision"] = "PREDICTION_ONLY" if lane == "LIMIT_ATTACK" else "PENDING"
        if lane == "HVTB_V1_6":
            z["hvtb_state"] = "WATCH"
            z["hvtb_action_state"] = "PENDING_NATIVE_60M_RR"
        elif lane == "LIMIT_ATTACK":
            z["limit_attack_entry_decision"] = "PREDICTION_ONLY_NATIVE_60M_RR_REQUIRED"

    z["promotion_conditions"] = _merge_unique(
        z.get("promotion_conditions"),
        ["NXT_NATIVE_60M_PASS=PASS", "RR_GE_2=PASS", "NO_CHASE=PASS"],
    )
    return z


def _native_feature_map(self, e, current):
    """Read current native NXT features for exact KIS READY members only."""
    try:
        live_rows, _, _, _, _ = self.source("NXT")
        latest = self.fetch(self.kis + "/api/ledger/latest/rows?market=NXT&limit=5000")
        authority_codes = {
            e.code(r) for r in (latest.get("rows") or [])
            if isinstance(r, dict) and e.valid_code(e.code(r))
        }
        rows = [
            r for r in (live_rows or [])
            if isinstance(r, dict) and e.code(r) in authority_codes
        ]
        obj = self.local("nxt", rows, current)
        fmap = {
            e.code(f): f for f in (obj.get("rows", []) if isinstance(obj, dict) else [])
            if isinstance(f, dict) and e.valid_code(e.code(f))
        }
        return fmap, {
            "status": obj.get("status") if isinstance(obj, dict) else "PENDING",
            "authority_scope": len(authority_codes),
            "native_feature_rows": len(fmap),
            "pending_series": obj.get("pending_series") if isinstance(obj, dict) else None,
        }
    except Exception as exc:
        return {}, {
            "status": "PENDING",
            "error": type(exc).__name__,
            "authority_scope": 0,
            "native_feature_rows": 0,
        }


def _build_snapshot_r82(self, e, current, recovery=False):
    snap = _orig_build_r81(self, e, current, recovery)
    if not isinstance(snap, dict):
        return snap

    snap["version"] = VERSION
    snap["native_entry_gate_policy"] = (
        "WATCH_EARLY__PROMOTION_REQUIRES_NATIVE_60M_PASS_AND_RR_GE_2_AND_NO_CHASE"
    )

    # Recovery rows are proxy-only and must remain non-promotable.
    if recovery:
        for lane in NATIVE_ENTRY_LANES:
            snap.setdefault("lanes", {})[lane] = [
                _enforce_row(z, None) for z in (snap.get("lanes", {}).get(lane) or [])
            ]
        snap["native_entry_gate_counts"] = {
            lane: {
                "rows": len(snap.get("lanes", {}).get(lane) or []),
                "pass": 0,
                "blocked": len(snap.get("lanes", {}).get(lane) or []),
                "proxy": len(snap.get("lanes", {}).get(lane) or []),
            }
            for lane in NATIVE_ENTRY_LANES
        }
        _save_snapshot(self, snap)
        return snap

    fmap, diag = _native_feature_map(self, e, current)
    snap["native_entry_gate_diagnostics"] = diag
    counts = {}

    for lane in NATIVE_ENTRY_LANES:
        new_rows = []
        passed = blocked = proxy = 0
        for row in (snap.get("lanes", {}).get(lane) or []):
            c = e.code(row)
            z = _enforce_row(row, fmap.get(c))
            new_rows.append(z)
            if z.get("proxy_only") is True:
                proxy += 1
            elif z.get("native_entry_gate_status") == "PASS":
                passed += 1
            else:
                blocked += 1
        snap.setdefault("lanes", {})[lane] = new_rows
        counts[lane] = {
            "rows": len(new_rows),
            "pass": passed,
            "blocked": blocked,
            "proxy": proxy,
        }

    snap["native_entry_gate_counts"] = counts
    _save_snapshot(self, snap)
    return snap


def apply(e):
    """Apply R8.1, replacing only its snapshot builder with the R8.2 gate."""
    _r81.VERSION = VERSION
    _r81._r8.VERSION = VERSION
    _r81._build_snapshot_r81 = _build_snapshot_r82
    result = _orig_apply_r81(e)
    e.NXT_LANE_REPAIR_R8_VERSION = VERSION
    return VERSION if result else VERSION


def patch_dashboard_file(path):
    # No visual behavior change in R8.2; reuse verified R8.1 UI patch.
    return _r81.patch_dashboard_file(path)


def selftest():
    # Pure gate tests: early ARMED is downgraded; full 60m+2R survives.
    early = {
        "lane": "HVTB_V1_6", "classification": "ARMED",
        "live_board_state": "ARMED", "decision": "PENDING",
        "no_chase": False, "gate_pending": [], "gate_failures": [],
    }
    f30 = {
        "gate_status": "PENDING", "nxt_bar_count": 30,
        "nxt_span_minutes": 30, "nxt_recent60_count": 30,
        "entry_price": 100, "stop_price": 95, "target_price": 112,
    }
    a = _enforce_row(early, f30)
    assert a["classification"] == "WATCH"
    assert a["native_entry_gate_status"] == "BLOCKED"
    assert "NXT_NATIVE_60M_PASS" in a["gate_pending"]

    f60 = {
        "gate_status": "PASS", "nxt_bar_count": 60,
        "nxt_span_minutes": 60, "nxt_recent60_count": 60,
        "entry_price": 100, "stop_price": 95, "target_price": 112,
    }
    b = _enforce_row(early, f60)
    assert b["classification"] == "ARMED"
    assert b["native_entry_gate_status"] == "PASS"
    assert b["native_rr"] >= 2.0

    low_rr = dict(f60, target_price=108)
    c = _enforce_row(early, low_rr)
    assert c["classification"] == "WATCH"
    assert "RR_GE_2" in c["gate_failures"]

    chase = dict(early, no_chase=True)
    d = _enforce_row(chase, f60)
    assert d["classification"] == "REJECT"
    assert "NO_CHASE" in d["gate_failures"]

    proxy = dict(early, proxy_only=True, classification="WATCH")
    p = _enforce_row(proxy, None)
    assert p["classification"] == "WATCH"
    assert p["native_entry_gate_status"] == "PROXY_NOT_ELIGIBLE"

    assert VERSION == "NXT_LANE_REPAIR_R8_2_20261128"
    return "NXT_LANE_REPAIR_R8_2_SELFTEST_PASS"
