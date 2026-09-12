"""Turkish-aware text, number and date normalisation.

This module is deliberately *pure*: no I/O, no network, no ambient clock. Relative
dates ("Bugun", "Dun") are resolved against a ``now`` the caller injects, so parsing
stays deterministic and unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

try:
    from zoneinfo import ZoneInfo

    ISTANBUL = ZoneInfo("Europe/Istanbul")
except Exception:  # pragma: no cover - Windows ships no tz database
    # Windows has no system tz database, so zoneinfo needs the `tzdata` package.
    # Falling back to the machine clock is a real correctness risk here — a run on
    # a machine set to any other timezone shifts every "Bugün"/"Dün" by a day — so
    # it warns instead of degrading quietly.
    ISTANBUL = None

__all__ = [
    "tr_key",
    "tr_fold",
    "collapse_ws",
    "label_matches",
    "parse_price",
    "parse_int",
    "parse_float",
    "parse_bucket",
    "parse_tr_date",
    "istanbul_today",
    "istanbul_now_iso",
    "canon_fuel",
    "canon_transmission",
    "canon_seller",
    "canon_damage_state",
    "canon_yes_no",
]


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

# Turkish casefolding is a minefield: "I".lower() yields "i̇" (i + COMBINING
# DOT ABOVE) and "I".lower() yields "i" where Turkish wants a dotless one. Rather
# than fight locale-dependent casing we fold everything to bare ASCII and match on
# that, so label lookups never depend on the process locale.
_TR_FOLD = {
    "ı": "i",  # dotless i
    "İ": "i",  # dotted capital I
    "I": "i",
    "i": "i",
    "ş": "s",  # s-cedilla
    "Ş": "s",
    "ğ": "g",  # g-breve
    "Ğ": "g",
    "ü": "u",
    "Ü": "u",
    "ö": "o",
    "Ö": "o",
    "ç": "c",
    "Ç": "c",
    "â": "a",
    "Â": "a",
    "î": "i",
    "Î": "i",
    "û": "u",
    "Û": "u",
}
_TR_TABLE = str.maketrans(_TR_FOLD)

# Combining marks left over when input arrives already decomposed.
_COMBINING = re.compile(r"[̀-ͯ]")

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def collapse_ws(value: Optional[str]) -> str:
    """Trim and collapse every whitespace run (incl. NBSP) to a single space."""
    if not value:
        return ""
    return _WS.sub(" ", str(value).replace("\xa0", " ")).strip()


def tr_key(value: Optional[str]) -> str:
    """Fold a Turkish label to a stable ASCII match key.

    >>> tr_key("Motor Gücü")
    'motor gucu'
    >>> tr_key("Ağır Hasar Kayıtlı:")
    'agir hasar kayitli'
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = text.translate(_TR_TABLE)
    text = _COMBINING.sub("", unicodedata.normalize("NFD", text))
    text = _NON_ALNUM.sub(" ", text.lower())
    return text.strip()


def tr_fold(value: Optional[str]) -> str:
    """Lowercase Turkish prose to ASCII while keeping digits and punctuation.

    ``tr_key`` is for matching *labels*: it also strips punctuation, which would
    turn "45.000 TL" into "45 000 TL" and is wrong for free text. This keeps the
    text intact and only folds the letters, so a regex over seller prose can be
    written in plain ASCII and still match "İkinci anahtar" — which ``str.lower()``
    turns into "i̇kinci" (i + combining dot) and never matches.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFC", str(value)).translate(_TR_TABLE)
    text = _COMBINING.sub("", unicodedata.normalize("NFD", text))
    return text.lower()


def label_matches(label: str, aliases: Iterable[str]) -> bool:
    """True when ``label`` folds to the same key as any of ``aliases``."""
    key = tr_key(label)
    return any(key == tr_key(a) for a in aliases)


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #

_CURRENCY = [
    ("TRY", ("tl", "try", "₺", "turk lirasi")),
    ("USD", ("usd", "$", "dolar")),
    ("EUR", ("eur", "€", "euro")),
    ("GBP", ("gbp", "£", "sterlin")),
]

_NUM_RE = re.compile(r"-?\d[\d.,\s ]*")


def _digits_to_number(raw: str) -> Optional[float]:
    """Interpret a Turkish-formatted numeric string.

    Turkish groups thousands with "." and marks decimals with ",", but sahibinden is
    not consistent, so the separator role is inferred from whichever comes last.
    """
    s = raw.replace("\xa0", "").replace(" ", "").strip().rstrip(".,")
    if not s:
        return None
    neg = s.startswith("-")
    s = s.lstrip("-")
    if not any(ch.isdigit() for ch in s):
        return None

    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_comma > last_dot:
        # "1.250.000,50" -> decimal comma
        s = s.replace(".", "").replace(",", ".")
    elif last_dot > last_comma:
        tail = s[last_dot + 1 :]
        if len(tail) == 3:
            # "1.250.000" / "125.000" -> grouping dots only
            s = s.replace(".", "").replace(",", "")
        else:
            # "1.6" -> a genuine decimal point
            s = s.replace(",", "")
    else:
        s = s.replace(",", "").replace(".", "")

    try:
        value = float(s)
    except ValueError:
        return None
    return -value if neg else value


def parse_float(value: object) -> Optional[float]:
    """Extract the first number from a messy string. ``"1.598 cc" -> 1598.0``"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.search(str(value))
    return _digits_to_number(m.group(0)) if m else None


