"""KRX-only LS EOD repair, isolated from the native KRX/NXT collectors.

Only the existing LS provider's get_daily_bars through a separately constructed
FastHistory500 is used. No KIS quote/request timestamp becomes an EOD candle.
Never publishes trading entries, never changes the native scanner's venue.
"""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from .engine import now, stamp, code, expected_closed_daily_date, valid_code, number

class KRXHistoryRepair:
    def __init__(self, operations, get_history_engine, busy=lambda: False,
                 state_path='/data/operations/krx_history_repair_state.json',
                 sleep=time.sleep, clock=now):
        self.operations = operations
        self.get_history_engine = get_history_engine
        self.busy = busy
        self.path = Path(state_path)
        self.sleep = sleep
        self.clock = clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.failed_until = {}
        self.state = {
            'status':'PENDING', 'reason':'NOT_STARTED', 'venue':'KRX',
            'expected_date':None, 'universe':0, 'ready':0, 'stale':0,
            'updated':0, 'attempted':0, 'failed':0, 'depth_lt_200':0,
            'last_error':None, 'last_updated_code':None, 'asof':None,
            'cache_path':str(self.operations.history.path) if hasattr(self.operations,'history') else None,
            'input_complete':False, 'writes_verified':False,
        }
        # Only operational counters are restored. Never restore READY from a prior
        # process without rechecking the KIS manifest and actual SQLite bars.
        try:
            old = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(old,dict):
                for k in ('updated','attempted','failed','last_updated_code'):
                    if k in old:self.state[k]=old[k]
        except (OSError,ValueError,TypeError):pass

    @staticmethod
    def allowed(current):
        # Weekend or weekday 21:00-08:00 only. Avoid KRX regular, NXT pre/after.
        return current.weekday() >= 5 or current.hour < 8 or current.hour >= 21

    def snapshot(self):
        with self.lock:return dict(self.state)

    def _set(self, **updates):
        with self.lock:
            self.state.update(updates)
            snap = dict(self.state)
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(snap,ensure_ascii=False,default=str),encoding='utf-8')
            tmp.replace(self.path)
        except OSError:pass

    @staticmethod
    def _store_inventory(worker, symbols):
        """One indexed SQLite read instead of thousands of last_date() queries.
        Safely falls back to the original store API for mocked/older stores.
        """
        store=worker.store
        if getattr(store,'conn',None) is not None:
            lock=getattr(store,'_lock',threading.RLock())
            with lock:
                rows=store.conn.execute(
                    "SELECT symbol, MAX(timestamp), COUNT(*) FROM bars "
                    "WHERE provider='LS' AND venue='KRX' GROUP BY symbol"
                ).fetchall()
            result={s:(date.fromisoformat(str(d)),int(n)) for s,d,n in rows if s in symbols and d}
            return {s:result.get(s,(None,0)) for s in symbols}
        result={}
        for s in symbols:
            last=store.last_date('LS','KRX',s)
            result[s]=(last,0)
        return result

    @staticmethod
    def _validate_latest(worker, symbol, expected):
        """Check real cache date, valid OHLC values, and reject fabricated future bars.
        No OHLC values are created or rewritten by this method.
        """
        last=worker.store.last_date('LS','KRX',symbol)
        if last != expected:
            raise ValueError('LS_LAST_DATE_NOT_EQUAL_KIS_COMPLETED_DAY')
        if hasattr(worker.store,'read'):
            df=worker.store.read('LS','KRX',symbol,limit=1)
            if df is None or df.empty:raise ValueError('LS_LAST_BAR_NOT_READABLE')
            r=df.iloc[-1]
            o,h,l,c=(number(r.get(k)) for k in ('open','high','low','close'))
            vol=number(r.get('volume'))
            if any(z is None or z <= 0 for z in (o,h,l,c)) or vol is None or vol < 0 or not (l<=min(o,c)<=max(o,c)<=h):
                raise ValueError('LS_LAST_BAR_OHLC_INVALID')
        return last

    def batch(self, current=None, max_calls=None, pacing=None):
        current = current or self.clock()
        if os.getenv('KRX_DAILY_REPAIR_ENABLED','true').strip().lower() not in ('1','true','yes','on'):
            self._set(status='DISABLED',reason='CONFIG_DISABLED',asof=stamp(current));return self.snapshot()
        if not self.allowed(current):
            self._set(status='PAUSED',reason='SESSION_PROTECTION',asof=stamp(current));return self.snapshot()
        try:
            if self.busy():
                self._set(status='PAUSED',reason='NATIVE_SCAN_RUNNING',asof=stamp(current));return self.snapshot()
        except Exception:
            self._set(status='PENDING',reason='BUSY_STATE_UNAVAILABLE',asof=stamp(current));return self.snapshot()
        rows,manifest,complete,source,errors=self.operations.source('KRX')
        expected_s=expected_closed_daily_date(manifest,current,'KRX')
        if not complete or not expected_s:
            self._set(status='PENDING',reason='KIS_UNIVERSE_OR_COMPLETED_DAY_UNVERIFIED',
                      input_complete=False,source=source,source_errors=errors,
                      expected_date=expected_s,asof=stamp(current))
            return self.snapshot()
        expected=date.fromisoformat(expected_s)
        try:
            worker=self.get_history_engine('KRX')
            if getattr(worker,'venue',None)!='KRX':raise RuntimeError('LS_WORKER_VENUE_NOT_KRX')
            store_path=getattr(worker.store,'path',None)
            source_path=getattr(getattr(self.operations,'history',None),'path',None)
            if store_path is not None and source_path is not None and Path(store_path).resolve()!=Path(source_path).resolve():
                raise RuntimeError('REPAIR_WRITES_DIFFERENT_CACHE_FROM_OPERATIONS')
        except Exception as exc:
            self._set(status='PENDING',reason='LS_HISTORY_ADAPTER_UNAVAILABLE',
                      last_error=f'{type(exc).__name__}:{str(exc)[:80]}',asof=stamp(current))
            return self.snapshot()
        unique={code(r):r for r in rows if valid_code(code(r))}
        if len(unique)!=len(rows):
            self._set(status='PENDING',reason='KIS_DUPLICATE_CODES',input_complete=False,asof=stamp(current));return self.snapshot()
        try:
            inv=self._store_inventory(worker,set(unique))
        except Exception as exc:
            self._set(status='PENDING',reason='LS_CACHE_READ_ERROR',last_error=type(exc).__name__,asof=stamp(current))
            return self.snapshot()
        ready=0; missing=[];depth_lt_200=0;future_count=0
        for symbol,r in unique.items():
            last,nbar=inv.get(symbol,(None,0))
            if nbar and nbar<200:depth_lt_200+=1
            if last is not None and last>expected:
                future_count+=1;continue
            if last==expected:ready+=1
            else:missing.append((symbol,r,last))
        if future_count:
            self._set(status='PENDING',reason='LS_CACHE_FUTURE_TRADE_DATE',future_dates=future_count,
                      input_complete=True,expected_date=expected_s,universe=len(unique),
                      ready=ready,stale=len(missing),depth_lt_200=depth_lt_200,asof=stamp(current))
            return self.snapshot()
        # Preserve 500 existing symbols' priority, then market-cap >=700억원, then rest.
        missing.sort(key=lambda z:(0 if z[2] else 1,0 if (number(z[1].get('market_cap_100m')) or 0)>=700 else 1,z[0]))
        self._set(status='READY' if not missing else 'REPAIRING',
                  reason='ALL_KIS_SYMBOLS_HAVE_CURRENT_LS_DAILY' if not missing else 'LS_DAILY_BARS_BEHIND_KIS',
                  expected_date=expected_s,universe=len(unique),ready=ready,stale=len(missing),
                  depth_lt_200=depth_lt_200,input_complete=True,source=source,source_errors=errors,
                  asof=stamp(current),cache_path=str(source_path) if source_path else None)
        eligible=[x for x in missing if time.monotonic()>=self.failed_until.get(x[0],0)]
        if missing and not eligible:
            self._set(status='PENDING',reason='LS_SYMBOL_RETRY_COOLDOWN',asof=stamp(current));return self.snapshot()
        try: n = int(max_calls if max_calls is not None else os.getenv('KRX_DAILY_REPAIR_BATCH','16'))
        except (ValueError,TypeError):n=16
        n=max(0,min(n,30))
        try: pace = float(pacing) if pacing is not None else max(3.0,float(os.getenv('KRX_DAILY_REPAIR_PACING_SECONDS','4.0')))
        except (ValueError,TypeError):pace=4.0
        pace=max(0.0,pace)
        consecutive=0
        for symbol,row,last in eligible[:n]:
            if self.stop.is_set() or not self.allowed(self.clock()):break
            try:
                if self.busy():break
            except Exception:break
            self._set(last_attempt_code=symbol,attempted=int(self.snapshot()['attempted'])+1,asof=stamp())
            try:
                worker.bootstrap_one(symbol)
                self._validate_latest(worker,symbol,expected)
                consecutive=0
                self._set(updated=int(self.snapshot()['updated'])+1,last_updated_code=symbol,
                          ready=int(self.snapshot()['ready'])+1,stale=max(0,int(self.snapshot()['stale'])-1),
                          last_error=None,writes_verified=True,asof=stamp())
            except Exception as exc:
                consecutive+=1
                self.failed_until[symbol]=time.monotonic()+1800
                self._set(failed=int(self.snapshot()['failed'])+1,
                          last_failed_code=symbol,last_error=f'{type(exc).__name__}:{str(exc)[:90]}',
                          reason='LS_FETCH_FAILED_OR_BAR_INVALID',asof=stamp())
                if consecutive>=3:
                    self._set(status='PENDING',reason='LS_BROKER_CIRCUIT_OPEN',asof=stamp())
                    break
            if pace:self.sleep(pace)
        # Recount on next batch to confirm persisted source cache, rather than
        # treating a request completed timestamp as proof of EOD coverage.
        return self.snapshot()

    def start(self):
        if self.thread and self.thread.is_alive():return
        def loop():
            self.stop.wait(40)
            while not self.stop.is_set():
                try:
                    result=self.batch()
                    wait=900 if result['reason'] in (
                        'LS_BROKER_CIRCUIT_OPEN','LS_HISTORY_ADAPTER_UNAVAILABLE',
                        'KIS_UNIVERSE_OR_COMPLETED_DAY_UNVERIFIED','LS_SYMBOL_RETRY_COOLDOWN'
                    ) else 45 if result['status']=='REPAIRING' else 600
                except Exception as exc:
                    self._set(status='PENDING',reason='UNEXPECTED_REPAIR_EXCEPTION',last_error=type(exc).__name__,asof=stamp())
                    wait=900
                self.stop.wait(wait)
        self.thread=threading.Thread(target=loop,name='krx-ls-daily-repair-v3',daemon=True)
        self.thread.start()
