"""Adapters use only existing local objects; never start a broker client or collect bars."""
from .engine import Operations, number, code, dt, stamp, fresh, tri, valid_code
from .trading_calendar import recent_sessions
from pathlib import Path
import json, hashlib, threading, os, sqlite3

class _OperationsUnavailable:
    """Last-resort shell: keep the parent web service alive if Operations cannot start."""
    def __init__(self, errors):
        self.errors=list(errors)
        self.runtime={'status':'PENDING','error':'OPERATIONS_STARTUP_UNAVAILABLE','startup_storage_errors':self.errors}
        self.thread=None
        self.stop=threading.Event()
    def attach(self, **providers):
        return self
    def start(self):
        return None
    def api(self, raw_path):
        return 503, {'status':'PENDING','error':'OPERATIONS_STARTUP_UNAVAILABLE','startup_storage_errors':self.errors}

def _new_operations():
    """Create Operations without allowing SQLite/storage failure to kill the parent web process."""
    preferred=(os.getenv('OPERATIONS_DATA_DIR') or '/data/operations_v7').strip()
    fallback=(os.getenv('OPERATIONS_FALLBACK_DIR') or '/tmp/operations_v7').strip()
    errors=[]
    attempted=set()
    for data_dir, mode in ((preferred,'PERSISTENT'),(fallback,'EPHEMERAL_FALLBACK')):
        if not data_dir or data_dir in attempted:
            continue
        attempted.add(data_dir)
        try:
            ops=Operations(data_dir=data_dir)
            ops.storage_mode=mode
            ops.startup_storage_errors=list(errors)
            return ops
        except (sqlite3.Error,OSError) as exc:
            errors.append(f'{mode}:{type(exc).__name__}:{exc}')
        except Exception as exc:
            errors.append(f'{mode}:{type(exc).__name__}')
    return _OperationsUnavailable(errors)

