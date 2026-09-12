"""Regression tests for bugs an adversarial review found and confirmed.

Each test names the failure it prevents. They are kept apart from
``test_parsers.py`` because they document defects rather than behaviour: if one of
these starts failing, a specific known-bad behaviour has come back.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

import make_fixtures as mf
from sahibinden_scraper.cli import collect_html_files, main
from sahibinden_scraper.config import lint_config, load_selectors
from sahibinden_scraper.detail import parse_detail_page
from sahibinden_scraper.exporters import build_dataset
from sahibinden_scraper.extract import label_pairs, label_value, parse_html
from sahibinden_scraper.models import Listing
from sahibinden_scraper.search import detect_page_kind, parse_search_page
from sahibinden_scraper.store import RunStore, stable_key

NOW = date(2026, 9, 8)


@pytest.fixture(scope="module")
def config():
    return load_selectors()


# --------------------------------------------------------------------------- #
# Search columns
# --------------------------------------------------------------------------- #

def _series_scoped(html: str) -> str:
    """The layout of a URL already narrowed to one series: the Marka/Seri/Model
    columns are gone, from both the header and the rows."""
    html = html.replace(
        "<th></th><th>Marka</th><th>Seri</th><th>Model</th><th>İlan Başlığı</th>",
        "<th>İlan Başlığı</th>",
    )
    return re.sub(r'\s*<td class="searchResultsTagAttributeValue">[^<]*</td>', "", html)


@pytest.mark.parametrize(
    "transform,label",
    [
        (lambda h: h, "full header"),
        (_series_scoped, "series-scoped URL (narrower header)"),
        (lambda h: re.sub(r"<thead>.*?</thead>", "", h, flags=re.S), "no header at all"),
    ],
)
def test_year_and_km_survive_every_column_layout(config, transform, label):
    """A <thead> index counts ALL columns; the attribute family holds only three.

    Using the header index to subscript the family read the KM value as the model
    year on any pre-filtered URL — an 8-year-old car reported as year 96000 — and it
    failed silently, because 96000 is a perfectly valid integer.
    """
    page = parse_search_page(transform(mf.search_page()), config, now=NOW)
    first = page.listings[0]
    assert first.year == 2018, f"{label}: year misread"
    assert first.km == 96000, f"{label}: km misread"
    assert first.color == "Beyaz", f"{label}: colour misread"


def test_a_kilometre_can_never_be_accepted_as_a_model_year(config):
    html = mf.search_page().replace(
        '<td class="searchResultsAttributeValue">2018</td>',
        '<td class="searchResultsAttributeValue">185.000</td>',
        1,
    )
    page = parse_search_page(html, config, now=NOW)
    assert page.listings[0].year != 185000


def test_search_rows_populate_the_thumbnail(config):
    """The selector was configured but never read, so merge_listing's backfill of
    thumbnail_url could not fire for a --fast run."""
    page = parse_search_page(mf.search_page(), config, now=NOW)
    assert page.listings[0].thumbnail_url
    assert page.listings[0].thumbnail_url.startswith("https://")


# --------------------------------------------------------------------------- #
# Turkish casefolding in page classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "heading",
    ["İlan bulunamadı", "İLAN BULUNAMADI", "Aradığınız ilan yayından kaldırılmıştır"],
)
def test_removed_listing_detected_whatever_the_casing(config, heading):
    """`"İ".lower()` is two codepoints, so a str.lower() comparison against the
    marker never matched and a dead listing was misread as a block — the scraper
    then sat waiting for a human to rescue an ad that no longer exists."""
    html = f"<html><body><h1>{heading}</h1>{'x' * 200}</body></html>"
    assert detect_page_kind(html, config) == "gone"


# --------------------------------------------------------------------------- #
# Label extraction
# --------------------------------------------------------------------------- #

DIV_INFO = """
<div class="classifiedDetail"><div class="infoGrid">
  <div class="row"><div class="k">Marka</div><div class="v">Ford</div></div>
  <div class="row"><div class="k">Yıl</div><div class="v">2019</div></div>
  <div class="row"><div class="k">Model</div><div class="v">1.6 TDI BlueMotion</div></div>
  <div class="row"><div class="k">Muayene</div><div class="v">06/2027</div></div>
