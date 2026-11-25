# -*- coding: utf-8 -*-
from __future__ import annotations
import csv, io, json, os, re, sqlite3, threading, time, traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
import requests

KST=timezone(timedelta(hours=9))
VERSION='SMART_REACCEL_MTF_BACKTEST_V2_0'
RESULT_ROOT=Path('/data/results/smart_reaccel_mtf_v2')
DAILY_URL='https://aikstockdata.com/data/public/daily/quotes_{date}.csv'
TARGET_DAYS=int(os.getenv('SMART_REACCEL_BT_DAYS','10'))
MAX_EVENTS=int(os.getenv('SMART_REACCEL_BT_MAX_EVENTS','500'))
MIN_CAP_100M=float(os.getenv('PULSE_MIN_MARKET_CAP_100M','700'))
LS_INTERVAL=max(1.08,float(os.getenv('SMART_REACCEL_BT_LS_INTERVAL','1.12')))
EXCLUDE=('ETF','ETN','스팩','SPAC','리츠','REIT','인버스','레버리지')


def _num(v,default=None):
    try:return float(str(v).replace(',','').replace('%','').strip()) if v not in (None,'') else default
    except:return default

def _ordinary(name):
    s=str(name or '').strip(); u=s.upper().replace(' ','')
    if not s or any(t.upper() in u for t in EXCLUDE): return False
    if re.search(r'(우|우B|우C|우선주)$',s): return False
    return True

def _safe(x):
    if isinstance(x,(np.bool_,)):return bool(x)
    if isinstance(x,(np.integer,)):return int(x)
    if isinstance(x,(np.floating,)):return None if not np.isfinite(x) else float(x)
    if isinstance(x,(pd.Timestamp,datetime,date)):return str(x)
    if isinstance(x,dict):return {str(k):_safe(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)):return [_safe(v) for v in x]
    try:
        if pd.isna(x): return None
    except: pass
    return x

