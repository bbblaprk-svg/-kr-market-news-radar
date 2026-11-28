"""NXT lane repair R8.3.1 startup-safe compatibility overlay.

Purpose
-------
Preserve all R8.2 trading gates and the R8.3 visibility contract while making
deploy/restart behavior fail-closed and non-blocking.

Repairs
-------
1. Safely migrate retained EOD snapshots from R8/R8.1/R8.2/R8.3 to R8.3.1
   without network reconstruction when the snapshot passes authority checks.
2. Old/migrated native-entry rows are never allowed to remain ARMED/ACTION;
   they are retained as WATCH/PENDING until a fresh native NXT scan confirms
   >=60 minutes, RR>=2 and NO_CHASE.
3. NXT board requests never synchronously rebuild a missing snapshot. If there
   is truly no usable retained snapshot, API returns a fast PENDING response and
   the normal background Operations scan performs reconstruction.
4. candidate_pool_group visibility contract is preserved.
5. KRX scan, KIS/LS collectors, broker auth and storage schemas are untouched.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from . import nxt_lane_repair_r8_3 as _r83
except ImportError:
    import nxt_lane_repair_r8_3 as _r83

_r82 = _r83._r82
_r81 = _r82._r81
_r8 = _r81._r8

VERSION = "NXT_LANE_REPAIR_R8_3_1_20261128"
SUPPORTED = _r83.SUPPORTED

ACCEPTED_VERSIONS = {
    "NXT_LANE_REPAIR_R8_20261128",
    "NXT_LANE_REPAIR_R8_1_20261128",
    "NXT_LANE_REPAIR_R8_2_20261128",
    "NXT_LANE_REPAIR_R8_3_20261128",
    VERSION,
}
PROMOTION_CLASSES = {"ARMED", "ACTION", "PRIME", "PRE_EXPLOSION_ARMED"}
NATIVE_ENTRY_LANES = ("NXT_POST_ACCEL_LANE", "HVTB_V1_6", "LIMIT_ATTACK")
CONCRETE_LANES = _r83.CONCRETE_LANES

_orig_apply_r83 = _r83.apply
_orig_build_r83 = _r83._build_snapshot_r83
_orig_patch_dashboard_file = _r83.patch_dashboard_file


def _stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _expected_authority(snap):
    manifest = (snap or {}).get("manifest") or {}
    raw = manifest.get("expected_symbols")
    if raw in (None, "", "UNAVAILABLE"):
        raw = (snap or {}).get("authority_expected")
    try:
        return int(float(raw)) if raw is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _snapshot_is_safe(snap):
    """Accept only structurally valid NXT EOD snapshots.

    R8's original 445-row snapshot is rejected when the embedded READY manifest
    expects 444. R8.1+ 444-authority snapshots migrate immediately.
    """
    if not isinstance(snap, dict):
        return False, "NOT_DICT"
    version = str(snap.get("version") or "")
    if version not in ACCEPTED_VERSIONS:
        return False, "UNSUPPORTED_VERSION"
    if snap.get("market") not in (None, "NXT"):
        return False, "WRONG_MARKET"
    if not isinstance(snap.get("lanes"), dict):
        return False, "LANES_MISSING"
    try:
        total = int(float(snap.get("authority_total") or 0))
    except (TypeError, ValueError, OverflowError):
        total = 0
    if total <= 0:
        return False, "AUTHORITY_EMPTY"
    expected = _expected_authority(snap)
    if expected is not None and expected > 0 and total != expected:
        return False, "AUTHORITY_MISMATCH"
    manifest = snap.get("manifest") or {}
    if manifest and manifest.get("status") not in (None, "READY"):
        return False, "MANIFEST_NOT_READY"
    if not snap.get("trade_date"):
        return False, "TRADE_DATE_MISSING"
    return True, "SAFE"


def _group(row):
    return "MONITOR" if str((row or {}).get("classification") or "") == "REJECT" else "FOCUS"


def _migrate_snapshot_dict(raw):
    """Convert a safe retained snapshot to R8.3.1 without inventing entry signals."""
    ok, reason = _snapshot_is_safe(raw)
    if not ok:
        return None, reason

    old_version = str(raw.get("version") or "")
    snap = copy.deepcopy(raw)
    snap["version"] = VERSION
    snap["market"] = "NXT"
    snap["migration_from_version"] = old_version
    snap["migration_at"] = _stamp()
    snap["migration_policy"] = "RETAIN_EOD_FAIL_CLOSED_NO_NETWORK_REBUILD"
    snap["visibility_contract_version"] = VERSION

    lanes = snap.setdefault("lanes", {})
    visibility_counts = {}

    for lane in CONCRETE_LANES:
        out = []
        focus = monitor = 0
        for src in lanes.get(lane) or []:
            if not isinstance(src, dict):
                continue
            z = dict(src)

            # Any retained EOD row from a pre-R8.3.1 deployment is reference
            # evidence only. Native entry lanes cannot retain ARMED/ACTION.
            if lane in NATIVE_ENTRY_LANES:
                if str(z.get("classification") or "") in PROMOTION_CLASSES:
                    z["classification"] = "WATCH"
                if z.get("classification") != "REJECT":
                    z["live_board_state"] = "MIGRATED_EOD_WATCH"
                    z["decision"] = "PENDING"
                z["proxy_only"] = True
                z["native_entry_gate_enforced"] = True
                z["native_entry_gate_status"] = "PROXY_NOT_ELIGIBLE"
                z["native_provider_gate_status"] = "PENDING"
                gates = dict(z.get("gates") or {})
                gates["NXT_NATIVE_60M_PASS"] = "PENDING"
                gates["RR_GE_2"] = "PENDING"
                gates["NO_CHASE"] = "FAIL" if z.get("no_chase") is True else "PASS"
                z["gates"] = gates
                pending = list(z.get("gate_pending") or [])
                for item in ("NXT_NATIVE_60M_PASS", "RR_GE_2"):
                    if item not in pending:
                        pending.append(item)
                z["gate_pending"] = pending
                conditions = list(z.get("promotion_conditions") or [])
                for item in ("NXT_NATIVE_60M_PASS=PASS", "RR_GE_2=PASS", "NO_CHASE=PASS"):
                    if item not in conditions:
                        conditions.append(item)
                z["promotion_conditions"] = conditions

            group = _group(z)
            z["candidate_pool_group"] = group
            z["candidate_visibility_contract"] = "NXT_R8_3_1"
            if group == "FOCUS":
                focus += 1
            else:
                monitor += 1
            out.append(z)
        lanes[lane] = out
        visibility_counts[lane] = {
            "rows": len(out), "focus": focus, "monitor": monitor
        }

    snap["visibility_contract_counts"] = visibility_counts
    snap["mode"] = "MIGRATED_EOD_RETAINED"
    snap["startup_safe"] = True
    return snap, "MIGRATED"


def _raw_snapshot_path(self):
    return _r8._snapshot_path(self)


def _write_snapshot(self, snap):
    _r8._save_snapshot(self, snap)


def _compat_read_snapshot(self):
    """Read current or legacy retained snapshot and atomically migrate once."""
    cached = getattr(self, "_nxt_r8_snapshot", None)
    if isinstance(cached, dict):
        if cached.get("version") == VERSION:
            return cached
        migrated, _ = _migrate_snapshot_dict(cached)
        if isinstance(migrated, dict):
            _write_snapshot(self, migrated)
            return migrated

    p = _raw_snapshot_path(self)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

    if isinstance(raw, dict) and raw.get("version") == VERSION:
        self._nxt_r8_snapshot = raw
        return raw

    migrated, reason = _migrate_snapshot_dict(raw)
    if not isinstance(migrated, dict):
        setattr(self, "_nxt_r8_snapshot_reject_reason", reason)
        return None
    _write_snapshot(self, migrated)
    return migrated


def _build_snapshot_r831(self, e, current, recovery=False):
    """Delegate fresh construction to R8.3/R8.2, then stamp R8.3.1 contract."""
    snap = _orig_build_r83(self, e, current, recovery)
    if not isinstance(snap, dict):
        return snap
    snap["version"] = VERSION
    snap["startup_safe"] = True
    snap["visibility_contract_version"] = VERSION
    _r83._assign_pool_groups(snap)
    _write_snapshot(self, snap)
    return snap


def _fast_pending_board(e, self, raw_path, lane):
    """Non-blocking fail-closed response when no retained NXT snapshot exists."""
    try:
        q = parse_qs(urlparse(raw_path).query)
        limit = max(20, min(10000, int((q.get("limit") or ["500"])[0])))
        offset = max(0, int((q.get("offset") or ["0"])[0]))
    except (TypeError, ValueError):
        limit, offset = 500, 0
    reason = getattr(self, "_nxt_r8_snapshot_reject_reason", "SNAPSHOT_UNAVAILABLE")
    return 200, e.clean({
        "market": "NXT",
        "rows": [],
        "total": 0,
        "offset": offset,
        "status": "PENDING",
        "scan": {
            "status": "PENDING",
            "nxt_independent": True,
            "nxt_r8_version": VERSION,
            "nxt_r8_mode": "STARTUP_PENDING_NONBLOCKING",
            "source_errors": [reason],
            "lanes": {
                lane: {
                    "observed": 0,
                    "status": "PENDING",
                    "prime": 0,
                    "evaluated_scope": 0,
                    "universe_complete": False,
                }
            },
        },
        "version": getattr(e, "VERSION", VERSION),
        "nxt_r8_version": VERSION,
        "focus_total": 0,
        "monitor_total": 0,
        "registry_total": 0,
        "monitor_rows": [],
        "research_total": 0,
        "research_rows": [],
        "pool_status": "STARTUP_PENDING",
        "nxt_eod_retained": False,
        "nxt_eod_trade_date": None,
        "nxt_authority_total": 0,
        "startup_nonblocking": True,
        "retry_after_seconds": 15,
    })


def apply(e):
    """Install startup-safe compatibility before the full R8.3 chain is applied."""
    # Current version must be visible to every nested module before R8's base
    # closures are created.
    _r83.VERSION = VERSION
    _r82.VERSION = VERSION
    _r81.VERSION = VERSION
    _r8.VERSION = VERSION

    # Every layer captured its own _read_snapshot symbol at import time.
    # Replace all of them so no layer rejects a valid legacy retained snapshot.
    _r8._read_snapshot = _compat_read_snapshot
    _r81._read_snapshot = _compat_read_snapshot
    _r82._read_snapshot = _compat_read_snapshot
    _r83._read_snapshot = _compat_read_snapshot

    # R8.3 apply resolves this builder global at call time.
    _r83._build_snapshot_r83 = _build_snapshot_r831

    result = _orig_apply_r83(e)

    # Prewarm/migrate the retained EOD snapshot immediately when Operations is
    # constructed, before HTTP requests arrive.
    if not getattr(e.Operations, "_nxt_r831_init_wrapped", False):
        original_init = e.Operations.__init__

        def init_r831(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            snap = _compat_read_snapshot(self)
            self.runtime = dict(self.runtime or {})
            self.runtime["nxt_r8"] = {
                "version": VERSION,
                "status": "READY" if isinstance(snap, dict) else "PENDING",
                "mode": snap.get("mode") if isinstance(snap, dict) else "STARTUP_PENDING_NONBLOCKING",
                "captured_at": snap.get("captured_at") if isinstance(snap, dict) else None,
                "authority_total": snap.get("authority_total") if isinstance(snap, dict) else 0,
                "migration_from_version": snap.get("migration_from_version") if isinstance(snap, dict) else None,
                "startup_safe": True,
            }

        e.Operations.__init__ = init_r831
        e.Operations._nxt_r831_init_wrapped = True

    # Guard the NXT board before R8's older API wrapper can attempt a synchronous
    # network rebuild. KRX always falls through untouched.
    if not getattr(e.Operations, "_nxt_r831_api_wrapped", False):
        base_api = e.Operations.api

        def api_r831(self, raw_path):
            u = urlparse(raw_path)
            q = parse_qs(u.query)
            market = (q.get("market") or ["KRX"])[0]
            lane = (q.get("lane") or [None])[0]
            kind = u.path.rsplit("/", 1)[-1]

            if market == "NXT" and kind == "board" and lane in SUPPORTED:
                snap = _compat_read_snapshot(self)
                if not isinstance(snap, dict):
                    return _fast_pending_board(e, self, raw_path, lane)

            status, payload = base_api(self, raw_path)
            if kind == "status" and isinstance(payload, dict):
                snap = _compat_read_snapshot(self)
                payload = dict(payload)
                payload["nxt_r8"] = {
                    "version": VERSION,
                    "status": "READY" if isinstance(snap, dict) else "PENDING",
                    "mode": snap.get("mode") if isinstance(snap, dict) else "STARTUP_PENDING_NONBLOCKING",
                    "captured_at": snap.get("captured_at") if isinstance(snap, dict) else None,
                    "authority_total": snap.get("authority_total") if isinstance(snap, dict) else 0,
                    "migration_from_version": snap.get("migration_from_version") if isinstance(snap, dict) else None,
                    "startup_safe": True,
                }
            return status, payload

        e.Operations.api = api_r831
        e.Operations._nxt_r831_api_wrapped = True

    e.NXT_LANE_REPAIR_R8_VERSION = VERSION
    return VERSION if result else VERSION


def patch_dashboard_file(path):
    """No new visual behavior: retain the verified R8.3 NXT-only fallback."""
    status = _orig_patch_dashboard_file(path)
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    marker = "NXT_R8_3_1_STARTUP_SAFE"
    if marker not in s:
        anchor = "</script></body></html>"
        if s.count(anchor) != 1:
            raise RuntimeError("NXT_R8_3_1_UI_MARKER_ANCHOR_COUNT=" + str(s.count(anchor)))
        s = s.replace(anchor, "// " + marker + "\n" + anchor, 1)
        p.write_text(s, encoding="utf-8")
    return "PATCHED_R8_3_1"


def selftest():
    assert _r83.selftest() == "NXT_LANE_REPAIR_R8_3_SELFTEST_PASS"

    legacy = {
        "version": "NXT_LANE_REPAIR_R8_2_20261128",
        "market": "NXT",
        "trade_date": "2026-11-27",
        "authority_total": 444,
        "authority_expected": 444,
        "manifest": {"status": "READY", "expected_symbols": 444},
        "lanes": {
            "NXT_POST_ACCEL_LANE": [
                {"code": "A", "classification": "ARMED", "decision": "BUY"},
                {"code": "B", "classification": "REJECT", "no_chase": True},
            ],
            "HVTB_V1_6": [
                {"code": "C", "classification": "PRIME"},
            ],
            "LIMIT_ATTACK": [],
            "V17_ACTION_PRE": [],
            "ASYMMETRIC_RS": [],
            "CAPITAL_HANDOFF": [],
            "RE_EXPLOSION_25": [],
        },
    }
    migrated, reason = _migrate_snapshot_dict(legacy)
    assert reason == "MIGRATED"
    assert migrated["version"] == VERSION
    assert migrated["authority_total"] == 444
    a = migrated["lanes"]["NXT_POST_ACCEL_LANE"][0]
    b = migrated["lanes"]["NXT_POST_ACCEL_LANE"][1]
    c = migrated["lanes"]["HVTB_V1_6"][0]
    assert a["classification"] == "WATCH"
    assert a["decision"] == "PENDING"
    assert a["proxy_only"] is True
    assert a["native_entry_gate_status"] == "PROXY_NOT_ELIGIBLE"
    assert a["candidate_pool_group"] == "FOCUS"
    assert b["candidate_pool_group"] == "MONITOR"
    assert c["classification"] == "WATCH"

    bad = copy.deepcopy(legacy)
    bad["version"] = "NXT_LANE_REPAIR_R8_20261128"
    bad["authority_total"] = 445
    migrated_bad, why = _migrate_snapshot_dict(bad)
    assert migrated_bad is None
    assert why == "AUTHORITY_MISMATCH"

    assert VERSION == "NXT_LANE_REPAIR_R8_3_1_20261128"
    return "NXT_LANE_REPAIR_R8_3_1_SELFTEST_PASS"
