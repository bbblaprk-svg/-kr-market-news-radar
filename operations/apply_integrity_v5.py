#!/usr/bin/env python3
"""One-time migration of existing KRX/NXT operations to evidence-based candidate lifecycle.

This is a build-time source migration, NOT a background patch or runtime monkeypatch.
It refuses unrecognized source hashes and never alters KIS/LS source collectors.

Recommended:
    python apply_integrity_v5.py --repo . --export
    PYTHONPATH=SAFE_V5_STAGING python -m unittest operations.test_engine operations.test_candidate_integrity -q

After inspection, copy exactly the four generated operations files to GitHub.
Use --apply only in a checked-out backup branch and review the generated diff.
"""
from __future__ import annotations
import argparse
import difflib
import hashlib
import pathlib
import shutil

# Exact GitHub main source blob SHAs from 2026-11-16 19:58 KST.
EXPECTED = {
    'engine.py': '22509df0a5e20ded43c602514c77931587d9305b',
    'dashboard.html': '6014c60364d0a78769c58c655d165796452e8fa6',
    'test_engine.py': '39120e18070207d355e7337eda1c56384b7a5c97',
}
HISTORICAL_FIXTURE = {
    'engine.py':'58131fc1f81187663f0c30e069475efeb6085e40',
    'dashboard.html':'133a711a1b90ae24447e6a6958d5354ded46d37b',
    'test_engine.py':'4799a74c2bb09667aff224c5500bb2da7ce86dcf',
}


def git_blob_sha(s: str) -> str:
    b = s.encode('utf-8')
    return hashlib.sha1(b'blob ' + str(len(b)).encode() + b'\0' + b).hexdigest()


def once(s: str, before: str, after: str, title: str) -> str:
    count = s.count(before)
    if count != 1:
        raise ValueError(f'SAFE STOP: {title}; expected 1 match, got {count}')
    return s.replace(before, after, 1)


