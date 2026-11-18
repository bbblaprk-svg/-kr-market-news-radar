"""Read-only operational decision projection. Never imports broker clients or writes source caches."""
from __future__ import annotations
import json, math, os, re, sqlite3, threading, time, urllib.request
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .trading_calendar import recent_sessions, last_closed_session, is_snapshot_session
from .universe_audit import classify_window, universe_membership_audit
from .candidate_integrity import verified_scope, data_hold, native_nxt_minute_proven

VERSION = 'KRX_NXT_OPERATIONS_V7_CLEAN_BOOTSAFE'
KST = timezone(timedelta(hours=9))
LANES = ('NXT_POST_ACCEL_LANE', 'V17_ACTION_PRE', 'ASYMMETRIC_RS', 'CAPITAL_HANDOFF', 'RE_EXPLOSION_25')
VIEW_LANES = LANES + ('RS_HANDOFF',)
VETO = {'RISK_OFF_ACCEL', 'PROGRAM_SELL_ACCEL', 'BREADTH_COLLAPSE'}
NEWS_BLOCK = {'PRICED_IN', 'SELL_THE_NEWS', 'NEGATIVE_CONFIRMED', 'OVERHANG', 'POSITIVE_NEWS_FAILED'}
NEWS_PASS = {'PASS', 'CLEAR', 'POSITIVE_CONFIRMED', 'NEGATIVE_ABSORBED'}
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

def code(r):
    raw = str(pick(r, 'code', 'symbol') or '').strip().upper()
    return raw.zfill(6) if raw.isdigit() and 0<len(raw)<=6 else raw

def valid_code(value):
    # KRX identifiers may contain letters, e.g. 0120G0; never silently drop them.
    return bool(re.fullmatch(r'[A-Z0-9]{6}', str(value or '')))
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

def closed_trade_date_from_manifest(manifest, market=None):
    """Only a completed, explicitly venue-bound KIS scan gives a scan date.

    This date is NOT a KRX daily close price or a complete OHLC candle; those
    must be read independently from the LS daily cache and validated below.
    """
    m=manifest or {}
    if not isinstance(m,dict) or m.get('status') != 'READY':return None
    if market and m.get('market') not in (None,market):return None
    rid=str(m.get('round_id') or '')
    r=re.fullmatch(r'(\d{8})_(\d{6})_(KRX|NXT)',rid)
    if not r or (market and r.group(3)!=market):return None
    round_day=trade_date(r.group(1))
    explicit=trade_date(m.get('trade_date')) if m.get('trade_date') is not None else round_day
    if not round_day or not explicit or explicit!=round_day:return None
    began=dt(m.get('started_at'))
    ended=dt(m.get('completed_at'))
    if not began or not ended or ended<began:return None
    if began.date().isoformat()!=round_day or ended.date().isoformat()!=round_day:return None
    # `round_id` time is a KST scan start marker, not the exchange closing bar.
    hhmmss=began.strftime('%H%M%S')
    if hhmmss != r.group(2):return None
    return round_day

def expected_closed_daily_date(manifest,current,market=None):
    """Only accept the most recently *closed* XKRX trading session.

    A KIS scan completed at e.g. 14:43 cannot establish a 15:30 closing bar.
    After close the KIS session date is checked against an independent XKRX
    session calendar. LS OHLCV is separately validated for exactly this date.
    """
    d=closed_trade_date_from_manifest(manifest,market)
    if not d:return None
    if market=='KRX' or market is None:
        last=last_closed_session(current)
        return d if last is not None and d==last else None
    return d if d<=current.date().isoformat() else None

def fresh(at, current, seconds=180):
    d = dt(at)
    return d is not None and -5 <= (current-d).total_seconds() <= seconds

def runtime_quote_grade(row,current,seconds=180):
    at=row.get('source_timestamp')
    if not fresh(at,current,seconds):
        return 'STALE'
    tq=str(row.get('source_timestamp_quality') or '')
    qq=str(row.get('quality') or '')
    if tq=='EXCHANGE_TIMESTAMP' and qq not in ('EXPIRED','REST_STALE','STALE'):
        return 'EXCHANGE'
    if qq=='REST_CURRENT' and tq in ('REQUEST_TIMESTAMP','REQUEST_TIME_ONLY','REQUEST_TIME'):
        return 'REST_CURRENT'
    return 'REFERENCE'

def apply_runtime_quote(row,quote,current):
    out=dict(row or {})
    grade=runtime_quote_grade(quote,current)
    out['runtime_quote_grade']=grade
    out['runtime_quote_checked_at']=stamp(current)
    if grade=='STALE':
        out['runtime_quote_status']='PENDING_STALE'
        return out, False
    p=number(quote.get('price'))
    if p is None:
        out['runtime_quote_status']='PENDING_PRICE'
        return out, False
    old_at=dt(out.get('price_asof'));new_at=dt(quote.get('source_timestamp'))
    if old_at and new_at and new_at<old_at:
        out['runtime_quote_status']='IGNORED_OLDER'
        return out, False
    before=(out.get('price'),out.get('change_pct'),out.get('volume'),out.get('turnover'),out.get('price_asof'),out.get('timestamp_quality'),out.get('quote_quality'),out.get('runtime_quote_status'))
    out.update(price=p,change_pct=number(quote.get('change_rate')),
               volume=number(quote.get('volume')),turnover=number(quote.get('turnover')),
               price_asof=quote.get('source_timestamp'),
               timestamp_quality=quote.get('source_timestamp_quality'),
               quote_quality=quote.get('quality'),
               runtime_quote_status='READY' if grade=='EXCHANGE' else 'REFERENCE_CURRENT',
               runtime_no_chase=(number(quote.get('change_rate')) is not None and number(quote.get('change_rate'))>5))
    after=(out.get('price'),out.get('change_pct'),out.get('volume'),out.get('turnover'),out.get('price_asof'),out.get('timestamp_quality'),out.get('quote_quality'),out.get('runtime_quote_status'))
    return out, before!=after

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
        verified=[]
        for t,o,h,l,p,v,tv in reversed(rows):
            d=trade_date(t)
            if not d or d!=str(t) or is_snapshot_session(d) is not True:
                continue  # never promote synthetic weekend/invalid trade dates
            vals=(number(o),number(h),number(l),number(p))
            vol=number(v)
            if any(x is None or x<=0 for x in vals) or vol is None or vol<0:
                continue
            op,hi,lo,close=vals
            if lo>min(op,close) or hi<max(op,close) or lo>hi:
                continue
            verified.append(dict(date=d,open=op,high=hi,low=lo,close=close,volume=vol,turnover=number(tv)))
        return verified
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
            dates=[str(r[0]) for r in con.execute("""
                SELECT DISTINCT timestamp FROM bars
                WHERE provider='LS' AND venue='KRX'
                  AND open>0 AND high>0 AND low>0 AND close>0
                  AND volume>=0 AND low<=open AND low<=close
                  AND high>=open AND high>=close
                  AND strftime('%w',timestamp) NOT IN ('0','6')
                ORDER BY timestamp DESC LIMIT 260
            """).fetchall() if trade_date(str(r[0]))==str(r[0]) and is_snapshot_session(str(r[0])) is True]
            count=con.execute("SELECT COUNT(DISTINCT symbol) FROM bars WHERE provider='LS' AND venue='KRX'").fetchone()[0]
            return HistorySnapshot(con,count),sorted(dates),'READY' if dates else 'PENDING_EMPTY_CACHE'
        except (sqlite3.Error,OSError) as e:
            if con:con.close()
            return {}, [], 'PENDING_'+type(e).__name__

def history_metrics(bars, asof):
    """MA and RVOL require exact preceding XKRX sessions, not just N rows.

    Missing past sessions must not shift the meaning of MA20/60/200. Date-only
    candle rows are never reinterpreted as intraday price or signal timestamps.
    """
    x=[r for r in bars if asof and r['date']<=asof]
    ix={r['date']:r for r in x}
    out={'history_asof':x[-1]['date'] if x else None,'history_bars':len(x)}
    for n in (5,20,60,120,200):
        sessions=recent_sessions(asof,n)
        values=[number(ix[d]['close']) if d in ix else None for d in sessions] if sessions else []
        out['ma'+str(n)]=(sum(values)/n if len(values)==n and all(z is not None and z>0 for z in values) else None)
    sessions=recent_sessions(asof,6)
    if sessions and all(d in ix for d in sessions):
        prev=[number(ix[d]['volume']) for d in sessions[:-1]]
        last=number(ix[sessions[-1]]['volume'])
        out['rvol_eod5']=last/(sum(prev)/5) if all(z is not None and z>=0 for z in prev) and sum(prev)>0 and last is not None else None
    return out