class LS:
    def __init__(self):
        self.key=os.getenv('LS_APP_KEY','').strip(); self.secret=os.getenv('LS_APP_SECRET','').strip()
        self.base=os.getenv('LS_BASE_URL','https://openapi.ls-sec.co.kr:8080').rstrip('/')
        self.path=os.getenv('LS_CHART_PATH','/stock/chart'); self.s=requests.Session(); self.token=None; self.exp=0.; self.last=0.; self.lock=threading.RLock()
    @property
    def configured(self): return bool(self.key and self.secret)
    def wait(self):
        with self.lock:
            g=time.monotonic()-self.last
            if g<LS_INTERVAL: time.sleep(LS_INTERVAL-g)
            self.last=time.monotonic()
    def auth(self):
        if self.token and time.monotonic()<self.exp-60:return
        if not self.configured: raise RuntimeError('LS_NOT_CONFIGURED')
        last=''
        for u in [self.base+'/oauth2/token',self.base+'/oauth2/tokenP']:
            try:
                self.wait(); r=self.s.post(u,headers={'content-type':'application/x-www-form-urlencoded'},data={'grant_type':'client_credentials','appkey':self.key,'appsecretkey':self.secret,'scope':'oob'},timeout=20)
                if r.status_code!=200: last=f'HTTP{r.status_code}:{r.text[:120]}'; continue
                j=r.json(); self.token=j.get('access_token');
                if self.token: self.exp=time.monotonic()+max(600,int(j.get('expires_in') or 86400)); return
            except Exception as e:last=str(e)
        raise RuntimeError('LS_TOKEN:'+last)
    def post(self,tr,body,cont='N',key=''):
        self.auth()
        for attempt in range(4):
            self.wait(); h={'content-type':'application/json; charset=UTF-8','authorization':f'Bearer {self.token}','tr_cd':tr,'tr_cont':cont,'tr_cont_key':key}
            mac=os.getenv('LS_MAC_ADDRESS','').strip()
            if mac:h['mac_address']=mac
            r=self.s.post(self.base+self.path,headers=h,json=body,timeout=25)
            txt=r.text[:300]
            if r.status_code==200:
                j=r.json(); cd=str(j.get('rsp_cd') or '')
                if cd in ('','00000','0000','0') or (j.get(f'{tr}OutBlock1') or []): return j,{k.lower():v for k,v in r.headers.items()}
                if cd!='IGW00201': raise RuntimeError(f'LS {tr} {cd}:{j.get("rsp_msg")}')
            if 'IGW00201' in txt and attempt<3:
                time.sleep((attempt+1)*2.2); continue
            raise RuntimeError(f'LS {tr} HTTP{r.status_code}:{txt}')
        raise RuntimeError('LS_RETRY_EXHAUSTED')
    def minute1_day(self,code,ds):
        body={'t8412InBlock':{'shcode':code,'ncnt':1,'qrycnt':500,'nday':'1','sdate':ds.replace('-',''),'stime':'','edate':ds.replace('-',''),'etime':'','cts_date':'','cts_time':'','comp_yn':'N'}}
        j,_=self.post('t8412',body); out=[]
        for z in j.get('t8412OutBlock1') or []:
            try:
                d=str(z.get('date') or ''); t=''.join(c for c in str(z.get('time') or '') if c.isdigit()).zfill(6)[-6:]
                dt=pd.Timestamp(f'{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}',tz='Asia/Seoul')
                if dt.strftime('%H%M')<'0900' or dt.strftime('%H%M')>'1530':continue
                op,hi,lo,cl=[float(z.get(k) or 0) for k in ('open','high','low','close')]; vol=float(z.get('jdiff_vol') or z.get('volume') or 0)
                if min(op,hi,lo,cl)<=0:continue
                out.append({'datetime':dt,'open':op,'high':hi,'low':lo,'close':cl,'volume':max(0.,vol)})
            except: pass
        return pd.DataFrame(out).sort_values('datetime').drop_duplicates('datetime') if out else pd.DataFrame()


def load_cap_map():
    d=Path('/data'); files=sorted(d.glob('cap_map_*.json'),reverse=True)
    for p in files:
        try:return {str(k).zfill(6):float(v) for k,v in json.loads(p.read_text()).items() if _num(v) is not None}
        except:pass
    return {}

def daily_rows(ds):
    r=requests.get(DAILY_URL.format(date=ds.strftime('%Y%m%d')),timeout=20,headers={'User-Agent':'smart-reaccel-bt/2.0'})
    if r.status_code==404:return []
    r.raise_for_status(); return list(csv.DictReader(io.StringIO(r.content.decode('utf-8-sig',errors='replace'))))

def build_events():
    cap=load_cap_map(); ev=[]; audits=[]; d=datetime.now(KST).date(); got=0; scanned=0
    while got<TARGET_DAYS and scanned<65:
        scanned+=1
        if d.weekday()<5:
            try:
                rows=daily_rows(d)
                if rows:
                    got+=1; n=0
                    for r in rows:
                        code=str(r.get('종목코드') or r.get('srtnCd') or '').strip().zfill(6); name=str(r.get('종목명') or r.get('itmsNm') or '').strip(); market=str(r.get('mrktCtg') or '').strip().upper()
                        if not re.fullmatch(r'\d{6}',code) or market not in {'KOSPI','KOSDAQ'} or not _ordinary(name):continue
                        cv=cap.get(code)
                        if cv is not None and cv<MIN_CAP_100M:continue
                        close=_num(r.get('clpr')); vs=_num(r.get('vs')); high=_num(r.get('hipr'))
                        if close is None or vs is None or high is None:continue
                        prev=close-vs
                        if prev<=0:continue
                        peak=(high/prev-1)*100
                        if peak>=5.0:
                            ev.append({'date':d.isoformat(),'code':code,'name':name,'market':market,'prev_close':prev,'day_peak_pct':peak,'cap_100m':cv}); n+=1
                    audits.append({'date':d.isoformat(),'rows':len(rows),'events':n})
            except Exception as e:audits.append({'date':d.isoformat(),'error':f'{type(e).__name__}:{e}'})
        d-=timedelta(days=1)
    ev=sorted(ev,key=lambda x:(x['date'],x['code']))
    if len(ev)>MAX_EVENTS:
        # deterministic stratified truncation: keep every high-peak event, then evenly fill lower-peak controls by date/code
        hi=[x for x in ev if x['day_peak_pct']>=15]
        lo=[x for x in ev if x['day_peak_pct']<15]
        keep=hi[:MAX_EVENTS]
        rem=max(0,MAX_EVENTS-len(keep))
        if rem and lo:
            idx=np.linspace(0,len(lo)-1,min(rem,len(lo))).astype(int)
            keep+= [lo[i] for i in idx]
        ev=sorted(keep,key=lambda x:(x['date'],x['code']))
    return ev,audits