def modify_engine(s: str) -> str:
    # Stable, independent status/evidence module; unchanged market/broker clients.
    s=once(s,'from .universe_audit import classify_window, universe_membership_audit',
            'from .universe_audit import classify_window, universe_membership_audit\n'
            'from .candidate_integrity import verified_scope, data_hold, native_nxt_minute_proven', 'module dependency')
    if "VERSION = 'KRX_NXT_OPERATIONS_V4_2_CANDIDATE_LIFECYCLE_FIX'" in s:
        s=once(s,"VERSION = 'KRX_NXT_OPERATIONS_V4_2_CANDIDATE_LIFECYCLE_FIX'",
               "VERSION = 'KRX_NXT_OPERATIONS_V5_EVIDENCE_LEDGER'",'version traceability')
    elif "VERSION = 'KRX_NXT_OPERATIONS_V4_1'" in s:
        s=once(s,"VERSION = 'KRX_NXT_OPERATIONS_V4_1'",
               "VERSION = 'KRX_NXT_OPERATIONS_V5_EVIDENCE_LEDGER'",'fixture version traceability')
    else:
        raise ValueError('SAFE STOP: unknown Operations VERSION')
    s=once(s,
           '                re25_window_ready=re25_window_exempt=re25_window_pending=0',
           '                re25_window_ready=re25_window_exempt=re25_window_pending=0\n'
           '                re25_evaluated_codes=set()  # audited per-stock verified completed 6-session windows',
           'RE25 per-stock evidence register')
    s=once(s,
           "                        if eligibility.state=='COMPLETE' and re25_window_complete:\n"
           "                            re25_window_ready+=1",
           "                        if eligibility.state=='COMPLETE' and re25_window_complete:\n"
           "                            re25_window_ready+=1\n"
           "                            re25_evaluated_codes.add(c)",
           'RE25 audited per-stock completion')

    old="""                    lane_complete=complete and applicable and (set(upstream.get('evaluated_codes',[])) or bool(upstream.get('universe_complete')) if lane=='V17_ACTION_PRE' else bool(fm.get('universe_complete')) if lane=='CAPITAL_HANDOFF' else bool(nxt.get('universe_complete')) if lane=='NXT_POST_ACCEL_LANE' else full_recent if lane=='RE_EXPLOSION_25' else len(alpha)>=len(rows))"""
    new="""                    if lane=='RE_EXPLOSION_25':
                        # A latest-date header alone is NOT proof all six OHLCV sessions exist.
                        # Each verified symbol may exit independently, even if the broad scan is partial.
                        evidence=verified_scope('KRX',lane,re25_evaluated_codes,
                            source_complete=bool(complete and len(re25_reference_window)==6),
                            last_close=global_history_latest,expected_close=expected_daily,
                            reason='KRX_SIX_SESSION_KIS_LS_VERIFIED') if market=='KRX' else None
                        lane_complete=set(evidence.evaluated_codes) if evidence and evidence.source_complete else set()
                    else:
                        # An upstream 'complete' flag from an old snapshot is
                        # not evidence of a newly evaluated rejection.
                        # Require fresh producer run time; do NOT infer exchange
                        # quote freshness or remove a candidate on stale data.
                        if lane=='V17_ACTION_PRE':
                            ready=set(upstream.get('evaluated_codes',[])) if fresh(upstream.get('generated_at'),current,1200) else set()
                        elif lane=='CAPITAL_HANDOFF':
                            ready=bool(fm.get('universe_complete')) and fresh(fm.get('generated_at'),current,1200)
                        elif lane=='NXT_POST_ACCEL_LANE':
                            # integration.nxt() has source_asof=None by design;
                            # use native per-stock minute-series timestamps and
                            # 60-bar coverage instead of a nonexistent global clock.
                            ready={c for c,z in nxtrows.items() if native_nxt_minute_proven(z,current)}
                        elif lane=='ASYMMETRIC_RS':
                            ready=bool(len(alpha)>=len(rows)) and fresh(alpha_obj.get('timestamp_kst'),current,1200)
                        else:ready=False
                        lane_complete=ready if complete and applicable else set()"""
    s=once(s,old,new,'per-code RE25 expiration evidence')

    # A verified MISS can update last_at; only a confirmed positive sighting
    # may lift a candidate above a newly discovered one in the board.
    old_sort = """" WHEN 'REJECT' THEN 4 ELSE 5 END, last_at DESC"+"""
    new_sort = """" WHEN 'REJECT' THEN 4 ELSE 5 END,"+
                     " COALESCE(json_extract(payload,'$.candidate_seen_at'),last_at) DESC, last_at DESC"+"""
    s=once(s,old_sort,new_sort,'sort on positive verified candidate_seen_at')

    # Row presence in an upstream feed is NOT proof of a tradeable signal.
    # Separate observations from completed per-security evaluations. This also
    # blocks false PRIME/outcome creation when an upstream feed is partial.
    old="""                row.update(candidate_seen_at=at,candidate_miss_count=0,candidate_stale=False,
                           candidate_last_miss_at=None,candidate_lifecycle='CONFIRMED_THIS_CYCLE')"""
    new="""                source_proven=(complete is True or
                               (isinstance(complete,set) and row['code'] in complete))
                if old and not old['active'] and not source_proven:
                    # A previously closed episode cannot be reopened by an
                    # unverified observation (even if an old upstream cache repeats it).
                    continue
                if not source_proven:
                    if row.get('classification')=='PRIME':row['classification']='WATCH'
                    row['decision']='PENDING'
                    row['data_status']='PENDING'
                    row['gate_pending']=sorted(set(list(row.get('gate_pending') or [])+['SOURCE_RECONFIRM_PENDING']))
                row.update(candidate_seen_at=at if source_proven else prev.get('candidate_seen_at'),
                           candidate_observed_at=at,
                           candidate_miss_count=0 if source_proven else int(prev.get('candidate_miss_count') or 0),
                           candidate_stale=False if source_proven else (prev.get('candidate_stale') is True),
                           candidate_last_miss_at=None if source_proven else prev.get('candidate_last_miss_at'),
                           candidate_lifecycle='CONFIRMED_THIS_CYCLE' if source_proven else 'OBSERVED_UNVERIFIED')"""
    s=once(s,old,new,'observed != evaluated candidate')
    # Keep ordering based on last validated selection; a delayed provider must
    # not refresh the sort priority of obsolete candidates every 10 minutes.
    s=once(s,
       """                row.update(first_seen_at=old['first_at'] if old else at,
                           occurred_at=at if changed or event in ('NEW','REENTER') else prev.get('occurred_at',at),
                           last_seen_at=at,occurrences=occ)""",
       """                row.update(first_seen_at=old['first_at'] if old else at,
                           occurred_at=at if source_proven and (changed or event in ('NEW','REENTER')) else prev.get('occurred_at',at),
                           last_seen_at=at,occurrences=occ)""",
       'unverified price cannot fake newly occurred signal time')
    s=once(s,
       """                c.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (key,market,lane,row['code'],1,json.dumps(clean(row),ensure_ascii=False),row['first_seen_at'],at,occ,sid))""",
       """                c.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (key,market,lane,row['code'],1,json.dumps(clean(row),ensure_ascii=False),
                           row['first_seen_at'],at if source_proven or not old else old['last_at'],occ,sid))""",
       'sort based on validated candidate activity')

    s=once(s,"'EXIT' if row['classification']=='REJECT' and prev.get('classification')!='REJECT'",
             "'EXIT' if source_proven and row['classification']=='REJECT' and prev.get('classification')!='REJECT'",
             'avoid unverified EXIT during updated row')
    s=once(s,"'REENTER' if prev.get('candidate_stale') is True",
             "'REENTER' if source_proven and prev.get('candidate_stale') is True",
             'avoid unverified REENTER')

    # A stored DATA_HOLD candidate may display reference prices, but must not
    # run a live rearm transition until the upstream evaluation is proven.
    s=once(s,
       """                    if lane=='RE_EXPLOSION_25':
                        newrow,_=re25_runtime_transition(newrow,q,current)""",
       """                    if newrow.get('candidate_lifecycle')=='DATA_HOLD':
                        newrow['runtime_lane_state']='DATA_HOLD'
                        newrow['re25_state_refresh_status']='PENDING_SOURCE_RECONFIRM' if lane=='RE_EXPLOSION_25' else None
                    elif lane=='RE_EXPLOSION_25':
                        newrow,_=re25_runtime_transition(newrow,q,current)""",
       'stale data cannot rearm a RE25 candidate')

    # State machine: only evaluated missing candidates increment true miss count.
    # A source outage is DATA_HOLD, cannot generate EXIT or consume the 3-cycle budget.
    start="                if fully_evaluated:"
    i=s.find(start)
    if i<0 or s.count(start)!=1:raise ValueError('SAFE STOP: candidate lifecycle entry changed')
    j=s.find('    def track(self,market,quotes,current):',i)
    if j<0:raise ValueError('SAFE STOP: next store method changed')
    old_branch=s[i:j]
    for key in ('candidate_miss_count','candidate_stale','ZERO_BASE_RESCAN_NOT_SELECTED',
                'SOURCE_INCOMPLETE'):
        if key not in old_branch:raise ValueError('SAFE STOP: prior lifecycle changed: '+key)
    replacement="""                if not fully_evaluated:
                    # Cannot equate missing provider/market evidence with a losing setup.
                    p=data_hold(p,at=at,reason='SOURCE_INCOMPLETE')
                    # Do not advance candidate last_at on a missing feed: old watch
                    # entries must not outrank genuinely refreshed stocks.
                    c.execute('UPDATE candidates SET payload=? WHERE key=?',
                              (json.dumps(clean(p),ensure_ascii=False),key))
                    continue
                # A true missing confirmation must be tied to a completed
                # per-stock evaluation proof. Three separate verified cycles
                # are required before a normal EXIT; source outages do not count.
                previous_miss_at=dt(p.get('candidate_last_miss_at'))
                scan_at=dt(at)
                distinct=not (previous_miss_at and scan_at and scan_at<=previous_miss_at)
                missed=min(max_misses,int(p.get('candidate_miss_count') or 0)+(1 if distinct else 0))
                if missed<max_misses:
                    p.update(classification='WATCH',decision='PENDING',data_status='PENDING',
                             candidate_miss_count=missed,candidate_stale=False,
                             candidate_last_miss_at=at if distinct else p.get('candidate_last_miss_at'),
                             candidate_lifecycle='VERIFIED_MISS_'+str(missed),
                             gate_pending=sorted(set(list(p.get('gate_pending') or [])+['CONFIRMED_RESCAN_NOT_SELECTED'])))
                    c.execute('UPDATE candidates SET payload=?,last_at=? WHERE key=?',
                              (json.dumps(clean(p),ensure_ascii=False),at,key))
                    continue
                c.execute('UPDATE candidates SET active=0,last_at=? WHERE key=?',(at,key))
                c.execute('INSERT INTO events(at,market,lane,code,name,event,reason,payload) VALUES(?,?,?,?,?,?,?,?)',
                          (at,market,lane,p['code'],p['name'],'EXIT',
                           'VERIFIED_CODE_NOT_SELECTED_3_CYCLES',old['payload']))
"""
    s=s[:i]+replacement+s[j:]
    return s


