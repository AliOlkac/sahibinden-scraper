"""Parser regression tests.

The tests that matter most here are the ones guarding the *silent* failures: a
locally-painted panel read as fully painted, brand/series read out of the year
column, a bucketed engine spec collapsed to a fake number. None of those crash;
they just produce confident, wrong data.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from sahibinden_scraper.config import lint_config, load_selectors
from sahibinden_scraper.detail import mine_description, parse_detail_page
from sahibinden_scraper.exporters import build_dataset, cohort_key
from sahibinden_scraper.search import (
    build_page_url,
    detect_page_kind,
    normalise_search_url,
    parse_search_page,
)
from sahibinden_scraper.textnorm import (
    canon_yes_no,
    parse_bucket,
    parse_price,
    parse_tr_date,
    tr_key,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = date(2026, 9, 8)


@pytest.fixture(scope="session")
def config():
    return load_selectors()


@pytest.fixture(scope="session")
def search_html():
    return (FIXTURES / "search_sample.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def detail_html():
    return (FIXTURES / "detail_sample.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Turkish text
# --------------------------------------------------------------------------- #

def test_turkish_labels_fold_to_the_same_key():
    # "İ".lower() is two codepoints and "YIL".lower() != "Yıl".lower(); a parser
    # built on str.lower() silently stops matching these labels.
    assert tr_key("Yıl") == tr_key("YIL") == tr_key("yil") == "yil"
    assert tr_key("Motor Gücü") == "motor gucu"
    assert tr_key("Ağır Hasar Kayıtlı:") == "agir hasar kayitli"
    assert tr_key("İlan No") == tr_key("ilan no")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.250.000 TL", (1250000, "TRY")),
        ("1.250.000", (1250000, "TRY")),
        ("32.500 €", (32500, "EUR")),
        ("12.345,67 TL", (12346, "TRY")),   # decimal comma, rounded to whole lira
        ("Sorunuz", (None, None)),
    ],
)
def test_price_parsing(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12 Ocak 2026", "2026-01-12"),
        ("12 Ocak", "2026-01-12"),
        ("20 Ekim", "2025-10-20"),   # ahead of today -> last year
        ("Bugün", "2026-09-08"),
        ("Dün", "2026-09-07"),
        ("12.01.2026", "2026-01-12"),
    ],
)
def test_turkish_dates(raw, expected):
    assert parse_tr_date(raw, now=NOW) == expected


def test_parse_tr_date_requires_an_explicit_clock():
    # Keyword-only and mandatory: "today" means today in Istanbul, and a run at
    # 01:30 TRT is on the previous UTC day.
    with pytest.raises(TypeError):
        parse_tr_date("Bugün")  # type: ignore[call-arg]


def test_yes_no_distinguishes_unknown_from_false():
    assert canon_yes_no("Var") is True
    assert canon_yes_no("Yok") is False
    assert canon_yes_no("Belirtilmemiş") is None
    assert canon_yes_no(None) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("101 - 125 hp", (101, 125, True)),
        ("1401 - 1600 cm3", (1401, 1600, True)),
        ("116 hp", (116, 116, False)),
        (None, (None, None, False)),
    ],
)
def test_bucketed_specs_keep_their_range(raw, expected):
    assert parse_bucket(raw) == expected


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def test_shipped_config_lints_clean(config):
    assert lint_config(config) == []


def test_lint_rejects_html_entities_in_vocab():
    # A key written "Benzin &amp; LPG" can never match, because values are entity
    # decoded before lookup — every LPG car would fall through to unmapped.
    bad = {
        "search": {}, "detail": {}, "guards": {}, "pagination": {},
        "vocab": {"fuel": {"Benzin &amp; LPG": "x"}},
    }
    problems = lint_config(bad)
    assert any("entity" in p for p in problems)


# --------------------------------------------------------------------------- #
# Search page
# --------------------------------------------------------------------------- #

def test_search_rows_parse(config, search_html):
    page = parse_search_page(search_html, config, now=NOW)
    assert page.total_count == 1842
    assert page.skipped_promo == 1, "promoted duplicate row must be skipped"
    assert len(page.listings) == 4
    first = page.listings[0]
    assert first.id == "1240940267"
    assert first.url.startswith("https://www.sahibinden.com/ilan/")
    assert first.year == 2018
    assert first.km == 96000
    assert first.price == 1250000


def test_taxonomy_and_attribute_columns_are_not_swapped(config, search_html):
    """The single most dangerous confusion on this page.

    td.searchResultsTagAttributeValue holds marka/seri/model;
    td.searchResultsAttributeValue holds yıl/km/renk. Four characters apart, both
    <td> in the same row. Swapping them yields "Ford" in the year column — which
    looks plausible enough to survive review.
    """
    page = parse_search_page(search_html, config, now=NOW)
    first = page.listings[0]
    assert first.brand == "Ford"
    assert first.series == "Focus"
    assert isinstance(first.year, int) and 1990 < first.year < 2100
    assert first.color == "Beyaz"


def test_search_splits_city_and_district(config, search_html):
    page = parse_search_page(search_html, config, now=NOW)
    assert (page.listings[0].city, page.listings[0].district) == ("İstanbul", "Kadıköy")


def test_foreign_currency_is_preserved_not_assumed_lira(config, search_html):
    page = parse_search_page(search_html, config, now=NOW)
    euro = [l for l in page.listings if l.currency == "EUR"]
    assert len(euro) == 1
    assert euro[0].price == 32500
    assert euro[0].price_try is None, "EUR must not be silently treated as TRY"


def test_relative_dates_resolve_against_the_injected_clock(config, search_html):
    page = parse_search_page(search_html, config, now=NOW)
    dates = {l.listed_date for l in page.listings}
    assert "2026-09-08" in dates  # "Bugün"
    assert "2026-09-07" in dates  # "Dün"


# --------------------------------------------------------------------------- #
# Detail page
# --------------------------------------------------------------------------- #

def test_detail_core_fields(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    assert listing.id == "1240940267"
    assert listing.year == 2018
    assert listing.km == 96000
    assert listing.fuel == "Dizel"
    assert listing.transmission == "Otomatik"
    assert listing.condition == "İkinci El"
    assert listing.seller_type == "Galeriden"
    assert listing.is_dealer is True
    assert listing.exchange_accepted is True
    assert listing.warranty is False
    assert listing.price_try == 1250000


def test_engine_specs_stay_buckets(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    assert listing.engine_power_is_bucket is True
    assert (listing.engine_power_hp_min, listing.engine_power_hp_max) == (101, 125)
    assert listing.engine_power_hp is None, "a bucket must not collapse to a number"
    assert (listing.engine_volume_cc_min, listing.engine_volume_cc_max) == (1401, 1600)
    # 1401-1600 ENDS on the 1600cc ÖTV/MTV edge without crossing it — every car in
    # that bucket is in the <=1600 band, so the tax class is not ambiguous.
    assert listing.tax_band_ambiguous is False


def test_tax_band_flag_fires_only_when_a_bucket_actually_straddles():
    from sahibinden_scraper.models import Listing

    on_edge = Listing(engine_volume_cc_min=1401, engine_volume_cc_max=1600)
    straddling = Listing(engine_volume_cc_min=1500, engine_volume_cc_max=1800)
    exact = Listing(engine_volume_cc_min=1598, engine_volume_cc_max=1598)
    assert on_edge.tax_band_ambiguous is False
    assert straddling.tax_band_ambiguous is True, "1500-1800 crosses the 1600cc band"
    assert exact.tax_band_ambiguous is None, "an exact figure has no ambiguity to report"


def test_locally_painted_panel_is_not_read_as_fully_painted(config, detail_html):
    """THE trap: "local-painted-new" contains "painted-new" as a substring.

    A naive membership test reclassifies every locally-painted panel as fully
    painted, which materially mis-prices the car and never raises an error.
    """
    listing = parse_detail_page(detail_html, config, now=NOW)
    damage = listing.damage
    assert damage.declared is True
    assert damage.status_by_panel["Sağ Ön Çamurluk"] == "lokal_boyali"
    assert damage.status_by_panel["Ön Tampon"] == "boyali"
    assert damage.status_by_panel["Sol Ön Kapı"] == "degismis"
    assert damage.painted == ["Ön Tampon"], "only the fully painted panel"
    assert damage.locally_painted == ["Sağ Ön Çamurluk"]
    assert damage.replaced == ["Sol Ön Kapı"]
    assert damage.painted_count == 2      # painted + locally painted
    assert damage.replaced_count == 1
    assert len(damage.original) == 10


def test_damage_lists_are_derived_from_one_map(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    d = listing.damage
    total = len(d.original) + len(d.painted) + len(d.locally_painted) + len(d.replaced)
    assert total == len(d.status_by_panel)


def test_features_split_into_present_and_absent(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    assert "ABS" in listing.features["Güvenlik"]
    # The site lists every option; the unticked ones are free information.
    assert "Gece Görüş" in listing.features_absent["Güvenlik"]
    assert "Sunroof" in listing.features_absent["İç Donanım"]
    # The boya/değişen summary renders as an h3+ul in the same container and must
    # not be mistaken for equipment.
    assert not any("Boyalı" in group for group in listing.features)


def test_unmapped_labels_land_in_extras(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    assert "muayene" in listing.extras
    # A mapped label must NOT appear in extras.
    assert "yil" not in listing.extras


def test_description_is_mined_but_flagged_as_seller_claim(config):
    html = (FIXTURES / "detail_otv.html").read_text(encoding="utf-8")
    listing = parse_detail_page(html, config, now=NOW)
    findings = listing.text_findings
    assert findings.otv_exempt is True, "ÖTV muafiyeti moves price 20-40%"
    assert findings.pert_record is True
    assert findings.tramer_amount == 45000
    assert findings.claims_clean is True
    assert findings.to_dict()["_provenance"] == "description_regex"


def test_contradiction_between_prose_and_panels_raises_a_red_flag(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    flags = listing.red_flags(reference_year=2026)
    assert any("hatasiz" in f for f in flags), (
        "the ad says hatasız/boyasız while the diagram declares painted panels"
    )


def test_images_are_upscaled_best_effort(config, detail_html):
    listing = parse_detail_page(detail_html, config, now=NOW)
    assert listing.image_urls
    assert all(u.startswith("https://") for u in listing.image_urls)


# --------------------------------------------------------------------------- #
# Page classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name,expected",
    [
        ("search_sample.html", "search"),
        ("detail_sample.html", "detail"),
        ("detail_removed.html", "gone"),
        ("interstitial.html", "interstitial"),
    ],
)
def test_page_classification(config, name, expected):
    html = (FIXTURES / name).read_text(encoding="utf-8")
    assert detect_page_kind(html, config) == expected


def test_removed_listing_is_not_mistaken_for_a_block(config):
    """A pulled listing returns HTTP 200 with no content container — exactly what
    a block looks like. Without a distinct verdict the scraper would sit waiting
    for a human to rescue a dead ad."""
    html = (FIXTURES / "detail_removed.html").read_text(encoding="utf-8")
    assert detect_page_kind(html, config) == "gone"


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #

def test_view_mode_is_forced_not_stripped(config):
    # View mode is sticky server-side, so removing the param leaves a previously
    # chosen Galeri view in force and no results table ever renders.
    url = normalise_search_url("https://www.sahibinden.com/ford-focus?viewType=Gallery", config)
    assert "viewType=Classic" in url
    assert "Gallery" not in url


def test_pagination_url_carries_offset(config):
    url = build_page_url("https://www.sahibinden.com/ford-focus", 100, config)
    assert "pagingOffset=100" in url
    assert "pagingSize=50" in url
    assert "pagingOffset" not in build_page_url("https://www.sahibinden.com/ford-focus", 0, config)


def test_user_query_params_survive_pagination(config):
    url = build_page_url("https://www.sahibinden.com/ford-focus?a5_min=2015&price_max=1500000", 50, config)
    assert "a5_min=2015" in url and "price_max=1500000" in url


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def test_dataset_reports_missing_fields_honestly():
    records = [
        {"id": "1", "brand": "Ford", "series": "Focus", "year": 2018, "fuel": "Dizel",
         "transmission": "Otomatik", "price_try": 1250000, "km": 96000, "trim": None},
        {"id": "2", "brand": "Ford", "series": "Focus", "year": 2018, "fuel": "Dizel",
         "transmission": "Otomatik", "price_try": 1400000, "km": 70000, "trim": None},
    ]
    payload = build_dataset(records, meta={"source_url": "x", "total_count": 900})
    meta = payload["meta"]
    # The single most valuable honesty field: null here means "scraper missed it",
    # not "the car lacks it".
    assert "trim" in meta["fields_empty_across_all_listings"]
    assert meta["collected_count"] == 2
    assert "field_dictionary" in meta and "reading_notes" in meta


def test_cohort_ranks_are_precomputed():
    records = [
        {"id": str(i), "brand": "Ford", "series": "Focus", "year": 2018, "fuel": "Dizel",
         "transmission": "Otomatik", "price_try": price, "km": 100000}
        for i, price in enumerate([1000000, 1200000, 1400000, 1600000, 1800000])
    ]
    payload = build_dataset(records, meta={})
    listings = payload["listings"]
    assert all(r["cohort_size"] == 5 for r in listings)
    assert listings[0]["price_pct_rank_in_cohort"] == 10.0
    assert listings[0]["price_vs_cohort_median_pct"] == pytest.approx(-28.6, abs=0.2)
    # Sorted by cohort then price, so comparable cars sit next to each other.
    assert [r["price_try"] for r in listings] == sorted(r["price_try"] for r in listings)


def test_cohort_key_separates_different_markets():
    petrol = {"brand": "Ford", "series": "Focus", "year": 2018,
              "fuel": "Benzin", "transmission": "Manuel"}
    diesel = {**petrol, "fuel": "Dizel", "transmission": "Otomatik"}
    assert cohort_key(petrol) != cohort_key(diesel)


def test_columnar_form_kicks_in_for_large_sets():
    records = [
        {"id": str(i), "brand": "Ford", "series": "Focus", "year": 2018,
         "fuel": "Dizel", "transmission": "Otomatik", "price_try": 1000000 + i, "km": 1000 * i}
        for i in range(200)
    ]
    payload = build_dataset(records, meta={})
    assert "columns" in payload and "rows" in payload
    assert len(payload["rows"]) == 200
    assert payload["meta"]["format"] == "columnar"