def spike_events(bars, recent_dates):
    """25% event ledger. This detects the event only; it does not make it a buy candidate."""
    events=[]
    for i in range(1,len(bars)):
        r, prev = bars[i], bars[i-1]
        p,h=number(prev['close']),number(r['high'])
        if r['date'] not in recent_dates or not p or h is None or h/p < 1.25: continue
        later=bars[i+1:]
        age=sum(d>r['date'] for d in recent_dates)
        after=[z for z in later if z['date'] in recent_dates]
        spike_close=number(r['close']); spike_vol=number(r['volume'])
        last=after[-1] if after else r
        last_close=number(last['close'])
        postv=[number(z['volume']) for z in after]
        post_ranges=[(number(z['high'])-number(z['low']))/number(z['close'])
                     for z in after if all(number(z[k]) is not None and number(z[k])>0 for k in ('high','low','close'))]
        spike_range=((number(r['high'])-number(r['low']))/spike_close
                     if spike_close and number(r['high']) and number(r['low']) else None)
        support=min(number(z['low']) for z in after) if after and all(number(z['low']) is not None for z in after) else None
        post_max_high=max(number(z['high']) for z in after) if after and all(number(z['high']) is not None for z in after) else None
        historical_extension=((post_max_high/h)-1)*100 if post_max_high and h and h>0 else None
        volume_ratio=(sum(postv)/len(postv)/spike_vol
                      if postv and spike_vol and spike_vol>0 and all(z is not None and z>=0 for z in postv) else None)
        post_range=(sum(post_ranges)/len(post_ranges) if post_ranges else None)
        support_distance=((last_close/support)-1)*100 if last_close and support and support>0 else None
        retest_to_close=((last_close/spike_close)-1)*100 if last_close and spike_close and spike_close>0 else None
        price_preserved=(last_close>=spike_close*.90) if last_close and spike_close else None
        volume_contracting=(volume_ratio<=.75) if volume_ratio is not None else None
        compression_confirmed=(post_range<=spike_range*.65) if post_range is not None and spike_range is not None and spike_range>0 else None
        support_near=(support_distance<=6 and -7<=retest_to_close<=3) if support_distance is not None and retest_to_close is not None else None
        events.append({
            'spike_date':r['date'],'spike_high_pct':round((h/p-1)*100,4),
            'spike_close':spike_close,'spike_high':h,'spike_age':age,'fast_new_spike':age==0,
            'price_preservation_pct':round(last_close/h*100,4) if last_close else None,
            'pullback_from_high_pct':round((last_close/h-1)*100,4) if last_close else None,
            'close_retention_pct':round(last_close/spike_close*100,4) if last_close and spike_close else None,
            'pullback_volume_ratio':volume_ratio,
            'post_range_pct':post_range*100 if post_range is not None else None,
            'spike_range_pct':spike_range*100 if spike_range is not None else None,
            'support_price':support,'support_distance_pct':support_distance,
            'retest_to_spike_close_pct':retest_to_close,
            'post_spike_max_high':post_max_high,
            'post_spike_max_extension_pct':historical_extension,
            'historical_reexpansion':(historical_extension>=5) if historical_extension is not None else False,
            'price_preserved':price_preserved,
            'volume_contracting':volume_contracting,
            'compression_confirmed':compression_confirmed,
            'support_near':support_near
        })
    return events

def qualify_re25_event(event, live_row):
    """Separate raw 25% events from second-wave entry candidates.

    EVENT_ONLY rows stay in the event ledger, but are never returned on the RE25
    candidate board. A candidate requires age 1-5 plus all four daily
    re-compression gates, and must not already be re-expanded intraday.
    """
    s=dict(event or {})
    age=s.get('spike_age')
    live_change=number(live_row.get('change_rate'))
    live_price=number(live_row.get('price'))
    high=number(s.get('spike_high'))
    extension=((live_price/high)-1)*100 if live_price and high and high>0 else None
    live_no_chase=live_change is not None and live_change>5
    source_no_chase=(s.get('no_chase') is True)
    historical_reexpansion=(s.get('historical_reexpansion') is True)
    already_expanded=bool(
        s.get('fast_new_spike') or
        live_no_chase or
        source_no_chase or
        historical_reexpansion or
        (extension is not None and extension>=8)
    )
    daily_setup=all(s.get(k) is True for k in (
        'price_preserved','volume_contracting','compression_confirmed','support_near'
    ))
    in_window=isinstance(age,int) and 1<=age<=5

    if s.get('fast_new_spike') or age==0:
        state='EVENT_ONLY_FAST_NEW_SPIKE'
    elif not in_window:
        state='EVENT_ONLY_OUT_OF_WINDOW'
    elif already_expanded:
        state='RED_NO_CHASE'
    elif not daily_setup:
        state='EVENT_ONLY_NOT_RECOMPRESSED'
    else:
        state='COMPRESSION_ARMED'

    s.update(
        re25_setup_state=state,
        re25_candidate_visible=(state in ('COMPRESSION_ARMED','RED_NO_CHASE')),
        re25_event_only=(state not in ('COMPRESSION_ARMED','RED_NO_CHASE')),
        re25_runtime_state=('RED_NO_CHASE' if state=='RED_NO_CHASE' else 'WATCH' if state=='COMPRESSION_ARMED' else state),
        re25_daily_setup_confirmed=daily_setup,
        re25_already_expanded=already_expanded,
        re25_live_change_pct=live_change,
        re25_extension_above_spike_high_pct=extension,
        re25_historical_reexpansion=historical_reexpansion,
        no_chase=True if already_expanded else s.get('no_chase'),
        lane_confirmed=(state=='COMPRESSION_ARMED')
    )
    return s


