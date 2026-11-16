#!/usr/bin/env python3
"""Conservative operations-only hotfix for bbblaprk-svg/-kr-market-news-radar.

Usage (at repository root):
  python KRX_NXT_OPERATIONS_HOTFIX_20261116_V4.py --check
  python KRX_NXT_OPERATIONS_HOTFIX_20261116_V4.py --apply

Does not modify the native KIS/LS collector, shared cache, or Dockerfile.
Exact anchors and atomic writes prevent accidental changes to unfamiliar versions.
"""
from __future__ import annotations
import argparse
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

VERSION = 'KRX_NXT_OPS_HOTFIX_20261116_D_FULLVIEW_SESSION_GUARDED'
FILES = ('operations/engine.py', 'operations/integration.py',
         'operations/dashboard.html', 'operations/test_engine.py', 'operations/test_v41.py')


def replace_exact(text, before, after, label):
    if after in text:
        return text
    n = text.count(before)
    if n != 1:
        raise RuntimeError(f'{label}: expected one anchor, got {n}')
    return text.replace(before, after, 1)


def engine_patch(src):
    s = src
    s = replace_exact(s,
        "NEWS_BLOCK = {'PRICED_IN', 'SELL_THE_NEWS', 'NEGATIVE_CONFIRMED'}",
        "NEWS_BLOCK = {'PRICED_IN', 'SELL_THE_NEWS', 'NEGATIVE_CONFIRMED', 'OVERHANG', 'NEGATIVE_OVERHANG', 'POSITIVE_NEWS_FAILED'}\nNEWS_ALLOW = {'PASS', 'CLEAR', 'POSITIVE_CONFIRMED'}",
        'news state sets')
    s = replace_exact(s,
        "gates['NEWS']=False if row.get('news_gate') in NEWS_BLOCK else (True if row.get('news_gate') in ('PASS','CLEAR','POSITIVE_CONFIRMED') else None)",
        "gates['NEWS']=False if row.get('news_gate') in NEWS_BLOCK else (True if row.get('news_gate') in NEWS_ALLOW else None)",
        'news gate')
    s = replace_exact(s,
        "def fresh(at, current, seconds=180):\n    d = dt(at)\n    return d is not None and -5 <= (current-d).total_seconds() <= seconds",
        "def fresh(at, current, seconds=180):\n    d = dt(at)\n    return d is not None and -5 <= (current-d).total_seconds() <= seconds\n\ndef verified_session_open(market,current,source_value=None):\n    # Use the bundled XKRX calendar; never infer a trading day from weekday alone.\n    explicit=tri(source_value)\n    session=is_snapshot_session(current.date().isoformat())\n    if session is False:return False\n    if session is not True:return explicit\n    hm=current.hour*60+current.minute\n    window=(540<=hm<930) if market=='KRX' else ((480<=hm<1200) if market=='NXT' else False)\n    return False if explicit is False else window",
        'calendar verified session helper')
    s = replace_exact(s,
        "global_daily_current=(global_history_latest==expected_daily) if expected_daily else None",
        "# An unclosed current-day LS bar must not displace the last\n                # verified completed KRX session in the EOD window.\n                global_daily_current=(expected_daily in dates) if expected_daily else None\n                verified_history_latest=max((d for d in dates if d<=expected_daily),default=None) if expected_daily else None\n                newer_unclosed_dates=[d for d in dates if expected_daily and d>expected_daily]",
        'completed daily session vs unclosed LS bar')
    s = replace_exact(s,
        "c=code(r);ls=alpha.get(c) or {};p=number(r.get('price'));q_at=r.get('source_timestamp')",
        "c=code(r);ls=alpha.get(c) or {};p=number(r.get('price'));q_at=r.get('source_timestamp')\n        price_evidence=('EXCHANGE_LIVE' if r.get('source_timestamp_quality')=='EXCHANGE_TIMESTAMP' and fresh(q_at,current) and r.get('quality') not in ('EXPIRED','REST_STALE','STALE') else ('REST_REFERENCE_ONLY' if r.get('quality')=='REST_CURRENT' else 'STALE_OR_UNVERIFIED'))",
        'source timestamp evidence')
    s = replace_exact(s,
        "source='KIS_AUTHORITY',price_source=r.get('source'),ls_source=",
        "source='KIS_AUTHORITY',price_source=r.get('source'),price_evidence=price_evidence,ls_source=",
        'KIS quote evidence classification')
    s = replace_exact(s,
        "                        out['session_open']=tri(r.get('session_open'))\n                        if current.weekday()>=5:out['session_open']=False\n                        elif out['session_open'] is True:\n                            hm=current.hour*60+current.minute;out['session_open']=(540<=hm<930) if market=='KRX' else (480<=hm<1200)",
        "                        out['session_open']=verified_session_open(market,current,r.get('session_open'))\n                        out['session_source']='KIS_ROW_PLUS_VERSIONED_XKRX_CALENDAR'",
        'calendar verified session status')
    s = replace_exact(s,
        "history_latest_date=global_history_latest,history_expected_date=expected_daily",
        "history_latest_date=(verified_history_latest if market=='KRX' else global_history_latest),history_unclosed_or_future_cache_dates=(newer_unclosed_dates if market=='KRX' else []),history_expected_date=expected_daily",
        'daily history reference labeling')
    s = replace_exact(s,
        "pending_axes=dict(pending),lanes={k:{'observed':len(v),'status':'READY' if v and coverage_complete else 'PENDING','prime':sum(z['classification']=='PRIME' for z in v)} for k,v in lanes.items()},scan_at=stamp(current)",
        "pending_axes=dict(pending),classification_counts=dict(Counter(z['classification'] for lane_rows in lanes.values() for z in lane_rows)),pending_decision_count=sum(z['decision']=='PENDING' for lane_rows in lanes.values() for z in lane_rows),gate_pending_counts=dict(Counter(k for lane_rows in lanes.values() for z in lane_rows for k in z.get('gate_pending',[]))),gate_failure_counts=dict(Counter(k for lane_rows in lanes.values() for z in lane_rows for k in z.get('gate_failures',[]))),quote_timestamp_quality_counts=dict(Counter(str(r.get('source_timestamp_quality') or 'UNAVAILABLE') for r in rows)),lanes={k:{'observed':len(v),'status':'READY' if v and coverage_complete else 'PENDING','prime':sum(z['classification']=='PRIME' for z in v)} for k,v in lanes.items()},scan_at=stamp(current)",
        'accurate full-scan dashboard counters')
    s = replace_exact(s,
        "            CREATE INDEX IF NOT EXISTS events_market_id ON events(market,id);",
        "            CREATE INDEX IF NOT EXISTS events_market_id ON events(market,id);\n            CREATE INDEX IF NOT EXISTS candidates_market_lane_active ON candidates(market,lane,active,last_at);",
        'candidate lookup index')
    needle="    def records(self,kind,market,limit=500,offset=0,lane=None):"
    insertion='''    def board(self,market,limit=200,offset=0,lane=None):
        # Correct server-side pagination and full-set counts for the selected lane.
        where='market=? AND active=1';params=[market]
        if lane:where+=' AND lane=?';params.append(lane)
        with self.connect() as c:
            total=c.execute('SELECT COUNT(*) FROM candidates WHERE '+where,params).fetchone()[0]
            try:
                count_rows=c.execute(
                    "SELECT COALESCE(json_extract(payload,'$.classification'),'UNAVAILABLE'),COUNT(*) "
                    "FROM candidates WHERE "+where+
                    " GROUP BY COALESCE(json_extract(payload,'$.classification'),'UNAVAILABLE')",
                    params).fetchall()
                counts={str(k):int(v) for k,v in count_rows}
                pending_count=c.execute(
                    "SELECT COUNT(*) FROM candidates WHERE "+where+
                    " AND json_extract(payload,'$.decision')='PENDING'",params).fetchone()[0]
                sql=('SELECT payload FROM candidates WHERE '+where+
                     " ORDER BY CASE json_extract(payload,'$.classification')"+
                     " WHEN 'PRIME' THEN 0 WHEN 'ARMED' THEN 1"+
                     " WHEN 'PRE_EXPLOSION_ARMED' THEN 2 WHEN 'WATCH' THEN 3"+
                     " WHEN 'REJECT' THEN 4 ELSE 5 END, last_at DESC"+
                     ' LIMIT ? OFFSET ?')
                rows=[json.loads(z[0]) for z in c.execute(sql,params+[limit,offset])]
            except sqlite3.OperationalError:
                all_rows=[json.loads(z[0]) for z in c.execute(
                    'SELECT payload FROM candidates WHERE '+where,params)]
                counts=dict(Counter(z.get('classification','UNAVAILABLE') for z in all_rows))
                pending_count=sum(z.get('decision')=='PENDING' for z in all_rows)
                all_rows.sort(key=lambda z:(RANK.get(z.get('classification'),9),
                    -(dt(z.get('last_seen_at')).timestamp() if dt(z.get('last_seen_at')) else 0)))
                rows=all_rows[offset:offset+limit]
        return {'total':total,'rows':rows,'offset':offset,'limit':limit,
                'counts':{'classification_counts':counts,
                          'pending_decision_count':pending_count}}

'''
    s = replace_exact(s,needle,insertion+needle,'SQL grade pagination')
    s = replace_exact(s,
        "            data=self.store.records('candidates',m,1000,0,lane)\n            rows=[r['payload'] for r in data['rows']];rows.sort(key=lambda r:(RANK.get(r['classification'],9),-(dt(r.get('occurred_at')).timestamp() if dt(r.get('occurred_at')) else 0)),reverse=False)",
        "            data=self.store.board(m,limit,offset,lane)\n            rows=data['rows']",
        'board API bounded SQL')
    s = replace_exact(s,
        "'rows':rows[offset:offset+limit],'total':data['total']",
        "'rows':rows,'total':data['total'],'counts':data.get('counts',{})",
        'board API pagination output')
    return s


