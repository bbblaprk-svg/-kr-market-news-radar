"""Audit KIS cross-snapshot membership and verified KRX structural bar gaps.

A missing candle is never inferred to be a suspension or an IPO from its absence.
Structural exemption needs positive exchange/broker reference evidence and is
NON-EVALUABLE, never a successful trading signal or a complete OHLC series.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date

TRUSTED_REFERENCES = {'KRX', 'KIS_VERIFIED_REFERENCE'}


def _date(value):
    if not isinstance(value, str):return None
    try:
        parsed=date.fromisoformat(value)
        return parsed.isoformat() if parsed.year>=2000 else None
    except ValueError:
        return None


def _codes(rows):
    result=[]
    for row in rows or []:
        if not isinstance(row,dict):continue
        raw=str(row.get('code') or row.get('symbol') or '').strip().upper()
        if raw.isdigit() and 0<len(raw)<=6:raw=raw.zfill(6)
        if len(raw)==6 and raw.isascii() and raw.isalnum():result.append(raw)
    return result


def universe_membership_audit(live_rows, latest_rows, live_total, latest_total, latest_manifest):
    """Compares sets, not raw row counts; NEVER treats REST request time as session time."""
    live=_codes(live_rows)
    historical=_codes(latest_rows)
    live_set=set(live)
    latest_set=set(historical)
    live_valid=(isinstance(live_total,int) and live_total>0 and len(live)==live_total and len(live_set)==live_total)
    latest_valid=(isinstance(latest_total,int) and latest_total>0 and len(historical)==latest_total and len(latest_set)==latest_total and
                  isinstance(latest_manifest,dict) and latest_manifest.get('status')=='READY')
    new=sorted(live_set-latest_set)
    old=sorted(latest_set-live_set)
    comparable=live_valid and latest_valid
    if not comparable:
        status='PENDING_INVALID_SOURCE_COUNTS'
    elif new or old:
        status='PENDING_CROSS_SNAPSHOT_MEMBERSHIP_DIFF'
    else:
        status='MATCHED_CODES_UNVERIFIED_SAME_SESSION'
    return dict(status=status, live_total=live_total, latest_total=latest_total,
                live_unique=len(live_set), latest_unique=len(latest_set),
                live_only_count=len(new), latest_only_count=len(old),
                live_only_examples=new[:30], latest_only_examples=old[:30],
                # The live feed supplies no verified exchange-session identity.
                same_session_verified=False,
                latest_round_id=(latest_manifest or {}).get('round_id') if isinstance(latest_manifest,dict) else None,
                membership_matched=bool(comparable and not new and not old),
                coverage_assertion='NO_CROSS_SNAPSHOT_EQUIVALENCE_ASSUMED')


@dataclass(frozen=True)
class WindowEligibility:
    state: str
    reason: str
    missing: tuple[str, ...]
    verified_structural_dates: tuple[str, ...]


def classify_window(row, required_sessions, observed_dates):
    """Classify a recent event window without assuming reasons for missing bars.

    Verified exemptions are *not* ordinary price-history completeness:
    those securities remain non-evaluable (entry blocked) and must be reported.
    """
    required=tuple(required_sessions or ())
    observed=set(observed_dates or ())
    if not required or len(required)!=len(set(required)) or any(_date(x)!=x for x in required):
        return WindowEligibility('DATA_PENDING','REQUIRED_SESSION_CALENDAR_UNVERIFIED',required,())
    missing=tuple(d for d in required if d not in observed)
    if not missing:
        return WindowEligibility('COMPLETE','ALL_REQUIRED_REAL_BARS_PRESENT',(),())
    if not isinstance(row,dict):
        return WindowEligibility('DATA_PENDING','SOURCE_SECURITY_METADATA_UNVERIFIED',missing,())
    proved=set()
    listed=_date(row.get('listing_date'))
    if (listed and row.get('listing_date_verified') is True and
        row.get('listing_date_source') in TRUSTED_REFERENCES):
        proved.update(d for d in missing if d<listed)
    # Require a certified event per period; a bare 'SUSPENDED' flag is inadequate.
    periods=row.get('verified_suspension_periods') or []
    if isinstance(periods,list):
        for period in periods:
            if not isinstance(period,dict) or period.get('verified') is not True or period.get('source') not in TRUSTED_REFERENCES:
                continue
            begin,end=_date(period.get('start')), _date(period.get('end'))
            if begin is None or end is None or begin>end:continue
            proved.update(d for d in missing if begin<=d<=end)
    if set(missing)<=proved:
        return WindowEligibility('STRUCTURAL_NOT_EVALUABLE','VERIFIED_PRELISTING_OR_SUSPENSION',missing,tuple(sorted(proved)))
    return WindowEligibility('DATA_PENDING','MISSING_BARS_NOT_ALL_STRUCTURALLY_VERIFIED',missing,tuple(sorted(proved)))