def re25_runtime_transition(row, quote, current):
    """Fast state transition for a fixed RE25 candidate pool.

    Candidate membership is NOT changed here. Only the row's runtime state is
    updated. Promotion needs two consecutive confirmations; degradation needs
    three consecutive failures unless hard support is broken.
    """
    out=dict(row or {})
    price=number(quote.get('price'))
    volume=number(quote.get('volume'))
    q_at=quote.get('source_timestamp')
    q_quality=quote.get('source_timestamp_quality')
    change=number(quote.get('change_rate'))
    quote_grade=runtime_quote_grade(quote,current)
    if price is None or quote_grade=='STALE':
        out['re25_state_refresh_status']='PENDING_QUOTE'
        out['runtime_quote_grade']=quote_grade
        return out, False

    state=str(out.get('re25_runtime_state') or (
        'RED_NO_CHASE' if out.get('re25_setup_state')=='RED_NO_CHASE'
        else 'WATCH' if out.get('re25_setup_state')=='COMPRESSION_ARMED'
        else out.get('re25_setup_state') or 'WATCH'
    ))
    previous_state=state

    # Update only current quote fields; do not rerun universe selection.
    out.update(
        price=price,
        change_pct=change,
        volume=volume,
        price_asof=q_at,
        timestamp_quality=q_quality,
        quote_quality=quote.get('quality'),
        re25_state_refresh_at=stamp(current),
        re25_state_refresh_status='READY' if quote_grade=='EXCHANGE' else 'REFERENCE_CURRENT',
        runtime_quote_grade=quote_grade
    )

    spike_high=number(out.get('spike_high'))
    second_high=number(out.get('post_spike_max_high'))
    existing_peak=number(out.get('rearm_peak_price'))
    peak=max([x for x in (existing_peak,second_high,spike_high,price) if x is not None], default=price)
    # Once a rearm watch begins, freeze the peak reference so the pullback band
    # does not chase price upward.
    if state in ('REARM_WATCH','REARM_TRIGGER_PENDING_AUTH','REARMED') and existing_peak is not None:
        peak=existing_peak
    out['rearm_peak_price']=peak

    spike_close=number(out.get('spike_close'))
    support=number(out.get('support_price'))
    if spike_close:
        conservative=spike_close*.93
        support=max([x for x in (support,conservative) if x is not None], default=conservative)
    out['rearm_support_reference']=support

    pullback=((price/peak)-1)*100 if peak else None
    out['rearm_pullback_from_peak_pct']=pullback

    prev_vol=number(out.get('rearm_prev_volume'))
    prev_at=dt(out.get('rearm_prev_at'))
    velocity=None
    if volume is not None and prev_vol is not None and prev_at and current>prev_at:
        sec=(current-prev_at).total_seconds()
        if 5<=sec<=180 and volume>=prev_vol:
            velocity=(volume-prev_vol)/sec*60
    prev_velocity=number(out.get('rearm_prev_volume_velocity'))
    out['rearm_volume_velocity']=velocity

    hard_break=bool(
        support is not None and price<support
    ) or bool(pullback is not None and pullback < -12)

    pullback_band=bool(
        pullback is not None and -12 <= pullback <= -5
    )
    not_chasing=(change is None or change<=5)
    volume_quiet=bool(
        velocity is not None and prev_velocity is not None and velocity <= prev_velocity*.85
    )
    quiet_ok=pullback_band and not_chasing and not hard_break and volume_quiet

    quiet_count=int(out.get('rearm_quiet_confirm_count') or 0)
    trigger_count=int(out.get('rearm_trigger_confirm_count') or 0)
    fail_count=int(out.get('rearm_fail_count') or 0)

    if hard_break:
        state='INVALIDATED'
        quiet_count=trigger_count=0
        fail_count=3
    elif state=='RED_NO_CHASE':
        quiet_count=quiet_count+1 if quiet_ok else 0
        if quiet_count>=2:
            state='REARM_WATCH'
            out['rearm_watch_started_at']=stamp(current)
            out['rearm_watch_price']=price
            out['rearm_breakout_level']=price*1.02
            out['rearm_watch_volume_velocity']=velocity
            trigger_count=0
            fail_count=0
    elif state in ('REARM_WATCH','REARM_TRIGGER_PENDING_AUTH'):
        breakout=number(out.get('rearm_breakout_level'))
        baseline=number(out.get('rearm_watch_volume_velocity'))
        price_ok=bool(breakout is not None and price>=breakout)
        vol_ok=bool(velocity is not None and baseline is not None and velocity>=max(baseline*1.30, baseline+1))
        trigger_ok=price_ok and vol_ok and not_chasing
        trigger_count=trigger_count+1 if trigger_ok else 0
        watch_price=number(out.get('rearm_watch_price'))
        soft_fail=bool(watch_price is not None and price<watch_price*.98) or bool(change is not None and change>5)
        fail_count=fail_count+1 if soft_fail else 0
        if trigger_count>=2:
            if quote_grade=='EXCHANGE':
                state='REARMED';fail_count=0;out['rearm_confirmed_at']=stamp(current)
            else:
                state='REARM_TRIGGER_PENDING_AUTH'
        elif fail_count>=3:
            state='RED_NO_CHASE';quiet_count=trigger_count=fail_count=0
    elif state=='REARMED':
        breakout=number(out.get('rearm_breakout_level'))
        hold_ok=bool(breakout is not None and price>=breakout*.99 and not_chasing)
        fail_count=0 if hold_ok else fail_count+1
        if fail_count>=3:
            state='REARM_WATCH'
            trigger_count=0
            fail_count=0

    out['rearm_quiet_confirm_count']=quiet_count
    out['rearm_trigger_confirm_count']=trigger_count
    out['rearm_fail_count']=fail_count
    out['re25_runtime_state']=state

    # Runtime state is the primary trading-status label. Core classification
    # remains untouched until the next scheduled full decision scan.
    if state=='RED_NO_CHASE':
        out['entry_phase']='재발산 · 추격금지 / 재응축 감시'
    elif state=='REARM_WATCH':
        out['entry_phase']='재응축 2회 확인 · 재돌파 대기'
    elif state=='REARM_TRIGGER_PENDING_AUTH':
        out['entry_phase']='재돌파 감지 · KIS 교환시각 확인 대기'
    elif state=='REARMED':
        out['entry_phase']='재무장 확인 · 진입 게이트 재검증'
    elif state=='INVALIDATED':
        out['entry_phase']='재무장 무효 · 지지 이탈'
    elif state=='WATCH':
        out['entry_phase']='재응축 후보 · 진입 조건 대기'

    out['rearm_prev_volume']=volume
    out['rearm_prev_at']=stamp(current)
    if velocity is not None:
        out['rearm_prev_volume_velocity']=velocity

    return out, state!=previous_state

def core_gates(row, current):
    """All required gates must pass. Unknown is never a successful observation."""
    gates={}
    gates['MARKET_STATE']=False if row.get('market_state') in VETO else (True if row.get('market_state') in ('NORMAL','RISK_ON','NEUTRAL') else None)
    gates['NEWS']=False if row.get('news_gate') in NEWS_BLOCK else (True if row.get('news_gate') in NEWS_PASS else None)
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
        # A verified per-symbol candle alone is not a full-universe authority.
        # Reject promotion until KIS live membership and the immutable completed
        # snapshot are reconciled. NXT post-acceleration stays independent.
        gates['KRX_UNIVERSE_MATCH']=tri(row.get('authority_universe_consistent'))
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
        gates['RE25_CANDIDATE_VISIBLE']=tri(row.get('re25_candidate_visible'))
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
    if classification=='PRIME': expansion_phase='ENTRY_CONFIRMED'
    elif row.get('re25_runtime_state')=='REARMED': expansion_phase='REARMED_ENTRY_WINDOW'
    elif gates.get('NO_CHASE') is False or row.get('fast_new_spike') is True or row.get('re25_already_expanded') is True: expansion_phase='ALREADY_EXPANDED_NO_CHASE'
    elif classification in ('ARMED','PRE_EXPLOSION_ARMED'): expansion_phase='PRE_ENTRY_SETUP'
    else: expansion_phase='PENDING'
    row.update(classification=classification,expansion_phase=expansion_phase,decision='PENDING' if pending or any(k in fail for k in ('DATA_FRESH','SIGNAL_FRESH','SESSION','NXT_INDEPENDENT_SESSION','KRX_DAILY_CURRENT','KRX_UNIVERSE_MATCH')) else classification,
               data_status='PENDING' if pending or not gates['DATA_FRESH'] or not gates['SIGNAL_FRESH'] or gates.get('KRX_DAILY_CURRENT') is False or ('KRX_UNIVERSE_MATCH' in gates and gates['KRX_UNIVERSE_MATCH'] is not True) else 'READY',
               gate_failures=fail, gate_pending=pending,
               promotion_conditions=[k+'=PASS' for k,v in row['gates'].items() if v!='PASS'],
               invalidation_conditions=['MARKET_STATE veto','NO_CHASE','뉴스 차단','손익비 2R 미달','명시된 지지/손절선 이탈'],
               entry_phase='진입 확인 신호' if classification=='PRIME' else ('이미 급등 · 추격금지' if gates['NO_CHASE'] is False or row.get('fast_new_spike') else '진입 전 · 조건 대기'))
    return row