def integration_patch(src):
    return replace_exact(src,
        "gate=state if state in ('PRICED_IN','SELL_THE_NEWS','NEGATIVE_CONFIRMED') else meta.get('news_gate')\n            if gate not in ('PRICED_IN','SELL_THE_NEWS','NEGATIVE_CONFIRMED','PASS','CLEAR','POSITIVE_CONFIRMED'):continue",
        "recognized=('PRICED_IN','SELL_THE_NEWS','NEGATIVE_CONFIRMED','OVERHANG','NEGATIVE_OVERHANG','POSITIVE_NEWS_FAILED','NEGATIVE_ABSORBED','PASS','CLEAR','POSITIVE_CONFIRMED')\n            gate=state if state in recognized else meta.get('news_gate')\n            if gate not in recognized:continue",
        'news adapter original states')


def dashboard_patch(src):
    s=src
    s=replace_exact(s,
        "let market='KRX',tab='V17_ACTION_PRE',board=[],events=[],outcomes=[],loaded=200,totalHistory=0,lastFocus=null,requestId=0,loading=false,lastGood=null,controllerActive=null;",
        "let market='KRX',tab='V17_ACTION_PRE',board=[],events=[],outcomes=[],loaded=200,totalHistory=0,lastFocus=null,requestId=0,loading=false,lastGood=null,controllerActive=null,scanSummary={},boardStats={},boardTotal=0;",
        'UI board state')
    s=replace_exact(s,
        "function stats(){let items=[['승격 확인 PRIME',board.filter(r=>r.classification==='PRIME').length,'green'],['진입 대기 ARMED',board.filter(r=>['ARMED','PRE_EXPLOSION_ARMED'].includes(r.classification)).length,''],['관찰 WATCH',board.filter(r=>r.classification==='WATCH').length,''],['검증 대기 PENDING',board.filter(r=>r.decision==='PENDING').length,'amber'],['차단 REJECT',board.filter(r=>r.classification==='REJECT').length,'red']];",
        "function stats(){const c=boardStats.classification_counts||{},pending=boardStats.pending_decision_count;let items=[['승격 확인 PRIME',c.PRIME??0,'green'],['진입 대기 ARMED',(c.ARMED??0)+(c.PRE_EXPLOSION_ARMED??0),''],['관찰 WATCH',c.WATCH??0,''],['검증 대기 PENDING',pending??0,'amber'],['차단 REJECT',c.REJECT??0,'red']];",
        'UI exact selected-lane counts')
    s=replace_exact(s,
        "shown=source.slice(0,['events','outcomes'].includes(tab)||$('allRows').checked?source.length:20);$('rowCount').textContent=shown.length+' / '+source.length+'건';$('countMeta').textContent=['events','outcomes'].includes(tab)?'전체 저장 '+totalHistory+'건':'종목 클릭 → 승격 조건·무효화·데이터 검증 상세';",
        "shown=source.slice(0,['events','outcomes'].includes(tab)||$('allRows').checked?source.length:20);$('rowCount').textContent=['events','outcomes'].includes(tab)?shown.length+' / '+source.length+'건':shown.length+' 표시 / 원장 '+boardTotal+'건';$('countMeta').textContent=['events','outcomes'].includes(tab)?'전체 저장 '+totalHistory+'건':(board.length<boardTotal?'현재 '+board.length+'건 수신 · 전체 후보 보기를 켜면 전량 조회':'전량 수신 · 전체보기 중 자동갱신 일시정지 · 수동 새로고침');",
        'UI total vs current page')
    s=replace_exact(s,
        "const kinds=['board','status',...(['events','outcomes'].includes(t)?[t]:[])];const results=await Promise.allSettled(kinds.map(k=>fetchJSON('/api/operations/'+k+'?market='+m+'&limit='+(k==='board'?1000:loaded),controller.signal)));",
        "const kinds=['board',...(['events','outcomes'].includes(t)?[t]:[])];const laneArg=['news','events','outcomes'].includes(t)?'':'&lane='+encodeURIComponent(t);const boardLimit=$('allRows').checked?10000:300;const results=await Promise.allSettled(kinds.map(k=>fetchJSON('/api/operations/'+k+'?market='+m+'&limit='+(k==='board'?boardLimit:loaded)+(k==='board'?laneArg:''),controller.signal)));",
        'UI lane scoped bounded/full requests')
    s=replace_exact(s,
        "const b=results[0];if(b.status!=='fulfilled')throw Error('원장 화면 수신 실패');board=b.value.rows||[];lastGood=Date.now();const scan=b.value.scan||{};$('scanTime')",
        "const b=results[0];if(b.status!=='fulfilled')throw Error('원장 화면 수신 실패');board=b.value.rows||[];boardStats=b.value.counts||{};boardTotal=b.value.total??board.length;lastGood=Date.now();const scan=b.value.scan||{};scanSummary=scan;$('scanTime')",
        'UI receive exact totals and counts')
    s=replace_exact(s,
        "if(results[2]?.status==='fulfilled'){const d=results[2].value;",
        "if(results[1]?.status==='fulfilled'){const d=results[1].value;",
        'UI history result index')
    s=replace_exact(s,
        "}else if(results.length>2){$('notice').textContent+=' 이력 수신 PENDING · 이전 표시 자료 유지.';}",
        "}else if(results.length>1){$('notice').textContent+=' 이력 수신 PENDING · 이전 표시 자료 유지.';}",
        'UI history error index')
    s=replace_exact(s,
        "<dt>시각의 품질</dt><dd>'+fmt(r.timestamp_quality)+'</dd>",
        "<dt>시각의 품질 / 현재가 증거</dt><dd>'+fmt(r.timestamp_quality)+' / '+fmt(r.price_evidence)+'</dd>",
        'UI quote provenance')
    s=replace_exact(s,
        "PRICED_IN · SELL_THE_NEWS · NEGATIVE_CONFIRMED 차단. 뉴스 누락은 CLEAR로 해석하지 않습니다.",
        "NEGATIVE_CONFIRMED · OVERHANG · NEGATIVE_OVERHANG · POSITIVE_NEWS_FAILED · PRICED_IN · SELL_THE_NEWS 차단. NEGATIVE_ABSORBED는 별도 검증 없으면 PENDING. 뉴스 누락은 CLEAR로 추정하지 않습니다.",
        'UI complete news legend')
    s=replace_exact(s,
        "KRX 일봉 복구 '+esc(scan.history_repair?.status||'PENDING')+",
        "KRX 일봉 복구 '+esc(scan.history_repair?.status==='PAUSED'&&scan.history_repair?.reason==='SESSION_PROTECTION'?'PAUSED(정규장 보호 · 정상 대기)':(scan.history_repair?.status||'PENDING'))+",
        'UI scheduled repair pause explanation')
    s=replace_exact(s,
        "' · 25% 사건 유니버스 '+(scan.re25_universe_status||'PENDING')+",
        "' · 미확정/미래 캐시일 '+(scan.history_unclosed_or_future_cache_dates||[]).join(', ')+' · 25% 사건 유니버스 '+(scan.re25_universe_status||'PENDING')+",
        'UI unclosed cache dates')
    s=replace_exact(s,
        " · 미확인 항목은 승격 근거에서 제외합니다.';if(results[1]?.status==='fulfilled')",
        " · 주요 검증대기 '+Object.entries(scan.gate_pending_counts||{}).sort((a,b)=>b[1]-a[1]).slice(0,5).map(([k,v])=>k+':'+v).join(', ')+' · 시각품질 '+Object.entries(scan.quote_timestamp_quality_counts||{}).map(([k,v])=>k+':'+v).join(', ')+' · 미확인 항목은 승격 근거에서 제외합니다.';if(results[1]?.status==='fulfilled')",
        'UI gate diagnostics')
    s=replace_exact(s,
        "if(m&&market!==m.dataset.market){market=m.dataset.market;board=[];events=[];outcomes=[];loaded=200;totalHistory=0;",
        "if(m&&market!==m.dataset.market){market=m.dataset.market;board=[];events=[];outcomes=[];boardStats={};boardTotal=0;scanSummary={};$('allRows').checked=false;loaded=200;totalHistory=0;",
        'UI market switch reset')
    s=replace_exact(s,
        "if(t){tab=t.dataset.tab;loaded=200;render();load(true)}",
        "if(t){tab=t.dataset.tab;loaded=200;board=[];boardStats={};boardTotal=0;$('allRows').checked=false;render();load(true)}",
        'UI tab switch reset')
    s=replace_exact(s,
        "$('refresh').onclick=()=>load(true);$('search').oninput=render;$('grade').onchange=render;$('allRows').onchange=render;",
        "$('refresh').onclick=()=>load(true);$('search').oninput=render;$('grade').onchange=render;$('allRows').onchange=()=>load(true);",
        'UI full candidate fetch on toggle')
    s=replace_exact(s,
        "setInterval(()=>{if(!document.hidden)load()},15000);",
        "setInterval(()=>{if(!document.hidden&&!$('allRows').checked)load()},15000);",
        'UI no 10k auto refresh')
    return s


