"""Operations runtime hotfix V10.

Purpose:
- Use the most recently CLOSED KRX daily session while an intraday KIS READY
  manifest belongs to the current session.
- Ignore an unclosed current-day LS daily candle in operational daily-history
  evaluation.
- Preserve request-time provenance; require independent LS evidence for entry.
- Infer market-session openness from the KST schedule when the source row does
  not carry a session_open flag.
- Keep exact cross-snapshot membership audit; verify common codes individually.
- Apply identical current entry risk gates before storage and display.
- Rank candidates across the bounded active lane before display pagination.
- Keep independent RS/rotation observations separate from actual investor flow.
- Tighten bounded Operations DB maintenance to prevent persistent-disk growth.

This module does not place orders, mutate broker caches, or change KIS/LS auth.
"""
from __future__ import annotations

import os
from datetime import time as dtime

REPAIR_VERSION = "OPS_V14_LANE_REPAIR_20261124"

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



def _cap_ceiling():
    """Configurable upper cap in 억원; V12 supplemental-lane default is retained."""
    try:
        return max(700, int(os.getenv("OPS_MID_SMALL_CAP_MAX_100M", "30000")))
    except (ValueError, TypeError):
        return 30000


def _finite_number(v):
    from math import isfinite
    if isinstance(v, bool):return None
    try:
        x=float(v)
        return x if isfinite(x) else None
    except (TypeError,ValueError,OverflowError):return None


def _entry_location(row):
    """Evaluate the currently available price without rewriting historical signal entry."""
    p,s,t=(_finite_number(row.get(k)) for k in ("price","stop_price","target_price"))
    if any(x is None for x in (p,s,t)):
        return None,None,None
    if not 0<s<p<t:
        return False,None,None
    rr=(t-p)/(p-s);risk=(p-s)/p
    return rr>=2 and risk<=.06,rr,risk


def _chase_limit(row):
    # Existing independent after-session NXT lane is allowed to observe 2.5--10%;
    # explicit NO_CHASE, session, retest, actual RR, and all other gates remain vetoes.
    return 10.0 if row.get("lane")=="NXT_POST_ACCEL_LANE" and row.get("market")=="NXT" else 5.0


def _chase_ok(row):
    ch=_finite_number(row.get("change_pct"))
    if row.get("no_chase") is True or row.get("news_gate")=="PRICED_IN":return False
    if row.get("runtime_no_chase") is True and _chase_limit(row)!=10.0:return False
    if ch is None:return None
    return ch<_chase_limit(row) if _chase_limit(row)==10.0 else ch<=5.0


def _entry_policy(row):
    """One entry-risk contract for both stored decisions and live presentation."""
    cap=_finite_number(row.get("market_cap_100m"));location,rr,risk=_entry_location(row)
    row["live_rr"]=rr;row["live_risk_pct"]=risk*100 if risk is not None else None
    checks={"CAP_MAX": None if cap is None else 700<=cap<=_cap_ceiling(),
            "LIVE_LOCATION_2R_MAX6":location,"NO_CHASE":_chase_ok(row)}
    if row.get("lane")=="RE_EXPLOSION_25":
        age=_finite_number(row.get("spike_age")); spike=_finite_number(row.get("spike_high_pct"))
        checks["RE25_EVENT_VALID"]=False if row.get("fast_new_spike") is True or row.get("re25_event_only") is True else None if age is None or spike is None else bool(age.is_integer() and 1<=age<=5 and spike>=25)
    return checks


def _explicit_exclusion(name):
    # A name is used only for negative filtering, never as positive common-stock proof.
    import re
    n = str(name or "").strip()
    return bool(n == "맥쿼리인프라" or re.search(r"(?:스팩|리츠)$", n)
                or re.search(r"(?:[0-9]+우B?|우B)$", n)
                or re.match(r"^(?:KODEX|TIGER|ACE|RISE|SOL|PLUS|HANARO|KOSEF|KINDEX|KBSTAR|ARIRANG|TIMEFOLIO)\s+", n, re.I)
                or re.search(r"(?:\sET[FN]| ETN)$", n, re.I))


def _hvtb_authority_adapter(row):
    """Read-only aliases matching Operations.base's actual KIS row contract."""
    r = dict(row)
    for dst, src in (("price_asof", "source_timestamp"), ("change_pct", "change_rate")):
        if r.get(dst) is None:
            r[dst] = r.get(src)
    typ = str(r.get("security_type") or r.get("instrument_type") or "").upper()
    if typ in ("ETF", "ETN", "PREFERRED", "SPAC", "REIT", "우선주", "스팩", "리츠") or _explicit_exclusion(r.get("name")):
        r["common_stock_confirmed"] = False
    elif not isinstance(r.get("common_stock_confirmed"),bool):
        r["common_stock_confirmed"] = True if typ in ("COMMON", "COMMON_STOCK", "보통주") else None
    return r


def _corroborated_rest(row,current,fresh):
    """Two independent price observations; request time is still NOT exchange time."""
    from math import isfinite
    if row.get("quote_quality")!="REST_CURRENT" or row.get("timestamp_quality") not in ("REQUEST_TIMESTAMP","REQUEST_TIME_ONLY","REQUEST_TIME"):
        return False
    if not fresh(row.get("price_asof"),current) or row.get("ls_crosscheck") is not True:
        return False
    p,lp=row.get("price"),row.get("ls_price")
    if not all(isinstance(v,(float,int)) and not isinstance(v,bool) and isfinite(v) and v>0 for v in (p,lp)) or abs(lp/p-1)>.005:
        return False
    if row.get("lane")=="HVTB_V1_6" and row.get("ls_5m_status")=="READY" and row.get("hvtb_action_state")=="ACTION":
        return fresh(row.get("ls_asof"),current,420) and fresh(row.get("signal_asof"),current,600)
    return row.get("ls_timestamp_quality")=="EXCHANGE_TIMESTAMP" and fresh(row.get("ls_asof"),current)


def _reclassify_after_verified_reference(row):
    """Upgrade only the independently corroborated price observation gate."""
    g=row.get("gates") or {}
    if g.get("DATA_FRESH")!="FAIL":return row
    g["DATA_FRESH"]="PASS"
    return _reclassify_gates(row)


def _reclassify_gates(row):
    """Recompute classification from the complete, explicitly recorded gate set."""
    g=row.get("gates") or {}
    fail=[k for k,v in g.items() if v=="FAIL"]
    pending=[k for k,v in g.items() if v=="PENDING"]
    hard={"MARKET_STATE","NEWS","NO_CHASE","SECURITY_COMMON","CAP_700","CAP_MAX","RR_GE_2","LIVE_LOCATION_2R_MAX6","RE25_EVENT_VALID"}
    if hard.intersection(fail):cl="REJECT"
    elif not fail and not pending:cl="PRIME"
    elif row.get("upstream_state")=="PRE_EXPLOSION_ARMED" and all(g.get(k)=="PASS" for k in ("DATA_FRESH","SIGNAL_FRESH","SESSION")) and not pending and fail==["ENTRY_CONFIRM"]:cl="PRE_EXPLOSION_ARMED"
    elif all(g.get(k)=="PASS" for k in ("LANE_SETUP","DATA_FRESH","SIGNAL_FRESH","SESSION")) and not pending and set(fail)<={"ENTRY_CONFIRM"}:cl="ARMED"
    else:cl="WATCH"
    exp=("ENTRY_CONFIRMED" if cl=="PRIME" else "REARMED_ENTRY_WINDOW" if row.get("re25_runtime_state")=="REARMED" else "ALREADY_EXPANDED_NO_CHASE" if g.get("NO_CHASE")=="FAIL" or row.get("fast_new_spike") is True or row.get("re25_already_expanded") is True else "PRE_ENTRY_SETUP" if cl in ("ARMED","PRE_EXPLOSION_ARMED") else "PENDING")
    pending_decision=bool(pending or set(fail).intersection({"DATA_FRESH","SIGNAL_FRESH","SESSION","NXT_INDEPENDENT_SESSION","KRX_DAILY_CURRENT","KRX_UNIVERSE_MATCH","KRX_AUTHORITY_SYMBOL"}))
    pending_data=bool(pending or set(fail).intersection({"DATA_FRESH","SIGNAL_FRESH","KRX_DAILY_CURRENT","KRX_UNIVERSE_MATCH","KRX_AUTHORITY_SYMBOL"}))
    row.update(classification=cl,decision="PENDING" if pending_decision else cl,data_status="PENDING" if pending_data else "READY",expansion_phase=exp,gate_failures=fail,gate_pending=pending,promotion_conditions=[k+"=PASS" for k,v in g.items() if v!="PASS"],entry_phase="진입 확인 신호" if cl=="PRIME" else "이미 급등 · 추격금지" if g.get("NO_CHASE")=="FAIL" or row.get("fast_new_spike") else "진입 전 · 조건 대기")
    return row


def _hvtb_project_row(row, meta, current, fresh, session_open):
    """Do not expose a stale persisted ACTION as a current entry signal."""
    if row.get("hvtb_action_state") != "ACTION":
        return row
    p, stop, target = (row.get(k) for k in ("price", "stop_price", "target_price"))
    valid = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (p, stop, target)) and 0 < stop < p < target
    rr = (target-p)/(p-stop) if valid else None
    if (meta.get("status") not in ("READY","PENDING","PARTIAL_VERIFIED") or meta.get("error") or row.get("authority_symbol_membership_verified") is not True or not fresh(meta.get("generated_at"), current, 600)
        or not fresh(row.get("signal_asof"), current, 600) or not fresh(row.get("price_asof"), current)
        or row.get("classification") != "PRIME" or row.get("decision") != "PRIME"
        or row.get("runtime_no_chase") is True or row.get("candidate_lifecycle") == "DATA_HOLD"
        or row.get("runtime_quote_grade") == "STALE" or not session_open("KRX", current, None)
        or not valid or rr < 2 or (p-stop)/p > .06):
        row.update(hvtb_action_state="ACTION_WAIT", hvtb_action_reason="LIVE_RECONFIRM_REQUIRED", classification="WATCH", decision="PENDING", runtime_lane_state="DATA_HOLD", entry_phase="진입 전 · 재검증 대기", expansion_phase="PENDING")
    return row


