"""Kategorizace inzerátů podle komponent a negativní filtry.

Cílem je poskládat sadu po dílech do celkového rozpočtu, takže každý
inzerát musí skončit u *správné* kategorie - jinak by se levný díl
poměřoval s cenovým stropem drahého dílu.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)


def normalize(text: Optional[str]) -> str:
    """Malá písmena bez diakritiky - 'Přesmykač' a 'presmykac' si odpovídají."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.lower().replace("-", " ").split())


def compact(text: Optional[str]) -> str:
    """Jen alfanumerické znaky - 'ST-R7000', 'ST R7000' i 'str7000' splynou."""
    return re.sub(r"[^a-z0-9]", "", normalize(text))


@dataclass
class Category:
    key: str
    label: str
    max_price: Optional[int]
    queries: List[str] = field(default_factory=list)
    match_any: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    is_bundle: bool = False

    # předpočítané normalizované tvary
    _match_norm: List[str] = field(default_factory=list, repr=False)
    _models_compact: List[str] = field(default_factory=list, repr=False)
    _exclude_norm: List[str] = field(default_factory=list, repr=False)

    @classmethod
    def from_dict(cls, data: Dict) -> "Category":
        category = cls(
            key=str(data["key"]),
            label=str(data.get("label", data["key"])),
            max_price=data.get("max_price"),
            queries=[str(q) for q in data.get("queries", [])],
            match_any=[str(m) for m in data.get("match_any", [])],
            models=[str(m) for m in data.get("models", [])],
            exclude=[str(e) for e in data.get("exclude", [])],
            is_bundle=bool(data.get("is_bundle", False)),
        )
        category._match_norm = [normalize(m) for m in category.match_any if normalize(m)]
        category._models_compact = [compact(m) for m in category.models if compact(m)]
        category._exclude_norm = [normalize(e) for e in category.exclude if normalize(e)]
        return category

    def matches(self, title_norm: str, title_compact: str) -> bool:
        if any(model in title_compact for model in self._models_compact):
            return True
        return any(term in title_norm for term in self._match_norm)

    def is_excluded(self, title_norm: str) -> bool:
        return any(term in title_norm for term in self._exclude_norm)

    @property
    def price_rank(self) -> int:
        """Pro výběr nejpřísnějšího limitu při více shodách."""
        return self.max_price if self.max_price is not None else 10 ** 9


class CategoryMatcher:
    """Přiřadí inzerát ke kategorii na základě titulku."""

    def __init__(self, categories: Sequence[Category], matching_cfg: Dict) -> None:
        self.categories = list(categories)
        self.by_key = {c.key: c for c in self.categories}
        self.bundle_category = next(
            (c for c in self.categories if c.is_bundle), None
        )
        self.component_categories = [c for c in self.categories if not c.is_bundle]

        self.bundle_min_matches = int(matching_cfg.get("bundle_min_matches", 3))
        self.fallback_to_query = bool(
            matching_cfg.get("fallback_to_query_category", True)
        )
        self._bundle_markers = [
            normalize(m) for m in matching_cfg.get("bundle_markers", []) if normalize(m)
        ]
        self._global_exclude = [
            normalize(e) for e in matching_cfg.get("global_exclude", []) if normalize(e)
        ]

        # Relevance: titulek musí zmiňovat 105 / GRX nebo některý kód modelu,
        # jinak jde o jiný díl (Ultegra, XTR, SRAM, ...).
        self._relevance_norm = [
            normalize(r) for r in matching_cfg.get("relevance_any", []) if normalize(r)
        ]
        # Čistě číselné termíny ("105") se v compact tvaru nehledají - jinak by
        # "10-51" (rozsah pastorků) falešně vyhovělo jako "105".
        self._relevance_compact = [
            compact(r) for r in self._relevance_norm if compact(r) and not r.isdigit()
        ]
        for category in self.categories:
            self._relevance_compact.extend(category._models_compact)

    # --- jednotlivé kroky (oddělené kvůli testovatelnosti) ---------------

    def is_globally_excluded(self, title_norm: str) -> bool:
        return any(term in title_norm for term in self._global_exclude)

    def is_relevant(self, title_norm: str, title_compact: str) -> bool:
        for term in self._relevance_norm:
            # "105" hledáme jako samostatné číslo, ať nechytáme "1050"
            if term.isdigit():
                if re.search(r"(?<!\d){}(?!\d)".format(re.escape(term)), title_norm):
                    return True
            elif term in title_norm:
                return True
        return any(model in title_compact for model in self._relevance_compact)

    def looks_like_bundle(self, title_norm: str, match_count: int) -> bool:
        if any(marker in title_norm for marker in self._bundle_markers):
            return True
        return match_count >= self.bundle_min_matches

    # --- hlavní API ------------------------------------------------------

    def classify(self, title: str, query_category: Optional[Category] = None) -> Optional[Category]:
        """Vrátí kategorii inzerátu, nebo ``None`` když ho máme zahodit."""
        title_norm = normalize(title)
        title_compact = compact(title)
        if not title_norm:
            return None

        if self.is_globally_excluded(title_norm):
            log.debug("Vyřazeno globálním filtrem: %s", title)
            return None

        if not self.is_relevant(title_norm, title_compact):
            log.debug("Vyřazeno (není 105/GRX ani známý model): %s", title)
            return None

        matched = [
            category
            for category in self.component_categories
            if category.matches(title_norm, title_compact)
            and not category.is_excluded(title_norm)
        ]

        if self.bundle_category is not None and self.looks_like_bundle(
            title_norm, len(matched)
        ):
            return self.bundle_category

        if matched:
            # Při více shodách vyhrává nejpřísnější strop. Tím se zabrání
            # tomu, aby se levný přesmykač poměřoval s limitem pro páky.
            return min(matched, key=lambda c: c.price_rank)

        if self.fallback_to_query and query_category is not None:
            if not query_category.is_excluded(title_norm):
                return query_category
        return None


def load_categories(config: Dict) -> List[Category]:
    raw = config.get("categories")
    if not raw:
        raise ValueError("config.json neobsahuje 'categories'")
    return [Category.from_dict(item) for item in raw]


def build_matcher(config: Dict) -> CategoryMatcher:
    return CategoryMatcher(load_categories(config), config.get("matching", {}))