def attach(app):
    ops=_new_operations()
    if isinstance(ops,_OperationsUnavailable):
        return ops
    def alpha(market):
        # Read the venue's native snapshot. No switching of the collector's current venue.
        return app['load_latest_for_venue'](market) or {}
    def native(name):
        obj=app.get(name)
        return obj.latest() if obj else {}
    v17_daily_cache = {}  # symbol => full-bar digest and unchanged native V1.7 result.
    def v17_all(rows,hist,current):
        try:
            import pandas as pd
            from v17_original_ls_bridge import V17
        except ImportError:
            return {'status':'PENDING','error':'V17_MODULE_UNAVAILABLE'}
        if V17 is None:return {'status':'PENDING','error':'V17_ENGINE_UNAVAILABLE'}
        cfg=V17.Config(); result=[]; missing=[]; rejected=[]
        for r in rows:
            c=code(r); bars=hist.get(c,[])
            if number(r.get('market_cap_100m')) is not None and number(r['market_cap_100m'])<700:continue
            required=recent_sessions(bars[-1]['date'],cfg.min_rows) if bars else None
            if len(bars)<cfg.min_rows or not required or not set(required)<={z['date'] for z in bars}:missing.append(c);continue
            # Hash all of the evaluated bars, not only the last five; historic
            # corrections to older candles must invalidate native V1.7 cache.
            digest=hashlib.blake2b(digest_size=16)
            for bar in bars[-260:]:
                digest.update(repr(tuple(bar.get(k) for k in ('date','open','high','low','close','volume','turnover'))).encode('utf-8'))
            signature=(len(bars), digest.hexdigest())
            prior=v17_daily_cache.get(c)
            if prior and prior[0]==signature:
                z=dict(prior[1])
            else:
                try:
                    x=pd.DataFrame(bars[-260:]).rename(columns={'date':'date'})
                    # Cached actual trade dates only. Never append weekend/holiday synthetic bars.
                    x['date']=pd.to_datetime(x['date']).dt.strftime('%Y%m%d')
                    z=dict(V17.analyze_one(x,cfg,c,r.get('name',c)))
                    z['venue']='KRX';z['signal_history_date']=bars[-1]['date']
                    z['signal_reference_date']=bars[-1]['date']
                    z.pop('signal_asof',None)
                    v17_daily_cache[c]=(signature,dict(z))
                except Exception:
                    missing.append(c);continue
            if z.get('status') not in ('REJECT','HISTORY_FAIL'):
                result.append(z)
            else:rejected.append(c)
        return {'status':'READY','candidates':result,'generated_at':None,'universe_complete':not missing,'history_pending':len(missing),'evaluated_codes':[code(r) for r in result]+rejected,'rejected_count':len(rejected),'scope':'ZERO_BASE_ALL_KIS_ROWS_READONLY_V17'}
    def nxt(rows,current):
        fn=app.get('_nxt_session_features'); out=[]; pending=0
        if not fn:return {'status':'PENDING','rows':[]}
        for r in rows:
            c=code(r)
            # This native function only reads VOLUME_DIRECTION._series; no provider calls.
            f=fn(c,dict(r))
            if not f.get('nxt_bar_count'):pending+=1;continue
            f.update(code=c,venue='NXT',name=r.get('name'),signal_asof=f.get('nxt_last_at'))
            span=number(f.get('nxt_span_minutes'));count=number(f.get('nxt_bar_count'))
            enough=bool(span is not None and span>=60 and count is not None and count>=60 and (number(f.get('nxt_recent60_count')) or 0)>=60)
            reclaim=number(f.get('nxt_reclaim_ratio'));accel=number(f.get('nxt_volume_accel15'));hh=number(f.get('nxt_high_hold60'))
            turn=number(f.get('nxt_turnover_accel15'));positive=number(f.get('nxt_price15_pct'))
            f['reaccel_seen']=bool(reclaim>=.98 and accel>=1.5 and turn>=1.4 and positive>0) if all(x is not None for x in (reclaim,accel,turn,positive)) and enough else None
            f['high_hold60_confirmed']=hh>=.985 if hh is not None and enough else None
            f['price_hold']=f['high_hold60_confirmed']
            f['gate_status']='PASS' if enough and f.get('nxt_impulse_seen') and f.get('nxt_recompression_seen') and f['reaccel_seen'] and f['high_hold60_confirmed'] else 'PENDING'
            p=number(r.get('price'));stop=number(f.get('nxt_local_low60'));target=number(f.get('nxt_session_high'))
            f.update(entry_price=p,stop_price=stop,stop_source='NXT_OBSERVED_LOCAL_LOW60' if stop else None,target_price=target if p and target and target>p else None,target_source='NXT_OBSERVED_SESSION_HIGH' if p and target and target>p else None)
            out.append(f)
        return {'status':'READY','rows':out,'source_asof':None,'universe_complete':pending==0,'pending_series':pending}
    def news(market,current):
        obj=app.get('NEWS_IMPACT_SIDECAR');out={}
        if obj is None:return out
        try:items=obj.history(200).get('rows',[])
        except Exception:return out
        for z in reversed(items):
            c=str(z.get('ticker') or z.get('code') or '')
            # Preserve exact company mapping and original news classification.
            if not valid_code(c):continue
            at=z.get('published_at') or z.get('published_at_kst')
            if not fresh(at,current,86400):continue
            state=z.get('state');meta=z.get('metadata') or {}
            supported=('NEGATIVE_CONFIRMED','OVERHANG','POSITIVE_NEWS_FAILED','PRICED_IN','SELL_THE_NEWS','NEGATIVE_ABSORBED','PASS','CLEAR','POSITIVE_CONFIRMED')
            gate=state if state in supported else meta.get('news_gate')
            if gate not in supported:continue
            out[c]={'news_gate':gate,'news_published_at':at,'news_source':z.get('url') or z.get('source')}
        return out
    from .history_repair import KRXHistoryRepair
    def native_busy():
        state=app.get('STATE') or {}
        lock=app.get('LOCK')
        if lock is None:return bool(state.get('running'))
        with lock:return bool(state.get('running'))
    # A dedicated cache writer, NOT the shared lazy-history engine. Changing its
    # lookback/min-bars would otherwise mutate the native scanner's behavior.
    repair_worker=[None]
    repair_worker_lock=threading.RLock()
    def history_engine(venue):
        if venue!='KRX':raise ValueError('KRX_ONLY_REPAIR')
        with repair_worker_lock:
            if repair_worker[0] is None:
                from 오백종목_섹터에너지_알파하이킨아시_통합_V1_0 import FastHistory500
                ls=app['get_engine']().ls  # share broker session and OAuth, no extra credentials
                worker=FastHistory500(ls,db_path=str(ops.history.path),min_bars=220,lookback_days=520)
                worker.set_venue('KRX','K')
                repair_worker[0]=worker
            return repair_worker[0]
    repair=KRXHistoryRepair(ops,history_engine,native_busy)
    ops.attach(alpha=alpha,news=news,v17=lambda:native('V17_ORIGINAL_BRIDGE'),v17_all=v17_all,fullmarket=lambda:native('FULLMARKET_V3_BRIDGE'),nxt=nxt,history_repair_state=repair.snapshot)
    repair.start()  # isolated, session-aware, rate-limited EOD repair; native collectors untouched.
    return ops
