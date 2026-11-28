"""NXT lane repair R8.

Read-only projection over the existing KIS NXT authority rows, native NXT
minute-series features, and the already cached KRX completed daily history.
It never opens a broker session, changes KIS/LS collectors, or writes their
source caches.

Goals:
- make every dashboard lane meaningful on market=NXT;
- use the KIS NXT authority universe, not the broader LS runtime universe;
- keep KRX daily bars only as explicitly labelled reference history;
- persist one bounded EOD snapshot and retain it after 20:00 / over weekends;
- never manufacture BUY: projected rows remain WATCH/ARMED/PENDING and preserve
  NO_CHASE / risk gates.
"""
from __future__ import annotations

import json, math, os, re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "NXT_LANE_REPAIR_R8_20261128"
SUPPORTED = (
    "NXT_POST_ACCEL_LANE", "V17_ACTION_PRE", "ASYMMETRIC_RS",
    "CAPITAL_HANDOFF", "RS_HANDOFF", "HVTB_V1_6",
    "RE_EXPLOSION_25", "LIMIT_ATTACK",
    "RS_DAILY_DISCOVERY", "ROTATION_DAILY_PROXY",
)
_APPLIED = False


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _median(values):
    z = sorted(x for x in values if x is not None)
    if not z:
        return None
    n = len(z)
    return z[n // 2] if n % 2 else (z[n // 2 - 1] + z[n // 2]) / 2


def _common(row):
    typ = str(row.get("security_type") or row.get("instrument_type") or "").upper()
    name = str(row.get("name") or "")
    if typ in {"ETF","ETN","PREFERRED","SPAC","REIT","우선주","스팩","리츠"}:
        return False
    if re.search(r"(?:스팩|리츠)$", name) or re.search(r"(?:[0-9]+우B?|우B)$", name):
        return False
    if re.match(r"^(?:KODEX|TIGER|ACE|RISE|SOL|PLUS|HANARO|KOSEF|KINDEX|KBSTAR|ARIRANG|TIMEFOLIO)\s+", name, re.I):
        return False
    return True if typ in {"COMMON","COMMON_STOCK","보통주"} else None


def _session_active(e, current):
    if current.weekday() >= 5:
        return False
    hm = current.hour * 60 + current.minute
    start = 1000 if current.date().isoformat() == getattr(e, "SPECIAL_SESSION_DATE", "") else 930
    return start <= hm < 1200


def _snapshot_path(self):
    return Path(self.store.path).parent / "nxt_lane_r8_snapshot.json"


def _read_snapshot(self):
    z = getattr(self, "_nxt_r8_snapshot", None)
    if isinstance(z, dict):
        return z
    try:
        z = json.loads(_snapshot_path(self).read_text(encoding="utf-8"))
        if isinstance(z, dict) and z.get("version") == VERSION:
            self._nxt_r8_snapshot = z
            return z
    except Exception:
        pass
    return None


def _save_snapshot(self, snap):
    p = _snapshot_path(self)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, p)
    self._nxt_r8_snapshot = snap


def _alpha_map(self, e):
    obj = self.local("alpha", "NXT")
    out = {}
    if isinstance(obj, dict):
        for key in ("sector_live_pool","live_pool","top_watch","top_final"):
            for z in obj.get(key, []) or []:
                if isinstance(z, dict):
                    c = e.code(z)
                    if e.valid_code(c):
                        out[c] = z
    return out