def parse_int(value: object) -> Optional[int]:
    """Extract the first number and round it. ``"125.000 km" -> 125000``"""
    f = parse_float(value)
    return int(round(f)) if f is not None else None


def parse_price(value: Optional[str]) -> "tuple[Optional[int], Optional[str]]":
    """Return ``(amount, currency_code)`` for a price string.

    >>> parse_price("1.250.000 TL")
    (1250000, 'TRY')
    """
    if not value:
        return None, None
    text = collapse_ws(value)
    lowered = text.lower()
    key = tr_key(text)
    currency = None
    for code, tokens in _CURRENCY:
        if any(tok in lowered or tok in key for tok in tokens):
            currency = code
            break
    amount = parse_int(text)
    if amount is None:
        return None, currency
    # No symbol rendered on a Turkish marketplace almost always means lira.
    return amount, currency or "TRY"


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

_TR_MONTHS = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}

_DOTTED = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})\s+([^\W\d_]+)(?:\s+(\d{4}))?", re.UNICODE)


def parse_tr_date(value: Optional[str], *, now: Optional[date]) -> Optional[str]:
    """Parse a Turkish listing date into an ISO ``YYYY-MM-DD`` string.

    Handles ``"12 Ocak 2026"``, ``"12 Ocak"`` (year inferred from *now*), ``"Bugun"``,
    ``"Dun"``, ``"12.01.2026"`` and ``"2026-01-12"``.

    ``now`` is keyword-only and has no default on purpose: "today" for a listing on
    sahibinden means today *in Istanbul*, and a run started at 01:30 TRT is on the
    previous UTC day. Forcing every caller to pass a date keeps that decision in one
    place instead of silently borrowing the machine's clock.
    """
    if not value:
        return None
    text = collapse_ws(value)
    key = tr_key(text)

    if key in {"bugun", "bu gun"}:
        return now.isoformat() if now else None
    if key in {"dun", "dune"}:
        return (now - timedelta(days=1)).isoformat() if now else None

    m = _ISO.search(text)
    if m:
        year, month, day = (int(g) for g in m.groups())
        return _safe_date(year, month, day)

    m = _DOTTED.search(text)
    if m:
        day, month, year = (int(g) for g in m.groups())
        if year < 100:
            year += 2000
        return _safe_date(year, month, day)

    m = _DAY_MONTH.search(text)
    if m:
        day = int(m.group(1))
        month = _TR_MONTHS.get(tr_key(m.group(2)))
        if month:
            if m.group(3):
                year = int(m.group(3))
            elif now:
                year = now.year
                # A day/month ahead of today belongs to last year.
                if (month, day) > (now.month, now.day):
                    year -= 1
            else:
                return None
            return _safe_date(year, month, day)
    return None


def _safe_date(year: int, month: int, day: int) -> Optional[str]:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


_TZ_WARNED = False


def _warn_missing_tz() -> None:
    """Say it once, loudly. A silently wrong clock is worse than a missing one."""
    global _TZ_WARNED
    if _TZ_WARNED:
        return
    _TZ_WARNED = True
    warnings.warn(
        "Europe/Istanbul saat dilimi yüklenemedi (Windows'ta zoneinfo için `tzdata` "
        "paketi gerekir). Makinenizin yerel saati kullanılacak; makine İstanbul "
        "saatinde değilse 'Bugün'/'Dün' tarihleri bir gün kayabilir. Düzeltmek için: "
        "pip install tzdata",
        RuntimeWarning,
        stacklevel=3,
    )


def istanbul_today() -> date:
    """Today in Europe/Istanbul — the only clock a listing date means anything in."""
    if ISTANBUL is not None:
        return datetime.now(ISTANBUL).date()
    _warn_missing_tz()
    return datetime.now().date()


