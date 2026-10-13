"""営業日ユーティリティ（土日＋日本の祝日を除外）

- jpholiday があれば祝日も除外、無ければ土日のみ除外に自動フォールバック（fail-open）
- エージェントは JST の Mac で動く前提なので naive datetime = JST として扱う
"""

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

try:
    import jpholiday  # type: ignore

    _HAS_JPHOLIDAY = True
except ImportError:  # 未インストールでも土日判定だけで動かす
    jpholiday = None
    _HAS_JPHOLIDAY = False
    logger.warning("jpholiday not installed — 営業日判定は土日のみ除外で動作します")


def is_business_day(d: date) -> bool:
    """土日・日本の祝日でなければ True"""
    if d.weekday() >= 5:  # 5=土, 6=日
        return False
    if _HAS_JPHOLIDAY and jpholiday.is_holiday(d):
        return False
    return True


def business_days_elapsed(start: datetime, now: datetime) -> int:
    """start から now までに「丸ごと経過した営業日」の数を返す。

    start 当日と now 当日はどちらも数えない（部分日を経過扱いしないため）。
    例: 木曜にメンション → 金曜(1営業日) → 土日スキップ → 月曜10時時点では 1、
        火曜10時時点で 2（金＋月）。
    """
    days = 0
    cur = start.date() + timedelta(days=1)
    end = now.date()  # 当日は含めない
    while cur < end:
        if is_business_day(cur):
            days += 1
        cur += timedelta(days=1)
    return days