</div></div>
"""


def test_a_wrapper_div_cannot_win_over_the_real_row():
    """'div' is in the holder selector because some layouts are div grids — but a
    wrapper matched on its first descendant's label and returned the whole block,
    e.g. Model -> 'Marka Ford Yıl 2019 Model 1.6 TDI BlueMotion'."""
    soup = parse_html(DIV_INFO)
    assert label_value(soup, ["Model"]) == "1.6 TDI BlueMotion"
    assert label_value(soup, ["Yıl"]) == "2019"
    assert label_value(soup, ["Marka"]) == "Ford"


def test_label_value_and_label_pairs_agree_on_what_a_row_is():
    """They used different holder selectors, so `extras` came back empty on exactly
    the markup label_value could read — unmapped labels vanished silently."""
    soup = parse_html(DIV_INFO)
    pairs = label_pairs(soup)
    assert pairs.get("muayene") == "06/2027"
    for alias in ("Marka", "Yıl", "Model", "Muayene"):
        assert label_value(soup, [alias]) is not None


def test_unmapped_labels_reach_extras_on_div_markup(config):
    listing = parse_detail_page(DIV_INFO, config, now=NOW)
    assert listing.extras.get("muayene") == "06/2027"


# --------------------------------------------------------------------------- #
# Free-text mining
# --------------------------------------------------------------------------- #

RICH_DESCRIPTION = (
    "İkinci anahtar mevcuttur. ÖTV muafiyetli engelli aracıdır. "
    "Tramer kaydı 45.000 TL. Araç pert kayıtlıdır. Hatasız boyasız değişensiz. "
    "Yetkili servis bakımlıdır. LPG montajlıdır. Muayenesi 2027 yılına kadar. "
    "Krediye uygundur. Pazarlık payı vardır."
)


def test_description_mining_survives_turkish_capitals(config):
    """`mine_description` lowercased with str.lower(), so "İkinci anahtar" became
    "i̇kinci" (i + combining dot) and the ASCII pattern never matched — the finding
    disappeared with no error anywhere."""
    from sahibinden_scraper.detail import mine_description

    found = mine_description(RICH_DESCRIPTION, config)
    assert found.second_key is True, "İkinci anahtar missed"
    assert found.otv_exempt is True
    assert found.pert_record is True
    assert found.lpg_fitted is True
    assert found.service_history is True
    assert found.credit_eligible is True
    assert found.negotiable is True
    assert found.tramer_amount == 45000
    assert found.inspection_valid_year == 2027


def test_lint_rejects_a_turkish_character_in_a_mining_pattern():
    """The haystack is ASCII-folded before matching, so a pattern with a Turkish
    letter can never fire. Silently. The linter is the only thing that catches it."""
    bad = {"search": {}, "detail": {}, "guards": {}, "pagination": {},
           "text_mining": {"otv": r"ötv\s*muaf"}}
    problems = lint_config(bad)
    assert any("Türkçe karakter" in p for p in problems)


def test_tramer_amount_is_not_confused_by_a_stray_small_number(config):
    from sahibinden_scraper.detail import mine_description

    assert mine_description("Tramer 2 kere sorgulandı.", config).tramer_amount is None


# --------------------------------------------------------------------------- #
# Cohort statistics
# --------------------------------------------------------------------------- #

def _record(**kwargs):
    base = {
        "id": "1", "brand": "Ford", "series": "Focus", "year": 2018,
        "fuel": "Dizel", "transmission": "Otomatik", "price_try": 1000000, "km": 100000,
    }
    base.update(kwargs)
    return base


def test_a_lone_priced_car_is_not_reported_as_0pct_from_the_median():
    """With one priced member the median IS that member, and '0.0% vs cohort
    median' reads as market evidence when it is the car compared to itself."""
    payload = build_dataset([_record(id="1")], meta={})
    assert payload["listings"][0]["price_vs_cohort_median_pct"] is None


