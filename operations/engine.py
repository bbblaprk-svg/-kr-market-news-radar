"""Read-only operational decision projection. Never imports broker clients or writes source caches."""
from __future__ import annotations
import json, math, os, sqlite3, threading, time, urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = 'KRX_NXT_OPERATIONS_V1'
KST = timezone(timedelta(hours=9))
LANES = ('NXT_POST_ACCEL_LANE', 'V17_ACTION_PRE', 'ASYMMETRIC_RS', 'CAPITAL_HANDOFF', 'RE_EXPLOSION_25')
VETO = {'RISK_OFF_ACCEL', 'PROGRAM_SELL_ACCEL', 'BREADTH_COLLAPSE'}
NEWS_BLOCK = {'PRICED_IN', 'SELL_THE_NEWS', 'NEGATIVE_CONFIRMED'}
RANK = {'PRIME': 0, 'ARMED': 1, 'PRE_EXPLOSION_ARMED': 2, 'WATCH': 3, 'REJECT': 4}
MISSING = ('UNAVAILABLE', 'PENDING', '', None)

def now(): return datetime.now(KST)
def stamp(d=None): return (d or now()).isoformat(timespec='seconds')
def dt(v):
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return d.astimezone(KST) if d.tzinfo else None
    except (ValueError, TypeError): return None

def number(v):
    if isinstance(v, bool): return None
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError): return None

def pick(row, *keys):
    for k in keys:
        v = row.get(k)
        if v is not None and v != '' and not (isinstance(v, float) and not math.isfinite(v)): return v
    return None

def tri(v):
    if v is True or v == 'PASS': return True
    if v is False or v in ('FAIL', 'BLOCK', 'REJECT'): return False
    return None

