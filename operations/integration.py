"""Adapters use only existing local objects; never start a broker client or collect bars."""
from .engine import Operations, number, code, dt, stamp, fresh, tri
from pathlib import Path
import json

def attach(app):
    ops=Operations()
    def alpha(market):
        # Read the venue's native snapshot. No switching of the collector's current venue.
        return app['load_latest_for_venue'](market) or {}
    def native(name):
        obj=app.get(name)
        return obj.latest() if obj else {}
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
            if len(bars)<cfg.min_rows:missing.append(c);continue
            x=pd.DataFrame(bars[-260:]).rename(columns={'date':'date'})
            # Cached actual trade dates only. Never append a weekend/holiday or request-date synthetic bar.
            x['date']=pd.to_datetime(x['date']).dt.strftime('%Y%m%d')
            try:
                z=V17.analyze_one(x,cfg,c,r.get('name',c))
                z=dict(z); z['venue']='KRX';z['signal_history_date']=bars[-1]['date']
                # No fresh intraday trigger is inferred from a daily signal.
                z['signal_asof']=bars[-1]['date']+'T15:30:00+09:00'
                if z.get('status') not in ('REJECT','HISTORY_FAIL'):
                    result.append(z)
                else:rejected.append(c)
            except Exception:missing.append(c)
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
            if len(c)!=6 or not c.isdigit():continue
            at=z.get('published_at') or z.get('published_at_kst')
            if not fresh(at,current,86400):continue
            state=z.get('state');meta=z.get('metadata') or {}
            gate=state if state in ('PRICED_IN','SELL_THE_NEWS','NEGATIVE_CONFIRMED') else meta.get('news_gate')
            if gate not in ('PRICED_IN','SELL_THE_NEWS','NEGATIVE_CONFIRMED','PASS','CLEAR','POSITIVE_CONFIRMED'):continue
            out[c]={'news_gate':gate,'news_published_at':at,'news_source':z.get('url') or z.get('source')}
        return out
    ops.attach(alpha=alpha,news=news,v17=lambda:native('V17_ORIGINAL_BRIDGE'),v17_all=v17_all,fullmarket=lambda:native('FULLMARKET_V3_BRIDGE'),nxt=nxt)
    return ops