def _base(e, row, lane, current, reference_day=None):
    c = e.code(row)
    price = _num(e.pick(row, "price", "current_price"))
    ch = _num(e.pick(row, "change_rate", "change_pct"))
    at = e.pick(row, "source_timestamp", "price_asof")
    cap = _num(row.get("market_cap_100m"))
    return {
        "code": c, "name": row.get("name") or c, "market": "NXT", "venue": "NXT",
        "lane": lane, "price": price, "change_pct": ch,
        "volume": _num(row.get("volume")), "turnover": _num(row.get("turnover")),
        "price_asof": at, "signal_asof": at,
        "timestamp_quality": e.pick(row, "source_timestamp_quality", "timestamp_quality"),
        "quote_quality": e.pick(row, "quality", "quote_quality"),
        "market_cap_100m": cap, "common_stock_confirmed": _common(row),
        "classification": "WATCH", "decision": "PENDING", "data_status": "PENDING",
        "selection_path": [lane, "NXT_R8"], "upstream_state": "WATCH",
        "candidate_lifecycle": "CONFIRMED_THIS_CYCLE",
        "live_board_state": "WATCH", "entry_phase": "NXT 독립판정 · 진입조건 확인 대기",
        "news_gate": "CLEAR", "no_chase": bool(ch is not None and ch > 5),
        "history_asof": reference_day, "history_expected_asof": reference_day,
        "history_reference_source": "KRX_COMPLETED_DAILY_REFERENCE_FOR_NXT_EXECUTION",
        "nxt_authority_source": "KIS_NXT_AUTHORITY",
        "nxt_r8_version": VERSION,
        "gate_failures": [], "gate_pending": ["ENTRY_CONFIRM","RR_GE_2"],
        "gates": {"ENTRY_CONFIRM":"PENDING","RR_GE_2":"PENDING"},
        "promotion_conditions": ["NXT 실시간 구조 재확인","실측 RR>=2","NO_CHASE 통과"],
        "invalidation_conditions": ["시장 veto","NO_CHASE","지지 이탈"],
    }


def _closed_bars(hist, code, day):
    bars = [z for z in (hist.get(code, []) or []) if isinstance(z, dict)]
    if day:
        bars = [z for z in bars if str(z.get("date") or "") <= day]
    return bars[-260:]


def _daily_metrics(hist, codes, day):
    tmp = {}
    r5s, r20s = [], []
    for c in codes:
        b = _closed_bars(hist, c, day)
        closes = [_num(z.get("close")) for z in b]
        if len(closes) < 21 or any(x is None or x <= 0 for x in closes[-21:]):
            continue
        r5 = (closes[-1] / closes[-6] - 1) * 100
        r20 = (closes[-1] / closes[-21] - 1) * 100
        turns = [_num(z.get("turnover")) for z in b[-10:]]
        tv = None
        if len(turns) == 10 and all(x is not None and x >= 0 for x in turns) and sum(turns[:5]) > 0:
            tv = sum(turns[5:]) / sum(turns[:5])
        tmp[c] = {"ret5":r5,"ret20":r20,"turnover_ratio":tv,"bars":b}
        r5s.append(r5); r20s.append(r20)
    m5, m20 = _median(r5s), _median(r20s)
    for z in tmp.values():
        z["rs5"] = z["ret5"] - m5 if m5 is not None else None
        z["rs20"] = z["ret20"] - m20 if m20 is not None else None
        z["rs_score"] = (z["rs5"] or 0) + .5 * (z["rs20"] or 0)
    return tmp


def _limit_state(ch, f):
    if ch is None or ch < 7:
        return None
    bars = _num(f.get("nxt_bar_count")) or 0
    hold = _num(f.get("nxt_high_hold60"))
    reclaim = _num(f.get("nxt_reclaim_ratio"))
    accel = _num(f.get("nxt_volume_accel15"))
    turn = _num(f.get("nxt_turnover_accel15"))
    positive = _num(f.get("nxt_price15_pct"))
    structural = bars >= 30 and hold is not None and hold >= .97 and reclaim is not None and reclaim >= .97
    reaccel = structural and positive is not None and positive > 0 and (
        (accel is not None and accel >= 1.2) or (turn is not None and turn >= 1.2)
    )
    if not reaccel:
        return "LIMIT_WATCH"
    if ch >= 20 and hold >= .99:
        return "LIMIT_ATTACK"
    if ch >= 18:
        return "LIMIT_ATTACK_ARMED"
    return "CONTINUE_ARMED"


