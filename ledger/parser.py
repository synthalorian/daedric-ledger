"""Trade message parser.

Turns messy trade-chat messages into structured TradeOffer records.

Handles the patterns seen in game trade channels:
    wts daedric greatsword 2.5k
    WTB 3x ebony ingot 150 each
    selling troll skull x10 50g ea
    buying grand soul gem, 200 per
    wts iron sword (clean) 500 obo
    wtb flawless ruby 1.2m   (for the big-ticket modded economy)

Not handled (by design, flagged as unparsed):
    "pm me offers", messages with no price, multi-item messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    SELL = "sell"   # poster is selling (WTS)
    BUY = "buy"     # poster is buying (WTB)


@dataclass
class TradeOffer:
    side: Side
    item_text: str        # raw item text, pre-resolution
    price: int            # total price in gold for `quantity` units
    quantity: int = 1
    per_unit: bool = False  # price was stated per unit ("each", "ea", "per")
    raw: str = ""          # original message

    @property
    def unit_price(self) -> float:
        if self.per_unit:
            return float(self.price)
        return self.price / max(self.quantity, 1)


# --- patterns ---------------------------------------------------------------

_SIDE_RE = re.compile(
    r"\b(?P<side>wts|wtb|wtt|selling|buying|sell|buy|lf|looking\s+for)\b",
    re.IGNORECASE,
)

# Price: "2.5k", "1500", "1.2m", "50g", "500 gold", "2k5" is NOT supported (ambiguous)
_PRICE_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*(?P<mult>k|m|mil|g|gold|gp)?\b",
    re.IGNORECASE,
)

_QTY_RE = re.compile(
    r"\b(?:x\s*(?P<q1>\d+)|(?P<q2>\d+)\s*x)\b",
    re.IGNORECASE,
)

_PER_UNIT_RE = re.compile(r"\b(?:each|ea|per|a\s+piece|apiece)\b", re.IGNORECASE)

# Words that end the item-name region when scanning left-to-right
_NOISE_WORDS = {
    "obo", "firm", "nego", "negotiable", "fast", "quick", "cheap", "pm", "me",
    "offer", "offers", "hmu", "dm", "pls", "plz", "please", "clean", "fresh",
    "for", "only", "urgent",
}

_SIDE_MAP = {
    "wts": Side.SELL, "selling": Side.SELL, "sell": Side.SELL,
    "wtb": Side.BUY, "buying": Side.BUY, "buy": Side.BUY,
    "lf": Side.BUY, "looking for": Side.BUY,
}


def _parse_price(num: str, mult: str | None) -> int | None:
    num = num.replace(",", ".")
    try:
        value = float(num)
    except ValueError:
        return None
    m = (mult or "").lower()
    if m == "k":
        value *= 1_000
    elif m in ("m", "mil"):
        value *= 1_000_000
    # g/gold/gp: face value
    if value <= 0 or value > 10_000_000_000:
        return None
    return int(round(value))


def parse_trade(message: str) -> TradeOffer | None:
    """Parse a single trade-chat message. Returns None if not a parseable trade."""
    raw = message.strip()
    if not raw or raw.startswith(("!", "/", "?")):
        return None

    side_m = _SIDE_RE.search(raw)
    if not side_m:
        return None
    side_word = side_m.group("side").lower()
    side_word = re.sub(r"\s+", " ", side_word)
    side = _SIDE_MAP.get(side_word)
    if side is None or side_word == "wtt":
        return None  # trades-for-trades have no price signal

    rest = raw[side_m.end():].strip()
    rest = rest.lstrip(":-").strip()
    if not rest:
        return None

    # quantity
    quantity = 1
    qty_m = _QTY_RE.search(rest)
    if qty_m:
        quantity = int(qty_m.group("q1") or qty_m.group("q2"))
        if quantity <= 0 or quantity > 100_000:
            quantity = 1

    # price: last numeric token wins ("wts x 500 obo 600" -> 600 is fishy but
    # last-wins is the least surprising behavior for "dropped to 450")
    price: int | None = None
    price_span: tuple[int, int] | None = None
    for pm in _PRICE_RE.finditer(rest):
        p = _parse_price(pm.group("num"), pm.group("mult"))
        if p is not None:
            price = p
            price_span = pm.span()

    per_unit = bool(_PER_UNIT_RE.search(rest))

    if price is None:
        return None  # "pm offers" — no signal

    # item text: everything before the price, minus side word, qty token, noise
    item_region = rest[: price_span[0]] if price_span else rest
    tokens = []
    for tok in re.split(r"[\s,;]+", item_region):
        tok_clean = tok.strip("()[]{}\"'")
        if not tok_clean:
            continue
        if _QTY_RE.fullmatch(tok_clean):
            continue
        if tok_clean.lower() in _NOISE_WORDS:
            break  # noise marks the end of the item name
        tokens.append(tok_clean)
    item_text = " ".join(tokens).strip()
    if not item_text:
        return None

    return TradeOffer(
        side=side,
        item_text=item_text,
        price=price,
        quantity=quantity,
        per_unit=per_unit,
        raw=raw,
    )