class Store:
    """Append-only event ledger isolated from broker-owned source databases.

    V7 deliberately does not force WAL or touch any legacy operations DB.
    The caller selects a fresh data directory. SQLite stays on its default
    rollback journal so Render filesystem quirks cannot fail startup merely
    because a WAL/SHM sidecar cannot be created.
    """
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS candidates(key TEXT PRIMARY KEY,market TEXT,lane TEXT,code TEXT,active INTEGER,payload TEXT,first_at TEXT,last_at TEXT,occurrences INTEGER,signal_id TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,market TEXT,lane TEXT,code TEXT,name TEXT,event TEXT,reason TEXT,payload TEXT);
    CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY,quote_at TEXT,payload TEXT);
    CREATE TABLE IF NOT EXISTS outcomes(id TEXT PRIMARY KEY,market TEXT,lane TEXT,code TEXT,name TEXT,entry_at TEXT,entry_price REAL,stop REAL,target REAL,last_at TEXT,last_price REAL,mfe REAL,mae REAL,status TEXT,exit_at TEXT,exit_price REAL,exit_reason TEXT);
    CREATE INDEX IF NOT EXISTS events_market_id ON events(market,id);
    CREATE TABLE IF NOT EXISTS cycles(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT,market TEXT,status TEXT,payload TEXT);
    """
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.path,timeout=15)
        try:
            c.execute('PRAGMA busy_timeout=15000')
            c.executescript(self.SCHEMA)
            c.commit()
            check=c.execute('PRAGMA integrity_check').fetchone()
            if not check or check[0] != 'ok':
                raise sqlite3.DatabaseError('OPERATIONS_DB_INTEGRITY_CHECK_FAILED')
        finally:
            c.close()
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15)
        try:
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA busy_timeout=15000')
            with c:
                yield c
        finally:
            c.close()
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
        at=stamp(current);max_misses=max(1,int(os.getenv('OPERATIONS_CANDIDATE_MAX_MISSES','3')))
        with self.connect() as c:
            previous={r['key']:r for r in c.execute('SELECT * FROM candidates WHERE market=? AND lane=? AND active=1',(market,lane))}
            seen=set()
            for row in rows:
                key=market+':'+lane+':'+row['code'];seen.add(key)
                old=c.execute('SELECT * FROM candidates WHERE key=?',(key,)).fetchone()
                prev=json.loads(old['payload']) if old else {}
                if prev:
                    for rk in ('runtime_lane_state','runtime_upstream_state','runtime_capital_handoff_confirmed',
                               'runtime_quote_grade','runtime_quote_status','runtime_quote_checked_at','runtime_no_chase',
                               'state_refresh_at'):
                        if rk in prev:row[rk]=prev[rk]
                if lane=='RE_EXPLOSION_25' and prev:
                    for rk in ('re25_runtime_state','rearm_peak_price','rearm_support_reference',
                               'rearm_quiet_confirm_count','rearm_trigger_confirm_count','rearm_fail_count',
                               'rearm_watch_started_at','rearm_watch_price','rearm_breakout_level',
                               'rearm_watch_volume_velocity','rearm_confirmed_at',
                               'rearm_prev_volume','rearm_prev_at','rearm_prev_volume_velocity'):
                        if rk in prev:row[rk]=prev[rk]
                source_proven=(complete is True or
                               (isinstance(complete,set) and row['code'] in complete))
                if old and not old['active'] and not source_proven:
                    # A previously closed episode cannot be reopened by an
                    # unverified observation (even if an old upstream cache repeats it).
                    continue
                if not source_proven:
                    if row.get('classification')=='PRIME':row['classification']='WATCH'
                    row['decision']='PENDING'
                    row['data_status']='PENDING'
                    row['gate_pending']=sorted(set(list(row.get('gate_pending') or [])+['SOURCE_RECONFIRM_PENDING']))
                row.update(candidate_seen_at=at if source_proven else prev.get('candidate_seen_at'),
                           candidate_observed_at=at,
                           candidate_miss_count=0 if source_proven else int(prev.get('candidate_miss_count') or 0),
                           candidate_stale=False if source_proven else (prev.get('candidate_stale') is True),
                           candidate_last_miss_at=None if source_proven else prev.get('candidate_last_miss_at'),
                           candidate_lifecycle='CONFIRMED_THIS_CYCLE' if source_proven else 'OBSERVED_UNVERIFIED')
                state_keys=('classification','decision','upstream_state','gate_failures','gate_pending','spike_date',
                            're25_runtime_state','candidate_stale')
                changed=any(prev.get(k)!=row.get(k) for k in state_keys)
                event='NEW' if not old or not old['active'] else ('EXIT' if source_proven and row['classification']=='REJECT' and prev.get('classification')!='REJECT'
                       else 'REENTER' if source_proven and prev.get('candidate_stale') is True else 'CHANGE' if changed else 'HOLD')
                occ=(old['occurrences'] if old else 0)+(event in ('NEW','REENTER'))
                row.update(first_seen_at=old['first_at'] if old else at,
                           occurred_at=at if source_proven and (changed or event in ('NEW','REENTER')) else prev.get('occurred_at',at),
                           last_seen_at=at,occurrences=occ)
                sid=old['signal_id'] if old else None
                if row['classification']=='PRIME' and (not old or not old['active'] or prev.get('classification')!='PRIME'):
                    sid=key+':'+at
                    c.execute('INSERT OR IGNORE INTO outcomes(id,market,lane,code,name,entry_at,entry_price,stop,target,mfe,mae,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (sid,market,lane,row['code'],row['name'],at,row['entry_price'],row['stop_price'],row['target_price'],0,0,'TRACKING_SIGNAL'))
                row['signal_id']=sid
                if event!='HOLD' or (row.get('price_asof')!=prev.get('price_asof') and row.get('data_status')=='READY'):
                    reason='; '.join(row.get('gate_failures',[])+row.get('gate_pending',[])) or 'ALL_REQUIRED_GATES_PASS'
                    c.execute('INSERT INTO events(at,market,lane,code,name,event,reason,payload) VALUES(?,?,?,?,?,?,?,?)',
                              (at,market,lane,row['code'],row['name'],event,reason,json.dumps(clean(row),ensure_ascii=False)))
                c.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (key,market,lane,row['code'],1,json.dumps(clean(row),ensure_ascii=False),
                           row['first_seen_at'],at if source_proven or not old else old['last_at'],occ,sid))
            for key,old in previous.items():
                if key in seen:continue
                p=json.loads(old['payload'])
                fully_evaluated=(complete is True) or (isinstance(complete,set) and old['code'] in complete)
                if not fully_evaluated:
                    # Cannot equate missing provider/market evidence with a losing setup.
                    p=data_hold(p,at=at,reason='SOURCE_INCOMPLETE')
                    # Do not advance candidate last_at on a missing feed: old watch
                    # entries must not outrank genuinely refreshed stocks.
                    c.execute('UPDATE candidates SET payload=? WHERE key=?',
                              (json.dumps(clean(p),ensure_ascii=False),key))
                    continue
                # A true missing confirmation must be tied to a completed
                # per-stock evaluation proof. Three separate verified cycles
                # are required before a normal EXIT; source outages do not count.
                previous_miss_at=dt(p.get('candidate_last_miss_at'))
                scan_at=dt(at)
                distinct=not (previous_miss_at and scan_at and scan_at<=previous_miss_at)
                missed=min(max_misses,int(p.get('candidate_miss_count') or 0)+(1 if distinct else 0))
                if missed<max_misses:
                    p.update(classification='WATCH',decision='PENDING',data_status='PENDING',
                             candidate_miss_count=missed,candidate_stale=False,
                             candidate_last_miss_at=at if distinct else p.get('candidate_last_miss_at'),
                             candidate_lifecycle='VERIFIED_MISS_'+str(missed),
                             gate_pending=sorted(set(list(p.get('gate_pending') or [])+['CONFIRMED_RESCAN_NOT_SELECTED'])))
                    c.execute('UPDATE candidates SET payload=?,last_at=? WHERE key=?',
                              (json.dumps(clean(p),ensure_ascii=False),at,key))
                    continue
                c.execute('UPDATE candidates SET active=0,last_at=? WHERE key=?',(at,key))
                c.execute('INSERT INTO events(at,market,lane,code,name,event,reason,payload) VALUES(?,?,?,?,?,?,?,?)',
                          (at,market,lane,p['code'],p['name'],'EXIT',
                           'VERIFIED_CODE_NOT_SELECTED_3_CYCLES',old['payload']))
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
    def board(self,market,limit=80,offset=0,lane=None):
        """Bounded dashboard read. RE25 event-only rows stay in ledger, not buy board."""
        limit=max(20,min(300,int(limit)))
        where='market=? AND active=1';params=[market]
        if lane=='RS_HANDOFF':
            where+=' AND lane IN (?,?)';params.extend(['ASYMMETRIC_RS','CAPITAL_HANDOFF'])
        elif lane:
            where+=' AND lane=?';params.append(lane)
        re25_only=(lane=='RE_EXPLOSION_25')
        with self.connect() as c:
            try:
                scoped=where+" AND COALESCE(json_extract(payload,'$.candidate_stale'),0)=0"
                if re25_only:
                    scoped+=(" AND json_extract(payload,'$.re25_setup_state') IN ('COMPRESSION_ARMED','RED_NO_CHASE')"
                             " AND COALESCE(json_extract(payload,'$.re25_runtime_state'),'WATCH')!='INVALIDATED'")
                total=c.execute('SELECT COUNT(*) FROM candidates WHERE '+scoped,params).fetchone()[0]
                sql=('SELECT payload FROM candidates WHERE '+scoped+
                     " ORDER BY CASE json_extract(payload,'$.classification')"+
                     " WHEN 'PRIME' THEN 0 WHEN 'ARMED' THEN 1"+
                     " WHEN 'PRE_EXPLOSION_ARMED' THEN 2 WHEN 'WATCH' THEN 3"+
                     " WHEN 'REJECT' THEN 4 ELSE 5 END,"+
                     " COALESCE(json_extract(payload,'$.candidate_seen_at'),last_at) DESC, last_at DESC"+
                     ' LIMIT ? OFFSET ?')
                rows=[json.loads(z[0]) for z in c.execute(sql,params+[limit,offset])]
            except sqlite3.OperationalError:
                # Bounded fallback: never restore the old 1000-row full materialization.
                recs=[dict(r) for r in c.execute(
                    'SELECT payload,last_at FROM candidates WHERE '+where+
                    ' ORDER BY last_at DESC LIMIT 300',params)]
                pool=[json.loads(z['payload']) for z in recs]
                pool=[z for z in pool if z.get('candidate_stale') is not True]
                if re25_only:
                    pool=[z for z in pool if z.get('re25_setup_state') in ('COMPRESSION_ARMED','RED_NO_CHASE')
                          and z.get('re25_runtime_state')!='INVALIDATED']
                pool.sort(key=lambda r:(RANK.get(r.get('classification'),9),
                    -(dt(r.get('occurred_at')).timestamp() if dt(r.get('occurred_at')) else 0)))
                total=len(pool);rows=pool[offset:offset+limit]
        return {'total':total,'rows':rows,'offset':offset,'limit':limit}

    def active_payloads(self,market,lane=None,limit=1500):
        with self.connect() as c:
            where='market=? AND active=1';params=[market]
            if lane:
                where+=' AND lane=?';params.append(lane)
            rows=c.execute(
                'SELECT key,payload FROM candidates WHERE '+where+' ORDER BY last_at DESC LIMIT ?',
                params+[limit]).fetchall()
        return [(r['key'],json.loads(r['payload'])) for r in rows]

    def update_payload(self,key,payload,current,event=None,reason=None):
        at=stamp(current)
        with self.connect() as c:
            old=c.execute('SELECT * FROM candidates WHERE key=?',(key,)).fetchone()
            if not old:return
            c.execute('UPDATE candidates SET payload=?,last_at=? WHERE key=?',
                      (json.dumps(clean(payload),ensure_ascii=False),at,key))
            if event:
                c.execute('INSERT INTO events(at,market,lane,code,name,event,reason,payload) VALUES(?,?,?,?,?,?,?,?)',
                          (at,old['market'],old['lane'],old['code'],payload.get('name','UNAVAILABLE'),
                           event,reason or event,json.dumps(clean(payload),ensure_ascii=False)))

    def records(self,kind,market,limit=500,offset=0,lane=None):
        with self.connect() as c:
            where='market=?';params=[market]
            if kind=='candidates':where+=' AND active=1'
            if lane=='RS_HANDOFF':
                where+=' AND lane IN (?,?)';params.extend(['ASYMMETRIC_RS','CAPITAL_HANDOFF'])
            elif lane:
                where+=' AND lane=?';params.append(lane)
            total=c.execute(f'SELECT COUNT(*) FROM {kind} WHERE {where}',params).fetchone()[0]
            order='id DESC' if kind in ('events','cycles') else ('entry_at DESC' if kind=='outcomes' else 'last_at DESC')
            records=[dict(r) for r in c.execute(f'SELECT * FROM {kind} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?',params+[limit,offset])]
        for r in records:
            if 'payload' in r:r['payload']=json.loads(r['payload'])
        return {'total':total,'rows':records,'offset':offset,'limit':limit}

class Operations:
    def __init__(self,data_dir='/data/operations_v7',kis_base=None,history_path=None,fetch=get_json):
        self.store=Store(Path(data_dir)/'operations.sqlite3')
        from .persistence import Checkpoint
        self.checkpoint=Checkpoint(self.store);self.checkpoint.restore()
        self.kis=(kis_base or os.getenv('KIS_LEDGER_BASE_URL','https://quant-signal-v3-7-51-clean2.onrender.com')).rstrip('/')
        self.history=History(history_path or os.getenv('V17_ORIGINAL_HISTORY_DB',os.getenv('CACHE_DB','/data/alpha500_bars.sqlite3')))
        self.fetch=fetch;self.providers={};self.runtime={};self.lock=threading.Lock();self.stop=threading.Event();self.thread=None
        self.candidate_refresh_seconds=max(300,int(os.getenv('OPERATIONS_CANDIDATE_REFRESH_SECONDS','600')))
        self.state_refresh_seconds=max(20,int(os.getenv('OPERATIONS_STATE_REFRESH_SECONDS','30')))
        self.last_full_scan_at=None;self.last_state_refresh_at=None
        self.authority_audit={}  # KIS live vs immutable READY snapshot: per-market, per-call
        self.storage_mode='PERSISTENT' if str(Path(data_dir)).startswith('/data/') else 'EPHEMERAL_FALLBACK'
        self.startup_storage_errors=[]
    def storage_info(self):
        info=dict(self.checkpoint.info())
        info.update(operations_storage_mode=self.storage_mode,
                    operations_database_path=str(self.store.path),
                    startup_storage_errors=list(self.startup_storage_errors))
        return info
    def attach(self,**providers): self.providers=providers
    def local(self,name,*args):
        try:return self.providers[name](*args) if name in self.providers else {}
        except Exception as e:return {'status':'PENDING','error':type(e).__name__}
    def live_rows_only(self,market):
        """Lightweight live quote refresh. No latest-manifest/history/provider fan-out."""
        obj=self.fetch(self.kis+'/api/ledger/live/rows?market='+market+'&limit=5000')
        if obj.get('market') not in (None,market):raise ValueError('LIVE_MARKET_MISMATCH')
        rows=obj.get('rows')
        if not isinstance(rows,list):raise ValueError('NO_LIVE_ROWS')
        return [r for r in rows if isinstance(r,dict) and venue(r)==market and valid_code(code(r))]

    def refresh_candidate_states(self,current=None):
        if not self.lock.acquire(False):return {'status':'BUSY'}
        try:
            current=current or now();changes=[];updated=0;by_lane=Counter();stale_quotes=0
            krx_rows=self.live_rows_only('KRX');nxt_rows=self.live_rows_only('NXT')
            krx_quotes={code(r):r for r in krx_rows};nxt_quotes={code(r):r for r in nxt_rows}
            v17_obj=self.local('v17');v17_map={}
            if isinstance(v17_obj,dict):
                for z in v17_obj.get('candidates',[])+v17_obj.get('watch',[]):
                    if isinstance(z,dict) and valid_code(code(z)):v17_map[code(z)]=z
            alpha_obj=self.local('alpha','KRX');alpha_map={}
            if isinstance(alpha_obj,dict):
                for k in ('sector_live_pool','live_pool','top_watch','top_final'):
                    for z in alpha_obj.get(k,[]) or []:
                        if isinstance(z,dict) and valid_code(code(z)):alpha_map[code(z)]=z
            fm_obj=self.local('fullmarket');fm_map={}
            if isinstance(fm_obj,dict):
                for z in fm_obj.get('candidates',[]) or []:
                    if isinstance(z,dict) and valid_code(code(z)):fm_map[code(z)]=z
            nxt_obj=self.local('nxt',nxt_rows,current)
            nxt_map={code(z):z for z in (nxt_obj.get('rows',[]) if isinstance(nxt_obj,dict) else [])
                     if isinstance(z,dict) and valid_code(code(z))}
            for market,qmap in (('KRX',krx_quotes),('NXT',nxt_quotes)):
                for key,row in self.store.active_payloads(market,None,1500):
                    if row.get('candidate_stale') is True:continue
                    q=qmap.get(row.get('code'))
                    if not q:continue
                    before=(row.get('re25_runtime_state'),row.get('runtime_lane_state'),
                            row.get('runtime_upstream_state'),row.get('runtime_quote_grade'))
                    newrow,_=apply_runtime_quote(row,q,current)
                    if newrow.get('runtime_quote_grade')=='STALE':stale_quotes+=1
                    lane=row.get('lane');c=row.get('code')
                    if newrow.get('candidate_lifecycle')=='DATA_HOLD':
                        newrow['runtime_lane_state']='DATA_HOLD'
                        newrow['re25_state_refresh_status']='PENDING_SOURCE_RECONFIRM' if lane=='RE_EXPLOSION_25' else None
                    elif lane=='RE_EXPLOSION_25':
                        newrow,_=re25_runtime_transition(newrow,q,current)
                    elif lane=='V17_ACTION_PRE':
                        z=v17_map.get(c)
                        if z:
                            newrow['runtime_upstream_state']=pick(z,'status','pre_status','signal_track') or row.get('upstream_state')
                            newrow['runtime_signal_asof']=pick(z,'signal_asof','generated_at')
                            newrow['runtime_lane_state']='NATIVE_REFRESHED'
                        else:newrow['runtime_lane_state']='NATIVE_NOT_RECONFIRMED'
                    elif lane=='ASYMMETRIC_RS':
                        z=alpha_map.get(c)
                        if z:
                            for k in ('rs5','rs20','sector_breadth'):
                                v=pick(z,k,k.upper())
                                if v is not None:newrow[k]=number(v)
                            sec=pick(z,'sector','ENERGY_CLUSTER')
                            if sec is not None:newrow['sector']=sec
                            newrow['runtime_lane_state']='RS_REFRESHED'
                        else:newrow['runtime_lane_state']='RS_NOT_RECONFIRMED'
                    elif lane=='CAPITAL_HANDOFF':
                        z=fm_map.get(c)
                        if z:
                            newrow['runtime_capital_handoff_confirmed']=tri(z.get('capital_handoff_confirmed'))
                            newrow['runtime_upstream_state']=z.get('selection_state')
                            newrow['runtime_lane_state']='HANDOFF_REFRESHED'
                        else:newrow['runtime_lane_state']='HANDOFF_NOT_RECONFIRMED'
                    elif lane=='NXT_POST_ACCEL_LANE':
                        z=nxt_map.get(c)
                        if z:
                            for k in ('impulse_seen','recompression_seen','reaccel_seen','high_hold60_confirmed',
                                      'price_hold','entry_price','stop_price','target_price','stop_source','target_source','signal_asof'):
                                if k in z:newrow[k]=z[k]
                            newrow['high_hold60']=number(z.get('nxt_high_hold60'))
                            newrow['volume_accel15']=number(z.get('nxt_volume_accel15'))
                            newrow['turnover_accel15']=number(z.get('nxt_turnover_accel15'))
                            newrow['runtime_lane_state']='TRIGGERED_PENDING_CORE' if z.get('gate_status')=='PASS' else 'WATCH'
                        else:newrow['runtime_lane_state']='SERIES_PENDING'
                    if newrow.get('runtime_no_chase') is True and lane!='RE_EXPLOSION_25':
                        newrow['runtime_lane_state']='NO_CHASE'
                    newrow['state_refresh_at']=stamp(current)
                    after=(newrow.get('re25_runtime_state'),newrow.get('runtime_lane_state'),
                           newrow.get('runtime_upstream_state'),newrow.get('runtime_quote_grade'))
                    changed=before!=after
                    self.store.update_payload(key,newrow,current,'STATE_CHANGE' if changed else None,
                                              ('RUNTIME_STATE '+repr(before)+' -> '+repr(after)) if changed else None)
                    updated+=1;by_lane[lane]+=1
                    if changed:changes.append({'code':c,'lane':lane,'state':newrow.get('re25_runtime_state') or newrow.get('runtime_lane_state')})
            self.last_state_refresh_at=current
            self.runtime['state_refresh_at']=stamp(current);self.runtime['state_refresh_seconds']=self.state_refresh_seconds
            self.runtime['candidate_refresh_seconds']=self.candidate_refresh_seconds
            self.runtime['state_refresh_counts']=dict(by_lane);self.runtime['state_refresh_stale_quotes']=stale_quotes
            self.runtime['state_changes']=changes[-40:];self.runtime.pop('state_refresh_error',None)
            return {'status':'READY','updated':updated,'changed':len(changes),'by_lane':dict(by_lane),
                    'stale_quotes':stale_quotes,'at':stamp(current)}
        except Exception as exc:
            self.last_state_refresh_at=current or now();self.runtime['state_refresh_at']=stamp(self.last_state_refresh_at)
            self.runtime['state_refresh_error']=type(exc).__name__
            return {'status':'PENDING','error':type(exc).__name__}
        finally:self.lock.release()

    def source(self,market):
        """KIS live full-universe rows + independently verified immutable date manifest.

        Never interpret live REST request time as an exchange trading date. On a
        failed live feed a verified latest snapshot is a *reference*, not live.
        """
        errors=[]; completed={}; completed_rows=[]; completed_ok=False
        self.authority_audit[market]={'status':'PENDING_SOURCES_NOT_YET_COMPARED', 'membership_matched':False}
        try:
            completed=self.fetch(self.kis+'/api/ledger/latest/rows?market='+market+'&limit=5000')
            if completed.get('market') not in (None,market):raise ValueError('LATEST_MARKET_MISMATCH')
            manifest=completed.get('manifest') or {}
            if not closed_trade_date_from_manifest(manifest,market):
                errors.append('KIS_READY_SNAPSHOT_DATE_UNVERIFIED')
            a=completed.get('rows') or []
            completed_rows=[r for r in a if isinstance(r,dict) and venue(r)==market and valid_code(code(r))]
            completed_ok=(len(completed_rows)==len(a)==int(completed.get('total') or 0)>0 and len({code(r) for r in completed_rows})==len(completed_rows))
        except Exception as e:
            manifest={};errors.append('KIS_LATEST:'+type(e).__name__)
        try:
            obj=self.fetch(self.kis+'/api/ledger/live/rows?market='+market+'&limit=5000')
            if obj.get('market') not in (None,market):raise ValueError('LIVE_MARKET_MISMATCH')
            original=obj.get('rows')
            if not isinstance(original,list):raise ValueError('NO_LIVE_ROWS')
            rows=[r for r in original if isinstance(r,dict) and venue(r)==market and valid_code(code(r))]
            total=number(obj.get('total'))
            complete=(total is not None and total>0 and len(original)==len(rows) and len(rows)==int(total) and len({code(r) for r in rows})==len(rows))
            if not complete:errors.append('LIVE_INCOMPLETE_OR_INVALID_IDENTIFIERS')
            audit=universe_membership_audit(rows,completed_rows,len(original),int(completed.get('total') or 0) if isinstance(completed,dict) else 0,manifest)
            self.authority_audit[market]=audit
            if audit['status']=='PENDING_CROSS_SNAPSHOT_MEMBERSHIP_DIFF':
                # A 2403-row live set and a 2387-row immutable round are NOT the
                # same sample. Keep all live codes in discovery, but never claim
                # full-universe event completeness without reconciliation.
                errors.append('KIS_CROSS_SNAPSHOT_UNIVERSE_DELTA_UNVERIFIED')
            if complete: return rows,manifest,True,'/api/ledger/live/rows + latest/manifest',errors
            if completed_ok:
                errors.append('FALLBACK_LAST_KIS_READY_NOT_LIVE')
                return completed_rows,manifest,False,'/api/ledger/latest/rows',errors
            return rows,manifest,False,'/api/ledger/live/rows',errors
        except Exception as e:
            errors.append('KIS_LIVE:'+type(e).__name__)
            if completed_ok:
                errors.append('FALLBACK_LAST_KIS_READY_NOT_LIVE')
                return completed_rows,manifest,False,'/api/ledger/latest/rows',errors
        return [],manifest,False,None,errors
    def base(self,r,market,signal,signal_at,alpha,velocity,bars,current,reference_daily=None):
        c=code(r);ls=alpha.get(c) or {};p=number(r.get('price'));q_at=r.get('source_timestamp')
        # Generated_at/row's calendar date are not exchange signal timestamps.
        # Prefer an explicit signal event timestamp only; never synthesize 15:30.
        signal_at=signal.get('signal_asof') if dt(signal.get('signal_asof')) else None
        # LS numbers remain supplemental and never overwrite KIS current/flow values.
        ls_at=pick(ls,'source_timestamp','timestamp_kst','quote_at')
        ls_price=number(pick(ls,'price','current_price'))
        cross=(abs(ls_price/p-1)<=.005) if p and ls_price and fresh(ls_at,current) and fresh(q_at,current) else None
        typ=pick(r,'security_type','instrument_type'); common=True if typ in ('COMMON','COMMON_STOCK','보통주') else (False if typ in ('ETF','ETN','PREFERRED','SPAC','REIT') else None)
        hist=history_metrics(bars,reference_daily)
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
            current=current or now();hist,dates,hstate=self.history.read_all()
            global_history_latest=dates[-1] if dates else None
            recent=[];re25_reference_window=[];krx_reference_day=None
            upstream=self.local('v17');v17={code(x):x for x in upstream.get('candidates',[])+upstream.get('watch',[]) if isinstance(x,dict) and venue(x)=='KRX'}
            v17_at=upstream.get('generated_at')
            # Existing fullmarket output is a traceable discovery source, never a BUY authority.
            fm=self.local('fullmarket');fmrows={code(x):x for x in fm.get('candidates',[]) if isinstance(x,dict)}
            market_meta={}
            for market in ('KRX','NXT'):
                rows,manifest,complete,path,errors=self.source(market);qmap={code(r):r for r in rows}
                manifest_trade_date=closed_trade_date_from_manifest(manifest,market)
                expected_daily=expected_closed_daily_date(manifest,current,market)
                # Last 5 sessions come from versioned XKRX calendar, never the last
                # five cache dates (which may skip an entire trading session).
                if market=='KRX':
                    recent=recent_sessions(expected_daily,5) or []
                    re25_reference_window=recent_sessions(expected_daily,6) or []
                    krx_reference_day=expected_daily
                global_daily_current=(global_history_latest==expected_daily) if expected_daily else None
                source_audit=self.authority_audit.get(market,{})
                # Live rows are still scanned Zero-Base even if a historical
                # universe differs. Differences are reported, not silently dropped.
                universe_alignment_ok=source_audit.get('membership_matched') is True
                history_stale_symbols=history_ready_symbols=0
                history_exception_symbols=0
                history_exception_examples=[]
                re25_window_ready=re25_window_exempt=re25_window_pending=0
                re25_evaluated_codes=set()  # audited per-stock verified completed 6-session windows
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
                pending=Counter();full_recent=market=='KRX' and len(recent)==5 and global_daily_current is True
                for r in rows:
                    c=code(r)
                    # KRX daily references are shared for NXT moving averages,
                    # but neither venue may use a bar later than the confirmed day.
                    b_all=hist.get(c,[])
                    b=[z for z in b_all if krx_reference_day and z['date']<=krx_reference_day]
                    candidates=[]
                    last_symbol_date=trade_date(b[-1]['date']) if b else None
                    daily_last_current=bool(krx_reference_day and last_symbol_date==krx_reference_day)
                    bar_days={z['date'] for z in b}
                    recent5_complete=bool(daily_last_current and len(recent)==5 and set(recent)<=bar_days)
                    re25_window_complete=bool(daily_last_current and len(re25_reference_window)==6 and set(re25_reference_window)<=bar_days)
                    eligibility=classify_window(r,re25_reference_window,bar_days) if market=='KRX' else None
                    if market=='KRX':
                        if eligibility.state=='COMPLETE' and re25_window_complete:
                            re25_window_ready+=1
                            re25_evaluated_codes.add(c)
                        elif eligibility.state=='STRUCTURAL_NOT_EVALUABLE':
                            re25_window_exempt+=1
                            if not recent5_complete: history_exception_symbols+=1
                            if len(history_exception_examples)<40:
                                history_exception_examples.append({'code':c,'reason':eligibility.reason,'missing_dates':list(eligibility.missing)})
                        else:re25_window_pending+=1
                    # A valid final bar alone is not evidence of a complete
                    # five-session history or 200-bar MA depth.
                    daily_history_ready=(daily_last_current and recent5_complete) if market=='KRX' else None
                    if daily_history_ready is True:history_ready_symbols+=1
                    elif not (eligibility and eligibility.state=='STRUCTURAL_NOT_EVALUABLE'):
                        history_stale_symbols+=1
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
                    ev=spike_events(b,recent) if b and market=='KRX' and re25_window_complete and eligibility.state=='COMPLETE' else []
                    if ev:
                        s=dict(rerows.get(c,{ }));s.update(ev[-1]);s=qualify_re25_event(s,r)
                        s['events']=ev;s['recent5_complete']=full_recent and re25_window_complete
                        if s.get('re25_candidate_visible'):
                            live_price=number(r.get('price'));support=number(s.get('support_price'));prior_high=number(s.get('spike_high'))
                            if live_price and support and 0<support<live_price:
                                s.setdefault('entry_price',live_price);s.setdefault('stop_price',support);s.setdefault('stop_source','RE25_OBSERVED_POST_SPIKE_SUPPORT')
                            if live_price and prior_high and prior_high>live_price:
                                s.setdefault('target_price',prior_high);s.setdefault('target_source','RE25_OBSERVED_SPIKE_HIGH')
                        candidates.append(('RE_EXPLOSION_25',s,re25.get('source_asof'),s['re25_setup_state'],s['lane_confirmed']))
                    elif market=='KRX':pending['RE25_HISTORY_OR_EVENT']+=1
                    for lane,s,signal_at,upstate,confirmed in candidates:
                        s=dict(s)
                        if c in news:s.update(news[c])
                        out=self.base(r,market,s,signal_at,alpha,velocities.get(c,{}),b,current,reference_daily=krx_reference_day)
                        out.update(lane=lane,selection_path=[lane,str(s.get('signal_track') or upstate or 'PENDING')],upstream_state=upstate,lane_confirmed=confirmed)
                        out['history_expected_asof']=expected_daily
                        out['authority_universe_consistent']=universe_alignment_ok if market=='KRX' else None
                        out['history_current']=daily_history_ready if market=='KRX' and expected_daily else None
                        out['history_last_bar_current']=daily_last_current
                        out['history_recent5_complete']=recent5_complete
                        out['history_missing_recent_sessions']=[d for d in recent if d not in bar_days] if market=='KRX' else []
                        out['history_re25_predecessor_complete']=(re25_window_complete if market=='KRX' else None)
                        out['history_reference_source']='XKRX_CALENDAR_AND_KIS_READY_MANIFEST_AND_LS_KRX_OHLCV' if market=='KRX' else 'KRX_DAILY_REFERENCE_NOT_NXT_BAR'
                        out['history_200ma_ready']=out.get('ma200') is not None
                        out['history_quality']=('STRUCTURAL_NOT_EVALUABLE' if eligibility and eligibility.state=='STRUCTURAL_NOT_EVALUABLE' else 'CURRENT_5SESSIONS' if out['history_current'] is True else ('STALE_OR_GAPPED' if expected_daily and last_symbol_date else 'PENDING'))
                        out['history_structural_exception']=eligibility.reason if eligibility and eligibility.state=='STRUCTURAL_NOT_EVALUABLE' else None
                        # If a source cannot prove the current KRX daily series, no lane may promote it.
                        # This is a view-only check; underlying LS collectors are unchanged.
                        # Engine decisions are evidence, not a substitute for the shared hard gates.
                        for k in ('impulse_seen','recompression_seen','reaccel_seen','high_hold60_confirmed','spike_date','spike_high_pct','spike_age','fast_new_spike','price_preservation_pct','pullback_from_high_pct','close_retention_pct','pullback_volume_ratio','post_range_pct','spike_range_pct','support_price','support_distance_pct','retest_to_spike_close_pct','post_spike_max_high','post_spike_max_extension_pct','historical_reexpansion','price_preserved','volume_contracting','compression_confirmed','support_near','re25_setup_state','re25_candidate_visible','re25_event_only','re25_daily_setup_confirmed','re25_already_expanded','re25_live_change_pct','re25_extension_above_spike_high_pct','recent5_complete','events'):
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
                    if lane=='RE_EXPLOSION_25':
                        # A latest-date header alone is NOT proof all six OHLCV sessions exist.
                        # Each verified symbol may exit independently, even if the broad scan is partial.
                        evidence=verified_scope('KRX',lane,re25_evaluated_codes,
                            source_complete=bool(complete and len(re25_reference_window)==6),
                            last_close=global_history_latest,expected_close=expected_daily,
                            reason='KRX_SIX_SESSION_KIS_LS_VERIFIED') if market=='KRX' else None
                        lane_complete=set(evidence.evaluated_codes) if evidence and evidence.source_complete else set()
                    else:
                        # An upstream 'complete' flag from an old snapshot is
                        # not evidence of a newly evaluated rejection.
                        # Require fresh producer run time; do NOT infer exchange
                        # quote freshness or remove a candidate on stale data.
                        if lane=='V17_ACTION_PRE':
                            ready=set(upstream.get('evaluated_codes',[])) if fresh(upstream.get('generated_at'),current,1200) else set()
                        elif lane=='CAPITAL_HANDOFF':
                            ready=bool(fm.get('universe_complete')) and fresh(fm.get('generated_at'),current,1200)
                        elif lane=='NXT_POST_ACCEL_LANE':
                            # integration.nxt() has source_asof=None by design;
                            # use native per-stock minute-series timestamps and
                            # 60-bar coverage instead of a nonexistent global clock.
                            ready={c for c,z in nxtrows.items() if native_nxt_minute_proven(z,current)}
                        elif lane=='ASYMMETRIC_RS':
                            ready=bool(len(alpha)>=len(rows)) and fresh(alpha_obj.get('timestamp_kst'),current,1200)
                        else:ready=False
                        lane_complete=ready if complete and applicable else set()
                    self.store.reconcile(market,lane,data,lane_complete,current)
                self.store.track(market,qmap,current)
                krx_daily_coverage=(market=='KRX' and full_recent and complete and universe_alignment_ok and
                                    history_stale_symbols==0 and history_ready_symbols+history_exception_symbols==len(rows) and len(rows)>0)
                nxt_minute_coverage=(complete and nxt.get('status')=='READY' and nxt.get('universe_complete') is True and len(nxtrows)==len(rows) and len(rows)>0)
                # NXT price-action lane is not dependent on the KRX daily-history cache.
                coverage_complete=krx_daily_coverage if market=='KRX' else nxt_minute_coverage
                meta=dict(status='READY' if coverage_complete else 'PENDING',source_path=path,scan_scope='ZERO_BASE_AUTHORITY_ALL_ROWS',source_code_policy='ALPHANUMERIC_6_ALLOWED',rows_scanned=len(rows),complete=complete,source_errors=errors,manifest=manifest,history_state=hstate,history_symbols=len(hist),history_latest_date=global_history_latest,history_expected_date=expected_daily,history_manifest_trade_date=manifest_trade_date,history_latest_verified=(krx_daily_coverage if market=='KRX' else None),history_date_matches_kis=(global_daily_current if market=='KRX' else None),history_calendar_source=('XKRX_SNAPSHOT_2025_2027' if market=='KRX' and recent else 'CALENDAR_UNVERIFIED' if market=='KRX' else 'NOT_APPLICABLE'),history_recent5_expected=(recent if market=='KRX' else []),history_re25_predecessor_date=(re25_reference_window[0] if market=='KRX' and len(re25_reference_window)==6 else None),history_coverage_complete=(krx_daily_coverage if market=='KRX' else None),history_repair=(self.local('history_repair_state') if market=='KRX' else {'status':'NOT_APPLICABLE','reason':'KRX_ONLY'}),history_ready_symbols=history_ready_symbols,history_stale_symbols=history_stale_symbols,history_structural_exception_symbols=history_exception_symbols,history_structural_exception_examples=history_exception_examples,history_universe_audit=source_audit,history_recent5_status=('COMPLETE_EVALUABLE_WITH_VERIFIED_EXCLUSIONS' if krx_daily_coverage and history_exception_symbols else 'COMPLETE' if krx_daily_coverage else 'PENDING'),recent5_dates=recent,re25_universe_status=('NOT_APPLICABLE' if market=='NXT' else 'COMPLETE_EVALUABLE_WITH_VERIFIED_EXCLUSIONS' if full_recent and complete and universe_alignment_ok and re25_window_pending==0 and re25_window_exempt>0 and re25_window_ready+re25_window_exempt==len(rows) else 'COMPLETE' if full_recent and complete and universe_alignment_ok and re25_window_pending==0 and re25_window_exempt==0 and re25_window_ready==len(rows) else 'PENDING_PARTIAL_HISTORY'),re25_evaluable_symbols=re25_window_ready,re25_structural_exception_symbols=re25_window_exempt,re25_pending_symbols=re25_window_pending,
                          pending_axes=dict(pending),lanes={k:{'observed':len(v),'status':'READY' if v and coverage_complete else 'PENDING','prime':sum(z['classification']=='PRIME' for z in v)} for k,v in lanes.items()},scan_at=stamp(current),nxt_independent=True)
                market_meta[market]=meta
                with self.store.connect() as con:con.execute('INSERT INTO cycles(at,market,status,payload) VALUES(?,?,?,?)',(stamp(current),market,meta['status'],json.dumps(clean(meta),ensure_ascii=False)))
            self.checkpoint.save()
            self.last_full_scan_at=current
            self.runtime={'version':VERSION,'scan_at':stamp(current),'candidate_refresh_at':stamp(current),
                          'candidate_refresh_seconds':self.candidate_refresh_seconds,
                          'state_refresh_at':stamp(self.last_state_refresh_at) if self.last_state_refresh_at else None,
                          'state_refresh_seconds':self.state_refresh_seconds,
                          'markets':clean(market_meta),'storage':self.storage_info(),
                          'status':'READY' if all(v['status']=='READY' for v in market_meta.values()) else 'PENDING'}
            return self.runtime
        finally:
            if 'hist' in locals() and hasattr(hist,'close'):hist.close()
            self.lock.release()
    def start(self):
        if self.thread and self.thread.is_alive():return
        def loop():
            self.stop.wait(15)
            next_full=0.0;next_state=0.0
            while not self.stop.is_set():
                mono=time.monotonic()
                try:
                    if mono>=next_full:
                        self.scan()
                        next_full=time.monotonic()+self.candidate_refresh_seconds
                        next_state=time.monotonic()+self.state_refresh_seconds
                    elif mono>=next_state:
                        self.refresh_candidate_states()
                        next_state=time.monotonic()+self.state_refresh_seconds
                except Exception as exc:
                    # Preserve the last good market/scan payload. A transient
                    # SQLite/provider fault must not erase candidate metadata.
                    previous=dict(self.runtime or {})
                    previous.update(status='PENDING',error=type(exc).__name__,error_at=stamp(),
                                    error_context=('FULL_SCAN' if mono>=next_full else 'STATE_REFRESH'),
                                    candidate_refresh_seconds=self.candidate_refresh_seconds,
                                    state_refresh_seconds=self.state_refresh_seconds)
                    self.runtime=previous
                self.stop.wait(2)
        self.thread=threading.Thread(target=loop,daemon=True,name='operations-readonly');self.thread.start()
    def api(self,raw_path):
        u=urlparse(raw_path);q=parse_qs(u.query);m=(q.get('market') or ['KRX'])[0]
        if m not in ('KRX','NXT'):return 400,{'error':'market must be KRX or NXT'}
        lane=(q.get('lane') or [None])[0]
        if lane and lane not in VIEW_LANES:return 400,{'error':'invalid lane'}
        try:limit=max(20,min(10000,int((q.get('limit') or ['500'])[0])));offset=max(0,int((q.get('offset') or ['0'])[0]))
        except ValueError:return 400,{'error':'invalid pagination'}
        kind=u.path.rsplit('/',1)[-1]
        if kind=='status':return 200,clean(self.runtime or {'status':'PENDING','storage':self.storage_info(),'version':VERSION})
        if kind=='board':
            data=self.store.board(m,limit,offset,lane)
            rows=data['rows']
            return 200,clean({'market':m,'rows':rows,'total':data['total'],'offset':offset,
                              'status':(self.runtime.get('markets',{}).get(m) or {}).get('status','PENDING'),
                              'scan':self.runtime.get('markets',{}).get(m,{}),
                              'candidate_refresh_at':self.runtime.get('candidate_refresh_at'),
                              'candidate_refresh_seconds':self.runtime.get('candidate_refresh_seconds',self.candidate_refresh_seconds),
                              'state_refresh_at':self.runtime.get('state_refresh_at'),
                              'state_refresh_seconds':self.runtime.get('state_refresh_seconds',self.state_refresh_seconds),
                              'state_refresh_counts':self.runtime.get('state_refresh_counts',{}),
                              'state_refresh_stale_quotes':self.runtime.get('state_refresh_stale_quotes'),
                              'runtime_error':self.runtime.get('error'),'runtime_error_at':self.runtime.get('error_at'),
                              'runtime_error_context':self.runtime.get('error_context'),
                              'storage':self.storage_info(),'version':VERSION})
        if kind in ('events','outcomes'):return 200,clean(self.store.records(kind,m,limit,offset,lane))
        return 404,{'error':'not found'}