def _hvtb_patch_dashboard(html):
    """Exact anchors from deployed operations/dashboard.html; fail before writing if layout differs."""
    marker = "HVTB_OPERATIONS_UI_20261124"
    if marker in html:
        anchor="['HVTB_V1_6','HVTB 독립 발굴'],['news'"
        if html.count(anchor)==1:return html.replace(anchor,"['HVTB_V1_6','HVTB 독립 발굴'],['RS_DAILY_DISCOVERY','RS 독립 관찰'],['ROTATION_DAILY_PROXY','자금이양 관찰'],['news'",1)
        return html
    edits = [
        ("['RE_EXPLOSION_25','재급등 후보'],['news'", "['RE_EXPLOSION_25','재급등 후보'],['HVTB_V1_6','HVTB 독립 발굴'],['RS_DAILY_DISCOVERY','RS 독립 관찰'],['ROTATION_DAILY_PROXY','자금이양 관찰'],['news'"),
        ("id==='NXT_POST_ACCEL_LANE'?'<small>독립</small>':''", "['NXT_POST_ACCEL_LANE','HVTB_V1_6'].includes(id)?'<small>독립</small>':''"),
        ("let shown=[],latestScan={};", "let shown=[],latestScan={},hvtbMeta={}; // HVTB_OPERATIONS_UI_20261124"),
        ("function emptyStateMessage(){", "function emptyStateMessage(){\n if(tab==='HVTB_V1_6'){if(market!=='KRX')return '<strong>HVTB는 KRX 전용</strong>';return '<strong>'+esc(hvtbMeta.status||'PENDING')+' · '+esc(hvtbMeta.error||'표시 조건 충족 후보 없음')+'</strong><div class=\"cellnote\">평가 '+esc(hvtbMeta.evaluated_scope??'미확인')+'종목 · 후보 '+esc(hvtbMeta.observed??'미확인')+'건</div>';}"),
        ("badge(r.runtime_lane_state||r.classification)", "badge(tab==='HVTB_V1_6'?(r.hvtb_action_state||r.hvtb_state||r.classification):(r.live_board_state||r.runtime_lane_state||r.classification))"),
        ("esc((r.selection_path||[]).join(' → '))+'</div></td><td>'", "esc((r.selection_path||[]).join(' → '))+(tab==='HVTB_V1_6'?'<div class=\"cellnote\">'+esc(r.hvtb_discovery_origin)+' · '+esc((r.hvtb_paths||[]).join(', '))+' · '+esc(r.hvtb_reason)+'</div>':'')+'</div></td><td>'"),
        ("esc(r.expansion_phase||'PENDING')+'</div></td>'", "esc(r.expansion_phase||'PENDING')+(tab==='HVTB_V1_6'?'<div class=\"cellnote\">'+esc(r.hvtb_action_reason)+'</div>':'')+'</div></td>'"),
        ("tab==='RE_EXPLOSION_25'?'후보 풀은", "tab==='HVTB_V1_6'?'KRX 독립 발굴 · ORIGINAL / CHALLENGER / BOTH · '+esc(hvtbMeta.status||'PENDING')+' · 전체 탐색 '+esc(hvtbMeta.discovery_evaluated_scope??hvtbMeta.evaluated_scope??'미확인')+'종목 · 현재 검증 '+esc(hvtbMeta.reconfirmed_scope??'미확인')+'건 · 최종 ACTION '+esc(hvtbMeta.action??'미확인')+'건 · '+esc(hvtbMeta.error||'현재가 손익비 및 필수 게이트 확인') :tab==='RE_EXPLOSION_25'?'후보 풀은"),
        ("r.timestamp_quality==='EXCHANGE_TIMESTAMP'?'거래소 시각 확인 · 필수 게이트 별도':'KIS REST 참고가 · 체결시각 미검증'", "r.timestamp_quality==='EXCHANGE_TIMESTAMP'?'거래소 시각 확인 · 필수 게이트 별도':r.data_fresh_basis==='KIS_REST_PLUS_INDEPENDENT_LS_OBSERVATION'?'KIS REST + LS 관측 교차확인 · 체결시각 아님':'KIS REST 참고가 · 체결시각 미검증'"),
        ("source=source.filter(r=>(!search", "if(!['news','events','outcomes'].includes(tab)&&!$('allRows').checked&&!search)source=source.filter(r=>!['EXCLUDED_SECURITY','EVENT_ONLY'].includes(r.live_board_state)&&(tab==='RE_EXPLOSION_25'||r.live_board_state!=='NO_CHASE'));source=source.filter(r=>(!search"),
        ("latestScan=scan;$('scanTime')", "latestScan=scan;hvtbMeta=b.value.hvtb_lane_status||{};$('scanTime')"),
        ("if(results[1]?.status==='fulfilled')", "if(b.value.provider_diagnostics){const q=b.value.provider_diagnostics;const bad=Object.entries(q).filter(([k,v])=>v.error||v.status==='PENDING');if(bad.length)$('notice').textContent+=' · 공급자 확인: '+bad.map(([k,v])=>k+':'+(v.error||v.status)).join(', ');}if(b.value.runtime_error){$('notice').textContent+=' · 실행 오류 '+esc(b.value.runtime_error_context)+' / '+esc(b.value.runtime_error)+' / '+when(b.value.runtime_error_at);$('notice').className='notice error';}if(results[1]?.status==='fulfilled')"),
        ("if(market==='NXT'&&tab==='V17_ACTION_PRE')", "if(market==='NXT'&&['V17_ACTION_PRE','HVTB_V1_6','RS_DAILY_DISCOVERY','ROTATION_DAILY_PROXY'].includes(tab))"),
        ("if(t){tab=t.dataset.tab;loaded=200;render();load(true)}", "if(t){tab=t.dataset.tab;loaded=200;board=[];render();load(true)}"),
    ]
    for old,new in edits:
        if html.count(old)!=1:
            raise ValueError("HVTB_UI_ANCHOR_MISMATCH: "+old[:75])
        html=html.replace(old,new,1)
    return html


def _hvtb_install_dashboard():
    from pathlib import Path
    from tempfile import NamedTemporaryFile
    path = Path(__file__).resolve().with_name("dashboard.html")
    old = path.read_text(encoding="utf-8")
    new = _hvtb_patch_dashboard(old)
    if new == old:
        return "READY_ALREADY_PATCHED"
    tmp = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            tmp = Path(f.name)
            f.write(new)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    return "READY_HVTB_TAB"


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
        if tc.is_snapshot_session(current.date().isoformat()) is not True:
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
                             ((480 <= hm < 530) or (540.5 <= hm < 920) or (940 <= hm < 1200)) if market == "NXT" else False)
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
        # A partial global universe never proves a missing security. A separate exact
        # per-symbol intersection can prove identity without asserting full coverage.
        if row.get("market") == "KRX" and "authority_symbol_membership_verified" in row:
            row["authority_universe_consistent"] = row["authority_symbol_membership_verified"]
        if _explicit_exclusion(row.get("name")):
            row["common_stock_confirmed"] = False
        tq,qq = row.get("timestamp_quality"),row.get("quote_quality")
        rest_current=(qq == "REST_CURRENT" and tq in ("REQUEST_TIMESTAMP","REQUEST_TIME_ONLY","REQUEST_TIME")
                      and e.fresh(row.get("price_asof"),current))
        # Preserve request timestamp provenance. A fresh request price alone is not a trade.
        # Independent completed HVTB bars may be evaluated, but do not invent exchange time.
        out=original_core(row,current)
        if rest_current:
            out["data_fresh_basis"]="KIS_REST_REQUEST_TIME_REFERENCE_ONLY"
            if _corroborated_rest(out,current,e.fresh):
                out["data_fresh_basis"]="KIS_REST_PLUS_INDEPENDENT_LS_OBSERVATION"
                _reclassify_after_verified_reference(out)
        # Enforce the same location/cap/NO_CHASE rules before any signal is persisted.
        checks=_entry_policy(out)
        gates=out.get("gates") or {}
        if gates:
            # A provisional observation can still be WATCH; unknowns are never PRIME.
            for key,value in checks.items():
                if key=="NO_CHASE" and gates.get(key)=="FAIL" and value is True:
                    # Only override the native numerical +5 gate for the independent
                    # NXT lane; preserve any explicit or news veto.
                    if out.get("lane")!="NXT_POST_ACCEL_LANE" or out.get("no_chase") is True or out.get("news_gate")=="PRICED_IN":continue
                gates[key]="PASS" if value is True else "FAIL" if value is False else "PENDING"
            _reclassify_gates(out)
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

    import hashlib
    from collections import OrderedDict
    from threading import RLock
    scope_cache=OrderedDict(); scope_lock=RLock()

    def universe_membership_audit_v10(live_rows, latest_rows, live_total, latest_total, latest_manifest):
        audit=dict(original_audit(live_rows,latest_rows,live_total,latest_total,latest_manifest) or {})
        audit["exact_membership_matched"]=audit.get("membership_matched") is True
        if audit.get("status") not in ("MATCHED_CODES_UNVERIFIED_SAME_SESSION","PENDING_CROSS_SNAPSHOT_MEMBERSHIP_DIFF"):
            return audit
        live={e.code(r) for r in live_rows if e.valid_code(e.code(r))}
        latest={e.code(r) for r in latest_rows if e.valid_code(e.code(r))}
        if len(live)!=live_total or len(latest)!=latest_total:
            return audit
        common=live & latest
        key=hashlib.sha256((str(audit.get("latest_round_id"))+"|"+";".join(sorted(live))+"|"+";".join(sorted(latest))).encode()).hexdigest()[:24]
        # Only explicit classifications from the same completed KIS authority are copied.
        identities={e.code(r): _hvtb_authority_adapter(r).get("common_stock_confirmed") for r in latest_rows if e.code(r) in common}
        identities={k:v for k,v in identities.items() if isinstance(v,bool)}
        with scope_lock:
            scope_cache[key]=(frozenset(common),identities,e.closed_trade_date_from_manifest(latest_manifest, e.venue(live_rows[0]) if live_rows else None))
            scope_cache.move_to_end(key)
            while len(scope_cache)>8:scope_cache.popitem(last=False)
        audit.update(symbol_scope_key=key,symbol_scope_count=len(common),identity_verified_count=len(identities),
                     scope_policy="EXACT_INTERSECTION_ONLY__GLOBAL_MISMATCH_RETAINED")
        return audit

    def scope_entry(ops, market, symbol, current=None):
        audit=(getattr(ops,"authority_audit",{}) or {}).get(market,{})
        with scope_lock:
            proof=scope_cache.get(audit.get("symbol_scope_key"))
        if proof is None:return None,None
        day=proof[2]
        now=current or e.now()
        if not day or day not in (now.date().isoformat(),last_closed_session_v10(now)):
            return None,None
        return symbol in proof[0],proof[1].get(symbol)

    e._V11_SCOPE_ENTRY=scope_entry
    original_base=e.Operations.base
    def base_v11(self,r,market,*args,**kwargs):
        normalized=_hvtb_authority_adapter(r)
        evaluation_clock=kwargs.get("current") or (args[5] if len(args)>5 else None)
        match,identity=scope_entry(self,market,e.code(r),evaluation_clock)
        if normalized.get("common_stock_confirmed") is None and identity is not None:
            normalized["common_stock_confirmed"]=identity
        out=original_base(self,normalized,market,*args,**kwargs)
        if normalized.get("common_stock_confirmed") is not None:
            out["common_stock_confirmed"]=normalized["common_stock_confirmed"]
        out["authority_symbol_membership_verified"]=match
        return out
    e.Operations.base=base_v11

    e.universe_membership_audit = universe_membership_audit_v10

    # HVTB V1.6 is isolated and fail-closed: a patch error never takes down V10.
    try:
        _install_hvtb(e)
        e.HVTB_V16_SELFTEST = _hvtb_selftest()
    except Exception as exc:
        e.HVTB_V16_HOTFIX_ERROR = type(exc).__name__

    try:
        e.HVTB_DASHBOARD_STATUS = _hvtb_install_dashboard()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("HVTB dashboard integration failed")
        e.HVTB_DASHBOARD_STATUS = "ERROR:" + type(exc).__name__
    repair_errors={}
    for label,installer in (("AUDIT",lambda:_install_operations_audit(e)),("HISTORY",lambda:_install_history_cutoff_repair(e)),("CHECKPOINT",_install_compact_checkpoint),("DAILY_DISCOVERY",lambda:_install_independent_daily(e))):
        try:installer()
        except Exception as exc:repair_errors[label]=type(exc).__name__
    e.OPERATIONS_REPAIR_ERRORS=repair_errors
    e.VERSION = REPAIR_VERSION
    _APPLIED = True

# ---------------------------------------------------------------------------
# HVTB V1.6 LS READ-ONLY RUNTIME LANE
# ---------------------------------------------------------------------------