MOBILE_CSS="""
/* Permanent first-column quote view: no front-end API changes or synthetic prices. */
.table-scroll th:first-child,.table-scroll td:first-child{min-width:248px;max-width:280px}
.stock-button{display:block;width:100%;min-width:0;white-space:normal}
.stock-top,.stock-bottom{display:flex;align-items:baseline;justify-content:space-between;gap:7px;width:100%}
.stock-name{font-weight:750;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:113px}
.stock-quote{font-size:13px;font-weight:800;white-space:nowrap;line-height:1.4;color:var(--text)}
.stock-change{font-size:11px;font-weight:750;white-space:nowrap}
.stock-change.negative{color:var(--red)}.stock-change.positive{color:var(--green)}
.stock-quality{font-size:10px;line-height:1.4;color:var(--amber);white-space:normal;display:block;margin-top:3px}
.stock-quality.verified{color:var(--muted)}
@media(max-width:750px){.table-scroll th:first-child,.table-scroll td:first-child{min-width:240px;max-width:255px}.stock-name{max-width:105px}}
"""


def modify_dashboard(s: str) -> str:
    s=once(s,'</style>',MOBILE_CSS+'</style>','mobile CSS')
    old='function stock(r,i){return \'<button class="stock-button" data-detail="\'+i+\'">\'+esc(r.name)+\'<small>\'+esc(r.code)+\' · \'+esc(r.market)+\'</small></button>\'}'
    new="""function stock(r,i){
 const px=typeof r.price==='number'&&Number.isFinite(r.price)&&r.price>0?fmt(r.price,0)+'원':'시세 미수신';
 const ch=typeof r.change_pct==='number'&&Number.isFinite(r.change_pct)?r.change_pct:null;
 const pct=ch===null?'등락률 미확인':(ch>0?'+':'')+fmt(ch,2)+'%';
 const direction=ch===null?'':ch<0?' negative':ch>0?' positive':'';
 const late=r.quote_quality==='REST_STALE'||r.runtime_quote_grade==='STALE'||r.quote_quality==='EXPIRED';
 const source=px==='시세 미수신'?'가격 데이터 없음':late?'지연 시세 · 진입 검증 불가':r.timestamp_quality==='EXCHANGE_TIMESTAMP'?'거래소 시각 확인 · 필수 게이트 별도':'KIS REST 참고가 · 체결시각 미검증';
 const pxTime=missing(r.price_asof)?'시각 미확인':when(r.price_asof);
 return '<button class="stock-button" data-detail="'+i+'"><span class="stock-top"><span class="stock-name">'+esc(r.name)+'</span><span class="stock-quote">'+esc(px)+'</span></span><span class="stock-bottom"><small>'+esc(r.code)+' · '+esc(r.market)+'</small><span class="stock-change'+direction+'">'+esc(pct)+'</span></span><span class="stock-quality'+(late?'':' verified')+'">'+esc(source)+' · '+esc(pxTime)+'</span></button>';
}"""
    s=once(s,old,new,'fixed-position price/date display')
    s=once(s,'let shown=[];','let shown=[],latestScan={};','empty lane source state')
    s=once(s,'function render(){',"""function emptyStateMessage(){
 if(tab==='RE_EXPLOSION_25'&&market==='KRX'){
  const a=latestScan||{},s=a.re25_universe_status||'PENDING';
  if(!['COMPLETE','COMPLETE_EVALUABLE_WITH_VERIFIED_EXCLUSIONS'].includes(s))
   return '<strong>DATA_PENDING · 전체시장 재급등 판정 미완료</strong><div class="cellnote">평가 검증 '+esc(a.re25_evaluable_symbols??'확인중')+' / '+esc(a.rows_scanned??'확인중')+'종목 · 일봉 미확인 '+esc(a.re25_pending_symbols??'확인중')+'종목 · 이전 후보는 DATA_HOLD로 보존되며 매수 신호가 아닙니다.</div>';
  return '<strong>전체 평가 완료 · 현재 표시 조건 충족 후보 0건</strong>';
 }
 if(tab==='NXT_POST_ACCEL_LANE'&&market==='KRX')return '<strong>NXT는 NXT 시장 탭에서 조회</strong>';
 if((latestScan||{}).status!=='READY')return '<strong>DATA_PENDING · 평가 근거 미완료</strong>빈 화면은 후보 탈락을 의미하지 않습니다.';
 return '<strong>조건 충족 후보 0건</strong>';
}
function render(){""",'truthful pending vs zero candidates')
    s=once(s,"$('tbody').innerHTML=body||'<tr><td colspan=\"'+heads.length+'\"><div class=\"empty\"><strong>PENDING · 검증된 표시 자료 대기</strong>'+(tab==='NXT_POST_ACCEL_LANE'&&market==='KRX'?'NXT 후가속은 상단의 NXT 시장에서 확인할 수 있습니다.':'이 레인의 실제 판정 자료가 아직 없습니다. 후보를 임의로 생성하지 않습니다.')+'</div></td></tr>';",
           "$('tbody').innerHTML=body||'<tr><td colspan=\"'+heads.length+'\"><div class=\"empty\">'+emptyStateMessage()+'</div></td></tr>';",
           'blank state semantics')
    s=once(s,'const scan=b.value.scan||{};', 'const scan=b.value.scan||{};latestScan=scan;', 'state from API')
    s=once(s,'market=m.dataset.market;board=[];', 'market=m.dataset.market;latestScan={};board=[];', 'isolate KRX/NXT view state')
    return s