def _build_snapshot(self, e, current, recovery=False):
    rows, manifest, complete, source_path, errors = self.source("NXT")
    if not isinstance(rows, list) or not rows:
        return None
    if not isinstance(manifest, dict) or manifest.get("status") != "READY":
        return None

    hist, _, _ = self.history.read_all()
    day = None
    try:
        day = e.last_closed_session(current)
    except Exception:
        pass
    if not day:
        rid = str(manifest.get("round_id") or "")
        raw = rid[:8]
        day = raw[:4] + "-" + raw[4:6] + "-" + raw[6:] if len(raw)==8 and raw.isdigit() else None

    qmap = {e.code(r):r for r in rows if isinstance(r,dict) and e.valid_code(e.code(r))}
    codes = list(qmap)
    daily = _daily_metrics(hist, codes, day)
    alpha = _alpha_map(self, e)
    nxt_obj = self.local("nxt", rows, current)
    nxtrows = {e.code(z):z for z in (nxt_obj.get("rows",[]) if isinstance(nxt_obj,dict) else [])
               if isinstance(z,dict) and e.valid_code(e.code(z))}

    lanes = {k:[] for k in SUPPORTED if k != "RS_HANDOFF"}

    # NXT post-acceleration: native minute-series only.
    for c, f in nxtrows.items():
        r = qmap.get(c)
        if not r:
            continue
        ch = _num(e.pick(r,"change_rate","change_pct"))
        bars = _num(f.get("nxt_bar_count")) or 0
        if bars < 20 or ch is None or ch < 2.5:
            continue
        z = _base(e,r,"NXT_POST_ACCEL_LANE",current,day)
        impulse = f.get("nxt_impulse_seen") is True
        recompression = f.get("nxt_recompression_seen") is True
        reclaim = _num(f.get("nxt_reclaim_ratio"))
        accel = _num(f.get("nxt_volume_accel15"))
        turn = _num(f.get("nxt_turnover_accel15"))
        positive = _num(f.get("nxt_price15_pct"))
        hh = _num(f.get("nxt_high_hold60"))
        reaccel = bool(reclaim is not None and reclaim>=.98 and positive is not None and positive>0 and
                       ((accel is not None and accel>=1.5) or (turn is not None and turn>=1.4)))
        highhold = bool(hh is not None and hh>=.985)
        no_chase = ch >= 10
        cls = "REJECT" if no_chase else ("ARMED" if all((impulse,recompression,reaccel,highhold)) else "WATCH")
        z.update(classification=cls,live_board_state=cls,upstream_state="NXT_NATIVE_SESSION",
                 selection_path=["NXT_POST_ACCEL_LANE","NXT_NATIVE_SERIES_R8"],
                 impulse_seen=impulse,recompression_seen=recompression,reaccel_seen=reaccel,
                 high_hold60_confirmed=highhold,nxt_bar_count=bars,nxt_high_hold60=hh,
                 nxt_reclaim_ratio=reclaim,nxt_volume_accel15=accel,nxt_turnover_accel15=turn,
                 no_chase=no_chase,gate_failures=["NO_CHASE"] if no_chase else [],
                 gate_pending=[] if cls=="ARMED" else ["NXT_FULL_SEQUENCE"],
                 score=(4*sum((impulse,recompression,reaccel,highhold)) + min(ch,10)))
        lanes["NXT_POST_ACCEL_LANE"].append(z)

    # V1.7: same completed daily signal logic, intersected with KIS NXT authority.
    v17 = self.local("v17_all", rows, hist, current)
    for s in (v17.get("candidates",[]) if isinstance(v17,dict) else []):
        c=e.code(s); r=qmap.get(c)
        if not r:
            continue
        z=_base(e,r,"V17_ACTION_PRE",current,day)
        st=str(e.pick(s,"status","pre_status") or "WATCH")
        cls="ARMED" if s.get("chart_actionable") is True or "ACTION" in st else ("PRE_EXPLOSION_ARMED" if "PRE" in st else "WATCH")
        if (_num(z.get("change_pct")) or 0)>5:
            cls="REJECT"
        z.update(classification=cls,live_board_state=cls,upstream_state=st,
                 selection_path=["V17_ACTION_PRE","KRX_DAILY_SIGNAL_NXT_EXECUTION_R8"],
                 signal_reference_date=e.pick(s,"signal_reference_date","signal_history_date"),
                 gate_failures=["NO_CHASE"] if cls=="REJECT" else [],
                 gate_pending=["NXT_ENTRY_CONFIRM","RR_GE_2"])
        lanes["V17_ACTION_PRE"].append(z)

    # Asymmetric RS / handoff from completed daily history + NXT authority.
    for c,m in daily.items():
        r=qmap[c]; ch=_num(e.pick(r,"change_rate","change_pct")); cap=_num(r.get("market_cap_100m"))
        if cap is not None and cap<700:
            continue
        if _common(r) is False or ch is None:
            continue
        if m.get("rs5") is not None and m.get("rs20") is not None and m["rs5"]>0 and m["rs20"]>0:
            z=_base(e,r,"ASYMMETRIC_RS",current,day)
            cls="REJECT" if ch>5 else "WATCH"
            z.update(classification=cls,live_board_state=cls,rs5=m["rs5"],rs20=m["rs20"],
                     upstream_state="NXT_DAILY_RS_PROXY",selection_path=["ASYMMETRIC_RS","NXT_AUTHORITY_DAILY_RS_R8"],
                     proxy_score=m["rs_score"],turnover_ratio_5d=m.get("turnover_ratio"))
            lanes["ASYMMETRIC_RS"].append(z)
        a=alpha.get(c,{})
        handoff=a.get("capital_handoff_confirmed") is True or (
            m.get("turnover_ratio") is not None and m["turnover_ratio"]>=1.3 and (m.get("rs5") or 0)>0
        )
        if handoff:
            z=_base(e,r,"CAPITAL_HANDOFF",current,day)
            cls="REJECT" if ch>5 else "WATCH"
            z.update(classification=cls,live_board_state=cls,rs5=m.get("rs5"),rs20=m.get("rs20"),
                     turnover_ratio_5d=m.get("turnover_ratio"),upstream_state="NXT_HANDOFF_PROXY",
                     selection_path=["CAPITAL_HANDOFF","NXT_AUTHORITY_TURNOVER_RS_R8"])
            lanes["CAPITAL_HANDOFF"].append(z)

    # HVTB NXT equivalent: native minute structure + completed daily reference.
    for c,f in nxtrows.items():
        r=qmap.get(c); m=daily.get(c)
        if not r or not m:
            continue
        ch=_num(e.pick(r,"change_rate","change_pct")); bars=_num(f.get("nxt_bar_count")) or 0
        if ch is None or ch<1 or bars<30:
            continue
        hh=_num(f.get("nxt_high_hold60")); reclaim=_num(f.get("nxt_reclaim_ratio"))
        accel=_num(f.get("nxt_volume_accel15")); turn=_num(f.get("nxt_turnover_accel15"))
        pos=_num(f.get("nxt_price15_pct"))
        structure=hh is not None and hh>=.97 and reclaim is not None and reclaim>=.97 and pos is not None and pos>0
        energy=structure and ((accel is not None and accel>=1.15) or (turn is not None and turn>=1.15))
        if not energy:
            continue
        z=_base(e,r,"HVTB_V1_6",current,day)
        cls="REJECT" if ch>10 else ("ARMED" if ch<=5 and hh>=.985 else "WATCH")
        z.update(classification=cls,live_board_state=cls,upstream_state="HVTB_NXT_SESSION_PROXY",
                 selection_path=["HVTB_V1_6","NXT_SESSION_STRUCTURE_R8"],hvtb_state=cls,
                 hvtb_action_state="ACTION_WAIT" if cls!="REJECT" else "NO_CHASE",
                 hvtb_reason="NXT_HIGH_HOLD_RECLAIM_REACCEL",nxt_high_hold60=hh,
                 nxt_reclaim_ratio=reclaim,nxt_volume_accel15=accel,nxt_turnover_accel15=turn,
                 rs5=m.get("rs5"),rs20=m.get("rs20"))
        lanes["HVTB_V1_6"].append(z)

    # 25% spike lifecycle from completed KRX daily event history, current NXT execution quote.
    recent = e.recent_sessions(day,5) if day else None
    if recent and len(recent)==5:
        for c,r in qmap.items():
            b=_closed_bars(hist,c,day)
            if len(b)<2:
                continue
            try:
                ev=e.spike_events(b,recent)
            except Exception:
                ev=[]
            if not ev:
                continue
            s=e.qualify_re25_event(ev[-1],r)
            if not s.get("re25_candidate_visible"):
                continue
            z=_base(e,r,"RE_EXPLOSION_25",current,day)
            state=s.get("re25_setup_state") or "WATCH"
            cls="REJECT" if state=="RED_NO_CHASE" else "WATCH"
            z.update({k:v for k,v in s.items() if k not in ("code","name","market","venue")})
            z.update(classification=cls,live_board_state="REARM_MONITOR" if cls=="REJECT" else "WATCH",
                     upstream_state=state,selection_path=["RE_EXPLOSION_25","KRX_DAILY_EVENT_NXT_EXECUTION_R8"],
                     history_reference_source="KRX_25SPIKE_DAILY_EVENT_FOR_NXT_EXECUTION")
            lanes["RE_EXPLOSION_25"].append(z)

    # LIMIT_ATTACK NXT equivalent from native after-session structure.
    for c,f in nxtrows.items():
        r=qmap.get(c)
        if not r:
            continue
        ch=_num(e.pick(r,"change_rate","change_pct"))
        state=_limit_state(ch,f)
        if not state:
            continue
        z=_base(e,r,"LIMIT_ATTACK",current,day)
        hh=_num(f.get("nxt_high_hold60"))
        accel=_num(f.get("nxt_volume_accel15")); turn=_num(f.get("nxt_turnover_accel15"))
        threshold=max([x for x in (18,20,22,25) if ch is not None and ch>=x],default=None)
        no_chase=bool(ch is not None and ch>22)
        z.update(classification="ARMED" if state in ("CONTINUE_ARMED","LIMIT_ATTACK_ARMED","LIMIT_ATTACK") else "WATCH",
                 live_board_state=state,upstream_state=state,selection_path=["LIMIT_ATTACK","NXT_SESSION_LIMIT_PROXY_R8"],
                 limit_attack_state=state,limit_attack_threshold=threshold,
                 limit_attack_high_reclaim=hh,
                 limit_attack_reaccel_ratio=max([x for x in (accel,turn) if x is not None],default=None),
                 limit_attack_entry_decision="NO_CHASE_PREDICTION_ONLY" if no_chase else "ENTRY_REVIEW_NOT_AUTO_BUY",
                 no_chase=no_chase,decision="PREDICTION_ONLY")
        lanes["LIMIT_ATTACK"].append(z)

    # Deterministic compact ranking.
    for lane, items in lanes.items():
        if lane=="NXT_POST_ACCEL_LANE":
            items.sort(key=lambda z:(z.get("classification")=="ARMED",z.get("score") or 0,z.get("change_pct") or -999),reverse=True)
        elif lane=="LIMIT_ATTACK":
            order={"LIMIT_ATTACK":4,"LIMIT_ATTACK_ARMED":3,"CONTINUE_ARMED":2,"LIMIT_WATCH":1}
            items.sort(key=lambda z:(order.get(z.get("limit_attack_state"),0),z.get("change_pct") or -999),reverse=True)
        elif lane=="ASYMMETRIC_RS":
            items.sort(key=lambda z:z.get("proxy_score") or -999,reverse=True)
        else:
            items.sort(key=lambda z:(z.get("classification") in ("ARMED","PRE_EXPLOSION_ARMED"),z.get("change_pct") or -999),reverse=True)
        lanes[lane]=items[:80]

    rid=str(manifest.get("round_id") or "")
    trade_date=rid[:8]
    if len(trade_date)==8 and trade_date.isdigit():
        trade_date=trade_date[:4]+"-"+trade_date[4:6]+"-"+trade_date[6:]
    snap={
        "version":VERSION,"status":"READY","market":"NXT","captured_at":e.stamp(current),
        "trade_date":trade_date,
        "mode":"RECOVERED_EOD" if recovery else "LIVE_NXT",
        "source_path":source_path,"source_errors":errors,"authority_total":len(qmap),
        "authority_complete":bool(complete),"manifest":manifest,"reference_daily":day,
        "lanes":lanes,
    }
    _save_snapshot(self,snap)
    return snap