def _hvtb_live_signal(x, daily, price, current, *, max_age=10):
    """Pure live evaluator. Conservative 5m start-label convention; no future bars."""
    from datetime import timedelta
    import math
    def wait(reason, state="ACTION_WAIT", **kw):
        return dict(hvtb_action_state=state, hvtb_action_reason=reason, **kw)
    def num(v):
        try:
            v=float(v)
            return v if math.isfinite(v) else None
        except (ValueError, TypeError):
            return None
    if x is None or x.empty:
        return wait("NO_5M", ls_5m_status="PENDING")
    x=x.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    x=x[x.timestamp.map(lambda t: t.date()==current.date() and t<=current)]
    if x.empty:
        return wait("WRONG_SESSION", ls_5m_status="PENDING")
    for _, bar in x.iterrows():
        o,h,l,c,v=[num(bar[k]) for k in ("open","high","low","close","volume")]
        if None in (o,h,l,c,v) or min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c) or v<0:
            return wait("INVALID_OHLCV")
    last=x.iloc[-1]
    price=num(price)
    if (current-last.timestamp-timedelta(minutes=5)).total_seconds()>420:
        return wait("STALE_LS_5M", ls_5m_status="PENDING")
    if price is None or price<=0 or abs(float(last.close)/price-1)>0.005:
        return wait("KIS_LS_PRICE_MISMATCH", "PRICE_PENDING", ls_crosscheck=False)
    x=x[x.timestamp.map(lambda t: t+timedelta(minutes=5)<=current)].reset_index(drop=True)
    if len(x)<4:
        return wait("INSUFFICIENT_COMPLETED_5M")
    if (current-x.iloc[-1].timestamp-timedelta(minutes=5)).total_seconds()>420:
        return wait("STALE_COMPLETED_5M", ls_5m_status="PENDING")
    # A missing bar breaks the sequence; use only the most recent contiguous run.
    gaps=x.timestamp.diff().dt.total_seconds().fillna(300)
    breaks=x.index[gaps!=300]
    if len(breaks): x=x.iloc[breaks[-1]:].reset_index(drop=True)
    if len(x)<4: return wait("NONCONTIGUOUS_5M")
    candidate=None
    # Early ORB uses existing 5m provider only; no unverified 1m/3m API.
    if x.iloc[0].timestamp.hour==9 and x.iloc[0].timestamp.minute==0:
        orb=float(x.iloc[:2].high.max())
        for j in range(3,min(len(x),7)):
            b,r=x.iloc[j-1],x.iloc[j]
            atr=num(r.atr5); vw=num(r.vwap); rv=num(b.reaccel)
            if not atr or not vw or not rv: continue
            if b.close>orb and b.close>b.open and rv>=1.2 and r.low>=orb*.9965 and r.low<=orb*1.0035 and r.close>max(b.close,orb,vw) and r.close>r.open:
                candidate=dict(signal_asof=r.timestamp,stop_price=float(r.low)-.2*atr,target_price=None,bos_level=orb,retest_low=float(r.low),hvtb_signal_close=float(r.close),hvtb_entry_path="EARLY_ORB_5M")
    phase=0; start=0; level=low=None; hold=0
    for i in range(3,len(x)):
        r,p=x.iloc[i],x.iloc[i-1]
        atr=num(r.atr5); vw=num(r.vwap); rv=num(r.reaccel)
        if not atr or not vw: continue
        if candidate is not None and r.timestamp>candidate["signal_asof"]:
            if r.low<=candidate["stop_price"] or r.close<vw or r.close<candidate["bos_level"]*(1-0.0035):
                candidate=None; phase=0
            elif (r.timestamp-candidate["signal_asof"]).total_seconds()>max_age*60:
                candidate=None; phase=0
        if phase and (i-start>8 or r.close<vw*0.996 or (level is not None and r.close<level*0.985)):
            phase=0; level=low=None; hold=0
        if phase==0:
            if rv is not None and 1.2<=rv<=2.5 and r.close>r.open:
                phase=1; start=i; level=low=None; hold=0
            else: continue
        if phase==1:
            if r.close>=vw and (r.low>=vw*0.996 or p.close<p.vwap or r.low<vw): phase=2
            else: continue
        if phase==2:
            structure=float(x.iloc[i-3:i].high.max())
            if r.close>structure: level=structure; phase=3
            continue
        band=min(max(level*0.0035,atr*0.35),level*0.015)
        if phase==3:
            if r.low<=level+band and r.low>=level-band and r.close>=max(vw,level-band) and r.low>p.low:
                low=float(r.low); hold=1; phase=4
            continue
        if phase==4:
            if r.low<low or r.close<max(vw,level-band):
                phase=0; level=low=None; hold=0; continue
            hold+=1
            med=num(r.base5)
            if hold>=2 and r.close>r.open and r.close>p.high and med and .8*med<=r.volume<=2.5*med:
                stop=low-.2*atr
                highs=[num(z.get("high")) for z in daily[-20:]]
                # Nearest observed overhead high, not most distant optimistic target.
                targets=[h for h in highs if h is not None and h>float(r.close)]
                if candidate is None or r.timestamp>=candidate["signal_asof"]:
                    candidate=dict(signal_asof=r.timestamp, stop_price=stop, target_price=min(targets) if targets else None, bos_level=level, retest_low=low, hvtb_signal_close=float(r.close), hvtb_entry_path="BOS_RETEST_5M")
                phase=0; level=low=None; hold=0
    if candidate is None: return wait("NO_CURRENT_VALID_SEQUENCE")
    sig=candidate["signal_asof"]; age=(current-sig-timedelta(minutes=5)).total_seconds()/60
    stop=candidate["stop_price"]
    highs=[num(z.get("high")) for z in daily[-20:]]
    highs=[h for h in highs if h is not None and h>0]
    targets=[h for h in highs if h>candidate["hvtb_signal_close"]]
    target=min(targets) if targets else None
    target_source="HVTB_NEAREST_20D_HIGH"
    # Measured opening/rolling box projection, capped by 2 daily ATR proxy.
    if target is None and len(highs)>=20:
        ranges=[(num(z.get("high")),num(z.get("low"))) for z in daily[-20:]]
        ranges=[h-l for h,l in ranges if h is not None and l is not None and h>l>0]
        prior=x[x.timestamp<sig].tail(6)
        if len(ranges)>=14 and len(prior)>=3:
            height=float(prior.high.max()-prior.low.min())
            if height>0:
                target=candidate["bos_level"]+min(height,2*sum(ranges)/len(ranges))
                target_source="HVTB_PROJECTED_BOX_UNVALIDATED"
    candidate["target_price"]=target
    vw=float(x.iloc[-1].vwap)
    out=dict(candidate, entry_price=price, vwap=vw, ls_price=float(last.close), ls_asof=last.timestamp, ls_crosscheck=True, hvtb_signal_age_minutes=age, ls_5m_status="READY")
    if age<0 or age>max_age: return wait("SIGNAL_EXPIRED", "EXPIRED", **out)
    if stop<=0 or price<=stop or float(last.low)<=stop or price<vw or price<candidate["bos_level"]*.9965:
        return wait("LIVE_STRUCTURE_INVALID", "CANCEL", **out)
    if price/candidate["hvtb_signal_close"]-1>.015:
        return wait("LIVE_PRICE_EXTENSION_GT_1P5", "NO_CHASE", **out)
    risk=(price-stop)/price; rr=(target-price)/(price-stop) if target is not None else None
    out.update(hvtb_rr=rr, hvtb_live_risk_pct=risk*100)
    if risk>.06 or rr is None or rr<2: return wait("LIVE_RISK_OR_RR_FAILED", "NO_CHASE", **out)
    if target_source=="HVTB_PROJECTED_BOX_UNVALIDATED":
        return wait("PROJECTED_TARGET_REQUIRES_VALIDATION", "ACTION_WAIT", **out, target_source=target_source)
    return wait("FRESH_RETEST_REVERSAL_LIVE_RR", "ACTION", **out, vwap_hold=True, price_hold=True, box_reclaim_hold=True, stop_source="HVTB_RETEST_LOW_ATR_BUFFER", target_source=target_source)


def _hvtb_quality(z, bars, price, change):
    """Independent discovery on observed data; daily OHLCV is a proxy, not investor flow."""
    import math
    from statistics import median
    def num(v):
        try:
            v=float(v); return v if math.isfinite(v) else None
        except (TypeError, ValueError): return None
    price=num(price); change=num(change)
    if price is not None and price<=0: price=None
    a=z.get("absorption_evidence",{}); t=z.get("transfer_evidence",{})
    flow=bool(z.get("hvtb_quality_flow_fresh") and t.get("flow_improving"))
    cx=num(z.get("rs_current_excess_pct")); px=num(z.get("rs_prev_excess_pct"))
    rs_delta=cx-px if cx is not None and px is not None else None
    rs_up=rs_delta is not None and rs_delta>0 and cx>0
    valid=[]
    for b in bars[-21:]:
        h,l,c,v=map(num,(b.get("high"),b.get("low"),b.get("close"),b.get("volume")))
        if None in (h,l,c,v) or not h>=c>=l>0 or v<=0:
            valid=[]; continue
        valid.append((h,l,c,v))
    compression=None; accumulation=False; dd=None; ret5=None; ret20=None; extension=None; room=None
    if price and len(valid)>=10:
        widths=[(h-l)/c for h,l,c,v in valid]; vols=[v for h,l,c,v in valid]
        compression=median(widths[-3:])<=.8*median(widths[-10:-3]) and median(vols[-3:])<=median(vols[-10:-3])
        dd=price/max(h for h,l,c,v in valid[-20:])-1
        ret5=price/valid[-5][2]-1
        ret20=price/valid[-20][2]-1 if len(valid)>=20 else None
        atr=median([max(h-l,abs(h-valid[i-1][2]),abs(l-valid[i-1][2])) for i,(h,l,c,v) in enumerate(valid) if i>0][-14:])
        extension=(price-median([c for h,l,c,v in valid[-10:]]))/atr if atr>0 else None
        overhead=[h for h,l,c,v in valid if h>price]
        room=(min(overhead)/price-1)*100 if overhead else None
        # Multi-session directional volume proxy: do not label as foreign/institutional buying.
        up=sum(valid[i][3] for i in range(len(valid)-9,len(valid)) if valid[i][2]>valid[i-1][2])
        down=sum(valid[i][3] for i in range(len(valid)-9,len(valid)) if valid[i][2]<valid[i-1][2])
        accumulation=up>1.3*down and up>0 and sum(valid[i][2]>valid[i-1][2] for i in range(len(valid)-9,len(valid)))>=4
        # Avoid calling repeated high-volume rejection accumulation.
        recent=valid[-5:]
        rejection=sum(h>l and (c-l)/(h-l)<.35 for h,l,c,v in recent)
        accumulation=accumulation and rejection<3 and valid[-1][2]>=median([c for h,l,c,v in recent])
    defend=bool(a.get("higher_low") and (a.get("close_recovery") or a.get("high_range_close")))
    paths=[]
    if accumulation and compression is True and rs_up: paths.append("ACCUMULATION_PROXY")
    if flow and rs_up and (a.get("close_recovery") or a.get("high_range_close")) and dd is not None and dd<=-.12: paths.append("REVERSAL")
    if len(valid)>=10 and flow and rs_up and z.get("hvtb_fresh_catalyst_verified") and (a.get("close_recovery") or a.get("high_range_close")): paths.append("CATALYST_ACCEL")
    if flow and rs_up and defend and accumulation: paths.append("FLOW_RS_CONTINUATION_PROXY")
    priority=2*bool(paths)+2*accumulation+2*(compression is True)+2*flow+min(3,max(0,rs_delta or 0))+min(3,room or 0)
    priority-=max(0,(change or 0)-3)+20*max(0,(ret5 or 0)-.12)+10*max(0,(ret20 or 0)-.25)+max(0,(extension or 0)-2)
    return dict(hvtb_paths=paths,hvtb_v16_priority=z.get("focus_priority"),hvtb_v17_priority=priority,hvtb_independent_evidence=bool(paths),hvtb_compression=compression,hvtb_room_pct=room,hvtb_ret5=ret5,hvtb_ret20=ret20,hvtb_atr_extension=extension,hvtb_accumulation_proxy=accumulation,hvtb_flow_fresh_flag=flow,hvtb_same_time_rvol=None,hvtb_same_time_rvol_status="UNAVAILABLE_NO_HISTORICAL_INTRADAY",hvtb_flow_basis="FRESH_FLAG_PROXY" if flow else "UNCONFIRMED",hvtb_profile_status="UNAVAILABLE_NO_PRICE_VOLUME_PROFILE")


