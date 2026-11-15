"""Bounded, versioned XKRX session dates for completed KRX daily-bar validation.

The snapshot was generated from exchange_calendars XKRX 4.13.1 on 2026-11-15.
It is not a live exchange feed. Dates outside the bundled range are UNKNOWN,
not non-trading dates. KIS remains authoritative for the scan session, while
actual LS OHLCV remains the source for an EOD candle.
"""
from __future__ import annotations
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
import bisect
import json

CALENDAR_FILE=Path(__file__).with_name('krx_calendar_2025_2027.json')

@lru_cache(maxsize=1)
def _snapshot():
    obj=json.loads(CALENDAR_FILE.read_text(encoding='utf-8'))
    dates=tuple(obj['sessions'])
    if not dates or sorted(set(dates))!=list(dates):
        raise ValueError('KRX_SESSION_CALENDAR_INVALID')
    return obj,dates

def recent_sessions(expected: str | None, count: int=5):
    if not expected:return None
    try:
        obj, dates = _snapshot()
        if expected<obj['valid_from'] or expected>obj['valid_until']:
            return None
        i=bisect.bisect_left(dates,expected)
        if i>=len(dates) or dates[i]!=expected or i<count-1:
            return None
        return list(dates[i-count+1:i+1])
    except (OSError,ValueError,KeyError,TypeError):
        return None

def last_closed_session(current: datetime):
    """At/after 15:40 KST is the earliest accepted regular-session close.
    This is a *calendar cutoff*, not proof of a successful exchange EOD download.
    """
    try:
        obj, dates = _snapshot()
        today=current.date().isoformat()
        if today<obj['valid_from'] or today>obj['valid_until']:
            return None
        if current.time()>=time(15,40):
            pos=bisect.bisect_right(dates,today)-1
        else:
            pos=bisect.bisect_left(dates,today)-1
        return dates[pos] if pos>=0 else None
    except (OSError,ValueError,KeyError,TypeError):
        return None

def is_snapshot_session(value):
    try:
        obj,dates=_snapshot()
        if not isinstance(value,str) or value<obj['valid_from'] or value>obj['valid_until']:return None
        p=bisect.bisect_left(dates,value)
        return p<len(dates) and dates[p]==value
    except (OSError,ValueError,KeyError,TypeError):return None
