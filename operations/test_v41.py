"""V4.1 regression cases: KIS membership deltas, IPO/suspension, six-session gaps."""
from __future__ import annotations
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from .engine import Operations, KST, core_gates
from .universe_audit import classify_window, universe_membership_audit

WINDOW=['2026-11-06','2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13']
MON=datetime(2026,11,15,22,40,tzinfo=KST)
MANIFEST={'market':'KRX','status':'READY','round_id':'20261113_143514_KRX',
          'started_at':'2026-11-13T05:35:14Z','completed_at':'2026-11-13T05:43:43Z'}

def sec(code,**kwargs):
    return {'market':'KRX','code':code,'name':code,'price':101,'market_cap_100m':800,**kwargs}

class V41UniverseAuditTests(unittest.TestCase):
    def test_live_2403_vs_2387_is_unresolved_not_silently_full(self):
        latest=[sec(f'{i:06d}') for i in range(2387)]
        live=latest+[sec(f'{i:06d}') for i in range(2387,2403)]
        result=universe_membership_audit(live,latest,2403,2387,MANIFEST)
        self.assertEqual(result['status'],'PENDING_CROSS_SNAPSHOT_MEMBERSHIP_DIFF')
        self.assertEqual(result['live_only_count'],16)
        self.assertEqual(result['latest_only_count'],0)
        self.assertFalse(result['membership_matched'])
        self.assertFalse(result['same_session_verified'])
    def test_same_number_different_codes_is_mismatch(self):
        r=universe_membership_audit([sec('000001'),sec('000003')],
                 [sec('000001'),sec('000002')],2,2,MANIFEST)
        self.assertEqual((r['live_only_count'],r['latest_only_count']),(1,1))
        self.assertEqual(r['status'],'PENDING_CROSS_SNAPSHOT_MEMBERSHIP_DIFF')
    def test_duplicate_codes_are_invalid_not_matched(self):
        r=universe_membership_audit([sec('000001'),sec('000001')],
                                    [sec('000001'),sec('000002')],2,2,MANIFEST)
        self.assertEqual(r['status'],'PENDING_INVALID_SOURCE_COUNTS')
    def test_alphanumeric_codes_remain_in_universe(self):
        r=universe_membership_audit([sec('0120G0'),sec('000001')],
                 [sec('0120G0'),sec('000001')],2,2,MANIFEST)
        self.assertTrue(r['membership_matched'])
    def test_immutable_round_is_not_claimed_live_exchange_timestamp(self):
        r=universe_membership_audit([sec('000001')],[sec('000001')],1,1,MANIFEST)
        self.assertFalse(r['same_session_verified'])
        self.assertEqual(r['status'],'MATCHED_CODES_UNVERIFIED_SAME_SESSION')

class V41StructuralMissingTests(unittest.TestCase):
    def test_true_listing_evidence_exempts_missing_prelisting_days_only(self):
        row=sec('0120G0', listing_date='2026-11-12',listing_date_source='KRX',listing_date_verified=True)
        s=classify_window(row,WINDOW,['2026-11-12','2026-11-13'])
        self.assertEqual(s.state,'STRUCTURAL_NOT_EVALUABLE')
        self.assertEqual(len(s.missing),4)
    def test_unverified_listing_date_never_exempts(self):
        row=sec('0120G0',listing_date='2026-11-12')
        s=classify_window(row,WINDOW,['2026-11-12','2026-11-13'])
        self.assertEqual(s.state,'DATA_PENDING')
    def test_ipo_with_missing_post_listing_candle_not_exempt(self):
        row=sec('0120G0',listing_date='2026-11-12',listing_date_source='KRX',listing_date_verified=True)
        s=classify_window(row,WINDOW,['2026-11-12'])
        self.assertEqual(s.state,'DATA_PENDING')
        self.assertNotIn('2026-11-13',s.verified_structural_dates)
    def test_suspension_needs_verified_period_and_exact_coverage(self):
        row=sec('000001',verified_suspension_periods=[dict(start='2026-11-11',end='2026-11-13',source='KRX',verified=True)])
        self.assertEqual(classify_window(row,WINDOW,WINDOW[:3]).state,'STRUCTURAL_NOT_EVALUABLE')
        row['verified_suspension_periods'][0]['verified']=False
        self.assertEqual(classify_window(row,WINDOW,WINDOW[:3]).state,'DATA_PENDING')
    def test_bare_suspended_status_never_exempts(self):
        self.assertEqual(classify_window(sec('000001',trading_status='SUSPENDED'),WINDOW,WINDOW[:3]).state,'DATA_PENDING')
    def test_one_missing_intermediate_day_is_pending(self):
        s=classify_window(sec('000001'),WINDOW,[d for d in WINDOW if d!='2026-11-11'])
        self.assertEqual(s.state,'DATA_PENDING')
        self.assertEqual(s.missing,('2026-11-11',))
    def test_all_bars_present_complete(self):
        self.assertEqual(classify_window(sec('000001'),WINDOW,WINDOW).state,'COMPLETE')
    def test_nxt_minute_lane_retains_independent_data_status(self):
        from .test_engine import candidate, AT
        x=candidate(market='NXT',lane='NXT_POST_ACCEL_LANE')
        x['history_current']=False
        y=core_gates(x,AT)
        self.assertNotIn('KRX_UNIVERSE_MATCH',y['gates'])
        self.assertEqual(y['data_status'],'READY')

    def test_structural_missing_cannot_become_prime(self):
        from .test_engine import candidate, AT
        x=candidate()
        x['history_current']=False
        self.assertNotEqual(core_gates(x,AT)['classification'],'PRIME')

