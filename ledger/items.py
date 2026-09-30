"""Item database: canonical item list + fuzzy name matching.

The canonical list comes from tools/dump_items.py, which extracts FULL names
from every plugin in the Daedric Online install's Data folder. The JSON shape:

    {
      "items": [
        {"name": "Daedric Greatsword", "type": "WEAP", "value": 2500,
         "editor_id": "DaedricGreatsword", "plugin": "Skyrim.esm"},
        ...
      ]
    }

Matching strategy:
  1. Exact (case-insensitive) match.
  2. Alias match (common shorthand: "clay" -> claymore equivalents, etc.).
  3. rapidfuzz token_set_ratio against all names, with a minimum score.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz, process

# Minimum fuzzy score to accept a match (0-100).
# Trade chat shorthand is sloppy; 75 catches "daedric greatsword" from
# "daedric gs" via aliases, but keeps "iron dagger" from matching "iron sword".
MIN_SCORE = 75

# Community shorthand -> substring that should appear in the canonical name.
# Extend as the server's slang becomes known.
ALIASES: dict[str, str] = {
    "gs": "greatsword",
    "great sword": "greatsword",
    "1h": "",          # ambiguous, handled by heuristics later
    "2h": "",
    "daed": "daedric",
    "ebon": "ebony",
    "dwemer": "dwarven",
    "dwarvern": "dwarven",
    "sweetroll": "sweet roll",
    "armour": "armor",
    "bow of": "bow",   # noop guard, keeps tokenizer sane
}

# Types worth trading; everything else is dumped but deprioritized in ties.
TRADE_TYPES = {"WEAP", "ARMO", "MISC", "INGR", "ALCH", "SLGM", "SCRL", "AMMO", "BOOK", "KEYM", "LIGH"}


@dataclass
class Item:
    name: str
    type: str
    value: int = 0
    editor_id: str = ""
    plugin: str = ""

    @property
    def norm(self) -> str:
        return normalize(self.name)


@dataclass
class MatchResult:
    item: Item | None
    score: float = 0.0
    candidates: list[tuple[str, float]] = field(default_factory=list)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace, apply aliases."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = [ALIASES.get(w, w) for w in text.split()]
    return " ".join(w for w in words if w)


class ItemDB:
    def __init__(self, items: list[Item]):
        self.items = items
        self._by_norm: dict[str, Item] = {}
        self._choices: list[str] = []
        for it in items:
            n = it.norm
            if n and n not in self._by_norm:
                self._by_norm[n] = it
                self._choices.append(n)

    @classmethod
    def load(cls, path: str | Path) -> "ItemDB":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Item(**raw) for raw in data["items"]])

    def match(self, query: str) -> MatchResult:
        """Resolve free-text item name to a canonical Item."""
        q = normalize(query)
        if not q:
            return MatchResult(None)

        # 1. exact
        if q in self._by_norm:
            return MatchResult(self._by_norm[q], 100.0)

        # 2. alias-expanded exact
        # (normalize already applied aliases; try substring containment both ways)
        contains = [n for n in self._choices if q in n]
        if len(contains) == 1:
            return MatchResult(self._by_norm[contains[0]], 95.0)

        # 3. fuzzy
        results = process.extract(q, self._choices, scorer=fuzz.token_set_ratio, limit=5)
        if not results:
            return MatchResult(None)
        best, score, _ = results[0]
        candidates = [(name, sc) for name, sc, _ in results]
        if score >= MIN_SCORE:
            return MatchResult(self._by_norm[best], score, candidates)
        return MatchResult(None, score, candidates)

    def __len__(self) -> int:
        return len(self.items)
