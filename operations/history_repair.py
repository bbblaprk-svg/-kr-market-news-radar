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
from datetime import date, datetime, timedelta
from .trading_calendar import recent_sessions, is_snapshot_session
from .universe_audit import classify_window
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
            'structural_exempt':0, 'unverified_missing':0, 'structural_exempt_examples':[],
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
    def _valid_bar(o,h,l,c,v):
        values=[number(z) for z in (o,h,l,c,v)]
        if any(z is None for z in values):return False
        o,h,l,c,v=values
        return (o>0 and h>0 and l>0 and c>0 and v>=0 and
                l<=min(o,c)<=max(o,c)<=h)

    @classmethod
    def _store_inventory(cls,worker,symbols,required):
        """Two indexed SQLite passes: real date/depth and per-date OHLC validity.
        This catches a bad last candle and holes in the six XKRX sessions (five-event window plus predecessor).
        """
        store=worker.store
        result={s:(None,0,set()) for s in symbols}
        if getattr(store,'conn',None) is not None:
            lock=getattr(store,'_lock',threading.RLock())
            with lock:
                rows=store.conn.execute("""
                    SELECT symbol, MAX(timestamp),
                        SUM(CASE WHEN open>0 AND high>0 AND low>0 AND close>0
                          AND volume>=0 AND low<=open AND low<=close
                          AND high>=open AND high>=close THEN 1 ELSE 0 END)
                    FROM bars WHERE provider='LS' AND venue='KRX'
                    GROUP BY symbol
                """).fetchall()
                found={sym:(date.fromisoformat(str(d)),int(n or 0))
                       for sym,d,n in rows if sym in symbols and d}
                args=','.join('?' for _ in required)
                recent=store.conn.execute(f"""
                    SELECT symbol,timestamp,open,high,low,close,volume
                    FROM bars WHERE provider='LS' AND venue='KRX'
                      AND timestamp IN ({args})
                """,list(required)).fetchall()
            by_code={}
            for sym,day,o,h,l,c,v in recent:
                if sym in symbols and cls._valid_bar(o,h,l,c,v):
                    by_code.setdefault(sym,set()).add(day)
            for sym in symbols:
                last,n=found.get(sym,(None,0))
                result[sym]=(last,n,by_code.get(sym,set()))
            return result
        # No native SQLite connection (older cache implementation): retain
        # fail-closed behaviour; cannot infer five valid dates from last_date.
        for sym in symbols:
            result[sym]=(store.last_date('LS','KRX',sym),0,set())
        return result

    @classmethod
    def _validate_latest(cls,worker,symbol,expected,required):
        """No cache is READY unless its last candle and all six necessary sessions pass.
        """
        last=worker.store.last_date('LS','KRX',symbol)
        if last != expected:
            raise ValueError('LS_LAST_DATE_NOT_EQUAL_KIS_COMPLETED_DAY')
        if not hasattr(worker.store,'read'):
            raise ValueError('LS_BAR_READER_UNAVAILABLE')
        df=worker.store.read('LS','KRX',symbol,limit=260)
        if df is None or df.empty:raise ValueError('LS_LAST_BAR_NOT_READABLE')
        found=set()
        for _,r in df.iterrows():
            v=r.get('timestamp')
            if v is None: continue
            d=date.fromisoformat(str(v)[:10])
            if d.isoformat() in required:
                if not cls._valid_bar(*(r.get(k) for k in ('open','high','low','close','volume'))):
                    raise ValueError('LS_RECENT_BAR_OHLC_INVALID')
                found.add(d.isoformat())
        if set(required)-found:
            raise ValueError('LS_RECENT6_SESSIONS_MISSING')
        return last

    @classmethod
    def _fetch_safe_krx_ohlc(cls,worker,symbol,expected,current):
        """Repair-only full lookback; no global env changes or synthetic bars.

        Validate the provider's actual t8410 OHLCV before upserting to the
        native SQLite. Do not write future, holiday, invalid or duplicate bars.
        """
        if not hasattr(worker,'ls') or not hasattr(worker.store,'upsert'):
            # Legacy test adapter only; production requires native provider.
            return worker.bootstrap_one(symbol)
        df=worker.ls.get_daily_bars(symbol,current.date()-timedelta(days=520),current.date())
        if df is None or df.empty:raise ValueError('LS_EOD_FETCH_EMPTY')
        good=[]
        for _,r in df.iterrows():
            v=r.get('timestamp')
            if v is None:continue
            try:day=date.fromisoformat(str(v)[:10]).isoformat()
            except ValueError:continue
            if day>expected.isoformat() or is_snapshot_session(day) is not True:continue
            if not cls._valid_bar(*(r.get(k) for k in ('open','high','low','close','volume'))):continue
            good.append(r)
        if not good or not any(str(z.get('timestamp'))[:10]==expected.isoformat() for z in good):
            raise ValueError('LS_EXPECTED_EOD_BAR_NOT_PROVIDED')
        import pandas as pd
        safe=pd.DataFrame(good).drop_duplicates(subset=['timestamp'],keep='last')
        worker.store.upsert('LS','KRX',symbol,safe)
        return safe

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
        required=recent_sessions(expected_s,6)
        if required is None:
            self._set(status='PENDING',reason='KRX_CALENDAR_UNVERIFIED',
                      expected_date=expected_s,asof=stamp(current));return self.snapshot()
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
            inv=self._store_inventory(worker,set(unique),required)
        except Exception as exc:
            self._set(status='PENDING',reason='LS_CACHE_READ_ERROR',last_error=type(exc).__name__,asof=stamp(current))
            return self.snapshot()
        ready=0; missing=[];exempt=[];depth_lt_200=0;future_count=0
        for symbol,r in unique.items():
            last,nbar,valid_recent=inv.get(symbol,(None,0,set()))
            if nbar<200:depth_lt_200+=1
            if last is not None and last>expected:
                future_count+=1;continue
            eligibility=classify_window(r,required,valid_recent)
            # Missing days can be excused ONLY with explicit verified issuer
            # listing/suspension evidence, never from price/volume absence.
            if last==expected and eligibility.state=='COMPLETE':ready+=1
            elif eligibility.state=='STRUCTURAL_NOT_EVALUABLE':
                exempt.append({'code':symbol,'reason':eligibility.reason,'missing_dates':list(eligibility.missing)})
            else:missing.append((symbol,r,last))
        if future_count:
            self._set(status='PENDING',reason='LS_CACHE_FUTURE_TRADE_DATE',future_dates=future_count,
                      input_complete=True,expected_date=expected_s,universe=len(unique),
                      ready=ready,stale=len(missing),structural_exempt=len(exempt),unverified_missing=len(missing),depth_lt_200=depth_lt_200,asof=stamp(current))
            return self.snapshot()
        # Preserve 500 existing symbols' priority, then market-cap >=700억원, then rest.
        missing.sort(key=lambda z:(0 if z[2] else 1,0 if (number(z[1].get('market_cap_100m')) or 0)>=700 else 1,z[0]))
        audit=getattr(self.operations,'authority_audit',{}).get('KRX',{})
        alignment_ok=audit.get('membership_matched') is True
        done=not missing and alignment_ok
        self._set(status=('READY_WITH_VERIFIED_EXCLUSIONS' if exempt else 'READY') if done else ('REPAIRING' if missing else 'PENDING'),
                  reason=('VERIFIED_EXCLUSIONS_NON_EVALUABLE' if exempt else 'ALL_ELIGIBLE_SYMBOLS_HAVE_CURRENT_LS_DAILY') if done else ('LS_DAILY_BARS_BEHIND_KIS' if missing else 'KIS_CROSS_SNAPSHOT_UNIVERSE_DELTA_UNVERIFIED'),
                  expected_date=expected_s,universe=len(unique),ready=ready,stale=len(missing),
                  structural_exempt=len(exempt),structural_exempt_examples=exempt[:40],
                  unverified_missing=len(missing),universe_audit=audit,
                  depth_lt_200=depth_lt_200,recent5_expected=required,input_complete=True,source=source,source_errors=errors,
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
                self._fetch_safe_krx_ohlc(worker,symbol,expected,current)
                self._validate_latest(worker,symbol,expected,required)
                consecutive=0
                self._set(updated=int(self.snapshot()['updated'])+1,last_updated_code=symbol,
                          ready=int(self.snapshot()['ready'])+1,stale=max(0,int(self.snapshot()['stale'])-1),
                          last_error=None,writes_verified=True,asof=stamp())
            except Exception as exc:
                # A single unavailable stock (ordinary 404, suspension, or
                # no matching six-session candles) must not halt all 2403.
                # Global LS outages, authentication or throttling do stop requests.
                msg=str(exc).lower()
                systemic=any(token in msg for token in ('http 429','http 500','http 502','http 503','http 504',
                        '503.html','timeout','timed out','connection refused','rate limit','quota',
                        'http 401','http 403','access token'))
                consecutive=consecutive+1 if systemic else 0
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
