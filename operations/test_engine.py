import copy, json, sqlite3, tempfile, unittest
from datetime import datetime,timedelta
from pathlib import Path
from .engine import *
from .install import patch

AT=datetime(2026,11,13,16,30,tzinfo=KST)

def candidate(market='KRX',lane='V17_ACTION_PRE',symbol='000001'):
    return dict(code=symbol,name='검증용 가상종목',market=market,lane=lane,price=100,change_pct=1,price_asof=stamp(AT),timestamp_quality='EXCHANGE_TIMESTAMP',quote_quality='FRESH',signal_asof=stamp(AT),market_state='NORMAL',news_gate='CLEAR',history_current=True,common_stock_confirmed=True,market_cap_100m=700,session_open=True,entry_price=100,stop_price=96,target_price=110,target_source='OBSERVED_RESISTANCE',stop_source='OBSERVED_LOW',ls_crosscheck=True,vwap_hold=True,price_hold=True,lane_confirmed=True,upstream_state='TRIGGER',selection_path=[lane],impulse_seen=True,recompression_seen=True,reaccel_seen=True,high_hold60_confirmed=True)

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
        self.assertEqual(expected_closed_daily_date({'trade_date':'2026-11-13'},datetime(2026,11,15,19,0,tzinfo=KST)),'2026-11-13')
        self.assertIsNone(expected_closed_daily_date({'trade_date':'2026-11-16'},datetime(2026,11,16,10,0,tzinfo=KST)))
        self.assertIsNone(expected_closed_daily_date({'trade_date':'2026-11-10'},datetime(2026,11,15,19,0,tzinfo=KST)))
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
            return dict(market=m,manifest={'trade_date':'2026-11-13'},rows=[source] if m=='KRX' else [],total=1 if m=='KRX' else 0)
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
        r=candidate(lane='RE_EXPLOSION_25');r.update(spike_age=0,recent5_complete=True,price_preserved=True,volume_contracting=True,compression_confirmed=True,support_near=True)
        self.assertNotEqual(core_gates(r,AT)['classification'],'PRIME')
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