def _lane_rows(snap,lane):
    lanes=snap.get("lanes") or {}
    if lane=="RS_DAILY_DISCOVERY":
        return [dict(x, lane="RS_DAILY_DISCOVERY",
                     selection_path=["RS_DAILY_DISCOVERY","NXT_AUTHORITY_DAILY_RS_R8"])
                for x in (lanes.get("ASYMMETRIC_RS") or [])]
    if lane=="ROTATION_DAILY_PROXY":
        return [dict(x, lane="ROTATION_DAILY_PROXY",
                     selection_path=["ROTATION_DAILY_PROXY","NXT_AUTHORITY_TURNOVER_RS_R8"])
                for x in (lanes.get("CAPITAL_HANDOFF") or [])]
    if lane=="RS_HANDOFF":
        rows=list(lanes.get("ASYMMETRIC_RS") or [])+list(lanes.get("CAPITAL_HANDOFF") or [])
        rows.sort(key=lambda z:(z.get("classification")!="REJECT",z.get("proxy_score") or 0,z.get("change_pct") or -999),reverse=True)
        return rows
    return list(lanes.get(lane) or [])


def apply(e):
    global _APPLIED
    if _APPLIED:
        return VERSION

    if "HVTB_V1_6" not in e.VIEW_LANES:
        e.VIEW_LANES=tuple(dict.fromkeys((*e.VIEW_LANES,"HVTB_V1_6")))
    if "LIMIT_ATTACK" not in e.VIEW_LANES:
        e.VIEW_LANES=tuple(dict.fromkeys((*e.VIEW_LANES,"LIMIT_ATTACK")))

    original_scan=e.Operations.scan
    original_api=e.Operations.api

    def scan_r8(self,current=None):
        result=original_scan(self,current)
        if not isinstance(result,dict) or result.get("status")=="BUSY":
            return result
        t=current or e.now()
        try:
            if _session_active(e,t):
                _build_snapshot(self,e,t,False)
            elif _read_snapshot(self) is None:
                # Weekend/pre-open recovery from the latest immutable NXT READY snapshot.
                _build_snapshot(self,e,t,True)
        except Exception as exc:
            self.runtime["nxt_r8_error"]=type(exc).__name__
        snap=_read_snapshot(self)
        self.runtime["nxt_r8"]={
            "version":VERSION,"status":"READY" if isinstance(snap,dict) else "PENDING",
            "mode":snap.get("mode") if isinstance(snap,dict) else "UNAVAILABLE",
            "captured_at":snap.get("captured_at") if isinstance(snap,dict) else None,
            "authority_total":snap.get("authority_total") if isinstance(snap,dict) else 0,
        }
        return result

    def api_r8(self,raw_path):
        u=urlparse(raw_path); q=parse_qs(u.query)
        market=(q.get("market") or ["KRX"])[0]
        lane=(q.get("lane") or [None])[0]
        kind=u.path.rsplit("/",1)[-1]
        if market=="NXT" and kind=="board" and lane in SUPPORTED:
            snap=_read_snapshot(self)
            if snap is None:
                try:
                    snap=_build_snapshot(self,e,e.now(),True)
                except Exception:
                    snap=None
            if isinstance(snap,dict):
                try:
                    limit=max(20,min(10000,int((q.get("limit") or ["500"])[0])))
                except ValueError:
                    limit=500
                try:
                    offset=max(0,int((q.get("offset") or ["0"])[0]))
                except ValueError:
                    offset=0
                rows=_lane_rows(snap,lane)
                total=len(rows); selected=rows[offset:offset+limit]
                retained=not _session_active(e,e.now())
                return 200,e.clean({
                    "market":"NXT","rows":selected,"total":total,"offset":offset,
                    "status":"READY","scan":{
                        "status":"READY","nxt_independent":True,"nxt_r8_version":VERSION,
                        "nxt_r8_mode":"EOD_RETAINED" if retained else snap.get("mode"),
                        "scan_at":snap.get("captured_at"),"rows_scanned":snap.get("authority_total"),
                        "manifest":snap.get("manifest"),"source_errors":snap.get("source_errors"),
                        "lanes":{lane:{
                            "observed":total,"status":"READY",
                            "prime":sum(x.get("classification")=="PRIME" for x in rows),
                            "evaluated_scope":snap.get("authority_total"),
                            "universe_complete":snap.get("authority_complete")
                        }},
                    },
                    "candidate_refresh_at":snap.get("captured_at"),
                    "state_refresh_at":snap.get("captured_at"),
                    "version":getattr(e,"VERSION",VERSION),"nxt_r8_version":VERSION,
                    "focus_total":sum(x.get("classification")!="REJECT" for x in rows),
                    "monitor_total":sum(x.get("classification")=="REJECT" for x in rows),
                    "registry_total":total,
                    "monitor_rows":[x for x in rows if x.get("classification")=="REJECT"][:limit],
                    "research_total":0,"research_rows":[],
                    "pool_status":"CURRENT_FOCUS" if total else "NO_CURRENT_FOCUS",
                    "nxt_eod_retained":retained,
                    "nxt_eod_trade_date":snap.get("trade_date"),
                    "nxt_authority_total":snap.get("authority_total"),
                })
        return original_api(self,raw_path)

    e.Operations.scan=scan_r8
    e.Operations.api=api_r8
    e.NXT_LANE_REPAIR_R8_VERSION=VERSION
    _APPLIED=True
    return VERSION


