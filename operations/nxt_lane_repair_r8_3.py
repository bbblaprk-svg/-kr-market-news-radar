"""NXT lane repair R8.3 visibility-contract overlay.

R8.2 trading logic is unchanged. This overlay fixes only the API/UI contract
that caused valid NXT focus rows to be counted but hidden by the dashboard.
"""
from __future__ import annotations
from pathlib import Path

try:
    from . import nxt_lane_repair_r8_2 as _r82
except ImportError:
    import nxt_lane_repair_r8_2 as _r82

VERSION = "NXT_LANE_REPAIR_R8_3_20261128"
SUPPORTED = _r82.SUPPORTED

_read_snapshot = _r82._read_snapshot
_save_snapshot = _r82._save_snapshot
_orig_build_r82 = _r82._build_snapshot_r82
_orig_apply_r82 = _r82.apply

CONCRETE_LANES = (
    "NXT_POST_ACCEL_LANE",
    "V17_ACTION_PRE",
    "ASYMMETRIC_RS",
    "CAPITAL_HANDOFF",
    "HVTB_V1_6",
    "RE_EXPLOSION_25",
    "LIMIT_ATTACK",
)


def _pool_group(row):
    return "MONITOR" if str((row or {}).get("classification") or "") == "REJECT" else "FOCUS"


def _assign_pool_groups(snap):
    if not isinstance(snap, dict):
        return snap
    lanes = snap.setdefault("lanes", {})
    counts = {}
    for lane in CONCRETE_LANES:
        rows = []
        focus = monitor = 0
        for src in (lanes.get(lane) or []):
            z = dict(src)
            group = _pool_group(z)
            z["candidate_pool_group"] = group
            z["candidate_visibility_contract"] = "NXT_R8_3"
            if group == "FOCUS":
                focus += 1
            else:
                monitor += 1
            rows.append(z)
        lanes[lane] = rows
        counts[lane] = {"rows": len(rows), "focus": focus, "monitor": monitor}
    snap["visibility_contract_version"] = VERSION
    snap["visibility_contract_counts"] = counts
    return snap


def _build_snapshot_r83(self, e, current, recovery=False):
    snap = _orig_build_r82(self, e, current, recovery)
    if not isinstance(snap, dict):
        return snap
    snap["version"] = VERSION
    _assign_pool_groups(snap)
    _save_snapshot(self, snap)
    return snap


def apply(e):
    """Apply R8.2 unchanged, substituting only the R8.3 snapshot wrapper."""
    _r82.VERSION = VERSION
    _r82._r81.VERSION = VERSION
    _r82._r81._r8.VERSION = VERSION
    _r82._build_snapshot_r82 = _build_snapshot_r83
    result = _orig_apply_r82(e)
    e.NXT_LANE_REPAIR_R8_VERSION = VERSION
    return VERSION if result else VERSION


def patch_dashboard_file(path):
    """Keep R8.2 UI repairs and add a NXT-only visibility fallback."""
    status = _r82.patch_dashboard_file(path)
    p = Path(path)
    s = p.read_text(encoding="utf-8")

    marker = "NXT_R8_3_VISIBILITY_CONTRACT"
    if marker in s:
        return "ALREADY_R8_3"

    old = "else{source=source.filter(r=>r.candidate_pool_group==='FOCUS');}}"
    new = (
        "else{source=source.filter(r=>market==='NXT'?"
        "((r.candidate_pool_group||(r.classification==='REJECT'?'MONITOR':'FOCUS'))==='FOCUS'):"
        "r.candidate_pool_group==='FOCUS');}}"
    )
    if s.count(old) != 1:
        raise RuntimeError("NXT_R8_3_FOCUS_FILTER_ANCHOR_COUNT=" + str(s.count(old)))
    s = s.replace(old, new, 1)

    anchor = "</script></body></html>"
    if s.count(anchor) != 1:
        raise RuntimeError("NXT_R8_3_UI_MARKER_ANCHOR_COUNT=" + str(s.count(anchor)))
    s = s.replace(anchor, "// " + marker + "\n" + anchor, 1)

    if "market==='NXT'?((r.candidate_pool_group||" not in s:
        raise RuntimeError("NXT_R8_3_NXT_FALLBACK_MISSING")
    if ":r.candidate_pool_group==='FOCUS'" not in s:
        raise RuntimeError("NXT_R8_3_KRX_STRICT_FILTER_MISSING")

    p.write_text(s, encoding="utf-8")
    return "PATCHED_R8_3"


def selftest():
    assert _r82.selftest() == "NXT_LANE_REPAIR_R8_2_SELFTEST_PASS"

    snap = {
        "lanes": {
            "NXT_POST_ACCEL_LANE": [
                {"code": "A", "classification": "WATCH", "proxy_only": True},
                {"code": "B", "classification": "REJECT", "proxy_only": True},
            ],
            "HVTB_V1_6": [
                {"code": "C", "classification": "ARMED",
                 "native_entry_gate_status": "PASS"},
            ],
        }
    }
    _assign_pool_groups(snap)
    a, b = snap["lanes"]["NXT_POST_ACCEL_LANE"]
    c = snap["lanes"]["HVTB_V1_6"][0]
    assert a["candidate_pool_group"] == "FOCUS"
    assert b["candidate_pool_group"] == "MONITOR"
    assert c["candidate_pool_group"] == "FOCUS"
    assert snap["visibility_contract_counts"]["NXT_POST_ACCEL_LANE"] == {
        "rows": 2, "focus": 1, "monitor": 1
    }
    assert VERSION == "NXT_LANE_REPAIR_R8_3_20261128"
    return "NXT_LANE_REPAIR_R8_3_SELFTEST_PASS"