def _focus_partition(pool, limit):
    """Preserve original discovery counts; reserve the detailed budget for timely candidates.
    Rejected/expanded stocks are audit rows only, never consume focus slots.
    Unknown metadata is observation-only, not equivalent to a passed gate.
    """
    qualified=[]; rejected=[]
    for z in pool:
        ch=z.get("change_pct")
        if not isinstance(ch,(int,float)) or ch!=ch:
            reason="CHANGE_UNVERIFIED"
        elif ch>5:
            reason="NO_CHASE_EXPANDED"
        elif z.get("hvtb_explicit_veto") or z.get("overhang_veto"):
            reason="CONFIRMED_VETO"
        elif not z.get("history_current_for_hvtb"):
            reason="DAILY_PENDING"
        elif not z.get("hvtb_quote_current"):
            reason="QUOTE_PENDING"
        else:
            qualified.append(z);continue
        rejected.append((z,reason))
    return _hvtb_merge_pool(qualified,limit),rejected


def _hvtb_merge_pool(pool, limit):
    """Alternate original and challenger ranks without mixing uncalibrated scores."""
    def rank(z, key):
        v=z.get(key)
        return float(v) if isinstance(v,(int,float)) and v==v else -1e99
    original=sorted((z for z in pool if z.get("hvtb_v16_state")=="HVTB_FOCUS" and z.get("hvtb_state")=="HVTB_FOCUS"), key=lambda z:rank(z,"focus_priority"), reverse=True)
    challenger=sorted((z for z in pool if z.get("hvtb_paths") and z.get("hvtb_state")=="HVTB_FOCUS"),key=lambda z:(z.get("hvtb_v16_state")!="HVTB_FOCUS",rank(z,"hvtb_v17_priority")),reverse=True)
    rest=sorted(pool,key=lambda z:({"HVTB_FOCUS":2,"ARMED":1,"WATCH":0}.get(z.get("hvtb_state"),-1),z.get("hvtb_identity_verified") is True,rank(z,"hvtb_v17_priority")),reverse=True)
    result=[]; seen=set()
    def add(z):
        if z["code"] not in seen and len(result)<limit:
            seen.add(z["code"]); result.append(z)
    for i in range(max(len(original),len(challenger))):
        if i<len(original): add(original[i])
        if i<len(challenger): add(challenger[i])
        if len(result)>=limit: break
    for z in rest: add(z)
    return result


def _hvtb_closed_bars(bars, expected):
    """Local HVTB-only cutoff; never mutate shared history."""
    if not expected: return []
    cutoff=str(expected)[:10]
    by_date={}
    for b in bars or []:
        if not isinstance(b,dict): continue
        day=str(b.get("date") or "")[:10]
        if len(day)==10 and day<=cutoff: by_date[day]=b
    return [by_date[d] for d in sorted(by_date)]