def patch_dashboard_file(path):
    p=Path(path); s=p.read_text(encoding="utf-8")
    if "NXT_LANE_REPAIR_R8_UI" in s:
        return "ALREADY"

    # Remove every legacy auto-switch that forced NXT back to the post-accel tab.
    s=re.sub(
        r"if\(market==='NXT'&&(?:tab==='V17_ACTION_PRE'|\[[^\]]+\]\.includes\(tab\))\)tab='NXT_POST_ACCEL_LANE';",
        "", s
    )
    s=s.replace("if(market!=='KRX')return '<strong>HVTB는 KRX 전용</strong>';","")
    s=s.replace("if(market!=='KRX')return '<strong>LIMIT_ATTACK은 KRX 실시간 강세주 전용입니다.</strong>';","")
    s=s.replace("</script></body></html>","// NXT_LANE_REPAIR_R8_UI\n</script></body></html>",1)
    if "NXT_LANE_REPAIR_R8_UI" not in s:
        raise RuntimeError("NXT_R8_UI_MARKER_MISSING")
    p.write_text(s,encoding="utf-8")
    return "PATCHED"


def selftest():
    class E:
        SPECIAL_SESSION_DATE="2026-11-19"

    fri=datetime.fromisoformat("2026-11-27T16:00:00+09:00")
    sat=datetime.fromisoformat("2026-11-28T10:00:00+09:00")
    assert _session_active(E,fri) is True
    assert _session_active(E,sat) is False
    f={"nxt_bar_count":60,"nxt_high_hold60":.99,"nxt_reclaim_ratio":.99,
       "nxt_volume_accel15":1.6,"nxt_price15_pct":1}
    assert _limit_state(8,f)=="CONTINUE_ARMED"
    f2=dict(f,nxt_high_hold60=.995)
    assert _limit_state(20,f2)=="LIMIT_ATTACK"
    assert set(SUPPORTED)>={"V17_ACTION_PRE","HVTB_V1_6","RE_EXPLOSION_25","LIMIT_ATTACK","NXT_POST_ACCEL_LANE","RS_DAILY_DISCOVERY","ROTATION_DAILY_PROXY"}
    return "NXT_LANE_REPAIR_R8_SELFTEST_PASS"