TESTS="""
# Evidence-based candidate retention: persistent local SQLite path.
class IntegrityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'persist.db')
    def row(self,market='KRX',lane='RE_EXPLOSION_25',code='005930'):
        return dict(code=code,name='검증용종목',market=market,lane=lane,
                    classification='WATCH',decision='PENDING',data_status='PENDING',
                    gate_pending=[],re25_setup_state='COMPRESSION_ARMED',
                    re25_runtime_state='WATCH',entry_phase='조건 대기')
    def test_fifty_incomplete_scans_never_exit_or_count_real_miss(self):
        self.store.reconcile('KRX','RE_EXPLOSION_25',[self.row()],False,AT)
        for i in range(1,51):
            self.store.reconcile('KRX','RE_EXPLOSION_25',[],set(),AT+timedelta(minutes=10*i))
        v=self.store.board('KRX',lane='RE_EXPLOSION_25')
        self.assertEqual(v['total'],1)
        self.assertEqual(v['rows'][0]['candidate_miss_count'],0)
        self.assertEqual(v['rows'][0]['candidate_lifecycle'],'DATA_HOLD')
        ev=self.store.records('events','KRX')['rows']
        self.assertFalse(any(e['event']=='EXIT' for e in ev))
        self.assertEqual(sum(e['event']=='DATA_HOLD' for e in ev),0)
    def test_proven_complete_code_may_exit_but_other_code_cannot(self):
        for code in ('005930','0120G0'):
            self.store.reconcile('KRX','RE_EXPLOSION_25',[self.row(code=code)],False,AT)
        for i in range(1,4):self.store.reconcile('KRX','RE_EXPLOSION_25',[],{'0120G0'},AT+timedelta(minutes=10*i))
        survivors=self.store.board('KRX',lane='RE_EXPLOSION_25')['rows']
        self.assertEqual([s['code'] for s in survivors],['005930'])
        ev=self.store.records('events','KRX')['rows']
        self.assertEqual(len([e for e in ev if e['event']=='EXIT']),1)
        self.assertEqual(next(e['reason'] for e in ev if e['event']=='EXIT'),
                         'VERIFIED_CODE_NOT_SELECTED_3_CYCLES')
    def test_nxt_and_krx_same_code_is_isolated(self):
        self.store.reconcile('KRX','RE_EXPLOSION_25',[self.row()],False,AT)
        self.store.reconcile('NXT','NXT_POST_ACCEL_LANE',[self.row('NXT','NXT_POST_ACCEL_LANE')],False,AT)
        self.store.reconcile('NXT','NXT_POST_ACCEL_LANE',[],set(),AT+timedelta(minutes=1))
        self.assertEqual(self.store.board('KRX',lane='RE_EXPLOSION_25')['total'],1)
        self.assertEqual(self.store.board('NXT',lane='NXT_POST_ACCEL_LANE')['total'],1)
    def test_unverified_prime_cannot_open_a_tracked_trade(self):
        p=self.row()
        p.update(classification='PRIME',decision='PRIME',entry_price=100,
                 stop_price=96,target_price=112)
        self.store.reconcile('KRX','RE_EXPLOSION_25',[p],set(),AT)
        rows=self.store.board('KRX',lane='RE_EXPLOSION_25')['rows']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['classification'],'WATCH')
        self.assertEqual(rows[0]['candidate_lifecycle'],'OBSERVED_UNVERIFIED')
        self.assertEqual(self.store.records('outcomes','KRX')['total'],0)
    def test_partial_source_does_not_reenter_stale_candidate(self):
        p=self.row()
        self.store.reconcile('KRX','RE_EXPLOSION_25',[p],True,AT)
        for i in range(1,4):self.store.reconcile('KRX','RE_EXPLOSION_25',[],{'005930'},AT+timedelta(minutes=i))
        # An unverified observation must NOT reenter or become PRIME.
        self.store.reconcile('KRX','RE_EXPLOSION_25',[self.row()],set(),AT+timedelta(minutes=5))
        self.assertEqual(self.store.records('candidates','KRX')['total'],0)

    def test_outage_does_not_make_old_candidate_newest(self):
        first=self.row(code='005930')
        second=self.row(code='0120G0')
        self.store.reconcile('KRX','RE_EXPLOSION_25',[first],True,AT)
        self.store.reconcile('KRX','RE_EXPLOSION_25',[],set(),AT+timedelta(minutes=10))
        self.store.reconcile('KRX','RE_EXPLOSION_25',[second],True,AT+timedelta(minutes=20))
        self.store.reconcile('KRX','RE_EXPLOSION_25',[],set(),AT+timedelta(minutes=30))
        visible=self.store.board('KRX',lane='RE_EXPLOSION_25')['rows']
        self.assertEqual(visible[0]['code'],'0120G0')
        self.assertEqual(visible[1]['code'],'005930')
    def test_source_age_never_substitutes_for_executed_trade(self):
        self.assertFalse(fresh(stamp(AT-timedelta(minutes=30)),AT,1200))
        self.assertTrue(fresh(stamp(AT-timedelta(minutes=8)),AT,1200))
        self.assertFalse(fresh('2026-11-13',AT,1200))
    def test_scan_protects_stale_producer_snapshots(self):
        import inspect
        src=inspect.getsource(Operations.scan)
        self.assertIn("fresh(fm.get('generated_at'),current,1200)",src)
        self.assertIn("native_nxt_minute_proven(z,current)",src)
    def test_ui_has_pinned_quote_without_fabricated_exchange_time(self):
        html=Path(__file__).with_name('dashboard.html').read_text(encoding='utf-8')
        for keyword in ('stock-quote','REST_STALE','DATA_PENDING · 전체시장','function emptyStateMessage()'):
            self.assertIn(keyword,html)
"""