def test_cohort_size_and_priced_count_are_reported_separately():
    """A euro-priced car is in the cohort but not in the price population. Stating
    only cohort_size let '-4% vs the cohort median' read as a comparison against
    all N cars when it was against fewer."""
    records = [
        _record(id="1", price_try=1000000),
        _record(id="2", price_try=1400000),
        _record(id="3", price_try=None, currency="EUR"),
    ]
    listings = build_dataset(records, meta={})["listings"]
    assert all(r["cohort_size"] == 3 for r in listings)
    assert all(r["cohort_priced_count"] == 2 for r in listings)


def test_a_broken_damage_parser_is_visible_in_the_meta():
    """_empty_everywhere only looked at top-level keys, so a total damage-parse
    failure hid inside a non-empty dict and every car read as 'no damage'."""
    records = [
        _record(id=str(i), damage={"declared": False, "painted_count": None, "status_by_panel": {}})
        for i in range(3)
    ]
    meta = build_dataset(records, meta={})["meta"]
    gaps = meta["fields_empty_across_all_listings"]
    assert "damage.painted_count" in gaps
    assert "damage.status_by_panel" in gaps


def test_by_design_nulls_are_not_reported_as_scraper_gaps():
    """Listing a deliberate null as a coverage gap dilutes the one field that tells
    a model 'null here means we missed it'."""
    records = [_record(id=str(i), price_try=None, engine_power_hp=None,
                       engine_power_hp_min=101) for i in range(3)]
    meta = build_dataset(records, meta={})["meta"]
    assert "engine_power_hp" in meta["fields_null_by_design"]
    assert "engine_power_hp" not in meta["fields_empty_across_all_listings"]
    assert "price_try" in meta["fields_null_by_design"]


def test_export_does_not_mutate_the_caller_records():
    original = _record(id="1")
    snapshot = dict(original)
    build_dataset([original], meta={})
    assert original == snapshot


def test_dataset_is_json_serialisable():
    payload = build_dataset([_record(id="1")], meta={"scraped_at": "2026-09-08T12:00:00+03:00"})
    json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Run store
# --------------------------------------------------------------------------- #

def test_cache_keys_are_stable_across_processes():
    """The key was built from hash(), which Python randomises per interpreter run —
    so every resumed run missed the whole cache and refetched every page, which is
    exactly the traffic this tool exists not to generate."""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from sahibinden_scraper.store import stable_key;"
        "print(stable_key('https://example.com/a?b=1'))"
    )
    root = Path(__file__).resolve().parents[1]
    runs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=root).stdout.strip()
        for _ in range(3)
    }
    assert len(runs) == 1 and runs != {""}


def test_two_similar_urls_do_not_share_a_cache_file(tmp_path):
    store = RunStore(tmp_path, name="t")
    prefix = "https://www.sahibinden.com/ilan/vasita-otomobil-ford-focus-1-5-tdci-titanium"
    a = store.page_path("detail", f"{prefix}-aaa-111111/detay")
    b = store.page_path("detail", f"{prefix}-bbb-222222/detay")
    assert a != b