def _hvtb_recent_history(hist,symbol,expected,limit=24):
    """Only discovery uses a bounded EOD window. Full MA history is still used
    for selected rows. Reuse the already-open read-only SQLite connection.
    """
    if not expected:return []
    if getattr(hist,"con",None) is None:return _hvtb_closed_bars(hist.get(symbol,[]),expected)[-limit:]
    from operations.trading_calendar import is_snapshot_session
    from math import isfinite
    rows=hist.con.execute("""SELECT timestamp,open,high,low,close,volume,turnover FROM bars
         WHERE provider='LS' AND venue='KRX' AND symbol=? AND timestamp<=?
         ORDER BY timestamp DESC LIMIT ?""",(symbol,str(expected),max(3,int(limit)))).fetchall()
    valid=[]
    for d,o,h,l,c,v,t in reversed(rows):
        day=str(d)
        if len(day)!=10 or is_snapshot_session(day) is not True:continue
        try:
            o,h,l,c,v=map(float,(o,h,l,c,v))
            if not all(isfinite(x) for x in (o,h,l,c,v)) or not 0<l<=min(o,c)<=max(o,c)<=h or v<0:continue
        except (TypeError,ValueError):continue
        valid.append(dict(date=day,open=o,high=h,low=l,close=c,volume=v,turnover=t))
    return _hvtb_closed_bars(valid,expected)


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

    def normalize_authority(row):
        return _hvtb_authority_adapter(row)

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
        cache_key = (current.date().isoformat(), key)
        mono = time.monotonic()
        hit = cache.get(cache_key)
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
            vol = n(z.get("jdiff_vol", z.get("volume")))
            if vol is None or vol < 0:
                continue
            if all(x is not None and x > 0 for x in (op, hi, lo, cl)):
                data.append({"timestamp": ts, "open": op, "high": hi, "low": lo, "close": cl, "volume": vol})
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
        if len(cache) >= 64:
            cache.pop(next(iter(cache)))
        cache[cache_key] = (mono, df.copy())
        return df

    def prepare5(df):
        x = df.copy().sort_values("timestamp").reset_index(drop=True)
        pc = x["close"].shift(1)
        tr = pd.concat([x["high"] - x["low"], (x["high"] - pc).abs(), (x["low"] - pc).abs()], axis=1).max(axis=1)
        x["atr5"] = tr.rolling(14, min_periods=3).mean()
        typical = (x["high"] + x["low"] + x["close"]) / 3.0
        x["vwap"] = (typical * x["volume"]).cumsum() / x["volume"].cumsum().replace(0, float("nan"))
        x["med5"] = x["volume"].rolling(5, min_periods=3).median()
        x["base5"] = x["volume"].shift(1).rolling(5, min_periods=2).median()
        x["reaccel"] = x["volume"] / x["base5"].replace(0, float("nan"))
        x["bull"] = x["close"] > x["open"]
        return x

    def action(ops, symbol, daily, kis_price, current):
        try:
            df = minute5(ops, symbol, current)
            if df is None or df.empty:
                return {"hvtb_action_state":"ACTION_WAIT", "hvtb_action_reason":"NO_5M"}
            df = df[df.timestamp.map(lambda t: t.date()==current.date() and t<=current)]
            out = _hvtb_live_signal(prepare5(df), daily, kis_price, current)
            from datetime import timedelta
            if out.get("signal_asof") is not None:
                start=out["signal_asof"]
                out["hvtb_signal_bar_start"]=e.stamp(start.to_pydatetime() if hasattr(start,"to_pydatetime") else start)
                out["signal_asof"]=start+timedelta(minutes=5)
            for k in ("signal_asof", "ls_asof"):
                if k in out: out[k]=e.stamp(out[k].to_pydatetime() if hasattr(out[k], "to_pydatetime") else out[k])
            return out
        except Exception as exc:
            return {"hvtb_action_state":"ACTION_WAIT", "hvtb_action_reason":"LS_5M_UNAVAILABLE", "ls_5m_error":type(exc).__name__}

    def alpha_map(ops):
        obj = ops.local("alpha", "KRX")
        out = {}
        if not isinstance(obj, dict):
            return out
        for key in ("sector_live_pool", "live_pool", "top_watch", "top_final"):
            for z in obj.get(key, []) or []:
                if isinstance(z, dict) and e.valid_code(e.code(z)):
                    zz=dict(z)
                    if not e.pick(zz,"source_timestamp","timestamp_kst","price_asof"):
                        zz["timestamp_kst"]=e.pick(obj,"source_timestamp","timestamp_kst","generated_at")
                    if not zz.get("market_state") and obj.get("market_state"):
                        zz["market_state"]=obj["market_state"]
                    out[e.code(z)] = zz
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
        return None, prev, False, last

    def market_proxy(rows, hist, live, expected, proxy_now):
        cur, prev = [], []
        for r in rows:
            b = _hvtb_recent_history(hist,e.code(r),expected,3)
            a, p, _, _ = quote_bridge_return(r, b, expected, live)
            if a is not None and p is not None and b and b[-1].get("date")==expected and (not live or (e.fresh(r.get("price_asof"), proxy_now) and r.get("quality") not in ("EXPIRED","STALE","REST_STALE"))):
                cur.append(a); prev.append(p)
        return (median(cur) if cur else None), (median(prev) if prev else None), len(cur), len(prev)

    def upper(r, bars, fmrow, aux, newsrow, live, mcur, mprev, expected, current):
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

        aux_fresh = e.fresh(e.pick(aux, "source_timestamp", "timestamp_kst", "price_asof"), current, 1200)
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
        flow_improving = bool(aux_fresh and (
            (aux or {}).get("flow_reversal") is True
            or (aux or {}).get("capital_handoff_confirmed") is True
        ))
        transfer_evidence = {
            "capital_handoff": parent_handoff,
            "volume_reinflow": volume_reinflow,
            "flow_improving": flow_improving,
        }
        tr = any(transfer_evidence.values())
        independent_absorption = bool(close_recovery or high_range_close or higher_low)
        # A single volume-reinflow observation is not two independent confirmations.
        ar = bool(tr and absorption and (independent_absorption or parent_handoff or flow_improving))

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
        common_stock = common_flag is not False  # provisional discovery only; final gate requires explicit True
        cap_100m = n(r.get("market_cap_100m"))
        ctx = {
            "common_stock": common_stock,
            "market_cap_krw": cap_100m * 100_000_000 if cap_100m is not None else None,
            "market_state": r.get("market_state") or "UNAVAILABLE",
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
            "transfer_evidence": transfer_evidence,
            "absorption_evidence": absorption_evidence,
            "volume_reinflow_ratio": volume_reinflow_ratio,
            "hvtb_quality_flow_fresh": aux_fresh,
            "hvtb_fresh_catalyst_verified": bool(aux_fresh and aux.get("fresh_catalyst") is True),
        }

    def run_lane(ops, current, allow_action=True, live_clock=False, refresh_only=False):
        enabled = str(os.getenv("HVTB_V16_ENABLED", "true")).lower() not in {"0", "false", "no", "off"}
        if not enabled:
            ops.store.reconcile("KRX", LANE, [], set(), current)
            return {"status": "DISABLED", "observed": 0, "evaluated_scope": 0}
        cached=getattr(ops,"_hvtb_focus_cache",None)
        if refresh_only and (not isinstance(cached,dict) or not e.fresh(cached.get("asof"),current,max(600,int(getattr(ops,"candidate_refresh_seconds",600)))+180)):
            return {"status":"PENDING","error":"FOCUS_DISCOVERY_RECONFIRM_REQUIRED","observed":0,"evaluated_scope":0,"scan_scope":"FOCUS_STATE_REFRESH"}
        try:
            rows = [normalize_authority(r) for r in ops.live_rows_only("KRX")]
            hist, _, _ = ops.history.read_all()
        except Exception as exc:
            return {"status": "PENDING", "error": type(exc).__name__, "observed": 0, "evaluated_scope": 0}
        try:
            return _run_with_history(ops, e.now() if live_clock else current, allow_action, rows, hist, live_clock, refresh_only)
        finally:
            if hasattr(hist, "close"):
                hist.close()

    def _run_with_history(ops, current, allow_action, rows, hist, live_clock=False, refresh_only=False):
        meta = ((ops.runtime or {}).get("markets") or {}).get("KRX") or {}
        expected = meta.get("history_expected_date") or e.last_closed_session(current)
        amap = alpha_map(ops)
        fmrows, fm_fresh, fmobj = fullmarket_map(ops, current)
        news = ops.local("news", "KRX", current)
        news_meta = news.get("__meta__", {}) if isinstance(news, dict) else {}
        live = e.session_open_for_market("KRX", current, None) is True
        proxy_cache=getattr(ops,"_hvtb_market_proxy_cache",None)
        if refresh_only and isinstance(proxy_cache,dict) and proxy_cache.get("expected")==expected and e.fresh(proxy_cache.get("asof"),current,900):
            mcur,mprev,market_cur_count,market_prev_count=proxy_cache["values"]
        else:
            mcur,mprev,market_cur_count,market_prev_count=market_proxy(rows,hist,live,expected,current)
            ops._hvtb_market_proxy_cache={"expected":expected,"asof":e.stamp(current),"values":(mcur,mprev,market_cur_count,market_prev_count)}
        qmap = {e.code(r): r for r in rows}
        pool, evaluated, excluded_proven = [], set(), set()
        from collections import Counter
        diagnostics=Counter()
        required5=e.recent_sessions(expected,5) or []
        required21=e.recent_sessions(expected,21) or []
        # Zero-Base HVTB selection: evaluate the KIS authority universe itself.
        # FULLMARKET/CAPITAL_HANDOFF is evidence, not an input-universe gate.
        focus_cache=getattr(ops,"_hvtb_focus_cache",{}) or {}
        focus_codes=set(focus_cache.get("codes") or [])
        discovery_rows=[r for r in rows if e.code(r) in focus_codes] if refresh_only else rows
        for r in discovery_rows:
            c = e.code(r)
            if not e.valid_code(c):
                continue
            fmrow = fmrows.get(c, {})
            cap = n(r.get("market_cap_100m"))
            match,identity=e._V11_SCOPE_ENTRY(ops,"KRX",c,current)
            if r.get("common_stock_confirmed") is None and identity is not None:
                r["common_stock_confirmed"]=identity
            r["authority_symbol_membership_verified"]=match
            if cap is None or not 700 <= cap <= _cap_ceiling() or r.get("common_stock_confirmed") is False:
                diagnostics["CAP_OR_EXCLUDED_SECURITY"]+=1
                if match is True and (r.get("common_stock_confirmed") is False or (cap is not None and not 700<=cap<=_cap_ceiling())):
                    excluded_proven.add(c)
                continue
            if r.get("common_stock_confirmed") is None:diagnostics["SECURITY_UNVERIFIED"]+=1
            # Only a fresh explicit upstream state may supplement missing KIS state.
            if not r.get("market_state"):
                aux=amap.get(c,{})
                if e.fresh(e.pick(aux,"source_timestamp","timestamp_kst","price_asof"),current,1200):
                    r["market_state"]=aux.get("market_state")
                if not r.get("market_state") and fm_fresh:
                    r["market_state"]=fmrow.get("market_state") or fmobj.get("market_state")
            if not r.get("market_state"):diagnostics["MARKET_STATE_UNVERIFIED"]+=1
            bars = _hvtb_recent_history(hist,c,expected,24)
            z = upper(r, bars, fmrow if fm_fresh else {}, amap.get(c, {}), (news.get(c) if isinstance(news, dict) else {}), live, mcur, mprev, expected, current)
            z.update(code=c, name=r.get("name"), market="KRX", price=n(r.get("price")), change_pct=n(r.get("change_pct")), market_cap_100m=cap)
            days={b.get("date") for b in bars}
            z["history_current_for_hvtb"]=bool(required5 and set(required5)<=days and z.get("history_current_for_hvtb"))
            # Challenger windows must be consecutive trading sessions, not merely N rows.
            tail=[]
            index={b.get("date"):b for b in bars}
            for day in reversed(required21):
                if day not in index:break
                tail.append(index[day])
            tail.reverse()
            z.update(_hvtb_quality(z, tail, z.get("price"), z.get("change_pct")))
            z["hvtb_quote_current"]=e.fresh(r.get("price_asof"),current) and r.get("quality") not in ("EXPIRED","STALE","REST_STALE")
            z["hvtb_explicit_veto"]=str(r.get("market_state") or "").upper() in BLOCK_MARKET_STATES
            z["hvtb_identity_verified"]=r.get("common_stock_confirmed") is True
            z["hvtb_scope_verified"]=match is True
            z["hvtb_v16_state"] = z.get("hvtb_state")
            if (z.get("hvtb_paths") and z.get("hvtb_state") in {"WATCH", "ARMED"}
                and cap is not None and cap >= 700 and r.get("common_stock_confirmed") is not False
                and not z.get("overhang_veto") and z.get("history_current_for_hvtb")
                and str(r.get("market_state") or "").upper() not in BLOCK_MARKET_STATES):
                z.update(hvtb_state="HVTB_FOCUS", hvtb_reason="INDEPENDENT_DISCOVERY")
            if z.get("history_current_for_hvtb") and z.get("hvtb_quote_current") and match is True:
                evaluated.add(c)
            if z.get("hvtb_state") in {"HVTB_FOCUS", "ARMED", "WATCH"}:
                pool.append(z)
        original_count=sum(z.get("hvtb_v16_state")=="HVTB_FOCUS" for z in pool)
        challenger_count=sum(bool(z.get("hvtb_paths")) and z.get("hvtb_state")=="HVTB_FOCUS" for z in pool)
        added_count=sum(z.get("hvtb_reason")=="INDEPENDENT_DISCOVERY" for z in pool)
        pool, excluded = _focus_partition(pool, max(3, min(30, int(os.getenv("HVTB_V16_MAX_FOCUS", "12")))))
        for _,reason in excluded:diagnostics[reason]+=1
        if not refresh_only:
            ops._hvtb_focus_cache={"asof":e.stamp(current),"codes":[z["code"] for z in pool],"evaluated_scope":len(evaluated),"original_focus_count":original_count,"challenger_focus_count":challenger_count,"incremental_focus_count":added_count}
            focus_cache=ops._hvtb_focus_cache
        # A long full-market scan may age the initially received quotes. Re-read
        # once through the existing provider, never per-stock API fan-out.
        if live_clock and (e.now()-current).total_seconds()>90 and pool:
            try:
                updated={e.code(r):normalize_authority(r) for r in ops.live_rows_only("KRX")}
                current=e.now()
                for z in pool:
                    c=z["code"]; q=updated.get(c)
                    if q is None:
                        z["hvtb_quote_current"]=False;continue
                    # keep identity/state from the previous verified source if the new quote omits it
                    for k in ("common_stock_confirmed","market_state","authority_symbol_membership_verified"):
                        if q.get(k) is None:q[k]=qmap[c].get(k)
                    qmap[c]=q
                    z.update(price=n(q.get("price")),change_pct=n(q.get("change_pct")),hvtb_quote_current=e.fresh(q.get("price_asof"),current) and q.get("quality") not in ("EXPIRED","STALE","REST_STALE"))
                pool, late_excluded=_focus_partition(pool,len(pool))
                for _,reason in late_excluded:diagnostics[reason]+=1
            except Exception as exc:
                diagnostics["QUOTE_RECHECK_"+type(exc).__name__]+=1
        max_action = max(1, min(12, int(os.getenv("HVTB_V16_MAX_LS_ACTION", "8"))))
        action_count = count = 0
        outrows = []
        audit = ops.authority_audit.get("KRX", {})
        authority_ok = e.authority_scope_consistent(audit)
        for z in pool:
            c = z["code"]
            r = qmap[c]
            if live_clock:current=e.now()
            symbol_ok,identity=e._V11_SCOPE_ENTRY(ops,"KRX",c,current)
            if r.get("common_stock_confirmed") is None and identity is not None:r["common_stock_confirmed"]=identity
            if z.get("hvtb_state") == "HVTB_FOCUS":
                if not live or not allow_action:
                    z.update(hvtb_action_state="PREOPEN_FOCUS" if not live else "ACTION_WAIT", hvtb_action_reason="WAIT_LIVE_5M")
                elif z.get("overhang_veto") or r.get("common_stock_confirmed") is False or str(r.get("market_state") or "").upper() in BLOCK_MARKET_STATES:
                    z.update(hvtb_action_state="ACTION_WAIT", hvtb_action_reason="EXPLICIT_PREQUERY_VETO")
                elif not isinstance(z.get("change_pct"),(int,float)) or z["change_pct"]>5:
                    z.update(hvtb_action_state="NO_CHASE", hvtb_action_reason="NO_CHASE_EXPANDED")
                elif action_count >= 3 or count >= max_action:
                    z.update(hvtb_action_state="ACTION_WAIT", hvtb_action_reason="BOUNDED_SCAN_BUDGET")
                elif not e.fresh(r.get("price_asof"), current) or not z.get("history_current_for_hvtb"):
                    z.update(hvtb_action_state="ACTION_WAIT", hvtb_action_reason="STALE_AUTHORITY_OR_DAILY")
                else:
                    count += 1
                    z.update(action(ops, c, _hvtb_closed_bars(hist.get(c, []), expected), z.get("price"), current))
                    if live_clock:current=e.now()
            z["hvtb_discovery_origin"] = ("BOTH" if z.get("hvtb_v16_state")=="HVTB_FOCUS" and z.get("hvtb_paths") else "ORIGINAL" if z.get("hvtb_v16_state")=="HVTB_FOCUS" else "CHALLENGER" if z.get("hvtb_reason")=="INDEPENDENT_DISCOVERY" else "WATCH")
            signal = dict(z)
            signal["signal_track"] = "ACTION" if z.get("hvtb_action_state") == "ACTION" else z.get("hvtb_state")
            signal["lane_confirmed"] = z.get("hvtb_state") == "HVTB_FOCUS"
            if isinstance(news, dict) and isinstance(news.get(c), dict):
                # News may add context, never overwrite entry or discovery results.
                for nk,nv in news[c].items():
                    if nk.startswith("news_") or nk in {"fresh_catalyst", "catalyst_score"}:
                        signal[nk]=nv
            elif news_meta.get("status") == "READY":
                signal.setdefault("news_gate", None)
                signal.setdefault("news_source", "SIDECAR_HEALTHY_SYMBOL_COVERAGE_UNVERIFIED")
            if signal.get("news_gate") in e.NEWS_BLOCK:
                signal["hvtb_state"] = "VETO_REJECT"
                signal["hvtb_reason"] = "OVERHANG_OR_NEGATIVE_NEWS_VETO"
                signal["lane_confirmed"] = False
            if signal.get("hvtb_action_state") == "ACTION":
                market_state = str(r.get("market_state") or "").upper()
                if symbol_ok is not True or not e.session_open_for_market("KRX", current, r.get("session_open")) or market_state not in ("NORMAL","RISK_ON","NEUTRAL") or signal.get("news_gate") not in e.NEWS_PASS or signal.get("overhang_veto") or r.get("common_stock_confirmed") is not True:
                    missing=[]
                    if symbol_ok is not True:missing.append("AUTHORITY_SYMBOL")
                    if not e.session_open_for_market("KRX", current, r.get("session_open")):missing.append("SESSION")
                    if market_state not in ("NORMAL","RISK_ON","NEUTRAL"):missing.append("MARKET_STATE")
                    if signal.get("news_gate") not in e.NEWS_PASS:missing.append("NEWS")
                    if signal.get("overhang_veto"):missing.append("OVERHANG")
                    if r.get("common_stock_confirmed") is not True:missing.append("SECURITY_COMMON")
                    signal.update(hvtb_action_state="ACTION_WAIT",hvtb_action_reason="FINAL_GATE:"+','.join(missing),signal_track="HVTB_FOCUS")
            signal_at = signal.get("signal_asof") or e.stamp(current)
            base = ops.base(r, "KRX", signal, signal_at, amap, {}, _hvtb_closed_bars(hist.get(c, []), expected), current, reference_daily=expected)
            base.update(
                lane=LANE,
                selection_path=[LANE, str(signal.get("signal_track") or "PENDING")],
                upstream_state=signal.get("hvtb_state"),
                lane_confirmed=signal.get("lane_confirmed"),
                history_expected_asof=expected,
                history_current=signal.get("history_current_for_hvtb"),
                authority_universe_consistent=symbol_ok is True,
                authority_symbol_membership_verified=symbol_ok,
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
                      "ls_price", "ls_asof", "ls_crosscheck", "ls_5m_status", "ls_5m_error", "signal_asof"):
                if k in signal:
                    base[k] = signal[k]
            base["runtime_lane_state"] = signal.get("hvtb_action_state") or signal.get("hvtb_state") or "WATCH"
            base = e.core_gates(base, current)
            if base.get("hvtb_action_state")=="ACTION" and base.get("classification")!="PRIME":
                base.update(hvtb_action_state="ACTION_WAIT",hvtb_action_reason="COMMON_GATE_NOT_PRIME",runtime_lane_state="ACTION_WAIT",signal_track="HVTB_FOCUS",selection_path=[LANE,"HVTB_FOCUS"])
            if base.get("hvtb_action_state") != "ACTION":
                if base.get("classification") == "PRIME":
                    base.update(classification="WATCH", decision="PENDING", entry_phase="진입 전 · HVTB 확인 대기", expansion_phase="PENDING")
            base["unavailable_fields"] = [k for k in ("vwap", "rvol", "execution_strength", "rs5", "rs20", "foreign_net", "institution_net", "program_net", "entry_price", "stop_price", "target_price") if base.get(k) is None]
            if base.get("hvtb_action_state") == "ACTION" and base.get("classification") == "PRIME":
                action_count += 1
            outrows.append(base)
        # Every evaluated code came from the current KIS authority universe.
        # A stale/missing FULLMARKET sidecar must not make the independent HVTB
        # lane disappear; it only removes one TRANSFER evidence source.
        # A bounded ranking is not an exhaustive evaluation of entry setups.
        # Full discovery actually evaluated nonselected codes; state refresh did not.
        # Thus only full discovery may advance their verified miss counter.
        proven = {z["code"] for z in outrows if z["code"] in evaluated and e.fresh(z.get("price_asof"),current)}
        reconcile_scope=set(proven) if refresh_only else set(evaluated)|excluded_proven
        for row in outrows:
            if (row["code"] not in proven or not e.fresh(row.get("signal_asof"),current,600)) and row.get("hvtb_action_state") == "ACTION":
                row.update(hvtb_action_state="ACTION_WAIT", hvtb_action_reason="SOURCE_RECONFIRM_PENDING", classification="WATCH", decision="PENDING", runtime_lane_state="DATA_HOLD")
        ops.store.reconcile("KRX", LANE, outrows, reconcile_scope, current)
        if qmap:
            ops.store.track("KRX", qmap, current)
        lane_meta = {
            "observed": len(outrows),
            "status": "READY" if rows and authority_ok and len(evaluated)==len(rows) else "PARTIAL_VERIFIED" if evaluated else "PENDING",
            "prime": sum(z.get("classification") == "PRIME" for z in outrows),
            "focus": sum(z.get("hvtb_state") == "HVTB_FOCUS" for z in outrows),
            "action": sum(z.get("hvtb_action_state") == "ACTION" for z in outrows),
            "evaluated_scope": len(evaluated),
            "reconfirmed_scope":len(proven),
            "original_focus_count": original_count,
            "challenger_focus_count": challenger_count,
            "incremental_focus_count": added_count,
            "ls_queries_budget_used": count,
            "universe_complete": bool(rows and authority_ok and len(evaluated)==len(rows)),
            "scope_status": "PARTIAL_VERIFIED" if proven else "PENDING",
            "scan_scope":"FOCUS_STATE_REFRESH" if refresh_only else "ZERO_BASE_DISCOVERY",
            "discovery_asof":focus_cache.get("asof"),
            "discovery_evaluated_scope":focus_cache.get("evaluated_scope"),
            "discovery_original_count":focus_cache.get("original_focus_count"),
            "discovery_challenger_count":focus_cache.get("challenger_focus_count"),
            "market_proxy_asof":ops._hvtb_market_proxy_cache.get("asof"),
            "market_proxy_current_sample":market_cur_count,
            "market_proxy_previous_sample":market_prev_count,
            "discovery_diagnostics": dict(diagnostics),
            "eligible_focus_count": len(pool),
            "excluded_from_focus_count": len(excluded),
            "focus_preview": [{"code":z["code"],"name":z.get("name"),"change_pct":z.get("change_pct"),"reason":reason} for z,reason in excluded[:20]],
            "source": "HVTB_ORIGINAL_PLUS_CHALLENGER + KIS_ZERO_BASE_AUTHORITY + LS_T8412_BOUNDED",
        }
        markets = (ops.runtime or {}).setdefault("markets", {})
        krx = markets.setdefault("KRX", {})
        krx.setdefault("lanes", {})[LANE] = lane_meta
        ops.runtime["hvtb_v16"] = dict(lane_meta, generated_at=e.stamp(current), expected_daily=expected, market_proxy_current_pct=mcur, market_proxy_prev_pct=mprev)
        return lane_meta

    def publish(ops, meta, current):
        meta = dict(meta or {}, generated_at=e.stamp(current))
        ops.runtime.setdefault("markets", {}).setdefault("KRX", {}).setdefault("lanes", {})[LANE] = meta
        ops.runtime["hvtb_v16"] = meta
        return meta

    def guarded_lane(ops, current, live_clock=False, refresh_only=False):
        import logging
        if not ops.lock.acquire(False):
            return publish(ops, {"status":"PENDING", "error":"HVTB_SCAN_BUSY"}, current)
        try:
            return publish(ops, run_lane(ops, current, allow_action=True, live_clock=live_clock, refresh_only=refresh_only), e.now() if live_clock else current)
        except Exception as exc:
            logging.getLogger(__name__).exception("HVTB independent lane failed")
            return publish(ops, {"status":"PENDING", "error":type(exc).__name__, "error_stage":"HVTB_RUN_LANE"}, current)
        finally:
            ops.lock.release()

    original_scan = getattr(e.Operations, "_HVTB_V16_ORIGINAL_SCAN", e.Operations.scan)
    e.Operations._HVTB_V16_ORIGINAL_SCAN = original_scan

    def scan_hvtb(self, current=None):
        live_clock=current is None
        current = current or e.now()
        result = original_scan(self, current)
        if isinstance(result, dict) and result.get("status") == "BUSY":
            return result
        current=e.now() if live_clock else current
        guarded_lane(self, current, live_clock)
        self._hvtb_action_refresh_at = current
        return result

    e.Operations.scan = scan_hvtb

    original_refresh = getattr(e.Operations, "_HVTB_V16_ORIGINAL_REFRESH", e.Operations.refresh_candidate_states)
    e.Operations._HVTB_V16_ORIGINAL_REFRESH = original_refresh

    def refresh_hvtb(self, current=None):
        live_clock=current is None
        current = current or e.now()
        result = original_refresh(self, current)
        if isinstance(result, dict) and result.get("status") == "BUSY":
            return result
        current=e.now() if live_clock else current
        if e.session_open_for_market("KRX", current, None) is not True:
            return result
        last = getattr(self, "_hvtb_action_refresh_at", None)
        interval = max(90, int(os.getenv("HVTB_V16_ACTION_REFRESH_SECONDS", "120")))
        if last is None or (current - last).total_seconds() >= interval:
            self._hvtb_action_refresh_at = current
            guarded_lane(self, current, live_clock, refresh_only=True)
        return result

    e.Operations.refresh_candidate_states = refresh_hvtb

    # Only the HVTB projection is changed. Native candidate state / DB schema remain untouched.
    original_api = e.Operations.api
    def api_hvtb(self, raw_path):
        status, payload = original_api(self, raw_path)
        if status != 200 or not isinstance(payload, dict):
            return status, payload
        from urllib.parse import urlparse
        if urlparse(raw_path).path.rstrip('/').endswith('/board'):
            meta = (self.runtime or {}).get("hvtb_v16") or {"status":"PENDING", "error":getattr(e,"HVTB_V16_HOTFIX_ERROR","NOT_RUN")}
            payload["hvtb_lane_status"] = meta
            payload["hvtb_dashboard_status"] = getattr(e, "HVTB_DASHBOARD_STATUS", "PENDING")
            for row in payload.get("rows", []):
                if row.get("lane") != LANE:
                    continue
                _hvtb_project_row(row, meta, e.now(), e.fresh, e.session_open_for_market)
        return status, payload
    e.Operations.api = api_hvtb
    e.HVTB_V16_VERSION = REPAIR_VERSION


