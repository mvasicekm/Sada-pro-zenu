"""Odhad technických parametrů dílu z titulku inzerátu.

Vrací tři údaje, které rozhodují o kompatibilitě skupiny:

``speeds``
    počet rychlostí vzadu (10 / 11 / 12),
``front``
    počet převodníků vpředu (1 / 2 / 3),
``brake``
    ``rim`` (ráfkové), ``disc`` (kotoučové blíže neurčené),
    ``disc-hydraulic`` nebo ``disc-mechanical``.

Každý údaj může být ``None`` a to je **důležité**: neznámý parametr se nikdy
nehádá, protože na jeho základě pak stránka hlásí konflikty. Když dva nálezy
tvrdí u stejného parametru každý něco jiného (třeba titulek s ``BR-R7000``
i ``BR-R7070``), vrátí se radši ``None`` než jedna ze dvou možností.

Hlavní zdroj je tabulka kódů modelů v ``config.json`` → ``specs``; doplňkově
se čte text titulku. Pozor na dvě pasti, které tu řeší zvláštní kód:

* ``11-28`` a ``10-51`` jsou rozsahy pastorků, ne počty rychlostí,
* ``105`` a ``5800 Kč`` obsahují čísla, která vypadají jako údaje o modelu.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Sequence, Set

from categories import compact, normalize

# Rozumné rozsahy - cokoliv mimo ně je špatně přečtené číslo z titulku.
MIN_SPEEDS = 7
MAX_SPEEDS = 13
MAX_FRONT = 3

DEFAULT_COMPONENT_PREFIXES = ("st", "rd", "fd", "fc", "cs", "br", "bl", "sl", "cn")
DEFAULT_BRAKE_PREFIXES = ("st", "br", "bl")

# "2x11", "1×12" - vpředu i vzadu zároveň.
RE_FRONT_BY_REAR = re.compile(r"(?<!\d)(\d)\s*[x\u00d7]\s*(\d{1,2})(?!\d)")
# "11s", "11 sp", "12 rychlosti" - za číslem musí být písmeno, takže
# rozsahy pastorků jako "11-28" sem nespadnou.
RE_SPEEDS = re.compile(r"(?<!\d)(\d{1,2})\s*(?:s|sp|speed|rychl\w*)(?![a-z0-9])")
# Samotné "1x" / "2x" bez počtu rychlostí.
RE_FRONT = re.compile(r"(?<!\d)(\d)\s*[x\u00d7](?![a-z0-9])")

BRAKE_WORDS = (
    ("disc-hydraulic", ("hydraul",)),
    ("disc-mechanical", ("mechanick\\w* kotouc", "kotoucov\\w* mechanick")),
    ("rim", ("rafkov", "u-brake", "cantilever")),
    ("disc", ("kotouc", "disc", "disk")),
)

# Druh dílu. Kategorie z config.json nestačí: páky i třmeny spadají do
# "Páky / Brzdy", ale na rám s flat mount kotoučem lze jedno a druhé ne.
# Předpona kódu je spolehlivější než české slovo, proto se zkouší dřív.
PART_PREFIXES = (
    ("lever", "st"),     # dual control páka (řazení + brzda)
    ("brake", "br"),     # brzdový třmen
    ("brake-lever", "bl"),
    ("rear-derailleur", "rd"),
    ("front-derailleur", "fd"),
    ("crank", "fc"),
    ("cassette", "cs"),
    ("chain", "cn"),
)

PART_WORDS = (
    ("rear-derailleur", ("prehazovack",)),
    ("front-derailleur", ("presmykac",)),
    ("cassette", ("kazet", "pastork")),
    ("crank", ("klik", "prevodnik")),
    ("chain", ("retez",)),
    ("brake", ("trmen", "brzdov\\w* trmen", "caliper")),
    ("lever", ("pak\\w*", "dual\\w*", "sti", "radic")),
)

RE_ELECTRONIC = re.compile(r"\bdi2\b|\bepsp?\b|elektronick")

# Rozsah kazety "11-34" a velikosti převodníků "48/32". Čte se ze syrového
# titulku, protože normalize() mění pomlčku na mezeru a rozsah by zanikl.
RE_COGS = re.compile(r"(?<!\d)(\d{2})\s*[-\u2013]\s*(\d{2})(?!\d)")
RE_CHAINRINGS = re.compile(r"(?<!\d)(\d{2})\s*/\s*(\d{2})(?!\d)")

# Meze, za kterými už to není kazeta ani převodníky, ale jiné číslo z titulku.
COGS_MIN_RANGE = (9, 13)
COGS_MAX_RANGE = (23, 53)
CHAINRING_BIG_RANGE = (38, 56)
CHAINRING_SMALL_RANGE = (26, 46)


def _single(values: Set) -> Optional[object]:
    """Vrátí hodnotu jen při jednoznačné shodě, jinak None."""
    return values.pop() if len(values) == 1 else None


def _agree(first, second):
    """Spojí dva odhady. Když si protiřečí, je výsledek neznámý."""
    if first is None:
        return second
    if second is None:
        return first
    if first == second:
        return first
    # "disc" je obecnější tvar "disc-hydraulic" / "disc-mechanical",
    # takže to není rozpor, jen jedno z toho ví víc.
    pair = {str(first), str(second)}
    if "disc" in pair:
        other = (pair - {"disc"}).pop()
        if other.startswith("disc"):
            return other
    return None


def _code_present(code: str, title_compact: str, prefixes: Sequence[str],
                  require_prefix: bool = False) -> bool:
    """Je kód modelu v titulku? Hledá se v compact tvaru, takže
    ``ST-R7000``, ``ST R7000`` i ``str7000`` vyjdou nastejno.

    ``require_prefix`` vynutí, aby před kódem stála předpona dílu. To je
    nutné u brzd: ``CS-R7000`` je kazeta, ne ráfková brzda, takže typ brzdy
    smí určit jen ``BR-``/``ST-``/``BL-R7000``. Stejně se chrání čistě
    číselné kódy, které by jinak trefila cena v titulku - "5800 Kč" není
    jedenáctirychlostní skupina.
    """
    code_compact = compact(code)
    if not code_compact:
        return False
    numeric = not any(ch.isalpha() for ch in code_compact)
    if require_prefix or numeric:
        return any(prefix + code_compact in title_compact for prefix in prefixes)
    return code_compact in title_compact


def _from_models(table: Dict, title_compact: str, prefixes: Sequence[str],
                 cast=int, require_prefix: bool = False) -> Optional[object]:
    found = set()
    for value, codes in (table or {}).items():
        for code in codes or ():
            if _code_present(code, title_compact, prefixes, require_prefix):
                found.add(cast(value))
                break
    return _single(found)


def _speeds_from_text(title_norm: str) -> Optional[int]:
    found = set()
    for match in RE_FRONT_BY_REAR.finditer(title_norm):
        value = int(match.group(2))
        if MIN_SPEEDS <= value <= MAX_SPEEDS:
            found.add(value)
    for match in RE_SPEEDS.finditer(title_norm):
        value = int(match.group(1))
        if MIN_SPEEDS <= value <= MAX_SPEEDS:
            found.add(value)
    return _single(found)


def _front_from_text(title_norm: str) -> Optional[int]:
    found = set()
    for match in RE_FRONT_BY_REAR.finditer(title_norm):
        value = int(match.group(1))
        if 1 <= value <= MAX_FRONT:
            found.add(value)
    for match in RE_FRONT.finditer(title_norm):
        value = int(match.group(1))
        if 1 <= value <= MAX_FRONT:
            found.add(value)
    return _single(found)


def _pair_from_raw(raw: str, pattern, first_range, second_range, biggest_first):
    """Vytáhne dvojici čísel (rozsah kazety / převodníky) ze syrového titulku."""
    found = set()
    for match in pattern.finditer(raw.lower()):
        first, second = int(match.group(1)), int(match.group(2))
        big, small = (first, second) if biggest_first else (second, first)
        if not (first_range[0] <= big <= first_range[1]):
            continue
        if not (second_range[0] <= small <= second_range[1]):
            continue
        if big <= small:
            continue
        found.add((big, small))
    return _single(found)


def _cogs_from_raw(raw: str):
    """Vrátí ``(nejmensi, nejvetsi)`` pastorek, např. ``(11, 34)``."""
    pair = _pair_from_raw(raw, RE_COGS, COGS_MAX_RANGE, COGS_MIN_RANGE, False)
    return (pair[1], pair[0]) if pair else None


def _chainrings_from_raw(raw: str):
    """Vrátí ``(velky, maly)`` převodník, např. ``(48, 32)``."""
    return _pair_from_raw(raw, RE_CHAINRINGS, CHAINRING_BIG_RANGE,
                          CHAINRING_SMALL_RANGE, True)


def _part_from_title(title_norm: str, title_compact: str) -> Optional[str]:
    """Druh dílu podle předpony kódu, jinak podle českého názvu.

    Předpona musí začínat slovo, jinak by "rychlo*sti 11*-32" vypadalo jako
    kód páky ST-1132. Když titulek nabízí víc dílů najednou ("páky +
    přehazovačka"), vrací se None - taková nabídka se do jedné škatulky
    stejně nevejde.
    """
    found = set()
    for part, prefix in PART_PREFIXES:
        if re.search(r"\b" + prefix + r"[\s-]*[a-z]{0,2}\s*\d{3,4}", title_norm):
            found.add(part)
    if found:
        return _single(found)

    for part, patterns in PART_WORDS:
        for pattern in patterns:
            if re.search(r"\b" + pattern, title_norm):
                found.add(part)
                break
    return _single(found)


def _brake_from_text(title_norm: str) -> Optional[str]:
    for brake, patterns in BRAKE_WORDS:
        for pattern in patterns:
            if re.search(pattern, title_norm):
                return brake
    return None


def extract(title: str, specs_cfg: Optional[Dict] = None) -> Dict:
    """Vrátí ``{"speeds": .., "front": .., "brake": ..}``, klidně s None."""
    specs_cfg = specs_cfg or {}
    title_norm = normalize(title)
    title_compact = compact(title)

    component_prefixes = specs_cfg.get("component_prefixes") or DEFAULT_COMPONENT_PREFIXES
    brake_prefixes = specs_cfg.get("brake_prefixes") or DEFAULT_BRAKE_PREFIXES

    # Kód modelu a text titulku se musí shodnout. Když si protiřečí - třeba
    # "FC-RX810/820, 11/12 s", což je kus pasující na obojí - je výsledek
    # radši neznámý, než aby se stránka chytila jedné z možností.
    speeds = _agree(
        _from_models(specs_cfg.get("speed_models"), title_compact,
                     component_prefixes),
        _speeds_from_text(title_norm),
    )
    front = _agree(
        _from_models(specs_cfg.get("front_models"), title_compact,
                     component_prefixes),
        _front_from_text(title_norm),
    )
    brake = _agree(
        _from_models(specs_cfg.get("brake_models"), title_compact,
                     brake_prefixes, cast=str, require_prefix=True),
        _brake_from_text(title_norm),
    )

    # Kód modelu se hodí i ven - podle něj se pozná limit největšího
    # pastorku u přehazovačky.
    model = None
    for codes in (specs_cfg.get("speed_models") or {}).values():
        for code in codes or ():
            if _code_present(code, title_compact, component_prefixes):
                model = compact(code)
                break
        if model:
            break

    cogs = _cogs_from_raw(title)
    chainrings = _chainrings_from_raw(title)
    if chainrings and front is None:
        front = 2

    return {
        "speeds": speeds,
        "front": front,
        "brake": brake,
        "part": _part_from_title(title_norm, title_compact),
        "model": model,
        "electronic": bool(RE_ELECTRONIC.search(title_norm)) or None,
        "cog_min": cogs[0] if cogs else None,
        "cog_max": cogs[1] if cogs else None,
        "chainring_big": chainrings[0] if chainrings else None,
        "chainring_small": chainrings[1] if chainrings else None,
    }
