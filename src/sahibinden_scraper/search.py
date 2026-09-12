"""Parsing a search-results page, and building the paginated URLs to fetch."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import Tag

from .extract import Extractor, node_text, parse_html
from .models import Listing
from .textnorm import collapse_ws, parse_int, parse_price, parse_tr_date, tr_key

__all__ = [
    "SearchPage",
    "parse_search_page",
    "build_page_url",
    "normalise_search_url",
    "detect_page_kind",
]


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #

def normalise_search_url(url: str, config: "dict[str, Any]") -> str:
    """Apply the forced query params (view mode, sort order) to the user's URL.

    View mode is sticky server-side per session, so it is *forced* rather than
    stripped: removing the parameter would leave a previously chosen Galeri view in
    effect and the results table would never render.
    """
    pagination = config.get("pagination") or {}
    forced = pagination.get("force_params") or {}
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in forced.items():
        params[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def build_page_url(url: str, offset: int, config: "dict[str, Any]") -> str:
    """The same search URL at item offset ``offset``."""
    pagination = config.get("pagination") or {}
    size_param = pagination.get("size_param", "pagingSize")
    offset_param = pagination.get("offset_param", "pagingOffset")
    page_size = int(pagination.get("page_size", 50))

    parsed = urlparse(normalise_search_url(url, config))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params[size_param] = str(page_size)
    if offset:
        params[offset_param] = str(offset)
    else:
        params.pop(offset_param, None)
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def detect_page_kind(html: str, config: "dict[str, Any]") -> str:
    """Classify a fetched page: ``search`` | ``detail`` | ``gone`` | ``login`` |
    ``interstitial`` | ``unknown``.

    Order matters. A removed listing returns HTTP 200 with none of the expected
    containers, which is exactly what a block looks like — without the ``gone``
    check the scraper would stop and wait for a human to rescue a dead ad.
    """
    guards = config.get("guards") or {}
    soup = parse_html(html)

    # tr_key, not str.lower(): sahibinden's removed-listing page is headed
    # "İlan bulunamadı", and "İ".lower() is TWO codepoints (i + combining dot), so a
    # str.lower() comparison against the marker "ilan bulunamadı" never matches and
    # a dead listing gets misread as a block — the scraper would then sit waiting
    # for a human to rescue an ad that no longer exists.
    folded = tr_key(node_text(soup.body or soup, separator=" ")[:6000])

    def _any_selector(selectors: Iterable[str]) -> bool:
        for selector in selectors or ():
            try:
                if soup.select_one(selector):
                    return True
            except Exception:
                continue
        return False

    def _any_marker(markers: Iterable[str]) -> bool:
        return any(tr_key(marker) in folded for marker in markers or () if marker)

    if _any_selector(guards.get("search_ready")):
        return "search"
    if _any_selector(guards.get("detail_ready")):
        return "detail"
    if _any_marker(guards.get("gone_text")):
        return "gone"
    if _any_marker(guards.get("interstitial_text")):
        return "interstitial"
    if len(html) < int(guards.get("min_body_bytes", 40000)):
        return "interstitial"
    if _any_marker(guards.get("login_text")):
        return "login"
    return "unknown"


# --------------------------------------------------------------------------- #
# Page parsing
# --------------------------------------------------------------------------- #

class SearchPage:
    """One parsed results page."""

    def __init__(self) -> None:
        self.listings: "list[Listing]" = []
        self.total_count: Optional[int] = None
        self.has_next: bool = False
        self.row_count: int = 0
        self.skipped_promo: int = 0
        self.warnings: "list[str]" = []
        self.coverage: "dict[str, Any]" = {}


def _row_classes(row: Tag) -> "set[str]":
    """Class tokens of a row. The raw HTML has trailing whitespace in the class
    attribute, so this always token-matches and never string-compares."""
    raw = row.get("class") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {c.strip() for c in raw if c and c.strip()}


def _is_promo(row: Tag, config: "dict[str, Any]") -> bool:
    search = config.get("search") or {}
    classes = _row_classes(row)
    if classes & set(search.get("skip_row_classes") or ()):
        return True
    needles = search.get("skip_row_class_contains") or ()
    return any(needle in cls for cls in classes for needle in needles)


_ASSUMED_ATTRIBUTE_ORDER = ["year", "km", "color"]


def _header_order(soup: Tag, extractor: Extractor, config: "dict[str, Any]") -> "list[str]":
    """The order the ``<thead>`` claims for the yıl/km/renk family.

    **Advisory only.** A header index cannot be used to read these cells: it counts
    every column while the attribute family holds only three, and it cannot be
    translated through the row's full ``<td>`` list either, because the thumbnail
    column does not always carry a matching ``<th>``. What the header *can* do is
    tell us the assumed family order no longer holds — which deserves a warning
    rather than a silently misread column.
    """
    header_cfg = (config.get("search") or {}).get("header") or {}
    aliases = header_cfg.get("aliases") or {}
    cells = extractor.nodes(soup, "search", "header")
    if not cells:
        return []
    order: "list[str]" = []
    for cell in cells:
        key = tr_key(node_text(cell))
        if not key:
            continue
        for field_name in _ASSUMED_ATTRIBUTE_ORDER:
            if field_name in order:
                continue
            if any(key == tr_key(n) for n in aliases.get(field_name) or ()):
                order.append(field_name)
    return order


# A model year outside this window is a misread column, not a car. Bounded loosely
# so classics still parse; the point is to reject a kilometre reading.
_YEAR_MIN, _YEAR_MAX = 1900, 2100


def _sane_year(value: Optional[int]) -> Optional[int]:
    """Reject a "year" that is obviously a kilometre or price that slipped column."""
    if value is None:
        return None
    return value if _YEAR_MIN <= value <= _YEAR_MAX else None


def _split_location(raw: str) -> "tuple[Optional[str], Optional[str]]":
    """``"İstanbul\\nKadıköy"`` -> ``("İstanbul", "Kadıköy")``."""
    text = collapse_ws(raw.replace("\n", "|"))
    parts = [p.strip() for p in re.split(r"[|/]", text) if p.strip()]
    if not parts:
        return None, None
    if len(parts) == 1:
        # Some rows render "İstanbul Kadıköy" with no separator at all.
        words = parts[0].split()
        if len(words) >= 2:
            return words[0], " ".join(words[1:])
        return parts[0], None
    return parts[0], parts[1]


def parse_search_page(
    html: str,
    config: "dict[str, Any]",
    *,
    now: date,
    base_url: Optional[str] = None,
) -> SearchPage:
    """Turn one results page into stub :class:`Listing` records.

    These stubs carry only what the table shows. Everything technical comes from
    the detail page; ``detail_fetched`` says which you are looking at.
    """
    page = SearchPage()
    soup = parse_html(html)
    extractor = Extractor(config)
    base = base_url or config.get("base_url") or "https://www.sahibinden.com"
    search_cfg = config.get("search") or {}

    total_raw = extractor.one(soup, "search", "total_count")
    page.total_count = parse_int(total_raw)

    page.has_next = bool(extractor.one(soup, "search", "next_page"))

    header_order = _header_order(soup, extractor, config)
    if header_order and header_order != _ASSUMED_ATTRIBUTE_ORDER:
        page.warnings.append(
            "sütun sırası beklenenden farklı: " + " / ".join(header_order)
            + " (yıl/km/renk varsayıldı, değerler kaymış olabilir)"
        )
    rows = extractor.nodes(soup, "search", "row")
    id_attr = search_cfg.get("row_id_attr", "data-id")
    store_attr = search_cfg.get("row_store_attr", "data-store_name")

    for row in rows:
        if _is_promo(row, config):
            page.skipped_promo += 1
            continue
        page.row_count += 1
        listing = _parse_row(
            row,
            extractor,
            config,
            base=base,
            now=now,
            id_attr=id_attr,
            store_attr=store_attr,
        )
        if listing.id or listing.url:
            page.listings.append(listing)
        else:
            page.warnings.append("kimliksiz satır atlandı")

    page.coverage = {"found": sorted(set(extractor.hits)), "missing": sorted(set(extractor.misses))}
    if not rows:
        page.warnings.append("hiç ilan satırı bulunamadı - görünüm modu veya seçiciler değişmiş olabilir")
    return page


def _parse_row(
    row: Tag,
    extractor: Extractor,
    config: "dict[str, Any]",
    *,
    base: str,
    now: date,
    id_attr: str,
    store_attr: str,
) -> Listing:
    listing = Listing()

    raw_id = row.get(id_attr)
    if isinstance(raw_id, (list, tuple)):
        raw_id = raw_id[0] if raw_id else None
    listing.id = collapse_ws(raw_id) or None

    href = extractor.one(row, "search", "fields", "url")
    if href:
        listing.url = urljoin(base, href)
    if not listing.id:
        listing.id = extractor.one(row, "search", "fields", "id")

    listing.title = extractor.one(row, "search", "fields", "title")

    price_raw = extractor.one(row, "search", "fields", "price")
    listing.price, listing.currency = parse_price(price_raw)
    if listing.currency == "TRY" and listing.price is not None:
        # Set here too, not only on the detail path: a --fast run has no detail
        # pages, and price_try is the field everything downstream sorts and ranks
        # on. Leaving it None would drop every listing out of its cohort stats.
        listing.price_try = listing.price
        listing.price_try_source = "native"

    # These are searchResultsAttributeValue cells — NOT the TagAttributeValue cells,
    # which hold marka/seri/model. See the config comment; the two are four
    # characters apart and swapping them produces plausible garbage.
    attribute_cells = extractor.many(row, "search", "fields", "attribute_cells")
    taxonomy_cells = extractor.many(row, "search", "fields", "taxonomy_cells")

    # The ONLY reliable invariant here is the order *within* the attribute family:
    # yıl, km, renk. A <thead> index cannot be used to subscript this list, because
    # the index counts every column while the list holds only three — and it cannot
    # be translated through the row's full <td> list either, because the thumbnail
    # column does not always carry a matching <th>, so the two are off by one.
    # Getting this wrong is silent: the KM value lands in `year` and an 8-year-old
    # car is reported as model year 96000. The header is therefore used only to
    # detect that the assumed order no longer holds, never to read a value.
    def _family(index: int) -> Optional[str]:
        return attribute_cells[index] if 0 <= index < len(attribute_cells) else None

    listing.year = _sane_year(parse_int(_family(0)))
    listing.km = parse_int(_family(1))
    listing.color = _family(2)

    if listing.year is None and attribute_cells:
        # Last resort: whichever cell actually looks like a model year.
        for index, cell in enumerate(attribute_cells):
            candidate = _sane_year(parse_int(cell))
            if candidate is not None:
                listing.year = candidate
                if index != 0:
                    listing.parse_warnings.append(
                        f"yıl sütunu beklenen konumda değil (index {index})"
                    )
                break

    listing.thumbnail_url = extractor.one(row, "search", "fields", "thumbnail_url")
    if listing.thumbnail_url:
        listing.thumbnail_url = urljoin(base, listing.thumbnail_url)

    # Taxonomy cell count varies with how narrow the filter already is, so nothing
    # here is positional beyond a best effort; the detail page is authoritative.
    if taxonomy_cells:
        listing.brand = taxonomy_cells[0] if len(taxonomy_cells) > 0 else None
        listing.series = taxonomy_cells[1] if len(taxonomy_cells) > 1 else None
        listing.model = taxonomy_cells[2] if len(taxonomy_cells) > 2 else None

    listing.listed_date = parse_tr_date(
        extractor.one(row, "search", "fields", "listed_date"), now=now
    )

    location_raw = extractor.one(row, "search", "fields", "location")
    if location_raw is None:
        node = row.select_one("td.searchResultsLocationValue, td[class*='LocationValue']")
        location_raw = node_text(node, separator="\n") if node else None
    if location_raw:
        listing.city, listing.district = _split_location(location_raw)

    store_name = row.get(store_attr)
    if isinstance(store_name, (list, tuple)):
        store_name = store_name[0] if store_name else None
    if store_name:
        listing.seller_name = collapse_ws(store_name) or None
        # Presence of the attribute is an inference about dealer-ness, so it only
        # ever seeds the field; the detail page's "Kimden" label overrides it.
        listing.is_dealer = True

    listing.detail_fetched = False
    return listing