def _install_history_cutoff_repair(e):
    """Keep the original paced off-hours repair. Use completed-session inventory;
    a future/unclosed cached row must not globally halt recovery of real EOD bars.
    """
    from operations.history_repair import KRXHistoryRepair as R
    from datetime import date
    from threading import RLock
    old_inventory=R._store_inventory
    old_validate=R._validate_latest
    @classmethod
    def inventory(cls,worker,symbols,required):
        if not required or getattr(worker.store,"conn",None) is None:return old_inventory(worker,symbols,required)
        cutoff=str(required[-1]); store=worker.store
        result={c:(None,0,set()) for c in symbols}
        with getattr(store,"_lock",RLock()):
            rows=store.conn.execute("""SELECT symbol,MAX(timestamp),COUNT(*) FROM bars
                 WHERE provider='LS' AND venue='KRX' AND timestamp<=? AND open>0 AND high>0 AND low>0 AND close>0
                 AND volume>=0 AND low<=open AND low<=close AND high>=open AND high>=close GROUP BY symbol""",(cutoff,)).fetchall()
            args=','.join('?' for _ in required)
            recent=store.conn.execute("SELECT symbol,timestamp,open,high,low,close,volume FROM bars WHERE provider='LS' AND venue='KRX' AND timestamp IN ("+args+")",list(required)).fetchall()
        found={}
        for c,d,n in rows:
            if c not in symbols:continue
            try:found[c]=(date.fromisoformat(str(d)),int(n))
            except (ValueError,TypeError):continue
        by={}
        for c,d,o,h,l,p,v in recent:
            if c in symbols and cls._valid_bar(o,h,l,p,v):by.setdefault(c,set()).add(str(d))
        for c in symbols:
            d,n=found.get(c,(None,0));result[c]=(d,n,by.get(c,set()))
        return result
    @classmethod
    def validate(cls,worker,symbol,expected,required):
        if getattr(worker.store,"conn",None) is None:return old_validate(worker,symbol,expected,required)
        if not required or str(required[-1])!=expected.isoformat():raise ValueError("LS_COMPLETED_WINDOW_UNVERIFIED")
        store=worker.store; args=','.join('?' for _ in required)
        with getattr(store,"_lock",RLock()):
            rows=store.conn.execute("SELECT timestamp,open,high,low,close,volume FROM bars WHERE provider='LS' AND venue='KRX' AND symbol=? AND timestamp IN ("+args+")",[symbol,*required]).fetchall()
        days={str(d) for d,o,h,l,p,v in rows if cls._valid_bar(o,h,l,p,v)}
        if not set(required)<=days:raise ValueError("LS_COMPLETED_WINDOW_UNVERIFIED")
        return expected
    R._store_inventory=inventory
    R._validate_latest=validate


