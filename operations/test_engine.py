import copy, json, sqlite3, tempfile, unittest
from datetime import datetime,timedelta
from pathlib import Path
from .engine import *
from .install import patch

AT=datetime(2026,11,13,16,30,tzinfo=KST)
KIS_C={'started_at':'2026-11-13T05:35:14+00:00','completed_at':'2026-11-13T05:43:43+00:00'}
KIS_MON={'started_at':'2026-11-16T05:35:14+00:00','completed_at':'2026-11-16T05:43:43+00:00'}

def candidate(market='KRX',lane='V17_ACTION_PRE',symbol='000001'):
    return dict(code=symbol,name='검증용 가상종목',market=market,lane=lane,price=100,change_pct=1,price_asof=stamp(AT),timestamp_quality='EXCHANGE_TIMESTAMP',quote_quality='FRESH',signal_asof=stamp(AT),market_state='NORMAL',news_gate='CLEAR',history_current=True,authority_universe_consistent=True,common_stock_confirmed=True,market_cap_100m=700,session_open=True,entry_price=100,stop_price=96,target_price=110,target_source='OBSERVED_RESISTANCE',stop_source='OBSERVED_LOW',ls_crosscheck=True,vwap_hold=True,price_hold=True,lane_confirmed=True,upstream_state='TRIGGER',selection_path=[lane],impulse_seen=True,recompression_seen=True,reaccel_seen=True,high_hold60_confirmed=True)

class EngineTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(Path(self.tmp.name)/'op.db')
    def evaluate(self,**updates):r=candidate();r.update(updates);return core_gates(r,AT)
    def test_prime_requires_all_gates(self):self.assertEqual(self.evaluate()['classification'],'PRIME')
    def test_all_market_vetoes(self):
        for state in VETO:self.assertEqual(self.evaluate(market_state=state)['classification'],'REJECT')
    def test_news_vetoes(self):
        for state in NEWS_BLOCK:self.assertEqual(self.evaluate(news_gate=state)['classification'],'REJECT')
    def test_missing_news_is_pending(self):self.assertEqual(self.evaluate(news_gate=None)['decision'],'PENDING')
    def test_rr_below_two(self):self.assertEqual(self.evaluate(target_price=107)['classification'],'REJECT')
    def test_no_generated_rr(self):self.assertEqual(self.evaluate(target_source=None)['gates']['RR_GE_2'],'PENDING')
    def test_invalid_stop_rejected(self):self.assertEqual(self.evaluate(stop_price=102)['gates']['RR_GE_2'],'FAIL')
    def test_no_chase(self):self.assertEqual(self.evaluate(change_pct=5.01)['classification'],'REJECT')
    def test_cap_boundary(self):self.assertEqual(self.evaluate(market_cap_100m=699)['classification'],'REJECT')
    def test_unknown_type(self):self.assertEqual(self.evaluate(common_stock_confirmed=None)['decision'],'PENDING')
    def test_request_timestamp_not_price_freshness(self):self.assertNotEqual(self.evaluate(timestamp_quality='REQUEST_TIMESTAMP')['classification'],'PRIME')
    def test_future_timestamp(self):self.assertFalse(fresh(stamp(AT+timedelta(minutes=2)),AT))
    def test_daily_stale_blocks_every_krx_lane(self):
        for lane in ('V17_ACTION_PRE','ASYMMETRIC_RS','CAPITAL_HANDOFF','RE_EXPLOSION_25'):
            r=candidate(lane=lane);r.update(history_current=False,history_asof='2026-11-10',history_expected_asof='2026-11-13')
            if lane=='RE_EXPLOSION_25':r.update(spike_age=2,recent5_complete=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True)
            z=core_gates(r,AT)
            self.assertNotEqual(z['classification'],'PRIME',lane)
            self.assertEqual(z['decision'],'PENDING',lane)
            self.assertEqual(z['gates']['KRX_DAILY_CURRENT'],'FAIL',lane)
            self.assertEqual(z['data_status'],'PENDING',lane)
    def test_nxt_postaccel_not_bound_to_krx_daily(self):
        r=candidate('NXT','NXT_POST_ACCEL_LANE');r['history_current']=False
        self.assertNotIn('KRX_DAILY_CURRENT',core_gates(r,AT)['gates'])
    def test_trade_date_only_no_fabricated_session_time(self):
        self.assertEqual(trade_date('2026-11-10'),'2026-11-10')
        self.assertEqual(trade_date('20261113'),'2026-11-13')
        self.assertIsNone(trade_date('2026-11-10T09:00:00+09:00'))
        self.assertIsNone(trade_date('2026-11-31'))
        self.assertEqual(expected_closed_daily_date({'trade_date':'2026-11-13','round_id':'20261113_143514_KRX','status':'READY',**KIS_C},datetime(2026,11,15,19,0,tzinfo=KST),'KRX'),'2026-11-13')
        self.assertIsNone(expected_closed_daily_date({'trade_date':'2026-11-16','round_id':'20261116_143514_KRX','status':'READY',**KIS_MON},datetime(2026,11,16,10,0,tzinfo=KST),'KRX'))
        self.assertIsNone(expected_closed_daily_date({'trade_date':'2026-11-10','round_id':'20261110_143514_KRX','status':'READY',**KIS_C},datetime(2026,11,15,19,0,tzinfo=KST),'KRX'))
        self.assertFalse(fresh('2026-11-10',AT))
    def test_v17_date_reference_never_signal_timestamp(self):
        import inspect
        from . import integration
        src=inspect.getsource(integration.attach)
        self.assertNotIn("bars[-1]['date']+'T15:30:00+09:00'",src)
        self.assertIn("z['signal_reference_date']=bars[-1]['date']",src)
    def test_stale_shared_cache_vs_kis_manifest_end_to_end(self):
        p=Path(self.tmp.name)/'history.db'
        with sqlite3.connect(p) as con:
            con.execute('CREATE TABLE bars(provider TEXT, venue TEXT, symbol TEXT, timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL)')
            for d in ['2026-11-06','2026-11-09','2026-11-10']:
                con.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX','000001',d,100,101,99,100,1000,10000))
        source=dict(code='000001',venue='KRX',name='검증용',price=100,change_rate=1,turnover=10000,volume=1000,market_cap_100m=1200,security_type='COMMON',source_timestamp=stamp(AT),source_timestamp_quality='EXCHANGE_TIMESTAMP',session_open=True,market_state='NORMAL')
        def mock_fetch(url):
            m='KRX' if 'market=KRX' in url else 'NXT'
            return dict(market=m,manifest={'trade_date':'2026-11-13','round_id':'20261113_143514_'+m,'status':'READY',**KIS_C},rows=[source] if m=='KRX' else [],total=1 if m=='KRX' else 0)
        op=Operations(self.tmp.name,history_path=p,fetch=mock_fetch)
        op.attach(v17=lambda: {'candidates':[dict(code='000001',venue='KRX',status='PRE_EXPLOSION_ARMED',signal_asof=stamp(AT),chart_actionable=True,vwap_hold=True,entry_price=100,stop_price=96,target_price=110,stop_source='LOW',target_source='HIGH',news_gate='CLEAR')], 'generated_at':stamp(AT)})
        status=op.scan(AT)
        self.assertEqual(status['markets']['KRX']['history_latest_date'],'2026-11-10')
        self.assertEqual(status['markets']['KRX']['history_expected_date'],'2026-11-13')
        self.assertIs(status['markets']['KRX']['history_latest_verified'],False)
        self.assertEqual(status['markets']['KRX']['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
        rows=op.api('/api/operations/board?market=KRX')[1]['rows']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['gates']['KRX_DAILY_CURRENT'],'FAIL')
        self.assertEqual(rows[0]['data_status'],'PENDING')
        self.assertEqual(rows[0]['history_asof'],'2026-11-10')
        self.assertEqual(rows[0]['history_expected_asof'],'2026-11-13')
    def test_kis_alpha_codes_are_not_dropped(self):
        self.assertTrue(valid_code('0120G0'))
        self.assertEqual(code({'code':'0120g0'}),'0120G0')
        self.assertFalse(valid_code('0120G0XXXX'))
        self.assertFalse(valid_code('00593-'))
        def mock_fetch(u):
            if '/latest/' in u:
                return {'market':'KRX','manifest':{'round_id':'20261113_143514_KRX','status':'READY',**KIS_C,'started_at':'2026-11-13T05:35:14+00:00'},'total':2,'rows':[{'market':'KRX','code':'0120G0'},{'market':'KRX','code':'005930'}]}
            return {'market':'KRX','total':2,'rows':[{'market':'KRX','code':'0120G0'},{'market':'KRX','code':'005930'}]}
        op=Operations(self.tmp.name,fetch=mock_fetch)
        rows,manifest,complete,path,errors=op.source('KRX')
        self.assertTrue(complete,errors)
        self.assertEqual(len(rows),2)
        self.assertEqual(closed_trade_date_from_manifest(manifest,'KRX'),'2026-11-13')

    def test_source_fails_closed_if_ready_scan_unavailable(self):
        def mock_fetch(u):
            if '/latest/' in u: return {'market':'KRX','total':1,'manifest':{'status':'COLLECTING','round_id':'20261113_143514_KRX'},'rows':[{'market':'KRX','code':'0120G0'}]}
            return {'market':'KRX','total':1,'rows':[{'market':'KRX','code':'0120G0'}]}
        op=Operations(self.tmp.name,fetch=mock_fetch)
        rows,manifest,complete,path,errors=op.source('KRX')
        self.assertTrue(complete)
        self.assertIsNone(expected_closed_daily_date(manifest,AT,'KRX'))

    def test_manifest_uses_source_round_and_no_request_clock(self):
        m={'status':'READY',**KIS_C,'market':'KRX','round_id':'20261113_143514_KRX','started_at':'2026-11-13T05:35:14+00:00'}
        self.assertEqual(expected_closed_daily_date(m,datetime(2026,11,15,19,tzinfo=KST),'KRX'),'2026-11-13')
        self.assertIsNone(expected_closed_daily_date({'status':'READY','trade_date':'2026-11-13'},datetime(2026,11,15,19,tzinfo=KST),'KRX'))
        self.assertIsNone(closed_trade_date_from_manifest({**m,'market':'NXT'},'KRX'))
        self.assertIsNone(closed_trade_date_from_manifest({**m,'round_id':'20261110_143514_KRX'}))
        self.assertIsNone(expected_closed_daily_date(m,datetime(2026,11,16,19,tzinfo=KST),'KRX'))

    def test_realistic_krx_count_2403_and_36_alphanumeric(self):
        rows=[{'market':'KRX','code':str(i).zfill(6),'name':'fixture'} for i in range(2367)]
        rows.extend({'market':'KRX','code':f'AB{i:04d}','name':'newly listed'} for i in range(36))
        def fetch(url):
            if '/latest/' in url:
                return {'market':'KRX','manifest':{'round_id':'20261113_143514_KRX','status':'READY',**KIS_C,'started_at':'2026-11-13T05:35:14+00:00','expected_symbols':2387},'total':2387,'rows':rows[:2387]}
            return {'market':'KRX','total':2403,'rows':rows}
        op=Operations(self.tmp.name,fetch=fetch)
        valid,manifest,complete,path,errors=op.source('KRX')
        self.assertEqual(len(valid),2403)
        self.assertEqual(sum(not code(r).isdigit() for r in valid),36)
        self.assertTrue(complete,errors)
        self.assertEqual(expected_closed_daily_date(manifest,datetime(2026,11,15,20,tzinfo=KST),'KRX'),'2026-11-13')

    def test_repair_on_weekday_is_paused_before_any_ls_call(self):
        from .history_repair import KRXHistoryRepair
        class Forbidden:
            def source(self,*args):raise AssertionError('must not fetch')
        r=KRXHistoryRepair(Forbidden(),lambda venue:(_ for _ in ()).throw(AssertionError('must not call')),
                           state_path=Path(self.tmp.name)/'state.json')
        self.assertEqual(r.batch(datetime(2026,11,16,10,tzinfo=KST))['reason'],'SESSION_PROTECTION')

    def test_fake_history_without_real_bars_never_claims_complete(self):
        from .history_repair import KRXHistoryRepair
        class FakeStore:
            def __init__(self):self.data={'000001':datetime(2026,11,10).date()}
            def last_date(self,provider,venue,symbol):
                selftest.assertEqual((provider,venue),('LS','KRX'))
                return self.data.get(symbol)
        class FakeHistory:
            venue='KRX'
            def __init__(self):self.store=FakeStore();self.calls=[]
            def bootstrap_one(self,symbol):
                self.calls.append(symbol);self.store.data[symbol]=datetime(2026,11,13).date()
        selftest=self
        h=FakeHistory()
        class FakeOp:
            def source(self,market):
                return [{'code':'000001','market':'KRX','market_cap_100m':900}, {'code':'0120G0','market':'KRX','market_cap_100m':800}],{'status':'READY',**KIS_C,'round_id':'20261113_143514_KRX','market':'KRX'},True,'/api/ledger/live/rows',[]
        r=KRXHistoryRepair(FakeOp(),lambda venue:h,state_path=Path(self.tmp.name)/'state.json',sleep=lambda s:None)
        out=r.batch(datetime(2026,11,15,20,tzinfo=KST),max_calls=2,pacing=0)
        self.assertEqual(out['updated'],0)
        self.assertGreater(out['stale'],0)
        self.assertEqual(set(h.calls),{'000001','0120G0'})
        self.assertNotEqual(r.batch(datetime(2026,11,15,20,tzinfo=KST),max_calls=2,pacing=0)['status'],'READY')
        self.assertFalse(KRXHistoryRepair.allowed(datetime(2026,11,16,10,tzinfo=KST)))

    def test_history_repair_does_not_claim_success_when_ls_old(self):
        from .history_repair import KRXHistoryRepair
        class FakeHistory:
            venue='KRX'
            def __init__(self):self.store=self
            def last_date(self,*args):return datetime(2026,11,10).date()
            def bootstrap_one(self,symbol):pass
        class FakeOp:
            def source(self,market):return [{'code':'0120G0','market':'KRX'}],{'status':'READY',**KIS_C,'round_id':'20261113_143514_KRX'},True,'latest',[]
        r=KRXHistoryRepair(FakeOp(),lambda venue:FakeHistory(),state_path=Path(self.tmp.name)/'state.json',sleep=lambda s:None)
        out=r.batch(datetime(2026,11,15,20,tzinfo=KST),max_calls=1,pacing=0)
        self.assertEqual(out['updated'],0)
        self.assertGreater(out['failed'],0)
        self.assertGreater(out['stale'],0)

    def test_dashboard_daily_date_rendering(self):
        html=Path(__file__).with_name('dashboard.html').read_text()
        self.assertIn("k==='history_asof'?dailyDate",html)
        self.assertIn("일봉 날짜 · 시각 없음",html)
    def test_stale_signal_not_prime(self):self.assertNotEqual(self.evaluate(signal_asof=stamp(AT-timedelta(days=1)))['classification'],'PRIME')
    def test_weekend_no_prime(self):
        r=candidate();r['session_open']=False;self.assertNotEqual(core_gates(r,AT)['classification'],'PRIME')
    def test_real_zero_not_unavailable(self):self.assertEqual(clean({'flow':0})['flow'],0);self.assertIsNone(number(True))
    def test_nan_never_json_number(self):self.assertEqual(clean(float('nan')),'UNAVAILABLE')
    def test_pending_does_not_mean_pass(self):self.assertIsNone(tri('PENDING'));self.assertIsNone(tri(None));self.assertFalse(tri(False))
    def test_nxt_separate_window(self):
        r=candidate('NXT','NXT_POST_ACCEL_LANE');self.assertEqual(core_gates(r,AT)['classification'],'PRIME')
        r=candidate('NXT','NXT_POST_ACCEL_LANE');self.assertNotEqual(core_gates(r,AT.replace(hour=14))['classification'],'PRIME')
    def test_nxt_missing_sixty_minutes(self):
        r=candidate('NXT','NXT_POST_ACCEL_LANE');r['high_hold60_confirmed']=None;self.assertEqual(core_gates(r,AT)['decision'],'PENDING')
    def test_fast_is_not_second_wave(self):
        r=candidate(lane='RE_EXPLOSION_25');r.update(spike_age=0,recent5_complete=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True,re25_candidate_visible=False)
        self.assertNotEqual(core_gates(r,AT)['classification'],'PRIME')
    def test_re25_fast_event_is_event_only(self):
        e=dict(spike_age=0,fast_new_spike=True,spike_high=130,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True)
        z=qualify_re25_event(e,{'price':128,'change_rate':2})
        self.assertTrue(z['re25_event_only']);self.assertFalse(z['re25_candidate_visible'])
        self.assertEqual(z['re25_setup_state'],'EVENT_ONLY_FAST_NEW_SPIKE')
    def test_re25_already_expanded_enters_red_no_chase_watch(self):
        e=dict(spike_age=2,fast_new_spike=False,spike_high=130,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True)
        z=qualify_re25_event(e,{'price':135,'change_rate':6.1})
        self.assertFalse(z['re25_event_only']);self.assertTrue(z['re25_candidate_visible'])
        self.assertEqual(z['re25_setup_state'],'RED_NO_CHASE');self.assertEqual(z['re25_runtime_state'],'RED_NO_CHASE')
    def test_re25_requires_all_four_recompression_gates(self):
        e=dict(spike_age=2,fast_new_spike=False,spike_high=130,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=False)
        z=qualify_re25_event(e,{'price':125,'change_rate':1})
        self.assertFalse(z['re25_candidate_visible']);self.assertEqual(z['re25_setup_state'],'EVENT_ONLY_NOT_RECOMPRESSED')
    def test_re25_compression_candidate_visible(self):
        e=dict(spike_age=2,fast_new_spike=False,spike_high=130,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True)
        z=qualify_re25_event(e,{'price':125,'change_rate':1})
        self.assertTrue(z['re25_candidate_visible']);self.assertEqual(z['re25_setup_state'],'COMPRESSION_ARMED')
    def test_re25_historical_second_wave_never_reappears(self):
        e=dict(spike_age=3,fast_new_spike=False,spike_high=130,historical_reexpansion=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True)
        z=qualify_re25_event(e,{'price':124,'change_rate':-1})
        self.assertTrue(z['re25_candidate_visible'])
        self.assertTrue(z['re25_historical_reexpansion'])
        self.assertEqual(z['re25_setup_state'],'RED_NO_CHASE')
        self.assertEqual(z['re25_runtime_state'],'RED_NO_CHASE')
    def test_re25_red_requires_two_quiet_confirmations(self):
        base=dict(code='000001',name='가상',market='KRX',lane='RE_EXPLOSION_25',
                  re25_setup_state='RED_NO_CHASE',re25_runtime_state='RED_NO_CHASE',
                  spike_high=130,spike_close=120,post_spike_max_high=132,support_price=112,
                  rearm_prev_volume=1000,rearm_prev_at=stamp(AT-timedelta(seconds=30)),
                  rearm_prev_volume_velocity=200)
        q={'price':123,'volume':1080,'change_rate':1,'source_timestamp':stamp(AT),
           'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
        a,_=re25_runtime_transition(base,q,AT)
        self.assertEqual(a['re25_runtime_state'],'RED_NO_CHASE')
        self.assertEqual(a['rearm_quiet_confirm_count'],1)
        b=dict(a,rearm_prev_at=stamp(AT),rearm_prev_volume=a['volume'],rearm_prev_volume_velocity=160)
        q2={'price':122.5,'volume':1140,'change_rate':.5,'source_timestamp':stamp(AT+timedelta(seconds=30)),
            'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
        c,_=re25_runtime_transition(b,q2,AT+timedelta(seconds=30))
        self.assertEqual(c['re25_runtime_state'],'REARM_WATCH')

    def test_re25_rearmed_requires_two_breakout_confirmations(self):
        base=dict(code='000001',name='가상',market='KRX',lane='RE_EXPLOSION_25',
                  re25_setup_state='RED_NO_CHASE',re25_runtime_state='REARM_WATCH',
                  spike_high=130,spike_close=120,post_spike_max_high=132,support_price=112,
                  rearm_peak_price=132,rearm_watch_price=122,rearm_breakout_level=124.44,
                  rearm_watch_volume_velocity=100,rearm_prev_volume=1000,
                  rearm_prev_at=stamp(AT-timedelta(seconds=30)),rearm_prev_volume_velocity=80)
        q={'price':125,'volume':1070,'change_rate':2,'source_timestamp':stamp(AT),
           'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
        a,_=re25_runtime_transition(base,q,AT)
        self.assertEqual(a['re25_runtime_state'],'REARM_WATCH')
        self.assertEqual(a['rearm_trigger_confirm_count'],1)
        b=dict(a,rearm_prev_at=stamp(AT),rearm_prev_volume=a['volume'])
        q2={'price':125.5,'volume':1140,'change_rate':2.2,'source_timestamp':stamp(AT+timedelta(seconds=30)),
            'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
        c,_=re25_runtime_transition(b,q2,AT+timedelta(seconds=30))
        self.assertEqual(c['re25_runtime_state'],'REARMED')

    def test_re25_rearmed_needs_three_soft_fails_to_downgrade(self):
        base=dict(code='000001',name='가상',market='KRX',lane='RE_EXPLOSION_25',
                  re25_setup_state='RED_NO_CHASE',re25_runtime_state='REARMED',
                  spike_high=130,spike_close=120,post_spike_max_high=132,support_price=112,
                  rearm_peak_price=132,rearm_breakout_level=125,rearm_fail_count=0,
                  rearm_prev_volume=1000,rearm_prev_at=stamp(AT-timedelta(seconds=30)),
                  rearm_prev_volume_velocity=100)
        cur=base
        for i in range(1,3):
            tm=AT+timedelta(seconds=30*i)
            q={'price':123,'volume':1000+50*i,'change_rate':1,'source_timestamp':stamp(tm),
               'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
            cur,_=re25_runtime_transition(cur,q,tm)
            self.assertEqual(cur['re25_runtime_state'],'REARMED')
        tm=AT+timedelta(seconds=90)
        q={'price':123,'volume':1180,'change_rate':1,'source_timestamp':stamp(tm),
           'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
        cur,_=re25_runtime_transition(cur,q,tm)
        self.assertEqual(cur['re25_runtime_state'],'REARM_WATCH')

    def test_re25_support_break_invalidates_immediately(self):
        base=dict(code='000001',name='가상',market='KRX',lane='RE_EXPLOSION_25',
                  re25_setup_state='RED_NO_CHASE',re25_runtime_state='REARM_WATCH',
                  spike_high=130,spike_close=120,post_spike_max_high=132,support_price=112,
                  rearm_peak_price=132,rearm_prev_volume=1000,
                  rearm_prev_at=stamp(AT-timedelta(seconds=30)),rearm_prev_volume_velocity=100)
        q={'price':110,'volume':1100,'change_rate':-3,'source_timestamp':stamp(AT),
           'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}
        z,_=re25_runtime_transition(base,q,AT)
        self.assertEqual(z['re25_runtime_state'],'INVALIDATED')

    def test_re25_board_hides_invalidated_runtime_state(self):
        base=candidate(lane='RE_EXPLOSION_25')
        x=core_gates(dict(base,re25_candidate_visible=True,re25_setup_state='RED_NO_CHASE',
                          re25_runtime_state='INVALIDATED',spike_age=2,recent5_complete=True,
                          price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True),AT)
        self.store.reconcile('KRX','RE_EXPLOSION_25',[x],False,AT)
        self.assertEqual(self.store.board('KRX',80,0,'RE_EXPLOSION_25')['total'],0)
    def test_re25_board_hides_event_only_rows(self):
        base=candidate(lane='RE_EXPLOSION_25')
        visible=core_gates(dict(base,re25_candidate_visible=True,re25_setup_state='COMPRESSION_ARMED',re25_runtime_state='WATCH',spike_age=2,recent5_complete=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True),AT)
        red=core_gates(dict(base,code='000002',re25_candidate_visible=True,re25_setup_state='RED_NO_CHASE',re25_runtime_state='RED_NO_CHASE',re25_event_only=False,no_chase=True,spike_age=2,recent5_complete=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True),AT)
        hidden=core_gates(dict(base,code='000003',re25_candidate_visible=False,re25_setup_state='EVENT_ONLY_FAST_NEW_SPIKE',re25_event_only=True,spike_age=0,recent5_complete=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True),AT)
        self.store.reconcile('KRX','RE_EXPLOSION_25',[visible,red,hidden],False,AT)
        page=self.store.board('KRX',80,0,'RE_EXPLOSION_25')
        self.assertEqual(page['total'],2);self.assertEqual(len(page['rows']),2)
        self.assertEqual({r['code'] for r in page['rows']},{base['code'],'000002'})
    def test_signal_lifecycle_dedup_restart_and_reentry(self):
        r=self.evaluate();self.store.reconcile('KRX',r['lane'],[r],True,AT)
        self.store.reconcile('KRX',r['lane'],[self.evaluate()],True,AT)
        self.assertEqual(self.store.records('events','KRX')['total'],1)
        self.assertEqual(self.store.records('outcomes','KRX')['total'],1)
        reopened=Store(self.store.path);reopened.reconcile('KRX',r['lane'],[self.evaluate()],True,AT)
        self.assertEqual(reopened.records('events','KRX')['total'],1)
        reopened.reconcile('KRX',r['lane'],[],False,AT+timedelta(minutes=1))
        self.assertEqual(reopened.records('events','KRX')['total'],1)
        self.assertEqual(reopened.records('candidates','KRX')['rows'][0]['payload']['decision'],'PENDING')
        reopened.reconcile('KRX',r['lane'],[],True,AT+timedelta(minutes=2))
        self.assertEqual(reopened.records('events','KRX')['rows'][0]['event'],'EXIT')
        reopened.reconcile('KRX',r['lane'],[self.evaluate()],True,AT+timedelta(minutes=3))
        p=reopened.records('candidates','KRX')['rows'][0]['payload'];self.assertEqual(p['occurrences'],2)
    def test_venue_collision_never_merges(self):
        for m in ('KRX','NXT'):
            r=core_gates(candidate(m),AT);self.store.reconcile(m,r['lane'],[r],True,AT)
        self.assertEqual(self.store.records('candidates','KRX')['total'],1);self.assertEqual(self.store.records('candidates','NXT')['total'],1)
    def test_per_code_verified_exit(self):
        r=self.evaluate();self.store.reconcile('KRX',r['lane'],[r],True,AT)
        self.store.reconcile('KRX',r['lane'],[],{'999999'},AT)
        self.assertEqual(self.store.records('candidates','KRX')['total'],1)
        self.store.reconcile('KRX',r['lane'],[],{r['code']},AT)
        self.assertEqual(self.store.records('candidates','KRX')['total'],0)
    def test_more_than_two_hundred_events_retained(self):
        for i in range(251):
            r=self.evaluate(code=str(i).zfill(6));self.store.reconcile('KRX',r['lane'],[r],False,AT)
        page=self.store.records('events','KRX',200,200);self.assertEqual(page['total'],251);self.assertEqual(len(page['rows']),51)
    def test_sampled_outcomes_after_signal_only(self):
        r=self.evaluate();self.store.reconcile('KRX',r['lane'],[r],True,AT)
        q={'000001':dict(price=110,source_timestamp=stamp(AT),source_timestamp_quality='EXCHANGE_TIMESTAMP')}
        self.store.track('KRX',q,AT);self.assertIsNone(self.store.records('outcomes','KRX')['rows'][0]['last_price'])
        q['000001']['source_timestamp']=stamp(AT+timedelta(minutes=1));self.store.track('KRX',q,AT+timedelta(minutes=1))
        out=self.store.records('outcomes','KRX')['rows'][0];self.assertEqual(out['status'],'CLOSED_SAMPLED');self.assertEqual(out['exit_price'],110)
    def test_velocity_same_market_exchange_only(self):
        r=dict(code='000001',volume=100,turnover=10000,source_timestamp=stamp(AT),source_timestamp_quality='EXCHANGE_TIMESTAMP')
        self.store.observe('KRX',[r],AT)
        r.update(volume=130,turnover=13000,source_timestamp=stamp(AT+timedelta(minutes=1)))
        v=self.store.observe('KRX',[r],AT+timedelta(minutes=1));self.assertEqual(v['000001']['volume_velocity'],30)
        self.assertIsNone(self.store.observe('NXT',[r],AT+timedelta(minutes=1))['000001']['volume_velocity'])
    def test_spike_high_not_close(self):
        bars=[dict(date='2026-11-10',close=100,high=101,low=99,volume=100),dict(date='2026-11-11',close=110,high=126,low=100,volume=1000),dict(date='2026-11-12',close=111,high=113,low=108,volume=300)]
        events=spike_events(bars,['2026-11-11','2026-11-12']);self.assertEqual(len(events),1);self.assertEqual(events[0]['spike_age'],1)
    def test_readonly_history_does_not_create_missing_db(self):
        p=Path(self.tmp.name)/'absent.db';self.assertEqual(History(p).read_all()[2],'PENDING_CACHE_NOT_FOUND');self.assertFalse(p.exists())
    def test_source_market_mismatch_pending(self):
        op=Operations(self.tmp.name,fetch=lambda url:{'market':'NXT','rows':[],'total':0});self.assertFalse(op.source('KRX')[2])
    def test_full_scope_no_top_cap(self):
        rows=[dict(code=str(i).zfill(6),market='KRX',name='fixture') for i in range(2403)]
        op=Operations(self.tmp.name,fetch=lambda url:{'market':'KRX','rows':rows,'total':2403});self.assertEqual(len(op.source('KRX')[0]),2403);self.assertTrue(op.source('KRX')[2])
    def test_runtime_quote_grade_rest_current_is_reference_not_exchange(self):
        q={'price':100,'change_rate':1,'source_timestamp':stamp(AT),
           'source_timestamp_quality':'REQUEST_TIMESTAMP','quality':'REST_CURRENT'}
        self.assertEqual(runtime_quote_grade(q,AT),'REST_CURRENT')
        z,changed=apply_runtime_quote(candidate(),q,AT)
        self.assertEqual(z['runtime_quote_status'],'REFERENCE_CURRENT');self.assertTrue(changed)
    def test_runtime_quote_stale_does_not_overwrite_price(self):
        r=candidate();r['price']=100
        q={'price':120,'change_rate':9,'source_timestamp':stamp(AT-timedelta(minutes=10)),
           'source_timestamp_quality':'REQUEST_TIMESTAMP','quality':'REST_STALE'}
        z,changed=apply_runtime_quote(r,q,AT)
        self.assertEqual(z['price'],100);self.assertFalse(changed);self.assertEqual(z['runtime_quote_grade'],'STALE')
    def test_partial_lane_hides_after_three_unconfirmed_cycles(self):
        r=core_gates(candidate(lane='ASYMMETRIC_RS'),AT)
        self.store.reconcile('KRX','ASYMMETRIC_RS',[r],False,AT)
        for n in range(1,4):self.store.reconcile('KRX','ASYMMETRIC_RS',[],False,AT+timedelta(minutes=10*n))
        row=self.store.active_payloads('KRX','ASYMMETRIC_RS')[0][1]
        self.assertTrue(row['candidate_stale']);self.assertEqual(row['candidate_miss_count'],3)
        self.assertEqual(self.store.board('KRX',80,0,'ASYMMETRIC_RS')['total'],0)
    def test_reconfirmed_candidate_resets_miss_counter(self):
        r=core_gates(candidate(lane='CAPITAL_HANDOFF'),AT)
        self.store.reconcile('KRX','CAPITAL_HANDOFF',[r],False,AT)
        self.store.reconcile('KRX','CAPITAL_HANDOFF',[],False,AT+timedelta(minutes=10))
        r2=core_gates(candidate(lane='CAPITAL_HANDOFF'),AT+timedelta(minutes=20))
        self.store.reconcile('KRX','CAPITAL_HANDOFF',[r2],False,AT+timedelta(minutes=20))
        row=self.store.active_payloads('KRX','CAPITAL_HANDOFF')[0][1]
        self.assertEqual(row['candidate_miss_count'],0);self.assertFalse(row['candidate_stale'])
    def test_re25_rest_current_can_watch_but_not_rearmed(self):
        base=dict(code='000001',name='가상',market='KRX',lane='RE_EXPLOSION_25',
                  re25_setup_state='RED_NO_CHASE',re25_runtime_state='REARM_WATCH',
                  spike_high=130,spike_close=120,post_spike_max_high=132,support_price=112,
                  rearm_peak_price=132,rearm_watch_price=122,rearm_breakout_level=124,
                  rearm_watch_volume_velocity=100,rearm_prev_volume=1000,
                  rearm_prev_at=stamp(AT-timedelta(seconds=30)),rearm_prev_volume_velocity=80,
                  rearm_trigger_confirm_count=1)
        q={'price':125,'volume':1070,'change_rate':2,'source_timestamp':stamp(AT),
           'source_timestamp_quality':'REQUEST_TIMESTAMP','quality':'REST_CURRENT'}
        z,_=re25_runtime_transition(base,q,AT)
        self.assertEqual(z['re25_runtime_state'],'REARM_TRIGGER_PENDING_AUTH')
        self.assertEqual(z['runtime_quote_grade'],'REST_CURRENT')

    def test_runtime_refresh_updates_all_non_re25_lanes_without_membership_change(self):
        quotes={
            'KRX':[{'code':'000001','name':'A','venue':'KRX','price':101,'change_rate':1,'volume':1000,'turnover':100000,
                    'source_timestamp':stamp(AT),'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'},
                   {'code':'000002','name':'B','venue':'KRX','price':102,'change_rate':1,'volume':1000,'turnover':100000,
                    'source_timestamp':stamp(AT),'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'},
                   {'code':'000003','name':'C','venue':'KRX','price':103,'change_rate':1,'volume':1000,'turnover':100000,
                    'source_timestamp':stamp(AT),'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}],
            'NXT':[{'code':'000004','name':'D','venue':'NXT','price':104,'change_rate':1,'volume':1000,'turnover':100000,
                    'source_timestamp':stamp(AT),'source_timestamp_quality':'EXCHANGE_TIMESTAMP','quality':'FRESH'}]
        }
        def fetch(url):
            market='NXT' if 'market=NXT' in url else 'KRX'
            return {'market':market,'rows':quotes[market],'total':len(quotes[market])}
        ops=Operations(data_dir=str(Path(self.tmp.name)/'ops2'),fetch=fetch,history_path=str(Path(self.tmp.name)/'none.db'))
        ops.attach(
            v17=lambda:{'candidates':[{'code':'000001','venue':'KRX','status':'ACTION'}],'watch':[]},
            alpha=lambda market:{'live_pool':[{'code':'000002','venue':'KRX','rs5':2.0,'rs20':1.0,'sector':'TEST'}]},
            fullmarket=lambda:{'candidates':[{'code':'000003','venue':'KRX','capital_handoff_confirmed':True,'selection_state':'HANDOFF'}]},
            nxt=lambda rows,current:{'rows':[{'code':'000004','venue':'NXT','gate_status':'PASS','nxt_high_hold60':.99,
                                              'nxt_volume_accel15':2.0,'nxt_turnover_accel15':2.0,'impulse_seen':True,
                                              'recompression_seen':True,'reaccel_seen':True,'high_hold60_confirmed':True}]}
        )
        for code_,lane_ in [('000001','V17_ACTION_PRE'),('000002','ASYMMETRIC_RS'),('000003','CAPITAL_HANDOFF')]:
            r=core_gates(candidate('KRX',lane_,code_),AT);ops.store.reconcile('KRX',lane_,[r],False,AT)
        r=core_gates(candidate('NXT','NXT_POST_ACCEL_LANE','000004'),AT);ops.store.reconcile('NXT','NXT_POST_ACCEL_LANE',[r],False,AT)
        out=ops.refresh_candidate_states(AT)
        self.assertEqual(out['status'],'READY')
        states={}
        for market_ in ('KRX','NXT'):
            for _,row in ops.store.active_payloads(market_):states[row['code']]=row.get('runtime_lane_state')
        self.assertEqual(states['000001'],'NATIVE_REFRESHED')
        self.assertEqual(states['000002'],'RS_REFRESHED')
        self.assertEqual(states['000003'],'HANDOFF_REFRESHED')
        self.assertEqual(states['000004'],'TRIGGERED_PENDING_CORE')

    def test_api_validation(self):
        op=Operations(self.tmp.name);self.assertEqual(op.api('/api/operations/board?market=ALL')[0],400);self.assertEqual(op.api('/api/operations/events?limit=no')[0],400)
    def test_additive_handler_patch(self):
        original='''from http.server import BaseHTTPRequestHandler
class X: pass

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        return self._dashboard()

def collector():
    return 'UNCHANGED'
def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
'''
        p=Path(self.tmp.name)/'app.py';p.write_text(original);patch(p);out=p.read_text();self.assertIn("def collector():\n    return 'UNCHANGED'",out);self.assertIn('"/legacy"',out);patch(p);self.assertEqual(out,p.read_text())

if __name__=='__main__':unittest.main()

class V3RepairSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sunday=datetime(2026,11,15,20,tzinfo=KST)
    def _fixture(self, dates=None, names=('000001','0120G0'), valid=True):
        from .history_repair import KRXHistoryRepair
        class DbStore:
            def __init__(self,p):
                import threading
                self.path=Path(p);self._lock=threading.RLock()
                self.conn=sqlite3.connect(self.path)
                self.conn.execute("CREATE TABLE bars(provider TEXT, venue TEXT, symbol TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL)")
                for c in names:
                    for d in dates or ['2026-11-06','2026-11-09','2026-11-10','2026-11-11','2026-11-12']:
                        self.conn.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX',c,d,100,101,99,100,100,1000))
                self.conn.commit()
            def last_date(self,provider,venue,symbol):
                z=self.conn.execute('SELECT MAX(timestamp) FROM bars WHERE provider=? AND venue=? AND symbol=?',(provider,venue,symbol)).fetchone()[0]
                return datetime.strptime(z,'%Y-%m-%d').date() if z else None
            def read(self,provider,venue,symbol,limit=1):
                import pandas as pd
                return pd.read_sql_query('SELECT timestamp,open,high,low,close,volume FROM bars WHERE provider=? AND venue=? AND symbol=? ORDER BY timestamp DESC LIMIT ?',self.conn,params=(provider,venue,symbol,limit))
        class Worker:
            venue='KRX'
            def __init__(self,db):self.store=db;self.calls=[]
            def bootstrap_one(self,symbol):
                self.calls.append(symbol)
                if not valid:return
                self.store.conn.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX',symbol,'2026-11-13',100,102,98,101,100,1000))
                self.store.conn.commit()
        db=DbStore(Path(self.tmp.name)/'hist.sqlite')
        worker=Worker(db)
        rows=[{'market':'KRX','code':c,'market_cap_100m':800} for c in names]
        class Op:
            history=type('H',(),{'path':db.path})()
            authority_audit={'KRX':{'membership_matched':True,'status':'MATCHED_CODES_UNVERIFIED_SAME_SESSION'}}
            def source(self,market):
                return rows,{'market':'KRX','status':'READY',**KIS_C,'round_id':'20261113_143514_KRX'},True,'KIS_LATEST',[]
        repair=KRXHistoryRepair(Op(),lambda venue:worker,state_path=Path(self.tmp.name)/'repair-state.json',
                                sleep=lambda seconds:None,clock=lambda:self.sunday)
        return repair,worker
    def test_actual_sqlite_cache_updates_and_recounts(self):
        repair,w=self._fixture()
        before=repair.batch(self.sunday,max_calls=2,pacing=0)
        self.assertEqual(before['updated'],2)
        self.assertEqual(before['ready'],2)
        self.assertEqual(before['stale'],0)
        self.assertEqual(repair.batch(self.sunday,max_calls=2,pacing=0)['status'],'READY')
        self.assertEqual(w.calls,['000001','0120G0'])
        self.assertEqual(w.store.last_date('LS','KRX','0120G0'),datetime(2026,11,13).date())
    def test_real_sqlite_future_does_not_count_as_ready(self):
        repair,w=self._fixture(dates=['2026-11-17'])
        result=repair.batch(self.sunday,max_calls=2,pacing=0)
        self.assertEqual(result['reason'],'LS_CACHE_FUTURE_TRADE_DATE')
        self.assertEqual(w.calls,[])
    def test_bad_ohlc_is_not_approved(self):
        repair,w=self._fixture(names=('000001',))
        def bad(symbol):
            w.store.conn.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX',symbol,'2026-11-13',110,100,111,105,100,1000))
            w.store.conn.commit()
        w.bootstrap_one=bad
        z=repair.batch(self.sunday,max_calls=1,pacing=0)
        self.assertEqual(z['updated'],0)
        self.assertEqual(z['failed'],1)
        self.assertIn('OHLC_INVALID',z['last_error'])
    def test_200bar_historical_depth_explicit(self):
        r,w=self._fixture(dates=['2026-11-13'])
        out=r.batch(self.sunday,max_calls=0,pacing=0)
        self.assertEqual(out['status'],'REPAIRING')
        self.assertEqual(out['depth_lt_200'],2)
        self.assertEqual(out['ready'],0)  # one correct date cannot prove recent5 completeness
    def test_broker_503_opens_circuit_after_three_failures(self):
        r,w=self._fixture(names=('000001','0120G0','000003','000004'))
        w.bootstrap_one=lambda symbol:(_ for _ in ()).throw(RuntimeError('LS t8410 HTTP 503'))
        out=r.batch(self.sunday,max_calls=4,pacing=0)
        self.assertEqual(out['reason'],'LS_BROKER_CIRCUIT_OPEN')
        self.assertEqual(out['attempted'],3)
        self.assertEqual(out['failed'],3)
    def test_isolated_404_does_not_block_remaining_symbols(self):
        r,w=self._fixture(names=('000001','0120G0','000003','000004'))
        w.bootstrap_one=lambda symbol:(_ for _ in ()).throw(RuntimeError('LS t8410 HTTP 404: symbol unavailable'))
        out=r.batch(self.sunday,max_calls=4,pacing=0)
        self.assertEqual(out['attempted'],4)
        self.assertEqual(out['failed'],4)
        self.assertNotEqual(out['reason'],'LS_BROKER_CIRCUIT_OPEN')
    def test_shared_scanner_cache_path_mismatch_blocks_writes(self):
        r,w=self._fixture()
        w.store.path=Path(self.tmp.name)/'WRONG_SQLITE.sqlite'
        out=r.batch(self.sunday,max_calls=2,pacing=0)
        self.assertEqual(out['reason'],'LS_HISTORY_ADAPTER_UNAVAILABLE')
        self.assertEqual(w.calls,[])
    def test_manifest_missing_fails_closed(self):
        r,w=self._fixture()
        r.operations.source=lambda market:([{'code':'000001','market':'KRX'}],{},True,'LIVE',[])
        out=r.batch(self.sunday,max_calls=2,pacing=0)
        self.assertEqual(out['reason'],'KIS_UNIVERSE_OR_COMPLETED_DAY_UNVERIFIED')
        self.assertEqual(w.calls,[])
    def test_duplicate_security_id_fails_closed(self):
        r,w=self._fixture()
        original=r.operations.source
        r.operations.source=lambda market:(original(market)[0]+[original(market)[0][0]],original(market)[1],True,'LIVE',[])
        out=r.batch(self.sunday,max_calls=2,pacing=0)
        self.assertEqual(out['reason'],'KIS_DUPLICATE_CODES')
        self.assertEqual(w.calls,[])
    def test_worker_is_isolated_from_shared_scanner(self):
        from . import integration
        import inspect
        s=inspect.getsource(integration.attach)
        self.assertIn('worker=FastHistory500(ls,db_path=str(ops.history.path),min_bars=220,lookback_days=520)',s)
        self.assertNotIn("return app['get_lazy_history_engine'](venue)",s)
    def test_full_content_v17_invalidation(self):
        from . import integration
        import inspect
        s=inspect.getsource(integration.attach)
        self.assertIn('for bar in bars[-260:]:',s)
        self.assertIn('hashlib.blake2b',s)

    def test_native_fast_history_bootstrap_real_contract(self):
        try:
            from 오백종목_섹터에너지_알파하이킨아시_통합_V1_0 import FastHistory500
            import pandas as pd
        except ImportError:
            self.skipTest('Native Docker module only present inside production image')
        class DummyLS:
            def __init__(self):self.calls=[]
            def get_daily_bars(self,symbol,start,end):
                self.calls.append((symbol,start,end))
                days=pd.bdate_range(end='2026-11-13',periods=230)
                return pd.DataFrame({'timestamp':days,'open':100.0,'high':102.0,'low':99.0,'close':101.0,'volume':100.0,'turnover':10000.0})
        ls=DummyLS()
        worker=FastHistory500(ls,db_path=str(Path(self.tmp.name)/'native.sqlite'),min_bars=220,lookback_days=520)
        worker.set_venue('KRX','K')
        df=worker.bootstrap_one('0120G0')
        self.assertEqual(len(ls.calls),1)
        self.assertEqual(ls.calls[0][0],'0120G0')
        self.assertEqual(worker.store.last_date('LS','KRX','0120G0'),datetime(2026,11,13).date())
        self.assertGreaterEqual(len(df),220)
        self.assertIsNone(worker.store.last_date('LS','NXT','0120G0'))

class V3HistoryQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'hist.db'
        self.conn=sqlite3.connect(self.path)
        self.conn.execute('CREATE TABLE bars(provider TEXT,venue TEXT,symbol TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL)')
    def add(self,dt_s,lo=99,hi=102,volume=100):
        self.conn.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX','005930',dt_s,100,hi,lo,101,volume,1000));self.conn.commit()
    def test_bad_ohlc_and_weekend_not_promotable_to_trade_bar(self):
        self.add('2026-11-11')
        self.add('2026-11-12',lo=104)
        self.add('2026-11-13',volume=-1)
        self.add('2026-11-14')
        self.conn.close()
        h,ds,status=History(self.path).read_all()
        self.assertEqual(status,'READY')
        self.assertEqual(h.get('005930')[0]['date'],'2026-11-11')
        self.assertEqual(len(h.get('005930')),1)
        self.assertNotIn('2026-11-14',ds)
        h.close()
    def test_valid_ls_friday_is_seen_after_eod(self):
        self.add('2026-11-11');self.add('2026-11-12');self.add('2026-11-13')
        self.conn.close()
        h,ds,status=History(self.path).read_all()
        self.assertEqual(h.get('005930')[-1]['date'],'2026-11-13')
        self.assertEqual(ds[-1],'2026-11-13')
        h.close()

class V3PositiveHistoryIntegrationTests(unittest.TestCase):
    def test_complete_repaired_krx_window_promotes_data_status_only(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'hist.sqlite'
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE bars(provider TEXT,venue TEXT,symbol TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL)')
                for sym in ('000001','0120G0'):
                    for d in ('2026-11-06','2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13'):
                        high=130 if d=='2026-11-12' and sym=='0120G0' else 101
                        con.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX',sym,d,100,high,99,100,1000,10000))
            def fetch(url):
                market='NXT' if 'market=NXT' in url else 'KRX'
                manifest={'status':'READY',**KIS_C,'market':market,'round_id':'20261113_143514_'+market,'started_at':'2026-11-13T05:35:14Z'}
                rows=[{'code':c,'market':'KRX','name':c,'price':100,'market_cap_100m':800,'source_timestamp':'2026-11-13T05:40:00Z','source_timestamp_quality':'REQUEST_TIME_ONLY'} for c in ('000001','0120G0')] if market=='KRX' else []
                return {'market':market,'total':len(rows),'rows':rows,'manifest':manifest}
            op=Operations(td,history_path=db,fetch=fetch)
            out=op.scan(datetime(2026,11,15,20,tzinfo=KST))
            krx=out['markets']['KRX']
            self.assertEqual(krx['history_latest_date'],'2026-11-13')
            self.assertEqual(krx['history_expected_date'],'2026-11-13')
            self.assertEqual(krx['history_ready_symbols'],2)
            self.assertEqual(krx['history_stale_symbols'],0)
            self.assertEqual(krx['re25_universe_status'],'COMPLETE')
            self.assertTrue(krx['history_latest_verified'])
            self.assertGreaterEqual(krx['lanes']['RE_EXPLOSION_25']['observed'],1)
            self.assertEqual(krx['lanes']['RE_EXPLOSION_25']['prime'],0)

class V3NxtIndependentDataTests(unittest.TestCase):
    def test_nxt_minute_complete_not_blocked_by_krx_daily_cache(self):
        with tempfile.TemporaryDirectory() as td:
            def fetch(url):
                venue='NXT' if 'market=NXT' in url else 'KRX'
                rows=[{'code':'000001','market':'NXT','name':'NXT-TEST','price':100,'market_cap_100m':1000}] if venue=='NXT' else [{'code':'000001','market':'KRX','name':'KRX-TEST','price':100}]
                return {'market':venue,'total':len(rows),'rows':rows,'manifest':{'status':'READY',**KIS_C,'market':venue,'round_id':'20261113_143514_'+venue,'started_at':'2026-11-13T05:35:14+00:00'}}
            op=Operations(td,history_path=Path(td)/'missing.db',fetch=fetch)
            op.attach(nxt=lambda rows,current:{'status':'READY','rows':[{'code':r['code'],'venue':'NXT','gate_status':'PENDING'} for r in rows], 'universe_complete':True})
            out=op.scan(datetime(2026,11,15,20,tzinfo=KST))
            self.assertEqual(out['markets']['KRX']['status'],'PENDING')
            self.assertEqual(out['markets']['NXT']['status'],'READY')
            self.assertEqual(out['markets']['NXT']['history_latest_verified'],'UNAVAILABLE')
            self.assertEqual(out['markets']['NXT']['history_repair']['status'],'NOT_APPLICABLE')
            self.assertEqual(out['markets']['NXT']['re25_universe_status'],'NOT_APPLICABLE')

class V4DateProvenanceAndCandleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.sunday=datetime(2026,11,15,21,0,tzinfo=KST)
        self.market='KRX'
        self.sessions=['2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13']
    def cache(self,days,overrides=None):
        p=Path(self.tmp.name)/'ls-cache.sqlite3';overrides=overrides or {}
        if p.exists():p.unlink()
        with sqlite3.connect(p) as c:
            c.execute('CREATE TABLE bars(provider TEXT,venue TEXT,symbol TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL)')
            for d in days:
                z=dict(open=100,high=102,low=99,close=101,volume=1000);z.update(overrides.get(d,{ }))
                c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',('LS','KRX','0120G0',d,z['open'],z['high'],z['low'],z['close'],z['volume'],10000))
        return p
    def fetch(self,url):
        m='NXT' if 'market=NXT' in url else 'KRX'
        rows=[{'market':'KRX','code':'0120G0','price':101,'market_cap_100m':900,'source_timestamp':'2026-11-13T05:43:00Z'}] if m=='KRX' else []
        return {'market':m,'total':len(rows),'rows':rows,'manifest':{'market':m,'status':'READY',**KIS_C,'round_id':f'20261113_143514_{m}'}}
    def audit(self,p):
        op=Operations(self.tmp.name,history_path=p,fetch=self.fetch)
        return op.scan(self.sunday)['markets']['KRX']
    def test_calendar_exact_last_five_are_9th_through_13th(self):
        from .trading_calendar import recent_sessions,last_closed_session
        self.assertEqual(recent_sessions('2026-11-13',5),self.sessions)
        self.assertEqual(recent_sessions('2026-11-13',6),['2026-11-06']+self.sessions)
        self.assertEqual(last_closed_session(self.sunday),'2026-11-13')
    def test_strict_eod_cutoff_vs_1435_kis_scan(self):
        from .trading_calendar import last_closed_session
        d=datetime(2026,11,13,15,35,tzinfo=KST)
        self.assertEqual(last_closed_session(d),'2026-11-12')
        self.assertIsNone(expected_closed_daily_date({'status':'READY','market':'KRX',**KIS_C,'round_id':'20261113_143514_KRX'},d,'KRX'))
        self.assertEqual(last_closed_session(d.replace(minute=45)),'2026-11-13')
        self.assertEqual(expected_closed_daily_date({'status':'READY','market':'KRX',**KIS_C,'round_id':'20261113_143514_KRX'},d.replace(minute=45),'KRX'),'2026-11-13')
    def test_manifest_has_to_prove_completed_143514_scan(self):
        x={'status':'READY','market':'KRX',**KIS_C,'round_id':'20261113_143514_KRX'}
        self.assertEqual(closed_trade_date_from_manifest(x,'KRX'),'2026-11-13')
        self.assertIsNone(closed_trade_date_from_manifest({**x,'completed_at':None},'KRX'))
        self.assertIsNone(closed_trade_date_from_manifest({**x,'started_at':'2026-11-13T05:35:15Z'},'KRX'))
        self.assertIsNone(closed_trade_date_from_manifest({**x,'completed_at':'2026-11-13T05:00:00Z'},'KRX'))
        self.assertIsNone(closed_trade_date_from_manifest({**x,'status':'COLLECTING'},'KRX'))
    def test_missing_entire_11th_day_never_changes_recent5_date_list(self):
        p=self.cache(['2026-11-06','2026-11-09','2026-11-10','2026-11-12','2026-11-13'])
        z=self.audit(p)
        self.assertEqual(z['history_recent5_expected'],self.sessions)
        self.assertEqual(z['history_re25_predecessor_date'],'2026-11-06')
        self.assertEqual(z['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
        self.assertEqual(z['history_ready_symbols'],0)
        self.assertFalse(z['history_latest_verified'])
    def test_equal_last_date_but_only_one_bar_is_not_ready(self):
        p=self.cache(['2026-11-13'])
        z=self.audit(p)
        self.assertEqual(z['history_latest_date'],'2026-11-13')
        self.assertEqual(z['history_expected_date'],'2026-11-13')
        self.assertEqual(z['history_ready_symbols'],0)
        self.assertEqual(z['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
    def test_corrupt_last_candle_is_not_masqueraded_as_valid_date(self):
        p=self.cache(['2026-11-06']+self.sessions,{'2026-11-13':{'open':110,'high':100,'low':111,'close':105}})
        z=self.audit(p)
        self.assertEqual(z['history_latest_date'],'2026-11-12')
        self.assertEqual(z['history_ready_symbols'],0)
        self.assertFalse(z['history_latest_verified'])
    def test_25pct_earliest_event_requires_prior_day(self):
        p=self.cache(self.sessions)
        z=self.audit(p)
        self.assertEqual(z['history_ready_symbols'],1)
        self.assertEqual(z['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
        p2=self.cache(['2026-11-06']+self.sessions)
        self.assertEqual(self.audit(p2)['re25_universe_status'],'COMPLETE')
    def test_ma200_requires_exact_200_sessions_not_last_200_available(self):
        from .trading_calendar import recent_sessions
        dates=recent_sessions('2026-11-13',200)
        self.assertEqual(len(dates),200)
        rows=[{'date':d,'close':100,'volume':100} for d in dates]
        self.assertEqual(history_metrics(rows,'2026-11-13')['ma200'],100)
        gapped=[r for r in rows if r['date']!='2026-11-11']
        gapped.append({'date':'2025-01-02','close':100,'volume':100})
        self.assertGreaterEqual(len(gapped),200)
        self.assertIsNone(history_metrics(gapped,'2026-11-13')['ma200'])
        self.assertIsNone(history_metrics(gapped,'2026-11-13')['ma5'])
    def test_unconfirmed_future_bar_excluded_from_indicators(self):
        days=['2026-11-06']+self.sessions
        bars=[{'date':d,'close':100,'volume':100} for d in days]
        bars.append({'date':'2026-11-16','close':900,'volume':900})
        metrics=history_metrics(bars,'2026-11-13')
        self.assertEqual(metrics['history_asof'],'2026-11-13')
        self.assertEqual(metrics['ma5'],100)
    def test_duplicate_kis_symbols_are_not_complete(self):
        def duplicate(url):
            d=self.fetch(url)
            if 'market=KRX' in url:
                d['rows']=[d['rows'][0],d['rows'][0]]
                d['total']=2
            return d
        o=Operations(self.tmp.name,fetch=duplicate)
        rows,manifest,complete,src,errors=o.source('KRX')
        self.assertEqual(len(rows),2)
        self.assertFalse(complete)
        self.assertIn('LIVE_INCOMPLETE_OR_INVALID_IDENTIFIERS',errors)
    def test_repair_worker_invalid_last_and_missing_recent_must_not_mark_ready(self):
        from .history_repair import KRXHistoryRepair
        p=self.cache(['2026-11-06','2026-11-09','2026-11-10','2026-11-12','2026-11-13'])
        class Store0:
            path=p
            conn=sqlite3.connect(p)
        class Worker:
            venue='KRX';store=Store0()
        class Op:
            history=type('H',(),{'path':p})()
            def source(self,market):
                return [{'code':'0120G0','market':'KRX'}],{'market':'KRX','status':'READY',**KIS_C,'round_id':'20261113_143514_KRX'},True,'KIS',[]
        w=Worker()
        repair=KRXHistoryRepair(Op(),lambda venue:w,state_path=Path(self.tmp.name)/'s.json',clock=lambda:self.sunday)
        z=repair.batch(self.sunday,max_calls=0,pacing=0)
        self.assertEqual(z['ready'],0)
        self.assertEqual(z['stale'],1)
        self.assertEqual(z['status'],'REPAIRING')
        w.store.conn.close()

# V4.1 regression suites are included in the pre-existing Docker build command:
# python -m unittest operations.test_engine -q
# They are also available separately as operations.test_v41.
from .test_v41 import (
    V41UniverseAuditTests, V41StructuralMissingTests,
    V41IntegrationTests, V41RepairClassificationTests,
)