def tests_patch(src):
    s=src
    s=replace_exact(s,
        "    def test_api_validation(self):",
        """    def test_calendar_verified_session_without_source_flag(self):
        monday=datetime(2026,11,16,9,33,tzinfo=KST)
        self.assertTrue(verified_session_open('KRX',monday,None))
        self.assertFalse(verified_session_open('KRX',monday.replace(hour=8,minute=59),None))
        self.assertTrue(verified_session_open('NXT',monday.replace(hour=16,minute=0),None))
        self.assertFalse(verified_session_open('KRX',datetime(2026,11,15,10,tzinfo=KST),None))
        self.assertFalse(verified_session_open('KRX',monday,False))
    def test_board_lane_counts_and_pagination_are_complete(self):
        for i in range(360):
            lane='V17_ACTION_PRE' if i<350 else 'ASYMMETRIC_RS'
            grade='PRIME' if i%10==0 else 'WATCH'
            r=candidate(lane=lane,symbol=str(i).zfill(6))
            r.update(classification=grade,decision='PENDING' if i%3==0 else grade,
                     first_seen_at=stamp(AT),last_seen_at=stamp(AT),occurrences=1)
            self.store.reconcile('KRX',lane,[r],False,AT+timedelta(seconds=i))
        page=self.store.board('KRX',300,0,'V17_ACTION_PRE')
        self.assertEqual(page['total'],350)
        self.assertEqual(len(page['rows']),300)
        self.assertEqual(sum(page['counts']['classification_counts'].values()),350)
        self.assertEqual(page['counts']['classification_counts']['PRIME'],35)
        full=self.store.board('KRX',10000,0,'V17_ACTION_PRE')
        self.assertEqual(len(full['rows']),350)
        other=self.store.board('KRX',10000,0,'ASYMMETRIC_RS')
        self.assertEqual(other['total'],10)
    def test_api_validation(self):""",
        'board and session regression tests')
    return s

