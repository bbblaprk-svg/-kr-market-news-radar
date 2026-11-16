"""Pure offline tests for permanent KRX/NXT lifecycle invariants."""
import unittest
from .candidate_integrity import EvaluationEvidence, verified_scope, data_hold, quote_label, empty_lane_message, native_nxt_minute_proven

class CandidateIntegrityTests(unittest.TestCase):
    def test_current_krx_full_6_bars_authorizes_only_evaluated_code(self):
        p=verified_scope('KRX','RE_EXPLOSION_25',{'005930','0120G0'},source_complete=True,
                         last_close='2026-11-16',expected_close='2026-11-16')
        self.assertTrue(p.can_expire('0120G0','KRX','RE_EXPLOSION_25'))
        self.assertFalse(p.can_expire('000660','KRX','RE_EXPLOSION_25'))
        self.assertFalse(p.can_expire('005930','NXT','RE_EXPLOSION_25'))
    def test_old_close_must_never_clear_a_newer_kis_day(self):
        p=verified_scope('KRX','RE_EXPLOSION_25',{'005930'},source_complete=True,
                         last_close='2026-11-13',expected_close='2026-11-16')
        self.assertFalse(p.can_expire('005930','KRX','RE_EXPLOSION_25'))
    def test_empty_set_cannot_trigger_market_exit(self):
        p=verified_scope('KRX','RE_EXPLOSION_25',set(),source_complete=True,
                         last_close='2026-11-16',expected_close='2026-11-16')
        self.assertFalse(p.can_expire('005930','KRX','RE_EXPLOSION_25'))
    def test_nxt_and_krx_same_code_never_mix(self):
        p=verified_scope('NXT','NXT_POST_ACCEL_LANE',{'005930'},source_complete=True)
        self.assertTrue(p.can_expire('005930','NXT','NXT_POST_ACCEL_LANE'))
        self.assertFalse(p.can_expire('005930','KRX','NXT_POST_ACCEL_LANE'))
        with self.assertRaises(ValueError):
            EvaluationEvidence('NXT','RE_EXPLOSION_25',frozenset(),True)
    def test_data_hold_is_idempotent_even_after_fifty_missing_scans(self):
        p={'classification':'PRIME','decision':'PRIME','candidate_lifecycle':'CONFIRMED_THIS_CYCLE',
           'candidate_seen_at':'2026-11-13T15:00+09:00','candidate_miss_count':1,
           'signal_id':'confirmed-signal-id','gate_pending':[]}
        for n in range(50):
            p=data_hold(p,at='2026-11-16T19:00+09:00')
        self.assertEqual(p['classification'],'WATCH')
        self.assertEqual(p['decision'],'PENDING')
        self.assertEqual(p['candidate_lifecycle'],'DATA_HOLD')
        self.assertEqual(p['candidate_miss_count'],1)
        self.assertEqual(p['prior_verified_classification'],'PRIME')
        self.assertEqual(p['candidate_seen_at'],'2026-11-13T15:00+09:00')
        self.assertEqual(p['signal_id'],'confirmed-signal-id')
    def test_data_hold_does_not_resurrect_old_stale_state(self):
        p=data_hold({'candidate_stale':True,'candidate_miss_count':3,'gate_pending':[]},at='T')
        self.assertEqual(p['candidate_lifecycle'],'HIDDEN_STALE')
    def test_quote_timestamp_does_not_claim_exchange(self):
        self.assertIn('체결시각 미검증',quote_label({'price':107400,'quote_quality':'REST_CURRENT',
                         'timestamp_quality':'REQUEST_TIMESTAMP'}))
        self.assertIn('지연',quote_label({'price':107400,'quote_quality':'REST_STALE',
                         'timestamp_quality':'REQUEST_TIMESTAMP'}))
        self.assertEqual(quote_label({'price':'UNAVAILABLE'}),'시세 미수신')
    def test_empty_after_stale_daily_is_not_no_stocks(self):
        s={'status':'PENDING','re25_universe_status':'PENDING_PARTIAL_HISTORY',
           're25_evaluable_symbols':0,'rows_scanned':2404}
        self.assertIn('DATA_PENDING',empty_lane_message('KRX','RE_EXPLOSION_25',s))
        self.assertIn('0/2404',empty_lane_message('KRX','RE_EXPLOSION_25',s))
        self.assertIn('0건',empty_lane_message('KRX','RE_EXPLOSION_25',
                                {'re25_universe_status':'COMPLETE'}))
    def test_nxt_native_session_time_has_strict_proof(self):
        from datetime import datetime,timezone,timedelta
        current=datetime(2026,11,16,19,50,tzinfo=timezone(timedelta(hours=9)))
        r={'venue':'NXT','nxt_last_at':'2026-11-16T19:48','nxt_bar_count':70,
           'nxt_span_minutes':69,'nxt_recent60_count':60}
        self.assertTrue(native_nxt_minute_proven(r,current))
        self.assertFalse(native_nxt_minute_proven(dict(r,nxt_last_at='2026-11-16T15:30'),current))
        self.assertFalse(native_nxt_minute_proven(dict(r,venue='KRX'),current))
        self.assertFalse(native_nxt_minute_proven(dict(r,nxt_recent60_count=44),current))
        self.assertFalse(native_nxt_minute_proven(dict(r,nxt_last_at='UNAVAILABLE'),current))
    def test_unknown_scope_fails_closed(self):
        with self.assertRaises(ValueError):
            EvaluationEvidence('UNKNOWN','RE_EXPLOSION_25',frozenset(),True)