def istanbul_now_iso() -> str:
    """Current Istanbul time as an ISO-8601 string including the offset."""
    if ISTANBUL is not None:
        return datetime.now(ISTANBUL).replace(microsecond=0).isoformat()
    _warn_missing_tz()
    return datetime.now().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# Bucketed specs
# --------------------------------------------------------------------------- #

_BUCKET = re.compile(
    r"(\d[\d.,]*)\s*(?:-|–|—|ile|to)\s*(\d[\d.,]*)",
    re.IGNORECASE,
)


def parse_bucket(value: Optional[str]) -> "tuple[Optional[int], Optional[int], bool]":
    """Parse a spec that sahibinden publishes as a *range*, not a number.

    "Motor Gücü" and "Motor Hacmi" are dropdown buckets: ``"101 - 125 hp"``,
    ``"1401 - 1600 cm3"``. Collapsing those to a midpoint invents precision the site
    never had and lets a model rank two cars that are provably indistinguishable, so
    the range is preserved and flagged.

    Returns ``(min, max, is_bucket)``. A plain number yields ``(n, n, False)``.

    >>> parse_bucket("101 - 125 hp")
    (101, 125, True)
    >>> parse_bucket("116 hp")
    (116, 116, False)
    """
    if value is None:
        return None, None, False
    text = collapse_ws(value)
    if not text:
        return None, None, False

    m = _BUCKET.search(text)
    if m:
        low = parse_int(m.group(1))
        high = parse_int(m.group(2))
        if low is not None and high is not None:
            if low > high:
                low, high = high, low
            return low, high, low != high

    # "1600 cm3 ve üzeri" / "50 hp'ye kadar" are open-ended buckets.
    number = parse_int(text)
    if number is None:
        return None, None, False
    key = tr_key(text)
    if "ve uzeri" in key or "uzeri" in key or "fazla" in key:
        return number, None, True
    if "kadar" in key or "alti" in key:
        return None, number, True
    return number, number, False


# --------------------------------------------------------------------------- #
# Controlled vocabularies
# --------------------------------------------------------------------------- #

def _lookup(value: Optional[str], table: "dict[str, str]") -> Optional[str]:
    """Exact key match first, then substring, else the cleaned original."""
    if not value:
        return None
    key = tr_key(value)
    if not key:
        return None
    if key in table:
        return table[key]
    for candidate, canon in table.items():
        if candidate in key:
            return canon
    return collapse_ws(value) or None


_FUEL = {
    "benzin lpg": "Benzin & LPG",
    "lpg benzin": "Benzin & LPG",
    "benzin gaz": "Benzin & LPG",
    "hibrit": "Hibrit",
    "hybrid": "Hibrit",
    "elektrikli": "Elektrik",
    "elektrik": "Elektrik",
    "dizel": "Dizel",
    "mazot": "Dizel",
    "benzin": "Benzin",
    "lpg": "LPG",
}

_TRANSMISSION = {
    "yari otomatik": "Yarı Otomatik",
    "yarim otomatik": "Yarı Otomatik",
    "triptronik": "Otomatik",
    "otomatik": "Otomatik",
    "manuel": "Manuel",
    "duz": "Manuel",
}

_SELLER = {
    "sahibinden": "Sahibinden",
    "galeriden": "Galeriden",
    "yetkili bayiden": "Yetkili Bayiden",
    "yetkili bayi": "Yetkili Bayiden",
}

_DAMAGE = {
    "lokal boyali": "lokal_boyali",
    "orijinal": "orijinal",
    "original": "orijinal",
    "boyanmis": "boyali",
    "boyali": "boyali",
    "degismis": "degismis",
    "degisen": "degismis",
}

_YES = {"var", "evet", "yes", "true", "1"}
_NO = {"yok", "hayir", "no", "false", "0"}


def canon_fuel(value: Optional[str]) -> Optional[str]:
    return _lookup(value, _FUEL)


def canon_transmission(value: Optional[str]) -> Optional[str]:
    return _lookup(value, _TRANSMISSION)


def canon_seller(value: Optional[str]) -> Optional[str]:
    return _lookup(value, _SELLER)


def canon_damage_state(value: Optional[str]) -> Optional[str]:
    return _lookup(value, _DAMAGE)


def canon_yes_no(value: object) -> Optional[bool]:
    """Map "Var"/"Yok", "Evet"/"Hayir" to a boolean; anything unknown -> ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    key = tr_key(str(value))
    if not key:
        return None
    if key in _YES:
        return True
    if key in _NO:
        return False
    return None