def resample(df,mins):
    if df.empty:return df
    x=df.set_index('datetime').sort_index()
    rule=f'{mins}min'
    y=x.resample(rule,origin='start_day',offset='0min',label='right',closed='right').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    y=y[(y.index.strftime('%H%M')>='0900')&(y.index.strftime('%H%M')<='1530')].reset_index()
    return y

def features(df,prev):
    x=df.copy(); c=x.close; v=x.volume
    x['ma5']=c.rolling(5).mean(); x['ma20']=c.rolling(20).mean(); x['s5']=x.ma5.diff(); x['s20']=x.ma20.diff()
    x['gap']=(x.ma20-x.ma5).abs()/x.ma20
    tp=(x.high+x.low+x.close)/3
    x['vwap']=(tp*v).cumsum()/v.replace(0,np.nan).cumsum()
    x['gain']=(x.close/prev-1)*100; x['max_gain']=(x.high.cummax()/prev-1)*100
    x['dhi']=x.high.cummax(); x['dlo']=x.low.cummin(); rng=(x.dhi-x.dlo).replace(0,np.nan); x['pres']=(x.close-x.dlo)/rng
    x['vol_med5']=v.rolling(5).median(); x['vol_mean3']=v.rolling(3).mean(); x['vol_prev3']=v.shift(3).rolling(3).mean()
    x['ma_turn']=(x.s5>0)&((x.s5.shift(1)<=0)|(x.s5.shift(2)<=0)|(x.s5.shift(3)<=0))
    x['gap_comp']=(x.gap<x.gap.shift(1))&(x.gap.shift(1)<=x.gap.shift(2))
    x['golden']=x.ma5>x.ma20
    x['vol_reaccel']=(v>x.vol_med5*1.15)&(x.close>x.close.shift(1))
    x['sell_exhaust']=(x.vol_mean3<=x.vol_prev3*0.92) | ((x.close>x.open)&(v>v.shift(1)*1.05))
    x['vwap_ok']=(x.close>=x.vwap*0.997) | ((x.close.shift(1)<x.vwap.shift(1))&(x.close>=x.vwap))
    x['early']=(x.gain>=5)&(x.gain<=12)&(x.max_gain<=18)
    x['hold']=(x.pres>=0.50)&(x.pres<=1.02)&((x.dhi/x.close-1)>=0.005)
    return x

def map5(v5,ts):
    if v5.empty:return None
    z=v5[v5.datetime<=ts]
    return None if z.empty else z.iloc[-1]