def _install_compact_checkpoint():
    # The native checkpoint uses raw SQLite serialization with a 32 MB guard.
    # Compact an in-memory copy only; existing DB and checkpoint schema unchanged.
    from operations.persistence import Checkpoint
    import sqlite3
    def save(self):
        if not self.dsn:return
        try:
            mem=sqlite3.connect(":memory:")
            try:
                with self.store.connect() as local:local.backup(mem)
                if mem.execute("PRAGMA page_count").fetchone()[0]*mem.execute("PRAGMA page_size").fetchone()[0]>32_000_000 and mem.execute("PRAGMA freelist_count").fetchone()[0]>0:mem.execute("VACUUM")
                snapshot=mem.serialize()
            finally:mem.close()
            if len(snapshot)>32_000_000:raise ValueError("CHECKPOINT_SIZE_REQUIRES_MIGRATION")
            with self.connection() as pg:
                self.table(pg)
                pg.execute("INSERT INTO operations_projection_checkpoint(key,snapshot) VALUES('operations-v1',%s) ON CONFLICT(key) DO UPDATE SET snapshot=EXCLUDED.snapshot,updated_at=NOW()",(snapshot,))
            self.status="POSTGRES_CHECKPOINTED";self.error=None
        except Exception as exc:
            self.error=type(exc).__name__;self.status="PENDING_CHECKPOINT_SAVE"
    Checkpoint.save=save


def _install_operations_audit(e):
    """Observation-only diagnostics across all lanes. Never modifies broker data."""
    import time
    from urllib.parse import urlparse,parse_qs
    original_local=e.Operations.local
    def local_v11(self,name,*args):
        result=original_local(self,name,*args)
        if isinstance(result,dict):
            key=name+":"+str(args[0]) if name in ("alpha","news","re25") and args and isinstance(args[0],str) else name
            meta=getattr(self,"_v11_provider_diagnostics",None)
            if not isinstance(meta,dict):meta={};self._v11_provider_diagnostics=meta
            if key not in meta and len(meta)>=24:meta.pop(next(iter(meta)))
            nm=result.get("__meta__") or {}
            if not isinstance(nm,dict):nm={}
            meta[key]={"status":result.get("status") or nm.get("status") or "UNREPORTED",
                       "error":result.get("error") or nm.get("reason"),
                       "generated_at":result.get("generated_at") or result.get("timestamp_kst") or nm.get("checked_at"),
                       "history_pending":result.get("history_pending"),"pending_series":result.get("pending_series"),
                       "evaluated_count":len(result.get("evaluated_codes") or []),
                       "universe_complete":result.get("universe_complete")}
        return result
    e.Operations.local=local_v11
    original_api=e.Operations.api
    def api_v11(self, raw_path):
        status,payload=original_api(self,raw_path)
        if status!=200 or not isinstance(payload,dict):return status,payload
        kind=urlparse(raw_path).path.rstrip('/').rsplit('/',1)[-1]
        if kind not in ("board","status"):return status,payload
        payload=dict(payload)
        payload["repair_version"]=REPAIR_VERSION
        payload["repair_errors"]=getattr(e,"OPERATIONS_REPAIR_ERRORS",{}).copy()
        payload["provider_diagnostics"]=getattr(self,"_v11_provider_diagnostics",{}).copy()
        payload["policy"]={"cap_min_100m":700,"cap_max_100m":_cap_ceiling(),"min_live_rr":2,"max_live_risk_pct":6,"krx_no_chase_above_pct":5,"nxt_post_no_chase_at_pct":10,"broker_identity_required_for_entry":True,"daily_proxy_is_not_investor_flow":True}
        def label_scan(scan):
            if not isinstance(scan,dict):return scan
            scan=dict(scan)
            lanes={}
            for name,info in (scan.get("lanes") or {}).items():
                if not isinstance(info,dict):continue
                info=dict(info)
                info["producer_status"]=info.get("status")
                if info.get("status")=="READY" and info.get("universe_complete") is not True:
                    info["status"]="PARTIAL_VERIFIED"
                lanes[name]=info
            scan["lanes"]=lanes
            return scan
        if kind=="status":
            # Do not mutate shared runtime or rewrite historical scan counts.
            payload=dict(payload)
            payload["repair_version"]=REPAIR_VERSION
            payload["provider_diagnostics"]=getattr(self,"_v11_provider_diagnostics",{}).copy()
            payload["markets"]={k:label_scan(v) for k,v in (payload.get("markets") or {}).items()}
        else:
            payload["scan"]=label_scan(payload.get("scan") or {})
            now=e.now()
            query=parse_qs(urlparse(raw_path).query)
            market=(query.get("market") or ["KRX"])[0]
            lane=(query.get("lane") or [None])[0]
            requested_limit=max(20,min(300,int((query.get("limit") or ["80"])[0])))
            requested_offset=max(0,int((query.get("offset") or ["0"])[0]))
            # Native Store.board is bounded at 300 per query. Rank a bounded,
            # complete active lane before slicing; retain native audit totals.
            # If a lane exceeds the cap, make the coverage limit explicit.
            native_board=getattr(self.store,"board",None)
            ranked_scope=None
            if callable(native_board) and lane:
                all_rows=[]; seen=set(); total=None
                try:
                    for start in range(0,1500,300):
                        page=native_board(market,300,start,lane)
                        if not isinstance(page,dict):raise ValueError("INVALID_BOARD_PAGE")
                        total=int(page.get("total",0)); batch=page.get("rows") or []
                        for item in batch:
                            key=(item.get("lane"),item.get("code"))
                            if key not in seen:seen.add(key);all_rows.append(item)
                        if len(batch)<300 or start+300>=total:break
                    payload["rows"]=all_rows
                    payload["total"]=total
                    ranked_scope=len(all_rows)
                    payload["ranking_scope"]={"evaluated":ranked_scope,"total":total,"complete":ranked_scope==total}
                except Exception as exc:
                    payload["ranking_scope"]={"complete":False,"error":type(exc).__name__}
            for row in payload.get("rows",[]):
                if not isinstance(row,dict):continue
                # Every row, including later native pages, must pass the same
                # HVTB-specific freshness projection before display.
                # Preserve the persisted classification before live projection.
                row["stored_classification"]=row.get("classification")
                row["stored_decision"]=row.get("decision")
                if row.get("lane")=="HVTB_V1_6":
                    _hvtb_project_row(row,payload.get("hvtb_lane_status") or {},now,e.fresh,e.session_open_for_market)
                ch=e.number(row.get("change_pct")); p=e.number(row.get("price")); st=e.number(row.get("stop_price")); t=e.number(row.get("target_price"))
                valid=all(v is not None for v in (p,st,t)) and 0<st<p<t
                rr=(t-p)/(p-st) if valid else None
                row["live_rr"]=rr
                cap=_finite_number(row.get("market_cap_100m"))
                excluded=_explicit_exclusion(row.get("name")) or row.get("common_stock_confirmed") is False or (cap is not None and not 700<=cap<=_cap_ceiling())
                chase=_chase_ok(row)
                if excluded:row["live_board_state"]="EXCLUDED_SECURITY"
                elif row.get("lane")=="RE_EXPLOSION_25" and (_entry_policy(row).get("RE25_EVENT_VALID") is False):row["live_board_state"]="EVENT_ONLY"
                elif chase is False:row["live_board_state"]="NO_CHASE"
                elif row.get("classification")=="REJECT":row["live_board_state"]="REJECT"
                elif row.get("candidate_lifecycle") in ("DATA_HOLD","OBSERVED_UNVERIFIED") or not e.fresh(row.get("price_asof"),now):row["live_board_state"]="DATA_HOLD"
                else:row["live_board_state"]=row.get("classification") or "WATCH"
                if row.get("classification")=="PRIME" or row.get("decision")=="PRIME":
                    gates=row.get("gates") or {}
                    fresh_trade=((row.get("timestamp_quality")=="EXCHANGE_TIMESTAMP" and e.fresh(row.get("price_asof"),now) and row.get("quote_quality") not in ("STALE","EXPIRED","REST_STALE")) or _corroborated_rest(row,now,e.fresh))
                    safe=(row.get("live_board_state")!="EVENT_ONLY" and all(v is True for v in _entry_policy(row).values()) and not excluded and chase is True and cap is not None and 700<=cap<=_cap_ceiling() and row.get("common_stock_confirmed") is True and (row.get("market")!="KRX" or row.get("authority_symbol_membership_verified") is True) and row.get("candidate_lifecycle") not in ("DATA_HOLD","OBSERVED_UNVERIFIED") and row.get("candidate_stale") is not True and fresh_trade and e.fresh(row.get("signal_asof"),now,600) and e.session_open_for_market(row.get("market"),now,row.get("session_open")) and (row.get("lane")!="NXT_POST_ACCEL_LANE" or (row.get("market")=="NXT" and ((1000 if now.date().isoformat()==e.SPECIAL_SESSION_DATE else 940)<=now.hour*60+now.minute<1200))) and bool(gates) and all(v=="PASS" for v in gates.values()) and rr is not None and rr>=2 and (p-st)/p<=.06 and row.get("target_source") not in (None,"","PENDING","UNAVAILABLE") and row.get("stop_source") not in (None,"","PENDING","UNAVAILABLE"))
                    if not safe:
                        row.update(classification="WATCH",decision="PENDING",entry_phase="진입 전 · 현재 상태 재검증",live_board_state="EXCLUDED_SECURITY" if excluded else "EVENT_ONLY" if row.get("live_board_state")=="EVENT_ONLY" else "NO_CHASE" if chase is False else "DATA_HOLD")
                        if row.get("lane")=="HVTB_V1_6":row.update(hvtb_action_state="ACTION_WAIT",hvtb_action_reason="LIVE_COMMON_GATE_RECONFIRM")
                        row["runtime_lane_state"]=row["live_board_state"]
            # Active candidates are ranked before pagination within the explicit bounded scope.
            rank={"PRIME":0,"ARMED":1,"PRE_EXPLOSION_ARMED":2,"WATCH":3,"DATA_HOLD":4,"NO_CHASE":5,"EXCLUDED_SECURITY":6,"REJECT":7,"EVENT_ONLY":8}
            if isinstance(payload.get("rows"),list):
                payload["rows"].sort(key=lambda z:rank.get(z.get("live_board_state"),4))
                if ranked_scope is not None:
                    payload["rows"]=payload["rows"][requested_offset:requested_offset+requested_limit]
                    payload["offset"]=requested_offset
        return status,payload
    e.Operations.api=api_v11