def modify_tests(s: str) -> str:
    if 'class IntegrityPersistenceTests' in s:
        raise ValueError('already integrated')
    if 'from .engine import *' not in s or 'class EngineTests(unittest.TestCase)' not in s:
        raise ValueError('unknown tests layout')
    # Previously source-outage cycles were incorrectly treated as misses.
    # Replace assertions in the pre-existing test, without weakening its checks.
    old_header='def test_partial_lane_hides_after_three_unconfirmed_cycles(self):'
    if s.count(old_header)!=1:raise ValueError('SAFE STOP: old partial-lane regression missing')
    s=s.replace(old_header,'def test_partial_lane_data_hold_does_not_count_as_failure(self):',1)
    s=once(s,"self.assertTrue(row['candidate_stale']);self.assertEqual(row['candidate_miss_count'],3)",
           "self.assertFalse(row['candidate_stale']);self.assertEqual(row['candidate_miss_count'],0)",
           'old outage test now verifies no false miss')
    s=once(s,"self.assertEqual(self.store.board('KRX',80,0,'ASYMMETRIC_RS')['total'],0)",
           "self.assertEqual(self.store.board('KRX',80,0,'ASYMMETRIC_RS')['total'],1)",
           'old outage test remains visible, not hidden')
    # The original lifecycle test asserted immediate EXIT. Now require three
    # distinct, truly verified scan cycles and preserve the data-hold assertion.
    old="""        reopened.reconcile('KRX',r['lane'],[],True,AT+timedelta(minutes=2))
        self.assertEqual(reopened.records('events','KRX')['rows'][0]['event'],'EXIT')"""
    new="""        for n in range(2,5):
            reopened.reconcile('KRX',r['lane'],[],True,AT+timedelta(minutes=n))
        self.assertEqual(reopened.records('events','KRX')['rows'][0]['event'],'EXIT')"""
    s=once(s,old,new,'three verified misses before exit')
    s=once(s,"reopened.reconcile('KRX',r['lane'],[self.evaluate()],True,AT+timedelta(minutes=3))",
           "reopened.reconcile('KRX',r['lane'],[self.evaluate()],True,AT+timedelta(minutes=5))",
           'reentry must occur after third completed verification')

    old="""        self.store.reconcile('KRX',r['lane'],[],{r['code']},AT)
        self.assertEqual(self.store.records('candidates','KRX')['total'],0)"""
    new="""        for n in range(1,4):
            self.store.reconcile('KRX',r['lane'],[],{r['code']},AT+timedelta(minutes=n))
        self.assertEqual(self.store.records('candidates','KRX')['total'],0)"""
    s=once(s,old,new,'per-stock triple confirmed exit')
    return s.rstrip()+'\n'+TESTS+'\n' 


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--check',action='store_true',help='print exact diffs, no changes')
    group.add_argument('--export',action='store_true',help='export complete ready-to-upload files without touching original')
    group.add_argument('--apply',action='store_true',help='apply directly on a checked-out backup branch only')
    p.add_argument('--repo',default='.')
    p.add_argument('--out',default='SAFE_V5_STAGING')
    p.add_argument('--historical-test-fixture',action='store_true',help='test old V4.1 fixture ONLY; not for deploy')
    args=p.parse_args();repo=pathlib.Path(args.repo).resolve();src=repo/'operations'
    expected=HISTORICAL_FIXTURE if args.historical_test_fixture else EXPECTED
    names=['engine.py','dashboard.html','test_engine.py'];before={};after={}
    for name in names:
        f=src/name
        if not f.exists():raise SystemExit(f'SAFE STOP: missing file {f}')
        before[name]=f.read_text(encoding='utf-8')
        digest=git_blob_sha(before[name])
        if digest!=expected[name]:raise SystemExit(f'SAFE STOP {name}: current GitHub SHA {digest} != audited {expected[name]}; no changes')
    base=pathlib.Path(__file__).resolve().parent
    for aux in ('candidate_integrity.py','test_candidate_integrity.py'):
        if not (base/aux).exists():raise SystemExit(f'SAFE STOP: missing {aux}')
    after={'engine.py':modify_engine(before['engine.py']),
           'dashboard.html':modify_dashboard(before['dashboard.html']),
           'test_engine.py':modify_tests(before['test_engine.py'])}
    for name in names:
        delta=''.join(difflib.unified_diff(before[name].splitlines(True),after[name].splitlines(True),
                      fromfile='old/'+name,tofile='new/'+name))
        print(name, 'original=',len(before[name]),'new=',len(after[name]),'diff lines=',len(delta.splitlines()))
        if args.check:print(delta)
    if args.apply:
        backup=repo/'.backup_v5_integrity'
        if backup.exists():raise SystemExit('SAFE STOP: existing backup directory must be moved first')
        backup.mkdir()
        for name in names:
            shutil.copy2(src/name,backup/name)
        for name in names:
            (src/name).write_text(after[name],encoding='utf-8')
        for name in ('candidate_integrity.py','test_candidate_integrity.py'):
            if (base/name).resolve() != (src/name).resolve():
                shutil.copy2(base/name,src/name)
        print('APPLIED. Original 3 files at',backup,'; build/test before committing.')
    elif args.export:
        output=(repo/args.out).resolve()
        if output==src or src in output.parents:raise SystemExit('output must be outside operations/')
        target=output/'operations';target.mkdir(parents=True,exist_ok=True)
        for name in names:(target/name).write_text(after[name],encoding='utf-8')
        for name in ('candidate_integrity.py','test_candidate_integrity.py'):
            shutil.copy2(base/name,target/name)
        print('EXPORTED 5 complete operations files:',target)
    else:print('SAFE CHECK ONLY; nothing written')

if __name__=='__main__':main()
