"""Persistent candidate-lifecycle invariants for KRX/NXT operations.

This module is deliberately broker-agnostic. It never fetches quotes, guesses
trading dates, promotes a signal, or writes to the KIS/LS primary ledgers.

Contract:
 - A missing scan result is *not* a failed trading setup.
 - EXIT requires affirmative per-symbol source evaluation evidence.
 - DATA_HOLD is observation state, not an entry signal or invalidation.
 - Venue isolation and source timestamps are mandatory presentation metadata.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from datetime import datetime

VALID_VENUES = frozenset({'KRX', 'NXT'})
_REJECT_QUOTES = frozenset({'EXPIRED', 'REST_STALE', 'STALE'})
_EXCHANGE_CLOCKS = frozenset({'EXCHANGE_TIMESTAMP'})


@dataclass(frozen=True)
class EvaluationEvidence:
    market: str
    lane: str
    evaluated_codes: frozenset[str]
    source_complete: bool
    last_verified_close: str | None = None
    expected_close: str | None = None
    reason: str = 'PENDING_SOURCE_VERIFICATION'

    def __post_init__(self):
        if self.market not in VALID_VENUES:
            raise ValueError('UNKNOWN_MARKET')
        if self.market == 'NXT' and self.lane == 'RE_EXPLOSION_25':
            raise ValueError('RE25_KRX_ONLY')

    def can_expire(self, code: str, market: str, lane: str) -> bool:
        """Only a specifically evaluated security may exit. Never infer from 0 candidates."""
        return (self.source_complete is True and market == self.market and
                lane == self.lane and code in self.evaluated_codes)


def verified_scope(market: str, lane: str, evaluated: set[str] | frozenset[str],
                   *, source_complete: bool, last_close: str | None = None,
                   expected_close: str | None = None,
                   reason: str = 'PENDING_SOURCE_VERIFICATION') -> EvaluationEvidence:
    # For completed KRX daily-signal lanes, the last close date must agree
    # with the KIS-completed expected date before permitting an EXIT.
    if lane == 'RE_EXPLOSION_25':
        source_complete = (source_complete is True and bool(last_close) and
                           bool(expected_close) and last_close == expected_close)
    codes = frozenset(str(s) for s in evaluated if isinstance(s, str) and len(s) == 6)
    return EvaluationEvidence(market, lane, codes, source_complete,
                              last_verified_close=last_close,
                              expected_close=expected_close,reason=reason)


def data_hold(prior: Mapping[str, Any], *, at: str,
              reason: str = 'SOURCE_INCOMPLETE') -> dict:
    """Freeze the last verified candidate without incrementing miss counters.

    This is allowed to hide BUY eligibility, never to manufacture an EXIT.
    Recovery proof and genuine price invalidations are handled separately.
    """
    p = dict(prior)
    already_held = p.get('candidate_lifecycle') == 'DATA_HOLD'
    p['prior_verified_classification'] = p.get('prior_verified_classification') or p.get('classification')
    p['prior_verified_decision'] = p.get('prior_verified_decision') or p.get('decision')
    p['classification'] = 'WATCH'
    p['decision'] = 'PENDING'
    p['data_status'] = 'PENDING'
    p['candidate_lifecycle'] = 'HIDDEN_STALE' if p.get('candidate_stale') is True else 'DATA_HOLD'
    p['data_hold_since'] = p.get('data_hold_since') or at
    p['data_hold_reason'] = reason
    p['last_evaluated_at'] = p.get('last_evaluated_at') or p.get('candidate_seen_at')
    # Preserve authentic candidate_seen_at, last_seen_at, miss counters and
    # previous signal outcomes; a data outage is not a new observation.
    p['candidate_miss_count'] = int(p.get('candidate_miss_count') or 0)
    p['gate_pending'] = sorted(set(list(p.get('gate_pending') or []) + [reason]))
    return p


def quote_label(row: Mapping[str, Any]) -> str:
    """Display grade: exchange time != REST request time; never upgrade it."""
    if not isinstance(row, Mapping) or row.get('price') in (None, 'UNAVAILABLE', 'PENDING', ''):
        return '시세 미수신'
    quality = str(row.get('quote_quality') or '')
    tq = str(row.get('timestamp_quality') or '')
    grade = str(row.get('runtime_quote_grade') or '')
    if quality in _REJECT_QUOTES or grade == 'STALE':
        return '지연 시세 · 진입 검증 불가'
    if tq in _EXCHANGE_CLOCKS:
        return '거래소시각 확인 · 게이트 별도'
    return 'KIS REST 참고가 · 체결시각 미검증'


def empty_lane_message(market: str, lane: str, scan: Mapping[str, Any]) -> str:
    """0 qualified != 0 examined. Failure reasons are not silently hidden."""
    if market == 'KRX' and lane == 'RE_EXPLOSION_25':
        if scan.get('re25_universe_status') not in (
            'COMPLETE', 'COMPLETE_EVALUABLE_WITH_VERIFIED_EXCLUSIONS'):
            ready = scan.get('re25_evaluable_symbols', '미확인')
            total = scan.get('rows_scanned', '미확인')
            return f'DATA_PENDING · {ready}/{total}개 종목만 평가 검증. 기존 후보 이력 보존 · 진입 금지'
        return '검증 완료 · 조건 충족 재급등 후보 0건'
    if market == 'KRX' and lane == 'NXT_POST_ACCEL_LANE':
        return 'NXT는 상단 NXT 시장에서 별도 조회'
    if scan.get('status') not in ('READY', 'COMPLETE'):
        return 'DATA_PENDING · 완전한 판정 근거가 없어 신규 후보를 확정할 수 없음'
    return '검증 완료 · 현재 조건 충족 후보 0건'


def native_nxt_minute_proven(row: Mapping[str, Any], current: datetime,
                             max_age_seconds: int = 1200) -> bool:
    """Proof of a *recent native NXT minute-series re-evaluation*.

    `nxt_last_at` comes specifically from the native NXT `_series` path. That
    legacy field is KST-local ISO (sometimes without an offset). This exception
    applies ONLY to its native series timestamp, never to REST price timestamps.
    Require 60 or more recent minute observations covering 60+ actual minutes.
    """
    if not isinstance(row, Mapping) or current.tzinfo is None:
        return False
    try:
        from datetime import timezone, timedelta
        stamp = row.get('nxt_last_at')
        if not isinstance(stamp, str) or not stamp.strip():
            return False
        at = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone(timedelta(hours=9)))
        age = (current - at).total_seconds()
        if age < -5 or age > max_age_seconds:
            return False
        if str(row.get('venue') or '') != 'NXT':
            return False
        return (float(row.get('nxt_bar_count') or 0) >= 60 and
                float(row.get('nxt_span_minutes') or 0) >= 60 and
                float(row.get('nxt_recent60_count') or 0) >= 60)
    except (ValueError, TypeError, OverflowError):
        return False