def signal_index(df,variant,v5=None):
    for i,r in df.iterrows():
        if i<20:continue
        base=bool(r.early and r.hold and r.sell_exhaust and r.vol_reaccel and r.vwap_ok)
        if not base:continue
        if variant in ('B','C','D','E') and not bool(r.ma_turn):continue
        if variant in ('C','E') and not bool(r.gap_comp or r.golden):continue
        if variant=='D' and not bool(r.golden):continue
        if variant=='E' and v5 is not None:
            q=map5(v5,r.datetime)
            if q is not None and pd.notna(q.ma20):
                veto=bool(q.s5<0 and q.s20<0 and q.close<q.vwap)
                if veto:continue
        return i
    return None

def outcome(df,i,tp):
    if i is None or i+1>=len(df):return None
    ent=float(df.iloc[i+1].open); sig_t=str(df.iloc[i].datetime); ent_t=str(df.iloc[i+1].datetime)
    stop=ent*0.94; target=ent*(1+tp)
    mfe=-999.; mae=999.; exit_px=float(df.iloc[-1].close); exit_t=str(df.iloc[-1].datetime); result='EOD'
    for j in range(i+1,len(df)):
        r=df.iloc[j]; mfe=max(mfe,(float(r.high)/ent-1)*100); mae=min(mae,(float(r.low)/ent-1)*100)
        hit_s=float(r.low)<=stop; hit_t=float(r.high)>=target
        if hit_s and hit_t:
            result='SL_AMBIG'; exit_px=stop; exit_t=str(r.datetime); break
        if hit_s:
            result='SL'; exit_px=stop; exit_t=str(r.datetime); break
        if hit_t:
            result='TP'; exit_px=target; exit_t=str(r.datetime); break
        # structure exit: two-bar MA5 down + price below VWAP, but not before 2 bars after entry
        if j>=i+3:
            r0=df.iloc[j-1]
            if r.s5<0 and r0.s5<0 and r.close<r.vwap:
                result='STRUCT_EXIT'; exit_px=float(r.close); exit_t=str(r.datetime); break
    ret=(exit_px/ent-1)*100
    return {'signal_time':sig_t,'entry_time':ent_t,'entry':ent,'exit_time':exit_t,'exit':exit_px,'result':result,'return_pct':ret,'mfe_pct':mfe,'mae_pct':mae}

def summarize(rows,tp):
    if not rows:return {'n':0}
    n=len(rows); wins=sum(r['result']=='TP' for r in rows); sl=sum(r['result'] in ('SL','SL_AMBIG') for r in rows)
    rets=np.array([r['return_pct'] for r in rows],float); pos=rets[rets>0].sum(); neg=-rets[rets<0].sum()
    return {'n':n,'tp_rate_pct':round(wins/n*100,2),'stop_rate_pct':round(sl/n*100,2),'avg_return_pct':round(float(rets.mean()),3),'median_return_pct':round(float(np.median(rets)),3),'profit_factor':round(float(pos/neg),3) if neg>0 else None,'avg_mfe_pct':round(float(np.mean([r['mfe_pct'] for r in rows])),3),'avg_mae_pct':round(float(np.mean([r['mae_pct'] for r in rows])),3),'expectancy_R':round(float(rets.mean()/6.0),3)}