def _independent_daily_discovery(rows, histories, required, cap_max=30000, *, proof=None):
    """Cross-sectional completed-day RS and sector rotation proxies. No actual flow claim."""
    from statistics import median
    from math import isfinite
    def num(v):
        try:
            x=float(v);return x if isfinite(x) else None
        except (TypeError,ValueError):return None
    samples=[];seen=set()
    if not required or len(required)!=21 or len(set(required))!=21 or list(required)!=sorted(required):return [],[],0
    for raw in rows:
        r=_hvtb_authority_adapter(raw); c=str(r.get('code') or ''); cap=num(r.get('market_cap_100m'))
        if not c or c in seen or not cap or not 700<=cap<=cap_max or r.get('common_stock_confirmed') is False:continue
        idx={b.get('date'):b for b in histories.get(c,[]) if isinstance(b,dict)}
        if not all(d in idx for d in required):continue
        b=[idx[d] for d in required]; cl=[num(z.get('close')) for z in b]; v=[num(z.get('volume')) for z in b]
        if any(x is None or x<=0 for x in cl) or any(x is None or x<0 for x in v):continue
        turnover=[num(z.get('turnover')) for z in b]
        # Use actual daily turnover only; do not synthesize monetary flows from OHLCV.
        tv_ok=all(x is not None and x>=0 for x in turnover) and sum(turnover[-10:-5])>0
        ret5=(cl[-1]/cl[-6]-1)*100;ret20=(cl[-1]/cl[0]-1)*100; prev5=(cl[-6]/cl[-11]-1)*100
        tv_ratio=sum(turnover[-5:])/sum(turnover[-10:-5]) if tv_ok else None
        ch=num(r.get('change_pct')); price=num(r.get('price'))
        sector=str(r.get('sector') or '').strip()
        seen.add(c)
        daily_returns=[(cl[i]/cl[i-1]-1)*100 for i in range(1,21)]
        samples.append(dict(code=c,name=r.get('name'),row=r,ret5=ret5,ret20=ret20,prev5=prev5,tv_ratio=tv_ratio,sector=sector,change=ch,price=price,cap=cap,daily_returns=daily_returns))
    if len(samples)<10:return [],[],len(samples)
    if proof is not None:proof.update(x['code'] for x in samples)
    m5=median(x['ret5'] for x in samples);m20=median(x['ret20'] for x in samples);mp=median(x['prev5'] for x in samples)
    daily_market=[median(x['daily_returns'][i] for x in samples) for i in range(20)]
    down_days=[i for i,ret in enumerate(daily_market) if ret<0]
    groups={}
    for x in samples:
        if x['sector'] and x['sector'].upper() not in ('UNKNOWN','UNAVAILABLE','NONE'):groups.setdefault(x['sector'],[]).append(x)
    rs=[];rot=[]
    for x in samples:
        if x['change'] is None or x['change']>5 or not x['price'] or x['price']<=0 or x['ret5']>12 or x['ret20']>30:continue
        d5=x['ret5']-m5;d20=x['ret20']-m20; delta=d5-(x['prev5']-mp)
        excess=[x['daily_returns'][i]-daily_market[i] for i in down_days]
        defense=median(excess) if excess else None
        defense_win=sum(v>0 for v in excess)/len(excess) if excess else None
        defense_confirmed=len(down_days)>=3 and defense is not None and defense>0 and defense_win>=.6
        base=dict(x,rs5=d5,rs20=d20,rs_delta=delta,benchmark_sample=len(samples),benchmark='ELIGIBLE_MID_SMALL_MEDIAN_COMPLETED_DAILY',downmarket_days=len(down_days),downmarket_excess=defense,downmarket_win_ratio=defense_win,rs_defense_confirmed=defense_confirmed)
        if d5>0 and d20>0 and delta>0:
            rs.append(dict(base,score=d5+0.5*d20+delta+(max(0,defense) if defense_confirmed else 0),rs_discovery_state='ASYMMETRIC_DEFENSE_WATCH' if defense_confirmed else 'RELATIVE_STRENGTH_WATCH'))
        g=groups.get(x['sector'],[])
        if len(g)<3 or x['tv_ratio'] is None or x['tv_ratio']<1.3 or d5<=0 or delta<=0:continue
        leaders=[z for z in g if z['code']!=x['code'] and z['prev5']>0 and z['prev5']>x['prev5'] and z['ret5']<z['prev5'] and z['ret5']<x['ret5'] and z['tv_ratio'] is not None and z['tv_ratio']<1]
        if leaders:
            lead=max(leaders,key=lambda z:z['prev5']);rot.append(dict(base,score=delta+5*(x['tv_ratio']-1),leader_code=lead['code'],leader_name=lead['name'],leader_turnover_ratio=lead['tv_ratio']))
    return sorted(rs,key=lambda x:(x['rs_defense_confirmed'],x['score']),reverse=True),sorted(rot,key=lambda x:x['score'],reverse=True),len(samples)


def _install_independent_daily(e):
    """Publish distinct supplemental watch lanes; native engines remain untouched."""
    names=('RS_DAILY_DISCOVERY','ROTATION_DAILY_PROXY')
    e.VIEW_LANES=tuple(dict.fromkeys((*e.VIEW_LANES,*names)))
    original=e.Operations.scan
    def scan(self,current=None):
        result=original(self,current)
        if not isinstance(result,dict) or result.get('status')=='BUSY':return result
        t=current or e.now()
        # Shared operations lock prevents concurrent reconciliation and scan metadata writes.
        if not hasattr(self,'source'):return result
        if not self.lock.acquire(False):return result
        hist=None
        try:
            rows,manifest,complete,_,errors=self.source('KRX')
            if current is None:t=e.now()
            day=e.expected_closed_daily_date(manifest,t,'KRX'); required=e.recent_sessions(day,21) if day else None
            hist,_,_=self.history.read_all()
            # Bound memory to 21 actual candles per code and retain only validated history.
            histories={e.code(r):_hvtb_recent_history(hist,e.code(r),day,21) for r in rows if e.valid_code(e.code(r))} if required else {}
            proof=set()
            # Use fresh, same-venue native sector labels only as reference enrichment;
            # no guessed industry, investor flow or new broker request.
            aux=self.local('alpha','KRX')
            sector_by_code={}
            if isinstance(aux,dict):
                for key in ('sector_live_pool','live_pool','top_watch','top_final'):
                    for z in aux.get(key,[]) or []:
                        if not isinstance(z,dict):continue
                        venue=e.venue(z) or e.venue(aux)
                        at=e.pick(z,'source_timestamp','timestamp_kst','price_asof') or aux.get('timestamp_kst')
                        label=e.pick(z,'sector','ENERGY_CLUSTER')
                        if venue=='KRX' and label and e.fresh(at,t,1200):sector_by_code[e.code(z)]=label
            normalized=[]
            for raw in rows:
                r=_hvtb_authority_adapter(raw);c=e.code(r)
                match,identity=e._V11_SCOPE_ENTRY(self,'KRX',c,t)
                if r.get('common_stock_confirmed') is None and identity is not None:r['common_stock_confirmed']=identity
                if not r.get('sector') and c in sector_by_code:r['sector']=sector_by_code[c]
                # EOD benchmark is independent of intraday freshness, but no stale
                # quote can create a new current watch candidate or a verified miss.
                if not e.fresh(r.get('price_asof'),t) or r.get('quality') in ('EXPIRED','REST_STALE','STALE'):
                    r['change_pct']=None;r['change_rate']=None
                normalized.append(r)
            rs,rot,n=_independent_daily_discovery(normalized,histories,required,cap_max=_cap_ceiling(),proof=proof)
            proven={e.code(r) for r in normalized if e.code(r) in proof and r.get('change_pct') is not None and r.get('common_stock_confirmed') is True and e._V11_SCOPE_ENTRY(self,'KRX',e.code(r),t)[0] is True}
            # Rotation has stricter evidence than RS: cannot expire a prior rotation
            # candidate when the sector or monetary turnover input is missing.
            from collections import Counter
            sector_counts=Counter(str(r.get('sector') or '').strip() for r in normalized if e.code(r) in proof)
            rot_proven={e.code(r) for r in normalized if e.code(r) in proven and str(r.get('sector') or '').strip() not in ('','UNAVAILABLE','UNKNOWN','NONE') and sector_counts[str(r.get('sector') or '').strip()]>=3 and all(_finite_number(b.get('turnover')) is not None for b in histories.get(e.code(r),[]))}
            self.runtime.pop('daily_discovery_error',None)
            for lane,items in zip(names,(rs,rot)):
                output=[]
                for z in items[:30]:
                    r=z['row']; c=z['code']; sig={'rs5':z['rs5'],'rs20':z['rs20'],'sector':z['sector']}
                    row=self.base(r,'KRX',sig,None,{}, {},histories.get(c,[]),t,reference_daily=day)
                    row.update(lane=lane,classification='WATCH',decision='PENDING',data_status='PENDING',lane_confirmed=False,history_current=True,history_expected_asof=day,selection_path=[lane,'COMPLETED_DAILY_PROXY_ONLY'],upstream_state='WATCH_PROXY',entry_phase='일봉 탐색 · 실제 수급 및 장중 진입 미확인',rs5=z['rs5'],rs20=z['rs20'],rs_delta=z['rs_delta'],proxy_score=z['score'],proxy_benchmark=z['benchmark'],proxy_benchmark_sample=n,turnover_ratio_5d=z['tv_ratio'],proxy_leader_code=z.get('leader_code'),proxy_leader_name=z.get('leader_name'),proxy_leader_turnover_ratio=z.get('leader_turnover_ratio'),gate_pending=['ACTUAL_FLOW_AND_INTRADAY_ENTRY_UNVERIFIED'],gate_failures=[],gates={'ACTUAL_FLOW_AND_INTRADAY_ENTRY':'PENDING'},promotion_conditions=['원천 레인 수급 및 진입 검증'],invalidation_conditions=['RS 약화','NO_CHASE'],live_board_state='WATCH')
                    output.append(row)
                # Preserve old observations on an unavailable input; no false EXIT.
                for row,z in zip(output,items[:30]):
                    row.update(rs_downmarket_days=z.get('downmarket_days'),rs_downmarket_excess=z.get('downmarket_excess'),rs_downmarket_win_ratio=z.get('downmarket_win_ratio'),rs_defense_confirmed=z.get('rs_defense_confirmed'),proxy_identity_status='VERIFIED_COMMON' if z['row'].get('common_stock_confirmed') is True else 'PENDING_SECURITY_IDENTITY')
                    if lane=='RS_DAILY_DISCOVERY':
                        row['selection_path'].append(z.get('rs_discovery_state','RELATIVE_STRENGTH_WATCH'))
                    else:
                        row['selection_path'].append('TURNOVER_ROTATION_PROXY_NOT_INVESTOR_FLOW')
                verified=proven if lane=='RS_DAILY_DISCOVERY' else rot_proven
                self.store.reconcile('KRX',lane,output,verified if complete else set(),t)
                self.runtime.setdefault('markets',{}).setdefault('KRX',{}).setdefault('lanes',{})[lane]={'status':'PARTIAL_VERIFIED' if n else 'PENDING','observed':len(output),'evaluated_scope':n,'verified_scope':len(verified),'prime':0,'universe_complete':False,'basis':'COMPLETED_DAILY_PROXY_ONLY','reference_date':day,'identity_verified_scope':sum(r.get('common_stock_confirmed') is True for r in normalized if e.code(r) in proof),'sector_known_scope':sum(bool(r.get('sector')) for r in normalized if e.code(r) in proof),'authority_complete':bool(complete)}
        except Exception as exc:
            self.runtime['daily_discovery_error']=type(exc).__name__
            for lane in names:
                self.runtime.setdefault('markets',{}).setdefault('KRX',{}).setdefault('lanes',{})[lane]={'status':'PENDING','error':type(exc).__name__,'universe_complete':False}
        finally:
            try:
                if hasattr(hist,'close'):hist.close()
            finally:self.lock.release()
        return result
    e.Operations.scan=scan


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
        "version": "HVTB_FINAL_PARALLEL_DISCOVERY_LIVE_GUARD",
        "tests": ["catalyst_not_mandatory", "overhang_hard_veto", "armed_not_buy", "cap_700eok"],
    }


__all__ = ["apply"]