def test_v41_patch(src):
    s=src
    s=replace_exact(s,
        '    def test_complete_latest_day_but_missing_11_11_still_pending(self):',
        '''    def test_intraday_ls_bar_does_not_displace_confirmed_friday(self):
        self.cache({'000001':WINDOW+['2026-11-16']})
        result=self.run_engine([sec('000001')])
        self.assertEqual(result['history_latest_date'],'2026-11-13')
        self.assertIn('2026-11-16',result['history_unclosed_or_future_cache_dates'])
        self.assertIs(result['history_date_matches_kis'],True)
    def test_complete_latest_day_but_missing_11_11_still_pending(self):''',
        'V41 unclosed Monday bar regression')
    return s


PATCHES = {
    'operations/engine.py': engine_patch,
    'operations/integration.py': integration_patch,
    'operations/dashboard.html': dashboard_patch,
    'operations/test_engine.py': tests_patch,
    'operations/test_v41.py': test_v41_patch,
}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true',help='Check exact patch anchors; do not write')
    g.add_argument('--apply',action='store_true',help='Patch operations-only files, compile, test and rollback on failure')
    ap.add_argument('--root',type=Path,default=Path.cwd())
    ap.add_argument('--skip-tests',action='store_true',help='Only for a dry isolated build check, not recommended for production')
    args=ap.parse_args()
    root=args.root.resolve()
    original={};updated={}
    for name,fn in PATCHES.items():
        p=root/name
        if not p.is_file():raise SystemExit(f'MISSING: {p}')
        old=p.read_text(encoding='utf-8')
        original[name]=old
        updated[name]=fn(old)
        if name.endswith('.py'):ast.parse(updated[name],filename=name)
        print(('OK' if old==updated[name] else 'PATCHABLE'),name)
    if args.check:
        print('CHECK_OK: no files changed')
        return
    changed=[name for name in FILES if updated[name]!=original[name]]
    if not changed:
        print('ALREADY_APPLIED')
        return
    # Stage every file first; no partial patch when anchors fail.
    backup=Path(tempfile.mkdtemp(prefix='krx-nxt-ops-hotfix-backup-'))
    try:
        for name in changed:
            dest=backup/name;dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_text(original[name],encoding='utf-8')
        for name in changed:
            p=root/name
            tmp=p.with_name(p.name+'.hotfix.tmp')
            tmp.write_text(updated[name],encoding='utf-8')
            os.replace(tmp,p)
        for name in changed:
            if name.endswith('.py'):
                subprocess.run([sys.executable,'-m','py_compile',name],cwd=root,check=True)
        if not args.skip_tests:
            for run_no in range(1, 6):
                print(f'FULL_REPOSITORY_REGRESSION_RUN {run_no}/5', flush=True)
                subprocess.run([sys.executable,'-m','unittest','operations.test_engine','-q'],
                               cwd=root,check=True)
            print('FULL_REPOSITORY_REGRESSION_5_OF_5_PASSED', flush=True)
        else:
            print('WARNING: full repository tests skipped; not production verified')
        print('HOTFIX_APPLIED_AND_VERIFIED',VERSION)
        print('MODIFIED_FILES:',', '.join(changed))
    except Exception:
        for name in changed:
            shutil.copy2(backup/name,root/name)
        print('FAILED: original files restored from backup',file=sys.stderr)
        raise
    finally:
        shutil.rmtree(backup,ignore_errors=True)


if __name__=='__main__':
    main()
