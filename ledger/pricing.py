"""Pricing: decay-weighted averages with outlier rejection.

Thin player-driven economies need honest stats:
  - exponential time decay (recent trades count more)
  - IQR outlier rejection (one troll listing can't crater the average)
  - sample-size reporting (an average of 2 listings is not gospel)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class PriceReport:
    item_name: str
    sell_avg: float | None      # weighted average asking price (WTS)
    buy_avg: float | None       # weighted average bid (WTB)
    midpoint: float | None      # best single "the price" number
    sell_count: int
    buy_count: int
    rejected_outliers: int
    oldest_days: float | None
    reliable: bool              # enough data to trust


def _iqr_filter(prices: list[float]) -> tuple[list[float], int]:
    """Drop outliers via the interquartile range fence. Small samples pass through."""
    if len(prices) < 4:
        return prices, 0
    s = sorted(prices)
    q1 = s[len(s) // 4]
    q3 = s[(3 * len(s)) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [p for p in prices if lo <= p <= hi]
    if not kept:  # pathological spread; keep everything rather than lie
        return prices, 0
    return kept, len(prices) - len(kept)


def _weighted_avg(rows: list[tuple[float, int]], half_life_days: float, now: float) -> float | None:
    """rows: [(unit_price, ts)]. Exponential decay by half-life."""
    if not rows:
        return None
    num = den = 0.0
    for price, ts in rows:
        age_days = max(0.0, (now - ts) / 86400.0)
        w = math.exp(-math.log(2) * age_days / half_life_days)
        num += price * w
        den += w
    return num / den if den else None


def compute_price_report(
    item_name: str,
    rows,  # sqlite rows: side, unit_price, quantity, ts, ...
    *,
    half_life_days: float = 14.0,
    min_sample: int = 3,
) -> PriceReport:
    now = time.time()
    sells = [(float(r["unit_price"]), int(r["ts"])) for r in rows if r["side"] == "sell"]
    buys = [(float(r["unit_price"]), int(r["ts"])) for r in rows if r["side"] == "buy"]

    sell_prices, rej_s = _iqr_filter([p for p, _ in sells])
    buy_prices, rej_b = _iqr_filter([p for p, _ in buys])
    sell_set = {(p, t) for p, t in sells if p in sell_prices}
    buy_set = {(p, t) for p, t in buys if p in buy_prices}

    sell_avg = _weighted_avg(list(sell_set), half_life_days, now)
    buy_avg = _weighted_avg(list(buy_set), half_life_days, now)

    if sell_avg is not None and buy_avg is not None:
        midpoint = (sell_avg + buy_avg) / 2
    else:
        midpoint = sell_avg if sell_avg is not None else buy_avg

    total = len(sells) + len(buys)
    oldest = min((t for _, t in sells + buys), default=None)
    oldest_days = (now - oldest) / 86400.0 if oldest else None

    return PriceReport(
        item_name=item_name,
        sell_avg=sell_avg,
        buy_avg=buy_avg,
        midpoint=midpoint,
        sell_count=len(sells),
        buy_count=len(buys),
        rejected_outliers=rej_s + rej_b,
        oldest_days=oldest_days,
        reliable=total >= min_sample,
    )