def test_a_torn_cache_file_is_refetched_not_parsed(tmp_path):
    """A run killed mid-write leaves a truncated file. Returning its empty contents
    counted as a cache HIT, so the resumed run 'parsed' nothing and recorded a
    listing with every field missing."""
    store = RunStore(tmp_path, name="t").open()
    path = store.page_path("detail", "123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert store.read_page("detail", "123") is None
    path.write_text("<html>kısa</html>", encoding="utf-8")
    assert store.read_page("detail", "123") is None
    store.write_page("detail", "123", "<html>" + "x" * 5000 + "</html>")
    assert store.read_page("detail", "123") is not None


def test_stub_rows_do_not_count_as_finished(tmp_path):
    """A --fast or budget-capped run writes search-only stubs. If those counted as
    done, every later resume skipped them and the detail pages were never fetched —
    the run looked complete while permanently missing every technical field."""
    store = RunStore(tmp_path, name="t").open()
    store.append_listing({"id": "1", "detail_fetched": False})
    store.append_listing({"id": "2", "detail_fetched": True})
    assert store.completed_ids() == {"1", "2"}
    assert store.completed_ids(require_detail=True) == {"2"}


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #

def test_unknown_output_format_fails_loudly(capsys):
    """It used to write nothing and report success."""
    assert main(["parse-local", "tests/fixtures", "--format", "json,xlsx"]) == 2
    assert "bilinmeyen çıktı biçimi" in capsys.readouterr().err


def test_missing_path_is_an_error_not_a_traceback():
    files, problems = collect_html_files([Path("kesinlikle-yok.html")])
    assert files == []
    assert problems and "bulunamadı" in problems[0]


def test_config_pointing_at_a_directory_is_a_clean_error(capsys, tmp_path):
    assert main(["doctor", "tests/fixtures/detail_sample.html", "--config", str(tmp_path)]) == 2
    assert "HATA" in capsys.readouterr().err


def test_lint_rejects_a_non_string_regex():
    """A YAML scalar that parsed as a number reached re.compile and raised
    TypeError out of the linter itself."""
    bad = {"search": {}, "detail": {}, "guards": {}, "pagination": {},
           "text_mining": {"tramer": 12345}}
    problems = lint_config(bad)
    assert any("metin olmalı" in p for p in problems)


def test_run_name_with_spaces_round_trips_through_export(tmp_path, capsys):
    """slugify was applied when reading a run name but not when writing it, so any
    --name with spaces or capitals was unreachable by `export` under any spelling."""
    runs, out = tmp_path / "runs", tmp_path / "out"
    assert main([
        "parse-local", "tests/fixtures/detail_sample.html",
        "--name", "Deneme Ad", "--runs", str(runs), "--out", str(out),
    ]) == 0
    capsys.readouterr()
    assert main(["export", "Deneme Ad", "--runs", str(runs), "--out", str(out)]) == 0


def test_export_of_an_unknown_run_does_not_create_a_phantom_directory(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert main(["export", "yok-boyle", "--runs", str(runs), "--out", str(tmp_path)]) == 2
    assert not (runs / "yok-boyle").exists()


def test_zero_is_a_real_value_for_the_caps():
    """`if settings.limit` treated 0 as "unset", so --limit 0 crawled everything."""
    from sahibinden_scraper.config import Settings

    settings = Settings(limit=0, max_pages=0, request_budget=0)
    for value in (settings.limit, settings.max_pages, settings.request_budget):
        assert value is not None and value == 0


def test_negative_caps_are_rejected():
    with pytest.raises(SystemExit):
        main(["scrape", "https://example.com", "--limit", "-5", "--dry-run"])


def test_doctor_json_still_honours_the_coverage_gate(tmp_path):
    """A gate that silently passes under --json is worse than no gate: CI goes
    green on a broken parser."""
    report = tmp_path / "r.json"
    code = main([
        "doctor", "tests/fixtures/interstitial.html",
        "--json", "--json-out", str(report), "--fail-under", "0.9",
    ])
    assert code == 4
    assert report.is_file()


# --------------------------------------------------------------------------- #
# Model invariants
# --------------------------------------------------------------------------- #

def test_red_flags_survive_a_completely_empty_listing():
    listing = Listing()
    assert isinstance(listing.red_flags(2026), list)
    assert isinstance(listing.to_dict(reference_year=2026), dict)
    assert isinstance(listing.digest(2026), str)