class V41IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'bars.db'
    def cache(self, data):
        with sqlite3.connect(self.db) as con:
            con.execute('CREATE TABLE bars(provider TEXT,venue TEXT,symbol TEXT,timestamp TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,turnover REAL)')
            for code,days in data.items():
                for d in days:
                    con.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)',
                        ('LS','KRX',code,d,100,102,99,101,1000,10000))
    def run_engine(self,live,latest=None):
        if latest is None:latest=live
        def fetch(url):
            if 'market=NXT' in url:return {'market':'NXT','total':0,'rows':[],
                'manifest':dict(MANIFEST,market='NXT',round_id='20261113_143514_NXT')}
            rows=latest if '/latest/' in url else live
            return {'market':'KRX','total':len(rows),'rows':rows,'manifest':MANIFEST}
        op=Operations(self.temp.name,history_path=self.db,fetch=fetch)
        return op.scan(MON)['markets']['KRX']
    def test_one_ipo_verified_exception_does_not_block_complete_evaluable_universe(self):
        self.cache({'000001':WINDOW,'0120G0':WINDOW[-2:]})
        rows=[sec('000001'),sec('0120G0',listing_date='2026-11-12',listing_date_source='KRX',listing_date_verified=True)]
        result=self.run_engine(rows)
        self.assertEqual(result['history_ready_symbols'],1)
        self.assertEqual(result['history_structural_exception_symbols'],1)
        self.assertEqual(result['history_stale_symbols'],0)
        self.assertTrue(result['history_latest_verified'])
        self.assertEqual(result['re25_universe_status'],'COMPLETE_EVALUABLE_WITH_VERIFIED_EXCLUSIONS')
        self.assertEqual(result['re25_structural_exception_symbols'],1)
    def test_ipo_without_provenance_blocks_full_universe(self):
        self.cache({'000001':WINDOW,'0120G0':WINDOW[-2:]})
        result=self.run_engine([sec('000001'),sec('0120G0',listing_date='2026-11-12')])
        self.assertEqual(result['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
        self.assertFalse(result['history_latest_verified'])
        self.assertGreater(result['history_stale_symbols'],0)
    def test_mismatch_16_live_only_keeps_full_scan_but_blocks_complete_claim(self):
        latest=[sec('000001')]
        live=latest+[sec('0120G0',listing_date='2026-11-12',listing_date_source='KRX',listing_date_verified=True)]
        self.cache({'000001':WINDOW,'0120G0':WINDOW[-2:]})
        result=self.run_engine(live,latest)
        self.assertEqual(result['rows_scanned'],2)
        self.assertEqual(result['history_universe_audit']['live_only_count'],1)
        self.assertFalse(result['history_latest_verified'])
        self.assertEqual(result['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
    def test_single_current_symbol_cannot_prime_when_cross_snapshot_differs(self):
        self.cache({'000001':WINDOW})
        base=sec('000001')
        live=[base,sec('0120G0')]
        def fetch(url):
            if 'market=NXT' in url:return {'market':'NXT','rows':[],'total':0,'manifest':dict(MANIFEST,market='NXT',round_id='20261113_143514_NXT')}
            rows=[base] if '/latest/' in url else live
            return {'market':'KRX','rows':rows,'total':len(rows),'manifest':MANIFEST}
        op=Operations(self.temp.name,history_path=self.db,fetch=fetch)
        op.attach(v17=lambda:{'candidates':[{'code':'000001','venue':'KRX','status':'PRE_EXPLOSION_ARMED','entry_price':100,'stop_price':96,'target_price':110,'stop_source':'OBSERVED','target_source':'OBSERVED','news_gate':'CLEAR'}]})
        op.scan(MON)
        rows=op.api('/api/operations/board?market=KRX')[1]['rows']
        matches=[x for x in rows if x['code']=='000001']
        self.assertTrue(matches)
        self.assertEqual(matches[0]['gates']['KRX_UNIVERSE_MATCH'],'FAIL')
        self.assertNotEqual(matches[0]['classification'],'PRIME')
        self.assertEqual(matches[0]['decision'],'PENDING')

    def test_complete_latest_day_but_missing_11_11_still_pending(self):
        self.cache({'000001':[d for d in WINDOW if d!='2026-11-11']})
        result=self.run_engine([sec('000001')])
        self.assertEqual(result['history_latest_date'],'2026-11-13')
        self.assertEqual(result['re25_universe_status'],'PENDING_PARTIAL_HISTORY')
        self.assertFalse(result['history_latest_verified'])

if __name__=='__main__':unittest.main()

class V41RepairClassificationTests(unittest.TestCase):
    def setUp(self):
        from .test_engine import V3RepairSafetyTests
        self.orig=V3RepairSafetyTests('test_actual_sqlite_cache_updates_and_recounts')
        self.orig.setUp()
        self.addCleanup(self.orig.doCleanups)
    def test_repair_verified_ipo_is_not_spammed_with_backfills(self):
        r,w=self.orig._fixture(dates=['2026-11-12','2026-11-13'],names=('0120G0',))
        old_source=r.operations.source
        r.operations.source=lambda market:([dict(x,listing_date='2026-11-12',
                   listing_date_source='KRX',listing_date_verified=True) for x in old_source(market)[0]],*old_source(market)[1:])
        z=r.batch(self.orig.sunday,max_calls=1,pacing=0)
        self.assertEqual(z['status'],'READY_WITH_VERIFIED_EXCLUSIONS')
        self.assertEqual(z['structural_exempt'],1)
        self.assertEqual(z['stale'],0)
        self.assertEqual(w.calls,[])
    def test_repair_unverified_ipo_still_missing_and_retried(self):
        r,w=self.orig._fixture(dates=['2026-11-12','2026-11-13'],names=('0120G0',))
        old=r.operations.source
        r.operations.source=lambda market:([dict(x,listing_date='2026-11-12') for x in old(market)[0]],*old(market)[1:])
        z=r.batch(self.orig.sunday,max_calls=0,pacing=0)
        self.assertEqual(z['status'],'REPAIRING')
        self.assertEqual(z['structural_exempt'],0)
        self.assertEqual(z['stale'],1)
    def test_repair_cross_snapshot_difference_cannot_claim_global_ready(self):
        r,w=self.orig._fixture(dates=WINDOW,names=('000001',))
        r.operations.authority_audit['KRX']={'membership_matched':False,'status':'PENDING_CROSS_SNAPSHOT_MEMBERSHIP_DIFF'}
        z=r.batch(self.orig.sunday,max_calls=1,pacing=0)
        self.assertEqual(z['status'],'PENDING')
        self.assertEqual(z['reason'],'KIS_CROSS_SNAPSHOT_UNIVERSE_DELTA_UNVERIFIED')
        self.assertEqual(z['ready'],1)
        self.assertEqual(z['stale'],0)
    def test_structural_stock_with_missing_post_listing_date_is_not_ready(self):
        r,w=self.orig._fixture(dates=['2026-11-12'],names=('0120G0',))
        old=r.operations.source
        r.operations.source=lambda market:([dict(x,listing_date='2026-11-12',listing_date_source='KRX',listing_date_verified=True) for x in old(market)[0]],*old(market)[1:])
        z=r.batch(self.orig.sunday,max_calls=0,pacing=0)
        self.assertEqual(z['stale'],1)
        self.assertEqual(z['structural_exempt'],0)
        self.assertEqual(z['status'],'REPAIRING')
