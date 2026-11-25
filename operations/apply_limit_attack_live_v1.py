#!/usr/bin/env python3
"""Build-time additive patch for LIMIT_ATTACK_LIVE_V1_0.

This patches only the already-copied /app/operations projection at image build
time. KIS/LS collectors, broker authentication, native V1.7, RE25 and NXT
engines are not modified.
"""
from __future__ import annotations
from pathlib import Path

MARK = "LIMIT_ATTACK_LIVE_V1_0_RUNTIME_PATCH"


def _once(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        raise RuntimeError(f"{label}_ANCHOR_COUNT={n}")
    return src.replace(old, new, 1)


def patch_engine(path: Path) -> None:
    s = path.read_text(encoding="utf-8")
    if MARK in s:
        return

    anchor = "from .candidate_integrity import verified_scope, data_hold, native_nxt_minute_proven\n"
    s = _once(
        s, anchor,
        anchor + "from .limit_attack import LimitAttackTracker\n# " + MARK + "\n",
        "ENGINE_IMPORT",
    )

    s = _once(
        s,
        "VIEW_LANES = LANES + ('RS_HANDOFF',)\n",
        "VIEW_LANES = LANES + ('RS_HANDOFF', 'LIMIT_ATTACK')\n",
        "ENGINE_VIEW_LANES",
    )

    anchor = "        self._storage_diag_cache={'at':0.0,'known':{}}\n"
    s = _once(
        s, anchor,
        anchor + "        self.limit_attack=LimitAttackTracker(Path(data_dir)/'limit_attack_state.json')\n",
        "ENGINE_INIT",
    )

    old = """            nxt_map={code(z):z for z in (nxt_obj.get('rows',[]) if isinstance(nxt_obj,dict) else [])
                     if isinstance(z,dict) and valid_code(code(z))}
            for market,qmap in (('KRX',krx_quotes),('NXT',nxt_quotes)):
"""
    new = """            nxt_map={code(z):z for z in (nxt_obj.get('rows',[]) if isinstance(nxt_obj,dict) else [])
                     if isinstance(z,dict) and valid_code(code(z))}
            # LIMIT_ATTACK is an additive read-only consumer of the same KIS live rows.
            # It does not alter candidate membership or any native lane.
            try:
                la_meta=self.limit_attack.refresh(
                    krx_rows,current,
                    contexts={'alpha':alpha_map,'v17':v17_map,'fullmarket':fm_map}
                )
            except Exception as exc:
                la_meta={'status':'PENDING','version':'LIMIT_ATTACK_LIVE_V1_0','error':type(exc).__name__}
            self.runtime['limit_attack']=la_meta
            for market,qmap in (('KRX',krx_quotes),('NXT',nxt_quotes)):
"""
    s = _once(s, old, new, "ENGINE_REFRESH")

    s = _once(
        s,
        "            data=self.store.board(m,limit,offset,lane)\n",
        "            data=self.limit_attack.board(m,limit,offset) if lane=='LIMIT_ATTACK' else self.store.board(m,limit,offset,lane)\n",
        "ENGINE_API_BOARD",
    )

    old = "                              'scan':self.runtime.get('markets',{}).get(m,{}),\n"
    new = old + "                              'limit_attack':data.get('meta') if lane=='LIMIT_ATTACK' else self.runtime.get('limit_attack'),\n"
    s = _once(s, old, new, "ENGINE_API_META")

    compile(s, str(path), "exec")
    path.write_text(s, encoding="utf-8")


def patch_dashboard(path: Path) -> None:
    s = path.read_text(encoding="utf-8")
    if MARK in s:
        return

    old = "const tabs=[['NXT_POST_ACCEL_LANE','NXT 후가속'],['V17_ACTION_PRE','V1.7 ACTION+PRE'],['RS_HANDOFF','비대칭RS·자금이양'],['RE_EXPLOSION_25','재급등 후보'],['news','뉴스 게이트'],['events','신규/이탈 기록'],['outcomes','사후검증']];"
    new = "const tabs=[['NXT_POST_ACCEL_LANE','NXT 후가속'],['V17_ACTION_PRE','V1.7 ACTION+PRE'],['LIMIT_ATTACK','상한가 공격'],['RS_HANDOFF','비대칭RS·자금이양'],['RE_EXPLOSION_25','재급등 후보'],['news','뉴스 게이트'],['events','신규/이탈 기록'],['outcomes','사후검증']];"
    s = _once(s, old, new, "DASH_TABS")

    old = """function stats(){let items=[['승격 확인 PRIME',board.filter(r=>r.classification==='PRIME').length,'green'],['진입 대기 ARMED',board.filter(r=>['ARMED','PRE_EXPLOSION_ARMED'].includes(r.classification)).length,''],['관찰 WATCH',board.filter(r=>r.classification==='WATCH').length,''],['검증 대기 PENDING',board.filter(r=>r.decision==='PENDING').length,'amber'],['차단 REJECT',board.filter(r=>r.classification==='REJECT').length,'red']];$('stats').innerHTML=items.map(([label,n,c])=>'<div class="stat"><div class="label">'+label+'</div><div class="number mono '+c+'">'+n+'</div><div class="label">'+market+' · 레인별 발생 건수</div></div>').join('')}"""
    new = """function stats(){let items=tab==='LIMIT_ATTACK'?[['CONTINUE 생존',board.filter(r=>r.limit_attack_state==='CONTINUE_ARMED').length,''],['LIMIT WATCH',board.filter(r=>r.limit_attack_state==='LIMIT_WATCH').length,''],['ATTACK ARMED',board.filter(r=>r.limit_attack_state==='LIMIT_ATTACK_ARMED').length,'amber'],['LIMIT ATTACK',board.filter(r=>r.limit_attack_state==='LIMIT_ATTACK').length,'green'],['표시 생존군',board.length,'']]:[['승격 확인 PRIME',board.filter(r=>r.classification==='PRIME').length,'green'],['진입 대기 ARMED',board.filter(r=>['ARMED','PRE_EXPLOSION_ARMED'].includes(r.classification)).length,''],['관찰 WATCH',board.filter(r=>r.classification==='WATCH').length,''],['검증 대기 PENDING',board.filter(r=>r.decision==='PENDING').length,'amber'],['차단 REJECT',board.filter(r=>r.classification==='REJECT').length,'red']];$('stats').innerHTML=items.map(([label,n,c])=>'<div class="stat"><div class="label">'+label+'</div><div class="number mono '+c+'">'+n+'</div><div class="label">'+market+' · 레인별 발생 건수</div></div>').join('')}"""
    s = _once(s, old, new, "DASH_STATS")

    anchor = "let shown=[],latestScan={};"
    limit_columns = """const limitColumns=[['현재가','price'],['등락률 %','change_pct'],['LIMIT 상태','limit_attack_state'],['임계구간 %','limit_attack_threshold'],['고점회복 비율','limit_attack_high_reclaim'],['재가속 배','limit_attack_reaccel_ratio'],['응축비','limit_attack_contraction_ratio'],['VWAP','vwap'],['VWAP HOLD','limit_attack_vwap_hold'],['섹터 동조수','limit_attack_sector_sync_count'],['체결강도','execution_strength'],['거래량 속도 주/분','limit_attack_volume_velocity'],['진입 판정','limit_attack_entry_decision'],['가격 기준시각','price_asof'],['상태전이시각','limit_attack_state_at']];
"""
    s = _once(s, anchor, limit_columns + anchor, "DASH_COLUMNS")

    old = "function emptyStateMessage(){\n if(tab==='RE_EXPLOSION_25'&&market==='KRX'){"
    new = """function emptyStateMessage(){
 if(tab==='LIMIT_ATTACK'){
  if(market!=='KRX')return '<strong>LIMIT_ATTACK은 KRX 실시간 강세주 전용입니다.</strong>';
  return '<strong>현재 CONTINUE_ARMED 이상 생존 종목 0건</strong><div class="cellnote">KRX 전체에서 +7% 이상 강세주를 30초 상태로 추적하며, 눌림·거래량 응축·VWAP 재지지·재가속 확인 전에는 표시하지 않습니다.</div>';
 }
 if(tab==='RE_EXPLOSION_25'&&market==='KRX'){"""
    s = _once(s, old, new, "DASH_EMPTY")

    s = _once(
        s,
        "else {heads=['종목 / 선정 경로','분류 / 진입 상태',...columns.map(x=>x[0])];",
        "else {const activeColumns=tab==='LIMIT_ATTACK'?limitColumns:columns;heads=['종목 / 선정 경로','분류 / 진입 상태',...activeColumns.map(x=>x[0])];",
        "DASH_ACTIVE_COLUMNS",
    )

    old = "(tab==='RE_EXPLOSION_25'?badge(r.re25_runtime_state||r.re25_setup_state):badge(r.runtime_lane_state||r.classification))"
    new = "(tab==='LIMIT_ATTACK'?badge(r.limit_attack_state):tab==='RE_EXPLOSION_25'?badge(r.re25_runtime_state||r.re25_setup_state):badge(r.runtime_lane_state||r.classification))"
    s = _once(s, old, new, "DASH_STATUS_BADGE")

    s = _once(
        s,
        "columns.map(([label,k])=>'<td>'+(k==='history_asof'?",
        "activeColumns.map(([label,k])=>'<td>'+(k==='history_asof'?",
        "DASH_COLUMN_RENDER",
    )

    old = "$('description').textContent=tab==='NXT_POST_ACCEL_LANE'?"
    new = "$('description').textContent=tab==='LIMIT_ATTACK'?'CONTINUE_ARMED 생존군만 후단에서 +18/+20/+22/+25 구간을 관찰합니다. 임계값 자체는 매수가 아니며, VWAP·고점회복·거래량 재가속·체결/섹터 동조를 확인해 LIMIT_ATTACK을 판정합니다. +25% 및 +22% 초과 구간은 NO_CHASE 예측 전용입니다.':tab==='NXT_POST_ACCEL_LANE'?"
    s = _once(s, old, new, "DASH_DESCRIPTION")

    s = _once(
        s,
        "if(market==='NXT'&&tab==='V17_ACTION_PRE')tab='NXT_POST_ACCEL_LANE';",
        "if(market==='NXT'&&['V17_ACTION_PRE','LIMIT_ATTACK'].includes(tab))tab='NXT_POST_ACCEL_LANE';",
        "DASH_NXT_SWITCH",
    )

    s = s.replace("</script></body></html>", "// " + MARK + "\n</script></body></html>", 1)
    if MARK not in s:
        raise RuntimeError("DASH_MARKER_NOT_INSTALLED")
    path.write_text(s, encoding="utf-8")


TEST_APPEND = '\n# LIMIT_ATTACK_LIVE_V1_0_TESTS\ndef _limit_attack_test_quote(code_, price_, change_, volume_, at_, high_=None, vwap_=105, execution_=140, sector_="SECURITY"):\n    return {\n        "code": code_, "name": code_, "market": "KRX", "price": price_,\n        "change_rate": change_, "volume": volume_, "turnover": 10_000_000_000,\n        "high": high_ if high_ is not None else price_, "vwap": vwap_,\n        "execution_strength": execution_, "sector": sector_, "market_cap_100m": 2000,\n        "source_timestamp": at_.isoformat(), "source_timestamp_quality": "EXCHANGE_TIMESTAMP", "quality": "FRESH",\n    }\n\nclass LimitAttackLiveSidecarTests(unittest.TestCase):\n    def setUp(self):\n        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)\n        self.tr=LimitAttackTracker(Path(self.tmp.name)/"limit_attack.json")\n        self.base=datetime(2026,11,25,9,30,tzinfo=KST)\n\n    def _rows(self,i,price_,change_,volume_,high_,execution_=140):\n        at_=self.base+timedelta(seconds=30*i)\n        return [\n            _limit_attack_test_quote("053800",price_,change_,volume_,at_,high_,105,execution_),\n            _limit_attack_test_quote("208350",106,6,1000+i*10,at_,106,102,120),\n            _limit_attack_test_quote("263860",106,6,1000+i*10,at_,106,102,120),\n        ],at_\n\n    def _to_attack(self):\n        seq=[(108,8,1000,108),(110,10,2000,110),(106,6,2200,110),(108,8,2800,110),(119,19,3400,119),(120,20,4000,120)]\n        for i,(p_,c_,v_,h_) in enumerate(seq):\n            rows_,at_=self._rows(i,p_,c_,v_,h_);self.tr.refresh(rows_,at_)\n        return self.tr.states["053800"]\n\n    def test_limit_attack_sequence(self):\n        s_=self._to_attack()\n        self.assertEqual(s_["limit_attack_state"],"LIMIT_ATTACK")\n        self.assertEqual(s_["limit_attack_threshold"],20.0)\n        self.assertGreaterEqual(s_["limit_attack_reaccel_ratio"],1.5)\n        self.assertEqual(s_["limit_attack_sector_sync_count"],2)\n\n    def test_limit_attack_plus25_no_chase(self):\n        self._to_attack();rows_,at_=self._rows(6,125,25,4600,125,160);self.tr.refresh(rows_,at_)\n        s_=self.tr.states["053800"]\n        self.assertEqual(s_["limit_attack_threshold"],25.0)\n        self.assertEqual(s_["limit_attack_entry_decision"],"NO_CHASE_PREDICTION_ONLY")\n\n    def test_limit_attack_structure_break_filters(self):\n        self._to_attack();rows_,at_=self._rows(6,102,2,4100,120,100);self.tr.refresh(rows_[:1],at_)\n        self.assertEqual(self.tr.states["053800"]["limit_attack_state"],"FAILED_CONVERSION")\n        self.assertEqual(self.tr.board("KRX")["total"],0)\n\n    def test_limit_attack_board_is_krx_only(self):\n        self.assertEqual(self.tr.board("NXT")["status"],"NOT_APPLICABLE")\n\n    def test_limit_attack_warmup_not_shown(self):\n        rows_,at_=self._rows(0,108,8,1000,108);self.tr.refresh(rows_,at_)\n        self.assertEqual(self.tr.board("KRX")["total"],0)\n'


def patch_tests(path: Path) -> None:
    s = path.read_text(encoding="utf-8")
    if "LIMIT_ATTACK_LIVE_V1_0_TESTS" in s:
        return
    s = s.rstrip() + "\n\n" + TEST_APPEND.strip() + "\n"
    compile(s, str(path), "exec")
    path.write_text(s, encoding="utf-8")


def patch_all(root: Path | str) -> None:
    root = Path(root)
    patch_engine(root / "engine.py")
    patch_dashboard(root / "dashboard.html")
    patch_tests(root / "test_engine.py")


if __name__ == "__main__":
    import sys
    patch_all(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent)
    print(MARK + "_PATCH_PASS")