def clean(v):
    if isinstance(v, dict): return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [clean(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v): return 'UNAVAILABLE'
    return 'UNAVAILABLE' if v is None else v

def code(r): return str(pick(r, 'code', 'symbol') or '').zfill(6)
def venue(r):
    v = str(pick(r, 'venue', 'exchange') or '').upper()
    if v in ('KRX', 'NXT'): return v
    v = str(r.get('market') or '').upper()
    return v if v in ('KRX', 'NXT') else None

def trade_date(v):
    """Only literal exchange daily dates, never reinterpret a calendar day as an intraday quote."""
    if not isinstance(v, str): return None
    try:
        if len(v)==10 and v[4:5]=='-' and v[7:8]=='-':
            return datetime.strptime(v,'%Y-%m-%d').date().isoformat()
        if len(v)==8 and v.isdigit():
            return datetime.strptime(v,'%Y%m%d').date().isoformat()
    except ValueError: pass
    return None

def expected_closed_daily_date(manifest, current):
    """Source-provided trade date, only after that trade day's close. No weekday inference."""
    trade=trade_date((manifest or {}).get('trade_date'))
    if not trade or trade>current.date().isoformat(): return None
    # A KIS manifest itself may be an old frozen snapshot. With no verified
    # holiday calendar, a >3-calendar-day-old manifest cannot certify freshness.
    if (current.date()-datetime.strptime(trade,'%Y-%m-%d').date()).days>3: return None
    if trade==current.date().isoformat() and (current.hour,current.minute)<(15,30): return None
    return trade

def fresh(at, current, seconds=180):
    d = dt(at)
    return d is not None and -5 <= (current-d).total_seconds() <= seconds

def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': VERSION, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=12) as r:
        if r.status != 200: raise ValueError('HTTP_' + str(r.status))
        return json.loads(r.read(16_000_000))

class HistorySnapshot:
    """Bounded per-symbol reads; never materialize the full market's daily bars in RAM."""
    def __init__(self, con, count): self.con=con; self.count=count
    def __len__(self): return self.count
    def get(self, symbol, default=None):
        rows=self.con.execute("SELECT timestamp,open,high,low,close,volume,turnover FROM bars WHERE provider='LS' AND venue='KRX' AND symbol=? ORDER BY timestamp DESC LIMIT 260",(symbol,)).fetchall()
        if not rows:return [] if default is None else default
        return [dict(date=t,open=o,high=h,low=l,close=p,volume=v,turnover=tv) for t,o,h,l,p,v,tv in reversed(rows) if len(str(t))==10]
    def close(self): self.con.close()

class History:
    """Existing broker-owned SQLite is opened in read-only mode, with bounded memory."""
    def __init__(self, path): self.path = Path(path)
    def read_all(self):
        if not self.path.exists(): return {}, [], 'PENDING_CACHE_NOT_FOUND'
        con=None
        try:
            con=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
            # No schema, upserts or write pragmas in the source cache.
            dates=[r[0] for r in con.execute("SELECT DISTINCT timestamp FROM bars WHERE provider='LS' AND venue='KRX' ORDER BY timestamp DESC LIMIT 6").fetchall() if len(str(r[0]))==10]
            count=con.execute("SELECT COUNT(DISTINCT symbol) FROM bars WHERE provider='LS' AND venue='KRX'").fetchone()[0]
            return HistorySnapshot(con,count),sorted(dates),'READY' if dates else 'PENDING_EMPTY_CACHE'
        except (sqlite3.Error,OSError) as e:
            if con:con.close()
            return {}, [], 'PENDING_'+type(e).__name__

def history_metrics(bars, asof):
    x = [r for r in bars if str(r['date']) <= asof]
    out = {'history_asof': x[-1]['date'] if x else None, 'history_bars': len(x)}
    closes = [number(r['close']) for r in x]
    for n in (5,20,60,120,200):
        out['ma'+str(n)] = sum(closes[-n:])/n if len(closes)>=n and all(z is not None and z>0 for z in closes[-n:]) else None
    if len(x) >= 6:
        vols=[number(r['volume']) for r in x[-6:-1]]
        last=number(x[-1]['volume'])
        out['rvol_eod5'] = last/(sum(vols)/5) if all(z is not None for z in vols) and sum(vols)>0 and last is not None else None
    return out

def spike_events(bars, recent_dates):
    events=[]
    for i in range(1,len(bars)):
        r, prev = bars[i], bars[i-1]
        p,h=number(prev['close']),number(r['high'])
        if r['date'] not in recent_dates or not p or h is None or h/p < 1.25: continue
        later=bars[i+1:]
        age = sum(d>r['date'] for d in recent_dates)
        after=[z for z in later if z['date'] in recent_dates]
        v=number(r['volume']); last=after[-1] if after else r
        postv=[number(z['volume']) for z in after]
        ranges=[(z['high']-z['low'])/z['close'] for z in after if all(number(z[k]) is not None and number(z[k])>0 for k in ('high','low','close'))]
        events.append({'spike_date':r['date'],'spike_high_pct':round((h/p-1)*100,4),'spike_close':number(r['close']),'spike_high':h,'spike_age':age,'fast_new_spike':age==0,
                       'price_preservation_pct':round(number(last['close'])/h*100,4) if number(last['close']) is not None else None,
                       'pullback_volume_ratio':sum(postv)/len(postv)/v if postv and v and all(z is not None for z in postv) else None,
                       'post_range_pct':sum(ranges)/len(ranges)*100 if ranges else None,
                       'support_price':min(z['low'] for z in after) if after and all(number(z['low']) is not None for z in after) else None})
    return events

def core_gates(row, current):
    """All required gates must pass. Unknown is never a successful observation."""
    gates={}
    gates['MARKET_STATE']=False if row.get('market_state') in VETO else (True if row.get('market_state') in ('NORMAL','RISK_ON','NEUTRAL') else None)
    gates['NEWS']=False if row.get('news_gate') in NEWS_BLOCK else (True if row.get('news_gate') in ('PASS','CLEAR','POSITIVE_CONFIRMED') else None)
    gates['NO_CHASE']=None if number(row.get('change_pct')) is None else number(row['change_pct'])<=5
    if row.get('no_chase') is True: gates['NO_CHASE']=False
    if row.get('news_gate')=='PRICED_IN': gates['NO_CHASE']=False
    gates['SECURITY_COMMON']=tri(row.get('common_stock_confirmed'))
    gates['CAP_700']=None if number(row.get('market_cap_100m')) is None else number(row['market_cap_100m'])>=700
    gates['DATA_FRESH']=fresh(row.get('price_asof'),current) and row.get('timestamp_quality')=='EXCHANGE_TIMESTAMP' and row.get('quote_quality') not in ('EXPIRED','REST_STALE','STALE')
    gates['SESSION']=tri(row.get('session_open'))
    gates['SIGNAL_FRESH']=fresh(row.get('signal_asof'),current,600)
    # All KRX-based daily-signal lanes require confirmed, completed KRX daily history.
    # NXT post-acceleration is independent and uses observed NXT minute bars instead.
    if row['lane'] != 'NXT_POST_ACCEL_LANE':
        gates['KRX_DAILY_CURRENT']=tri(row.get('history_current'))
    p,s,t = (number(row.get(k)) for k in ('entry_price','stop_price','target_price'))
    valid = p is not None and s is not None and t is not None and 0<s<p<t
    rr = (t-p)/(p-s) if valid else None
    row['rr']=rr
    # A target must be an observed level supplied by the engine, never generated as entry+2R.
    gates['RR_GE_2']=rr>=2 if valid and row.get('target_source') not in MISSING and row.get('stop_source') not in MISSING else (False if all(z is not None for z in (p,s,t)) and not valid else None)
    gates['LS_CROSSCHECK']=tri(row.get('ls_crosscheck'))
    holds=[tri(row.get(k)) for k in ('vwap_hold','price_hold','box_reclaim_hold','flow_reversal','sector_expansion')]
    gates['ENTRY_CONFIRM']=True if True in holds else (False if all(x is False for x in holds) else None)
    gates['LANE_SETUP']=tri(row.get('lane_confirmed'))
    if row['lane']=='NXT_POST_ACCEL_LANE':
        gates['NXT_INDEPENDENT_SESSION']=row['market']=='NXT' and current.hour*60+current.minute>=930 and current.hour*60+current.minute<1200 and row.get('session_open') is True
        for k in ('impulse_seen','recompression_seen','reaccel_seen','high_hold60_confirmed'):
            gates['NXT_'+k.upper()]=tri(row.get(k))
    if row['lane']=='RE_EXPLOSION_25':
        gates['SPIKE_AGE_1_5']=1<=row.get('spike_age',-1)<=5
        gates['FULL_RECENT5_HISTORY']=tri(row.get('recent5_complete'))
        for k in ('price_preserved','volume_contracting','compression_confirmed','support_near'):
            gates['RE25_'+k.upper()]=tri(row.get(k))
    row['gates']={k:'PASS' if v is True else 'FAIL' if v is False else 'PENDING' for k,v in gates.items()}
    hard={'MARKET_STATE','NEWS','NO_CHASE','SECURITY_COMMON','CAP_700','RR_GE_2'}
    fail=[k for k,v in gates.items() if v is False]
    pending=[k for k,v in gates.items() if v is None]
    if any(k in hard for k in fail): classification='REJECT'
    elif not fail and not pending: classification='PRIME'
    elif row.get('upstream_state')=='PRE_EXPLOSION_ARMED' and gates['DATA_FRESH'] and gates['SIGNAL_FRESH'] and gates['SESSION'] and not pending and fail==['ENTRY_CONFIRM']:
        classification='PRE_EXPLOSION_ARMED'
    elif gates['LANE_SETUP'] is True and gates['DATA_FRESH'] and gates['SIGNAL_FRESH'] and gates['SESSION'] and not pending and set(fail)<= {'ENTRY_CONFIRM'}:
        classification='ARMED'
    else: classification='WATCH'
    row.update(classification=classification,decision='PENDING' if pending or any(k in fail for k in ('DATA_FRESH','SIGNAL_FRESH','SESSION','NXT_INDEPENDENT_SESSION','KRX_DAILY_CURRENT')) else classification,
               data_status='PENDING' if pending or not gates['DATA_FRESH'] or not gates['SIGNAL_FRESH'] or gates.get('KRX_DAILY_CURRENT') is False else 'READY',
               gate_failures=fail, gate_pending=pending,
               promotion_conditions=[k+'=PASS' for k,v in row['gates'].items() if v!='PASS'],
               invalidation_conditions=['MARKET_STATE veto','NO_CHASE','뉴스 차단','손익비 2R 미달','명시된 지지/손절선 이탈'],
               entry_phase='진입 확인 신호' if classification=='PRIME' else ('이미 급등 · 추격금지' if gates['NO_CHASE'] is False or row.get('fast_new_spike') else '진입 전 · 조건 대기'))
    return row

class Store:
    """Append-only event ledger and sampled signal outcomes, separate from upstream files."""
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS candidates(key TEXT PRIMARY KEY,market TEXT,lane TEXT,code TEXT,active INTEGER,payload TEXT,first_at TEXT,last_at TEXT,occurrences INTEGER,signal_id TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,market TEXT,lane TEXT,code TEXT,name TEXT,event TEXT,reason TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY,quote_at TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY,market TEXT,lane TEXT,code TEXT,name TEXT,entry_at TEXT,entry_price REAL,stop REAL,target REAL,last_at TEXT,last_price REAL,mfe REAL,mae REAL,status TEXT,exit_at TEXT,exit_price REAL,exit_reason TEXT);
            CREATE INDEX IF NOT EXISTS events_market_id ON events(market,id);
            CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,market TEXT,status TEXT,payload TEXT);
            ''')
    def connect(self):
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL');return c
    def observe(self,market,rows,current):
        out={}
        with self.connect() as c:
            for r in rows:
                key=market+':'+code(r); at=r.get('source_timestamp'); t=dt(at)
                old=c.execute('SELECT * FROM observations WHERE key=?',(key,)).fetchone()
                p=json.loads(old['payload']) if old else None
                out[code(r)]={'turnover_velocity':None,'volume_velocity':None}
                if p and t and dt(old['quote_at']) and t>dt(old['quote_at']) and t.date()==dt(old['quote_at']).date():
                    seconds=(t-dt(old['quote_at'])).total_seconds()
                    if seconds<=300 and fresh(at,current) and r.get('source_timestamp_quality')=='EXCHANGE_TIMESTAMP':
                        for field,name in [('turnover','turnover_velocity'),('volume','volume_velocity')]:
                            a,b=number(r.get(field)),number(p.get(field))
                            if a is not None and b is not None and a>=b:out[code(r)][name]=(a-b)/seconds*60
                if not old or (t and dt(old['quote_at']) and t>dt(old['quote_at'])):
                    c.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?)',(key,at,json.dumps(clean(r))))
        return out
    def reconcile(self,market,lane,rows,complete,current):
        at=stamp(current)
        with self.connect() as c:
            previous={r['key']:r for r in c.execute('SELECT * FROM candidates WHERE market=? AND lane=? AND active=1',(market,lane))}
            seen=set()
            for row in rows:
                key=market+':'+lane+':'+row['code']; seen.add(key)
                old=c.execute('SELECT * FROM candidates WHERE key=?',(key,)).fetchone(); prev=json.loads(old['payload']) if old else {}
                state_keys=('classification','decision','upstream_state','gate_failures','gate_pending','spike_date')
                changed=any(prev.get(k)!=row.get(k) for k in state_keys)
                event='NEW' if not old or not old['active'] else ('EXIT' if row['classification']=='REJECT' and prev.get('classification')!='REJECT' else 'CHANGE' if changed else 'HOLD')
                # An occurrence is a new episode, not a timer tick or metric refresh.
                occ=(old['occurrences'] if old else 0)+(event=='NEW')
                row.update(first_seen_at=old['first_at'] if old else at,occurred_at=at if changed or event=='NEW' else prev.get('occurred_at',at),last_seen_at=at,occurrences=occ)
                sid=old['signal_id'] if old else None
                if row['classification']=='PRIME' and (not old or not old['active'] or prev.get('classification')!='PRIME'):
                    sid=key+':'+at
                    c.execute('INSERT OR IGNORE INTO outcomes(id,market,lane,code,name,entry_at,entry_price,stop,target,mfe,mae,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (sid,market,lane,row['code'],row['name'],at,row['entry_price'],row['stop_price'],row['target_price'],0,0,'TRACKING_SIGNAL'))
                row['signal_id']=sid
                # Identical source and state => no duplicate HOLD from polling or a restart.
                if event!='HOLD' or (row.get('price_asof')!=prev.get('price_asof') and row.get('data_status')=='READY'):
                    reason='; '.join(row['gate_failures']+row['gate_pending']) or 'ALL_REQUIRED_GATES_PASS'
                    c.execute('INSERT INTO events(at,market,lane,code,name,event,reason,payload) VALUES(?,?,?,?,?,?,?,?)',(at,market,lane,row['code'],row['name'],event,reason,json.dumps(clean(row),ensure_ascii=False)))
                c.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',(key,market,lane,row['code'],1,json.dumps(clean(row),ensure_ascii=False),row['first_seen_at'],at,occ,sid))
            if complete:
                for key,old in previous.items():
                    if key in seen:continue
                    if isinstance(complete,set) and old['code'] not in complete:
                        p=json.loads(old['payload']);p.update(data_status='PENDING',decision='PENDING',classification='WATCH',gate_pending=sorted(set(p.get('gate_pending',[])+['SOURCE_INCOMPLETE'])))
                        c.execute('UPDATE candidates SET payload=? WHERE key=?',(json.dumps(p,ensure_ascii=False),key));continue
                    p=json.loads(old['payload']);reason='ZERO_BASE_RESCAN_NOT_SELECTED'
                    c.execute('UPDATE candidates SET active=0,last_at=? WHERE key=?',(at,key))
                    c.execute('INSERT INTO events(at,market,lane,code,name,event,reason,payload) VALUES(?,?,?,?,?,?,?,?)',(at,market,lane,p['code'],p['name'],'EXIT',reason,old['payload']))
            else:
                # Partial/failed feed is an outage, never evidence of selection exit.
                for key,old in previous.items():
                    if key in seen:continue
                    p=json.loads(old['payload']);p.update(data_status='PENDING',decision='PENDING',classification='WATCH',gate_pending=sorted(set(p.get('gate_pending',[])+['SOURCE_INCOMPLETE'])))
                    c.execute('UPDATE candidates SET payload=? WHERE key=?',(json.dumps(p,ensure_ascii=False),key))
    def track(self,market,quotes,current):
        with self.connect() as c:
            for r in c.execute("SELECT * FROM outcomes WHERE market=? AND status='TRACKING_SIGNAL'",(market,)).fetchall():
                q=quotes.get(r['code']);p=number(q.get('price')) if q else None;at=q.get('source_timestamp') if q else None
                if not p or not fresh(at,current) or q.get('source_timestamp_quality')!='EXCHANGE_TIMESTAMP' or not dt(at) or dt(at)<=dt(r['entry_at']):continue
                if r['last_at'] and dt(at)<=dt(r['last_at']):continue
                ret=(p/r['entry_price']-1)*100;mfe=max(r['mfe'],ret);mae=min(r['mae'],ret)
                reason='STOP_OBSERVED' if p<=r['stop'] else ('TARGET_OBSERVED' if p>=r['target'] else None)
                c.execute('UPDATE outcomes SET last_at=?,last_price=?,mfe=?,mae=?,status=?,exit_at=?,exit_price=?,exit_reason=? WHERE id=?',
                          (at,p,mfe,mae,'CLOSED_SAMPLED' if reason else 'TRACKING_SIGNAL',at if reason else None,p if reason else None,reason,r['id']))
    def records(self,kind,market,limit=500,offset=0,lane=None):
        with self.connect() as c:
            where='market=?';params=[market]
            if kind=='candidates':where+=' AND active=1'
            if lane:where+=' AND lane=?';params.append(lane)
            total=c.execute(f'SELECT COUNT(*) FROM {kind} WHERE {where}',params).fetchone()[0]
            order='id DESC' if kind in ('events','cycles') else ('entry_at DESC' if kind=='outcomes' else 'last_at DESC')
            records=[dict(r) for r in c.execute(f'SELECT * FROM {kind} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?',params+[limit,offset])]
        for r in records:
            if 'payload' in r:r['payload']=json.loads(r['payload'])
        return {'total':total,'rows':records,'offset':offset,'limit':limit}

class Operations:
    def __init__(self,data_dir='/data/operations',kis_base=None,history_path=None,fetch=get_json):
        self.store=Store(Path(data_dir)/'operations.sqlite3')
        from .persistence import Checkpoint
        self.checkpoint=Checkpoint(self.store);self.checkpoint.restore()
        self.kis=(kis_base or os.getenv('KIS_LEDGER_BASE_URL','https://quant-signal-v3-7-51-clean2.onrender.com')).rstrip('/')
        self.history=History(history_path or os.getenv('V17_ORIGINAL_HISTORY_DB',os.getenv('CACHE_DB','/data/alpha500_bars.sqlite3')))
        self.fetch=fetch;self.providers={};self.runtime={};self.lock=threading.Lock();self.stop=threading.Event();self.thread=None
    def attach(self,**providers): self.providers=providers
    def local(self,name,*args):
        try:return self.providers[name](*args) if name in self.providers else {}
        except Exception as e:return {'status':'PENDING','error':type(e).__name__}
    def source(self,market):
        errors=[]
        for path in ('/api/ledger/live/rows','/api/ledger/latest/rows'):
            try:
                obj=self.fetch(self.kis+path+'?market='+market+'&limit=5000')
                if obj.get('market') not in (None,market):raise ValueError('MARKET_MISMATCH')
                rows=obj.get('rows'); manifest=obj.get('manifest') or {}
                if not isinstance(rows,list):raise ValueError('NO_ROWS')
                valid=[r for r in rows if isinstance(r,dict) and venue(r)==market and len(code(r))==6 and code(r).isdigit()]
                total=number(obj.get('total'))
                expected=number(manifest.get('expected_symbols'))
                complete=len(valid)==len(rows) and total is not None and len(valid)>=total and total>0 and (expected is None or len(valid)>=expected)
                if not complete:errors.append('INCOMPLETE_OR_MARKET_UNLABELED')
                return valid,manifest,complete,path,errors
            except Exception as e:errors.append(path+':'+type(e).__name__)
        return [],{},False,None,errors
    def base(self,r,market,signal,signal_at,alpha,velocity,bars,current):
        c=code(r);ls=alpha.get(c) or {};p=number(r.get('price'));q_at=r.get('source_timestamp')
        # Generated_at/row's calendar date are not exchange signal timestamps.
        # Prefer an explicit signal event timestamp only; never synthesize 15:30.
        signal_at=signal.get('signal_asof') if dt(signal.get('signal_asof')) else None
        # LS numbers remain supplemental and never overwrite KIS current/flow values.
        ls_at=pick(ls,'source_timestamp','timestamp_kst','quote_at')
        ls_price=number(pick(ls,'price','current_price'))
        cross=(abs(ls_price/p-1)<=.005) if p and ls_price and fresh(ls_at,current) and fresh(q_at,current) else None
        typ=pick(r,'security_type','instrument_type'); common=True if typ in ('COMMON','COMMON_STOCK','보통주') else (False if typ in ('ETF','ETN','PREFERRED','SPAC','REIT') else None)
        hist=history_metrics(bars,current.date().isoformat())
        out=dict(code=c,name=r.get('name') if r.get('name') and r.get('name')!=c else 'UNAVAILABLE',market=market,
            price=p,change_pct=number(r.get('change_rate')),turnover=number(r.get('turnover')),volume=number(r.get('volume')),
            price_asof=q_at,timestamp_quality=r.get('source_timestamp_quality'),quote_quality=r.get('quality'),signal_asof=signal_at,signal_reference_date=trade_date(pick(signal,'signal_reference_date','signal_history_date','date')),
            market_cap_100m=number(r.get('market_cap_100m')),common_stock_confirmed=common,
            source='KIS_AUTHORITY',price_source=r.get('source'),ls_source=ls.get('source','LS_READONLY_CACHE'),ls_asof=ls_at,ls_price=ls_price,ls_crosscheck=cross,
            foreign_net=number(r.get('foreign_net')),institution_net=number(r.get('institution_net')),program_net=number(r.get('program_net')),
            ls_foreign_reference=number(pick(ls,'foreign_net','foreign_level')),ls_institution_reference=number(pick(ls,'institution_net','institution_level')),ls_program_reference=number(pick(ls,'program_net','program_level')),
            execution_strength=number(r.get('execution_strength')),rvol=number(r.get('rvol')),vwap=number(r.get('vwap')),
            vwap_hold=tri(signal.get('vwap_hold')),price_hold=tri(signal.get('price_hold')),high_hold60=number(signal.get('nxt_high_hold60')),
            rs5=number(pick(signal,'rs5','RS5')),rs20=number(pick(signal,'rs20','RS20')),
            volume_accel15=number(signal.get('nxt_volume_accel15')),turnover_accel15=number(signal.get('nxt_turnover_accel15')),compression=tri(signal.get('compression_confirmed')),sector=pick(signal,'sector') or pick(ls,'ENERGY_CLUSTER','sector'),sector_breadth=number(signal.get('sector_breadth')),
            market_state=pick(r,'market_state') or pick(signal,'market_state'),news_gate=signal.get('news_gate'),news_published_at=signal.get('news_published_at'),news_source=signal.get('news_source'),
            no_chase=signal.get('no_chase'),entry_price=number(pick(signal,'entry_price','rr_entry_price')),stop_price=number(pick(signal,'stop_price','rr_stop_price')),target_price=number(pick(signal,'target_price','rr_target_price')),
            stop_source=pick(signal,'stop_source','rr_stop_source'),target_source=pick(signal,'target_source','rr_target_source'),
            session_open=tri(r.get('session_open')),box_reclaim_hold=tri(signal.get('box_reclaim_hold')),flow_reversal=tri(signal.get('flow_reversal')),sector_expansion=tri(signal.get('sector_expansion')),
            stop_rule='지정 손절선 이탈 시 무효. 값이 없으면 PENDING',**velocity,**hist)
        for n in (5,20,60,120,200):
            v=number(r.get('ma'+str(n)))
            if v is not None:out['ma'+str(n)]=v
        if out['vwap'] is not None and p:out['vwap_hold']=p>=out['vwap']
        out['provenance']={k: {'source':'KIS','asof':q_at} for k in ('price','change_pct','turnover','volume','foreign_net','institution_net','program_net','execution_strength','rvol')}
        for n in (5,20,60,120,200):out['provenance']['ma'+str(n)]={'source':'KIS' if number(r.get('ma'+str(n))) is not None else 'LS_KRX_DAILY_REFERENCE','asof':q_at if number(r.get('ma'+str(n))) is not None else hist['history_asof']}
        # NXT moving averages are explicitly KRX daily references, never NXT session averages.
        out['ma_basis']='KIS_SOURCE_OR_LS_KRX_DAILY_REFERENCE'
        return out
    def scan(self,current=None):
        if not self.lock.acquire(False):return {'status':'BUSY'}
        try:
            current=current or now();hist,dates,hstate=self.history.read_all();recent=dates[-5:]
            global_history_latest=dates[-1] if dates else None
            upstream=self.local('v17');v17={code(x):x for x in upstream.get('candidates',[])+upstream.get('watch',[]) if isinstance(x,dict) and venue(x)=='KRX'}
            v17_at=upstream.get('generated_at')
            # Existing fullmarket output is a traceable discovery source, never a BUY authority.
            fm=self.local('fullmarket');fmrows={code(x):x for x in fm.get('candidates',[]) if isinstance(x,dict)}
            market_meta={}
            for market in ('KRX','NXT'):
                rows,manifest,complete,path,errors=self.source(market);qmap={code(r):r for r in rows}
                manifest_trade_date=trade_date(manifest.get('trade_date'))
                expected_daily=expected_closed_daily_date(manifest,current)
                # Never claim the last 5 trading days are current without a verified KIS trade date.
                global_daily_current=(global_history_latest==expected_daily) if expected_daily else None
                history_stale_symbols=history_ready_symbols=0
                alpha_obj=self.local('alpha',market)
                # A stock-market name such as KOSPI does not constitute a venue tag.
                alpha={code(r):dict(r,timestamp_kst=pick(r,'timestamp_kst','source_timestamp') or alpha_obj.get('timestamp_kst')) for key in ('sector_live_pool','live_pool','top_watch','top_final') for r in alpha_obj.get(key,[]) if isinstance(r,dict) and (venue(r) or venue(alpha_obj))==market}
                velocities=self.store.observe(market,rows,current);lanes={k:[] for k in LANES}
                if market=='KRX':
                    uncapped=self.local('v17_all',rows,hist,current)
                    if uncapped.get('status')=='READY':
                        upstream=uncapped
                        evaluated=set(uncapped.get('evaluated_codes',[]));v17={c:z for c,z in v17.items() if c not in evaluated}
                        v17.update({code(x):x for x in uncapped.get('candidates',[]) if isinstance(x,dict)});v17_at=uncapped.get('generated_at')
                nxt=self.local('nxt',rows,current) if market=='NXT' else {}
                nxtrows={code(r):r for r in nxt.get('rows',[]) if isinstance(r,dict) and venue(r)=='NXT'}
                news=self.local('news',market,current)
                re25=self.local('re25',market);rerows={code(r):r for r in re25.get('rows',[]) if isinstance(r,dict) and venue(r)==market}
                pending=Counter();full_recent=bool(recent) and len(recent)==5 and global_daily_current is True
                for r in rows:
                    c=code(r);b=hist.get(c,[]);candidates=[]
                    last_symbol_date=trade_date(b[-1]['date']) if b else None
                    if expected_daily and last_symbol_date==expected_daily:history_ready_symbols+=1
                    else:history_stale_symbols+=1
                    if market=='NXT':
                        s=nxtrows.get(c,{})
                        if s:
                            candidates.append(('NXT_POST_ACCEL_LANE',s,nxt.get('source_asof'),s.get('gate_status'),s.get('gate_status')=='PASS'))
                        else:pending['NXT_SERIES_OR_SETUP']+=1
                    else:
                        s=v17.get(c)
                        if s:candidates.append(('V17_ACTION_PRE',s,v17_at,pick(s,'status','pre_status'),s.get('chart_actionable') is True or s.get('pre_status')=='PRE_EXPLOSION_ARMED'))
                        else:pending['V17_HISTORY_OR_SETUP']+=1
                    # Traverse EVERY authority row on every scan; no saved watchlist or rank cap is an input.
                    aux=alpha.get(c,{})
                    rs5,rs20=number(aux.get('rs5')),number(aux.get('rs20'))
                    if aux.get('asym_rs_class') in ('SUPER','STRONG') or (rs5 is not None and rs20 is not None and rs5>0 and rs20>0):
                        candidates.append(('ASYMMETRIC_RS',aux,pick(aux,'source_timestamp','timestamp_kst'),aux.get('asym_rs_class'),tri(aux.get('asym_setup_confirmed'))))
                    else:pending['RS5_RS20_OR_SETUP']+=1
                    f=fmrows.get(c,{}) if market=='KRX' else aux
                    rotation=any(z.get('lane')=='A3_ROTATION' for z in f.get('lane_details',[]))
                    if rotation or f.get('capital_handoff_confirmed') is True:
                        candidates.append(('CAPITAL_HANDOFF',f,fm.get('generated_at') if market=='KRX' else f.get('timestamp_kst'),f.get('selection_state'),tri(f.get('capital_handoff_confirmed'))))
                    else:pending['HANDOFF_CONFIRM']+=1
                    ev=spike_events(b,recent) if b else []
                    if ev:
                        s=dict(rerows.get(c,{ }));s.update(ev[-1]);s['events']=ev;s['recent5_complete']=full_recent and all(d in {z['date'] for z in b} for d in recent)
                        candidates.append(('RE_EXPLOSION_25',s,re25.get('source_asof'),s.get('setup_state','FAST_NEW_SPIKE' if ev[-1]['fast_new_spike'] else 'RE25_OBSERVED'),tri(s.get('lane_confirmed'))))
                    else:pending['RE25_HISTORY_OR_EVENT']+=1
                    for lane,s,signal_at,upstate,confirmed in candidates:
                        s=dict(s)
                        if c in news:s.update(news[c])
                        out=self.base(r,market,s,signal_at,alpha,velocities.get(c,{}),b,current)
                        out.update(lane=lane,selection_path=[lane,str(s.get('signal_track') or upstate or 'PENDING')],upstream_state=upstate,lane_confirmed=confirmed)
                        out['history_expected_asof']=expected_daily
                        out['history_current']=last_symbol_date==expected_daily if expected_daily else None
                        out['history_quality']='CURRENT' if out['history_current'] is True else ('STALE' if expected_daily and last_symbol_date else 'PENDING')
                        # If a source cannot prove the current KRX daily series, no lane may promote it.
                        # This is a view-only check; underlying LS collectors are unchanged.
                        # Engine decisions are evidence, not a substitute for the shared hard gates.
                        for k in ('impulse_seen','recompression_seen','reaccel_seen','high_hold60_confirmed','spike_date','spike_high_pct','spike_age','fast_new_spike','price_preservation_pct','pullback_volume_ratio','post_range_pct','support_price','price_preserved','volume_contracting','compression_confirmed','support_near','recent5_complete','events'):
                            if k in s:out[k]=s[k]
                        if lane=='NXT_POST_ACCEL_LANE':
                            for k,src in [('impulse_seen','nxt_impulse_seen'),('recompression_seen','nxt_recompression_seen')]:
                                if src in s:out[k]=s[src]
                        # Session calendar must come from an observed source; weekdays alone are insufficient.
                        out['session_open']=tri(r.get('session_open'))
                        if current.weekday()>=5:out['session_open']=False
                        elif out['session_open'] is True:
                            hm=current.hour*60+current.minute;out['session_open']=(540<=hm<930) if market=='KRX' else (480<=hm<1200)
                        out=core_gates(out,current)
                        out['unavailable_fields']=[k for k in ('name','vwap','price_hold','high_hold60','rvol','execution_strength','ma5','ma20','ma60','ma120','ma200','rs5','rs20','compression','sector_breadth','foreign_net','institution_net','program_net','news_gate','entry_price','stop_price','target_price') if out.get(k) is None]
                        lanes[lane].append(out)
                for lane,data in lanes.items():
                    applicable=lane!='NXT_POST_ACCEL_LANE' or market=='NXT'
                    # Never infer disappearance from a capped/failed upstream lane response.
                    lane_complete=complete and applicable and (set(upstream.get('evaluated_codes',[])) or bool(upstream.get('universe_complete')) if lane=='V17_ACTION_PRE' else bool(fm.get('universe_complete')) if lane=='CAPITAL_HANDOFF' else bool(nxt.get('universe_complete')) if lane=='NXT_POST_ACCEL_LANE' else full_recent if lane=='RE_EXPLOSION_25' else len(alpha)>=len(rows))
                    self.store.reconcile(market,lane,data,lane_complete,current)
                self.store.track(market,qmap,current)
                meta=dict(status='READY' if complete else 'PENDING',source_path=path,scan_scope='ZERO_BASE_AUTHORITY_ALL_ROWS',rows_scanned=len(rows),complete=complete,source_errors=errors,manifest=manifest,history_state=hstate,history_symbols=len(hist),history_latest_date=global_history_latest,history_expected_date=expected_daily,history_manifest_trade_date=manifest_trade_date,history_latest_verified=global_daily_current,history_ready_symbols=history_ready_symbols,history_stale_symbols=history_stale_symbols,recent5_dates=recent,re25_universe_status='COMPLETE' if full_recent and all(all(d in {z['date'] for z in hist.get(code(r),[])} for d in recent) for r in rows) else 'PENDING_PARTIAL_HISTORY',
                          pending_axes=dict(pending),lanes={k:{'observed':len(v),'status':'READY' if v else 'PENDING','prime':sum(z['classification']=='PRIME' for z in v)} for k,v in lanes.items()},scan_at=stamp(current),nxt_independent=True)
                market_meta[market]=meta
                with self.store.connect() as con:con.execute('INSERT INTO cycles(at,market,status,payload) VALUES(?,?,?,?)',(stamp(current),market,meta['status'],json.dumps(clean(meta),ensure_ascii=False)))
            self.checkpoint.save()
            self.runtime={'version':VERSION,'scan_at':stamp(current),'markets':clean(market_meta),'storage':self.checkpoint.info(),'status':'READY'}
            return self.runtime
        finally:
            if 'hist' in locals() and hasattr(hist,'close'):hist.close()
            self.lock.release()
    def start(self):
        if self.thread and self.thread.is_alive():return
        def loop():
            self.stop.wait(15)
            while not self.stop.is_set():
                try:self.scan()
                except Exception as e:self.runtime={'status':'PENDING','error':type(e).__name__,'at':stamp()}
                self.stop.wait(max(60,int(os.getenv('OPERATIONS_INTERVAL_SECONDS','120'))))
        self.thread=threading.Thread(target=loop,daemon=True,name='operations-readonly');self.thread.start()
    def api(self,raw_path):
        u=urlparse(raw_path);q=parse_qs(u.query);m=(q.get('market') or ['KRX'])[0]
        if m not in ('KRX','NXT'):return 400,{'error':'market must be KRX or NXT'}
        lane=(q.get('lane') or [None])[0]
        if lane and lane not in LANES:return 400,{'error':'invalid lane'}
        try:limit=max(20,min(10000,int((q.get('limit') or ['500'])[0])));offset=max(0,int((q.get('offset') or ['0'])[0]))
        except ValueError:return 400,{'error':'invalid pagination'}
        kind=u.path.rsplit('/',1)[-1]
        if kind=='status':return 200,clean(self.runtime or {'status':'PENDING','storage':self.checkpoint.info(),'version':VERSION})
        if kind=='board':
            data=self.store.records('candidates',m,1000,0,lane)
            rows=[r['payload'] for r in data['rows']];rows.sort(key=lambda r:(RANK.get(r['classification'],9),-(dt(r.get('occurred_at')).timestamp() if dt(r.get('occurred_at')) else 0)),reverse=False)
            return 200,clean({'market':m,'rows':rows[offset:offset+limit],'total':data['total'],'offset':offset,'status':(self.runtime.get('markets',{}).get(m) or {}).get('status','PENDING'),'scan':self.runtime.get('markets',{}).get(m,{}),'storage':self.checkpoint.info(),'version':VERSION})
        if kind in ('events','outcomes'):return 200,clean(self.store.records(kind,m,limit,offset,lane))
        return 404,{'error':'not found'}