class Runner:
    def __init__(self): self.ls=LS()
    def run(self):
        t0=datetime.now(KST); print('SMART_REACCEL_BT_START '+t0.isoformat(),flush=True)
        events,aud=build_events(); print(f'SMART_REACCEL_BT_UNIVERSE days={TARGET_DAYS} events={len(events)} ls={self.ls.configured}',flush=True)
        if not events: raise RuntimeError('EVENT_UNIVERSE_EMPTY')
        variants=['A','B','C','D','E']; frames=[2,3,5]; result_rows=[]; errors=[]
        for k,e in enumerate(events,1):
            if k==1 or k%20==0: print(f'SMART_REACCEL_BT_PROGRESS {k}/{len(events)} {e["date"]} {e["name"]}({e["code"]})',flush=True)
            try: one=self.ls.minute1_day(e['code'],e['date'])
            except Exception as ex: errors.append({**e,'error':f'{type(ex).__name__}:{ex}'}); continue
            if one.empty: errors.append({**e,'error':'EMPTY_1M'});continue
            for m in frames:
                f=features(resample(one,m),e['prev_close']); v5=features(resample(one,5),e['prev_close']) if m!=5 else f
                for v in variants:
                    # E only meaningful as MTF veto for 2m/3m; for 5m it reduces to same-TF veto
                    idx=signal_index(f,v,v5)
                    if idx is None:continue
                    for tp in (0.12,0.15):
                        o=outcome(f,idx,tp)
                        if o: result_rows.append({**e,'frame_min':m,'variant':v,'tp':int(tp*100),**o})
        summaries=[]
        for m in frames:
            for v in variants:
                for tp in (12,15):
                    rr=[r for r in result_rows if r['frame_min']==m and r['variant']==v and r['tp']==tp]
                    summaries.append({'frame_min':m,'variant':v,'tp':tp,**summarize(rr,tp/100)})
        # best: require at least 20 trades, rank by PF then expectancy, penalize stop rate
        eligible=[s for s in summaries if s.get('n',0)>=20]
        eligible.sort(key=lambda s:((s.get('profit_factor') or -1),(s.get('expectancy_R') or -99),-(s.get('stop_rate_pct') or 100)),reverse=True)
        report={'version':VERSION,'started_at':t0.isoformat(),'finished_at':datetime.now(KST).isoformat(),'target_days':TARGET_DAYS,'event_count':len(events),'successful_bar_events':len(set((r['date'],r['code']) for r in result_rows)),'error_events':len(errors),'variant_def':{'A':'base hold+sell exhaustion+volume reaccel+VWAP','B':'A+MA5 turn','C':'B+MA5/20 gap compression OR already golden','D':'B+MA5>MA20 golden required','E':'C+5m downtrend veto'},'summaries':summaries,'best':eligible[:10],'daily_audit':aud,'errors':errors[:100]}
        RESULT_ROOT.mkdir(parents=True,exist_ok=True); rid=datetime.now(KST).strftime('%Y%m%d_%H%M%S'); out=RESULT_ROOT/rid; out.mkdir(parents=True,exist_ok=True)
        pd.DataFrame(result_rows).to_csv(out/'trades.csv',index=False,encoding='utf-8-sig'); pd.DataFrame(summaries).to_csv(out/'summary.csv',index=False,encoding='utf-8-sig'); (out/'report.json').write_text(json.dumps(_safe(report),ensure_ascii=False,indent=2),encoding='utf-8')
        marker=RESULT_ROOT/f'AUTORUN_{TARGET_DAYS}D_DONE.json'; marker.write_text(json.dumps({'output_dir':str(out),'report':_safe(report)},ensure_ascii=False,indent=2),encoding='utf-8')
        print('SMART_REACCEL_BT_READY '+json.dumps(_safe({'output_dir':str(out),'event_count':len(events),'error_events':len(errors),'best':eligible[:10],'summaries':summaries}),ensure_ascii=False),flush=True)

def _autorun():
    if str(os.getenv('SMART_REACCEL_BT_AUTORUN','true')).lower() not in {'1','true','yes','on'}:return
    marker=RESULT_ROOT/f'AUTORUN_{TARGET_DAYS}D_DONE.json'
    if marker.exists():
        try:
            j=json.loads(marker.read_text()); print('SMART_REACCEL_BT_AUTORUN_SKIP '+json.dumps(_safe(j),ensure_ascii=False),flush=True)
        except: print('SMART_REACCEL_BT_AUTORUN_SKIP marker_exists',flush=True)
        return
    def job():
        try:
            time.sleep(20); Runner().run()
        except Exception as e: print('SMART_REACCEL_BT_ERROR '+f'{type(e).__name__}:{e}\n{traceback.format_exc()[-3000:]}',flush=True)
    threading.Thread(target=job,daemon=True,name='smart-reaccel-bt').start()
_autorun()
