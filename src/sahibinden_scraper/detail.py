"""Parsing one classified detail page into a fully populated :class:`Listing`."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import Tag

from .extract import Extractor, label_pairs, label_value, node_text, parse_html
from .models import DamageReport, Listing, TextFindings
from .textnorm import (
    canon_yes_no,
    collapse_ws,
    parse_bucket,
    parse_float,
    parse_int,
    parse_price,
    parse_tr_date,
    tr_fold,
    tr_key,
)

__all__ = ["parse_detail_page", "merge_listing", "mine_description"]


# --------------------------------------------------------------------------- #
# Class-token helpers
# --------------------------------------------------------------------------- #

def _class_tokens(node: Tag) -> "set[str]":
    raw = node.get("class") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {c.strip() for c in raw if c and c.strip()}


def _panel_state(tokens: "set[str]", state_tokens: "dict[str, list[str]]") -> Optional[str]:
    """Resolve a panel's damage state from its class tokens.

    THE TRAP: ``local-painted-new`` contains ``painted-new`` as a substring. A
    naive ``in`` test silently reclassifies every locally-painted panel as fully
    painted, which materially mis-prices the car and never raises. Two defences:
    whole-token set membership (not substring), and longest-token-first ordering so
    even a substring implementation would land on the right answer.
    """
    candidates: "list[tuple[int, str, str]]" = []
    for state, tokens_for_state in (state_tokens or {}).items():
        for token in tokens_for_state or ():
            candidates.append((len(token), token, state))
    for _, token, state in sorted(candidates, reverse=True):
        if token in tokens:
            return state
    return None


# --------------------------------------------------------------------------- #
# Free-text mining
# --------------------------------------------------------------------------- #

def mine_description(text: Optional[str], config: "dict[str, Any]") -> TextFindings:
    """Pull the things Turkish buyers care about out of the seller's prose.

    Everything produced here is an unverified seller claim. It is kept in its own
    block for exactly that reason.
    """
    findings = TextFindings()
    if not text:
        return findings
    patterns = config.get("text_mining") or {}
    # tr_fold, not str.lower(): "İkinci".lower() is "i" + COMBINING DOT ABOVE, so an
    # ASCII pattern never matches it and the finding is silently lost. Patterns in
    # the config are therefore written ASCII-folded too (the linter enforces it).
    haystack = tr_fold(text)

    def _search(name: str):
        pattern = patterns.get(name)
        if not pattern:
            return None
        try:
            return re.search(pattern, haystack, re.IGNORECASE | re.UNICODE)
        except re.error:
            return None

    if (m := _search("tramer")):
        amount = parse_int(m.group(1) if m.groups() else m.group(0))
        # A bare "tramer 2" is a sentence fragment, not a lira figure.
        if amount is not None and amount >= 1000:
            findings.tramer_amount = amount
    if _search("tramer_none"):
        findings.tramer_declared_none = True
    if _search("clean_claim"):
        findings.claims_clean = True
    if _search("pert"):
        findings.pert_record = True
    if _search("otv_exempt"):
        findings.otv_exempt = True
    if _search("lpg_fitted"):
        findings.lpg_fitted = True
    if _search("service_history"):
        findings.service_history = True
    if _search("second_key"):
        findings.second_key = True
    if _search("credit_ok"):
        findings.credit_eligible = True
    if _search("negotiable"):
        findings.negotiable = True
    if _search("no_negotiation"):
        findings.no_negotiation = True
    if (m := _search("inspection_year")):
        year = parse_int(m.group(1) if m.groups() else m.group(0))
        if year and 2020 <= year <= 2040:
            findings.inspection_valid_year = year
    return findings


# --------------------------------------------------------------------------- #
# Detail page
# --------------------------------------------------------------------------- #

def parse_detail_page(
    html: str,
    config: "dict[str, Any]",
    *,
    now: date,
    url: Optional[str] = None,
    description_chars: int = 400,
) -> Listing:
    listing = Listing()
    soup = parse_html(html)
    extractor = Extractor(config)
    base = config.get("base_url") or "https://www.sahibinden.com"
    detail_cfg = config.get("detail") or {}
    vocab = config.get("vocab") or {}

    scope = extractor.node(soup, "detail", "scope") or soup
    info = extractor.node(scope, "detail", "info_list") or scope

    listing.url = url
    listing.detail_fetched = True

    # -- identity ---------------------------------------------------------- #
    listing.id = (
        extractor.one(scope, "detail", "listing_id")
        or extractor.one(scope, "detail", "listing_id_input")
        or _id_from_url(url, detail_cfg.get("listing_id_url_regex"))
    )
    listing.title = extractor.one(scope, "detail", "title")

    # -- labels ------------------------------------------------------------ #
    labels = detail_cfg.get("labels") or {}
    pairs = label_pairs(info)
    if len(pairs) < 4:
        # The info list may be split across containers; widen to the whole page.
        pairs = {**label_pairs(scope), **pairs}

    def _label(field_name: str) -> Optional[str]:
        aliases = labels.get(field_name) or ()
        for alias in aliases:
            key = tr_key(alias)
            if key in pairs:
                return pairs[key]
        return label_value(scope, aliases)

    listing.brand = _label("brand")
    listing.series = _label("series")
    listing.model = _label("model")
    listing.trim = _label("trim")
    listing.year = parse_int(_label("year"))
    listing.km = parse_int(_label("km"))
    listing.body_type = _label("body_type")
    listing.drivetrain = _canon(_label("drivetrain"), vocab.get("drivetrain"))
    listing.color = _label("color")
    listing.condition = _canon(_label("condition"), vocab.get("condition"))
    listing.fuel = _canon(_label("fuel"), vocab.get("fuel"))
    listing.transmission = _canon(_label("transmission"), vocab.get("transmission"))
    listing.seller_type = _canon(_label("seller_type"), vocab.get("seller_type"))
    listing.plate_nationality = _label("plate_nationality")
    listing.warranty = canon_yes_no(_label("warranty"))
    listing.exchange_accepted = canon_yes_no(_label("exchange_accepted"))
    listing.avg_consumption_l_per_100km = parse_float(_label("avg_consumption_l_per_100km"))
    listing.fuel_tank_litres = parse_int(_label("fuel_tank_litres"))
    listing.listed_date = parse_tr_date(_label("listed_date"), now=now)

    # Buckets, not numbers — see models.Listing for why the range is preserved.
    (
        listing.engine_power_hp_min,
        listing.engine_power_hp_max,
        listing.engine_power_is_bucket,
    ) = parse_bucket(_label("engine_power_hp"))
    (
        listing.engine_volume_cc_min,
        listing.engine_volume_cc_max,
        listing.engine_volume_is_bucket,
    ) = parse_bucket(_label("engine_volume_cc"))

    if listing.seller_type:
        listing.is_dealer = listing.seller_type != "Sahibinden"

    # -- price -------------------------------------------------------------- #
    displayed = extractor.one(scope, "detail", "price_display")
    listing.price, listing.currency = parse_price(displayed)
    hidden = parse_int(extractor.one(scope, "detail", "price_value"))
    if listing.price is None and hidden is not None:
        listing.price = hidden
        listing.currency = listing.currency or "TRY"
    elif hidden is not None and listing.price is not None and hidden != listing.price:
        # Unknown whether the hidden input carries the original currency or a TRY
        # conversion, so a mismatch is reported rather than silently preferred.
        listing.parse_warnings.append(
            f"fiyat uyuşmazlığı: görünen={listing.price} gizli_alan={hidden}"
        )
    if listing.currency == "TRY" and listing.price is not None:
        listing.price_try = listing.price
        listing.price_try_source = "native"

    # -- location ------------------------------------------------------------ #
    crumbs = extractor.many(scope, "detail", "location_breadcrumb")
    if crumbs:
        listing.city = crumbs[0] if len(crumbs) > 0 else None
        listing.district = crumbs[1] if len(crumbs) > 1 else None
        listing.neighborhood = crumbs[2] if len(crumbs) > 2 else None
    listing.city = listing.city or _label("city")
    listing.district = listing.district or _label("district")

    # -- description ---------------------------------------------------------- #
    description_node = extractor.node(scope, "detail", "description")
    full_description = node_text(description_node, separator="\n") if description_node else None
    listing.text_findings = mine_description(full_description, config)
    if full_description and description_chars and len(full_description) > description_chars:
        listing.description = full_description[:description_chars].rstrip() + "…"
        listing.description_truncated = True
    else:
        listing.description = full_description

    # -- damage --------------------------------------------------------------- #
    listing.damage = _parse_damage(scope, extractor, config, _label("heavy_damage_record"))

    # -- features -------------------------------------------------------------- #
    listing.features, listing.features_absent = _parse_features(scope, extractor, config)
    if not listing.features and not listing.features_absent:
        listing.parse_warnings.append(
            "donanım listesi bulunamadı - araçta donanım olmaması ile seçici hatası ayırt edilemiyor"
        )

    # -- seller ---------------------------------------------------------------- #
    listing.seller_name = (
        extractor.one(scope, "detail", "seller", "store_name")
        or extractor.one(scope, "detail", "seller", "name")
    )

    # -- images ---------------------------------------------------------------- #
    listing.image_urls = _parse_images(scope, extractor, config, base)
    listing.image_count = len(listing.image_urls) or None
    listing.thumbnail_url = listing.image_urls[0] if listing.image_urls else None

    # -- leftovers -------------------------------------------------------------- #
    if detail_cfg.get("keep_unknown_labels", True):
        mapped = {tr_key(a) for aliases in labels.values() for a in (aliases or ())}
        listing.extras = {
            key: value for key, value in pairs.items() if key not in mapped
        }

    for required in detail_cfg.get("required_labels") or ():
        if getattr(listing, required, None) in (None, "", []):
            listing.parse_warnings.append(f"zorunlu alan okunamadı: {required}")

    return listing


def _id_from_url(url: Optional[str], pattern: Optional[str]) -> Optional[str]:
    if not url or not pattern:
        return None
    m = re.search(pattern, url)
    return m.group(1) if m and m.groups() else None


def _canon(value: Optional[str], table: Optional["dict[str, Any]"]) -> Optional[str]:
    """Map a raw value through a config vocabulary, folding Turkish case."""
    if not value:
        return None
    cleaned = collapse_ws(value)
    if not table:
        return cleaned or None
    key = tr_key(cleaned)
    for candidate, canon in table.items():
        if tr_key(str(candidate)) == key:
            return canon
    return cleaned or None


# --------------------------------------------------------------------------- #
# Sub-parsers
# --------------------------------------------------------------------------- #

def _parse_damage(
    scope: Tag,
    extractor: Extractor,
    config: "dict[str, Any]",
    heavy_damage_label: Optional[str],
) -> DamageReport:
    report = DamageReport()
    report.heavy_damage_record = canon_yes_no(heavy_damage_label)

    damage_cfg = ((config.get("detail") or {}).get("damage")) or {}
    state_tokens = damage_cfg.get("state_tokens") or {}
    panel_names = damage_cfg.get("panel_names") or {}

    panels = extractor.nodes(scope, "detail", "damage", "panels")
    for panel in panels:
        tokens = _class_tokens(panel)
        state = _panel_state(tokens, state_tokens)
        if not state:
            continue
        # Panel identity is the first class token that is not a state token.
        state_only = {t for values in state_tokens.values() for t in (values or ())}
        identity = next((t for t in _ordered_classes(panel) if t not in state_only), None)
        if not identity:
            continue
        report.status_by_panel[panel_names.get(identity, identity)] = state

    if report.status_by_panel:
        report.declared = True

    # The site's own counters, parsed independently as a crosscheck.
    report.painted_count_css = len(extractor.nodes(scope, "detail", "damage", "painted_count_css")) or None
    report.replaced_count_css = len(extractor.nodes(scope, "detail", "damage", "replaced_count_css")) or None

    text_nodes = extractor.nodes(scope, "detail", "damage", "text_list")
    if text_nodes:
        selected_class = damage_cfg.get("text_selected_class", "selected-damage")
        chosen = [
            node_text(n) for n in text_nodes if selected_class in _class_tokens(n)
        ]
        if chosen:
            report.summary_text = "; ".join(chosen)
            report.declared = True
        elif not report.declared:
            report.summary_text = "; ".join(node_text(n) for n in text_nodes[:4]) or None

    return report


def _ordered_classes(node: Tag) -> "list[str]":
    raw = node.get("class") or []
    if isinstance(raw, str):
        raw = raw.split()
    return [c.strip() for c in raw if c and c.strip()]


def _parse_features(
    scope: Tag,
    extractor: Extractor,
    config: "dict[str, Any]",
) -> "tuple[dict[str, list[str]], dict[str, list[str]]]":
    """Return ``(present, absent)`` feature maps.

    The site's ``<ul>`` enumerates *every* option in a group and marks the ones the
    car has with ``li.selected`` — so the absent set comes free, and for comparing
    listings it is often the more informative half.
    """
    features_cfg = ((config.get("detail") or {}).get("features")) or {}
    selected_class = features_cfg.get("selected_class", "selected")
    skip_pattern = features_cfg.get("skip_group_regex")

    present: "dict[str, list[str]]" = {}
    absent: "dict[str, list[str]]" = {}

    for heading in extractor.nodes(scope, "detail", "features", "groups"):
        group_name = node_text(heading)
        if not group_name:
            continue
        if skip_pattern:
            try:
                if re.search(skip_pattern, group_name, re.IGNORECASE):
                    continue  # this is the boya/değişen summary, parsed elsewhere
            except re.error:
                pass
        holder = heading.find_next_sibling()
        while holder is not None and getattr(holder, "name", None) not in ("ul", "ol", "div", None):
            holder = holder.find_next_sibling()
        if holder is None or getattr(holder, "name", None) not in ("ul", "ol", "div"):
            continue
        items = holder.find_all("li")
        if not items:
            continue
        for item in items:
            label = node_text(item)
            if not label:
                continue
            bucket = present if selected_class in _class_tokens(item) else absent
            bucket.setdefault(group_name, []).append(label)

    return present, absent


def _parse_images(
    scope: Tag,
    extractor: Extractor,
    config: "dict[str, Any]",
    base: str,
) -> "list[str]":
    images_cfg = ((config.get("detail") or {}).get("images")) or {}
    urls: "list[str]" = []
    seen: "set[str]" = set()

    for key in ("main", "thumbs", "lightbox", "src_fallback", "og_fallback"):
        for value in extractor.many(scope, "detail", "images", key):
            absolute = urljoin(base, value)
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)

    pattern = images_cfg.get("upscale_regex")
    replacement = images_cfg.get("upscale_replacement")
    if pattern and replacement:
        upscaled: "list[str]" = []
        for url in urls:
            try:
                # Best effort only: some listings 404 on the largest size, so a
                # rewrite must never replace a URL that already works.
                new = re.sub(pattern, replacement, url, count=1)
            except re.error:
                new = url
            upscaled.append(new or url)
        urls = upscaled
    return urls


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #

# Fields where a search/detail disagreement is real information (a price cut
# between the two fetches) rather than a parse error, so it is recorded.
_CONFLICT_FIELDS = ("price", "year", "km")


def merge_listing(stub: Listing, detail: Listing) -> Listing:
    """Overlay detail-page data on a search-row stub. Detail wins, conflicts noted."""
    merged = detail
    merged.id = detail.id or stub.id
    merged.url = detail.url or stub.url
    merged.title = detail.title or stub.title
    merged.first_seen_at = stub.first_seen_at or detail.first_seen_at

    for name in _CONFLICT_FIELDS:
        stub_value = getattr(stub, name, None)
        detail_value = getattr(merged, name, None)
        if stub_value is not None and detail_value is not None and stub_value != detail_value:
            merged.field_conflicts.append(f"{name}: liste={stub_value} detay={detail_value}")
        if detail_value is None and stub_value is not None:
            setattr(merged, name, stub_value)

    for name in (
        "currency", "brand", "series", "model", "color", "city", "district",
        "listed_date", "seller_name", "thumbnail_url",
    ):
        if getattr(merged, name, None) in (None, "", []):
            value = getattr(stub, name, None)
            if value not in (None, "", []):
                setattr(merged, name, value)

    if merged.is_dealer is None:
        merged.is_dealer = stub.is_dealer
    if merged.currency == "TRY" and merged.price is not None and merged.price_try is None:
        merged.price_try = merged.price
        merged.price_try_source = "native"

    merged.parse_warnings = list(dict.fromkeys(stub.parse_warnings + merged.parse_warnings))
    return merged
