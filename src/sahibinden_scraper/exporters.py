"""Turning a pile of listings into the single file you hand to a model.

The whole point of this tool is the export, so the decisions here matter more than
the scraping does:

* **A codebook travels with the data.** Field meanings, units and provenance are in
  the file, so the model never has to infer what ``tax_band_ambiguous`` means.
* **Coverage is stated honestly.** ``fields_empty_across_all_listings`` tells the
  model that a ``null`` means "the scraper missed this", not "the car lacks it" —
  without it every absent field silently reads as a fact about the car.
* **Truncation is stated honestly.** If the filter matched 5,000 cars and
  pagination could only reach 1,000, the model is told, so it cannot call the
  cheapest row "the cheapest on the market".
* **Comparisons a model does badly are precomputed.** Cohort membership, price rank
  and km rank within cohort cost nothing here and are unreliable across 300 rows in
  a prompt.
* **Records are ordered by cohort, then price.** Adjacency in the context window
  drives better comparison than scrape order does.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .models import FIELD_DICTIONARY, PROVENANCE, SCHEMA_VERSION

__all__ = [
    "build_dataset",
    "write_json",
    "write_csv",
    "cohort_key",
    "COLUMNAR_THRESHOLD",
]

# Above this many records the flat-record form costs meaningfully more tokens than
# a columns/rows form carrying the same data.
COLUMNAR_THRESHOLD = 120


def cohort_key(record: "dict[str, Any]") -> str:
    """Group comparable cars.

    ``brand|series|year`` alone pools a 1.5 diesel automatic with a 1.6 petrol
    manual — different markets, so fuel and transmission are part of the key.
    """
    parts = [
        (record.get("brand") or "?").strip().lower(),
        (record.get("series") or "?").strip().lower(),
        str(record.get("year") or "?"),
        (record.get("fuel") or "?").strip().lower(),
        (record.get("transmission") or "?").strip().lower(),
    ]
    return "|".join(parts)


def _widened_key(record: "dict[str, Any]") -> str:
    """Same cohort without the year, used when a strict cohort is too small."""
    parts = [
        (record.get("brand") or "?").strip().lower(),
        (record.get("series") or "?").strip().lower(),
        (record.get("fuel") or "?").strip().lower(),
        (record.get("transmission") or "?").strip().lower(),
    ]
    return "|".join(parts)


def _pct_rank(value: Optional[float], population: Sequence[float]) -> Optional[float]:
    """Percentile rank of ``value`` in ``population``: 0 = cheapest/lowest."""
    if value is None or len(population) < 2:
        return None
    below = sum(1 for other in population if other < value)
    equal = sum(1 for other in population if other == value)
    rank = (below + 0.5 * equal) / len(population)
    return round(100 * rank, 1)


def _annotate_cohorts(records: "list[dict[str, Any]]") -> None:
    """Attach cohort membership and within-cohort ranks, in place."""
    strict: "dict[str, list[dict[str, Any]]]" = {}
    for record in records:
        strict.setdefault(cohort_key(record), []).append(record)

    widened: "dict[str, list[dict[str, Any]]]" = {}
    for record in records:
        widened.setdefault(_widened_key(record), []).append(record)

    for key, group in strict.items():
        members = group
        was_widened = False
        if len(group) < 5:
            # A cohort of one makes "12% below the cohort median" meaningless.
            members = widened.get(_widened_key(group[0]), group)
            was_widened = len(members) > len(group)

        prices = [r["price_try"] for r in members if r.get("price_try") is not None]
        kms = [r["km"] for r in members if r.get("km") is not None]
        median_price = statistics.median(prices) if prices else None

        for record in group:
            record["cohort_key"] = key
            record["cohort_size"] = len(members)
            # cohort_size counts members; the statistics below are computed only
            # over members that HAVE the value. A euro-priced listing has no
            # price_try, so it is in the cohort but not in the price population —
            # stating both stops "-4% vs cohort median" being read as a comparison
            # against all N cars when it was against fewer.
            record["cohort_priced_count"] = len(prices)
            record["cohort_km_count"] = len(kms)
            record["cohort_widened"] = was_widened
            record["cohort_scope"] = "this_run_only"
            price = record.get("price_try")
            record["price_pct_rank_in_cohort"] = _pct_rank(price, prices)
            record["km_pct_rank_in_cohort"] = _pct_rank(record.get("km"), kms)
            if price is not None and median_price and len(prices) >= 2:
                # With one priced member the median IS that member, and a confident
                # "0.0% vs the cohort median" reads as market evidence when it is
                # just the car compared to itself.
                record["price_vs_cohort_median_pct"] = round(
                    100.0 * (price - median_price) / median_price, 1
                )
            else:
                record["price_vs_cohort_median_pct"] = None


# Fields whose emptiness is either good news or already explained elsewhere, so
# listing them as "the scraper missed this" would dilute the signal:
#   - health fields: empty means nothing went wrong
#   - bucket companions: null is the *correct* answer when the site published a
#     range, and the _min/_max pair carries the value
_NOT_A_COVERAGE_GAP = {
    "parse_warnings",
    "field_conflicts",
    "red_flags",
    "first_seen_at",
    "last_seen_at",
    "price_try_source",
    "features_absent",
    "extras",
}
_BUCKET_COMPANIONS = {
    "engine_power_hp": "engine_power_hp_min",
    "engine_volume_cc": "engine_volume_cc_min",
}

# Derived fields whose emptiness follows from the data rather than from a broken
# selector: no TRY-priced listing means no price_try, a one-car cohort means no
# ranks, an exact engine figure means no ambiguity flag. Reported separately so
# "the scraper missed this" keeps its meaning.
_NULL_BY_DESIGN = {
    "price_try",
    "price_try_source",
    "price_pct_rank_in_cohort",
    "km_pct_rank_in_cohort",
    "price_vs_cohort_median_pct",
    "tax_band_ambiguous",
    "engine_power_hp",
    "engine_volume_cc",
}

# Nested blocks worth descending into: a total damage-parse failure would otherwise
# be invisible, and every car would read as "no damage declared".
_NESTED_BLOCKS = ("damage", "text_findings")

_EMPTY = (None, "", [], {})


def _is_empty(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_is_empty(v) for v in value.values()) if value else True
    return value in _EMPTY


def _empty_everywhere(records: "list[dict[str, Any]]") -> "tuple[list[str], list[str]]":
    """``(coverage_gaps, null_by_design)`` — fields empty in *every* record.

    The single most valuable honesty pair in the export: without it a model reads
    every absent field as a fact about the car rather than a gap in the data.
    """
    if not records:
        return [], []
    keys: "set[str]" = set()
    for record in records:
        keys.update(record.keys())

    gaps: "list[str]" = []
    by_design: "list[str]" = []

    def _consider(name: str, empty_everywhere: bool) -> None:
        if not empty_everywhere:
            return
        (by_design if name in _NULL_BY_DESIGN else gaps).append(name)

    for key in sorted(keys):
        if key in _NOT_A_COVERAGE_GAP:
            continue
        if key in _NESTED_BLOCKS:
            # Descend one level so a broken damage parser is visible as
            # "damage.painted_count", not hidden inside a non-empty dict.
            subkeys: "set[str]" = set()
            for record in records:
                block = record.get(key)
                if isinstance(block, dict):
                    subkeys.update(k for k in block if not k.startswith("_"))
            for subkey in sorted(subkeys):
                _consider(
                    f"{key}.{subkey}",
                    all(_is_empty((record.get(key) or {}).get(subkey)) for record in records),
                )
            continue
        if not all(_is_empty(record.get(key)) for record in records):
            continue
        companion = _BUCKET_COMPANIONS.get(key)
        if companion and any(record.get(companion) is not None for record in records):
            by_design.append(key)
            continue
        _consider(key, True)
    return gaps, by_design


def build_dataset(
    records: "list[dict[str, Any]]",
    *,
    meta: "dict[str, Any]",
    columnar: Optional[bool] = None,
) -> "dict[str, Any]":
    """Assemble the final export payload."""
    records = [dict(r) for r in records]
    _annotate_cohorts(records)

    # Cohort-then-price ordering puts comparable cars next to each other in the
    # context window, which is worth more than chronological order.
    records.sort(
        key=lambda r: (
            r.get("cohort_key") or "",
            r.get("price_try") if r.get("price_try") is not None else float("inf"),
        )
    )
    for index, record in enumerate(records):
        record["index"] = index

    empty_fields, null_by_design = _empty_everywhere(records)
    warned = sum(1 for r in records if r.get("parse_warnings"))
    flagged = sum(1 for r in records if r.get("red_flags"))

    payload_meta = {
        "schema_version": SCHEMA_VERSION,
        **meta,
        "collected_count": len(records),
        "listings_with_parse_warnings": warned,
        "listings_with_red_flags": flagged,
        "detail_pages_fetched": sum(1 for r in records if r.get("detail_fetched")),
        "fields_empty_across_all_listings": empty_fields,
        "fields_null_by_design": null_by_design,
        "cohort_definition": "brand|series|year|fuel|transmission; <5 üye ise yıl kaldırılarak genişletilir",
        "provenance_legend": PROVENANCE,
        "field_dictionary": FIELD_DICTIONARY,
        "reading_notes": [
            "null = bilinmiyor. 0 veya 'yok' ile aynı şey DEĞİLDİR.",
            "fields_empty_across_all_listings içindeki alanlar hiçbir ilanda okunamadı; "
            "bunların null olması araç hakkında bir bilgi değil, scraper eksiğidir.",
            "fields_null_by_design ise tasarım gereği boştur (ör. hiçbir ilan TL değilse "
            "price_try, tek üyeli kohortta sıralamalar); bunlar eksiklik değildir.",
            "cohort_size kohorttaki ilan sayısıdır; fiyat karşılaştırmaları yalnızca "
            "cohort_priced_count kadar ilan üzerinden hesaplanır (farklı para birimleri hariç tutulur).",
            "text_findings altındaki her şey satıcının kendi metninden çıkarıldı; "
            "resmî kayıt sorgusu değildir.",
            "Motor gücü/hacmi aralık (bucket) olarak yayınlanır; is_bucket=true ise "
            "kesin değer yoktur ve iki aracı güce göre sıralamak yanlıştır.",
            "listed_date son güncelleme tarihidir; ücretli 'doping' ile bugüne çekilebilir.",
            "cohort_* alanları yalnızca bu çalışmadaki ilanlara göre hesaplanır, "
            "piyasanın tamamına göre değil.",
        ],
    }

    if columnar is None:
        columnar = len(records) > COLUMNAR_THRESHOLD

    if not columnar:
        return {"meta": payload_meta, "listings": records}

    columns: "list[str]" = []
    for record in records:
        for key in record:
            if key not in columns:
                columns.append(key)
    payload_meta["format"] = "columnar"
    payload_meta["format_note"] = (
        "Token tasarrufu için sütunlu biçim: 'columns' sütun adlarını, 'rows' ise "
        "her ilan için aynı sıradaki değerleri taşır."
    )
    return {
        "meta": payload_meta,
        "columns": columns,
        "rows": [[record.get(column) for column in columns] for record in records],
    }


def write_json(path: Path, payload: "dict[str, Any]", *, compact: bool = False) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        # separators matter: json.dumps' default ", " / ": " adds a surprising
        # amount of whitespace across a few hundred records.
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #

CSV_COLUMNS = [
    "id", "title", "price", "currency", "price_try", "year", "km", "age_years",
    "km_per_year", "brand", "series", "model", "fuel", "transmission", "body_type",
    "engine_power_hp_min", "engine_power_hp_max", "engine_volume_cc_min",
    "engine_volume_cc_max", "color", "condition", "damage_painted", "damage_replaced",
    "heavy_damage_record", "tramer_text", "otv_exempt", "warranty", "seller_type",
    "seller_name", "city", "district", "listed_date", "feature_count",
    "cohort_key", "cohort_size", "price_vs_cohort_median_pct", "red_flags", "url",
]


def _flatten_for_csv(record: "dict[str, Any]") -> "dict[str, Any]":
    damage = record.get("damage") or {}
    findings = record.get("text_findings") or {}
    row = {key: record.get(key) for key in CSV_COLUMNS}
    row["damage_painted"] = damage.get("painted_count")
    row["damage_replaced"] = damage.get("replaced_count")
    row["heavy_damage_record"] = damage.get("heavy_damage_record")
    row["tramer_text"] = findings.get("tramer_amount")
    row["otv_exempt"] = findings.get("otv_exempt")
    row["red_flags"] = " | ".join(record.get("red_flags") or [])
    return row


def write_csv(path: Path, records: Iterable["dict[str, Any]"]) -> Path:
    """Excel-friendly CSV.

    ``utf-8-sig`` so Excel on Windows renders ş/ğ/ı/İ correctly, and ``;`` as the
    delimiter because a comma-decimal locale uses semicolon as its list separator —
    a comma-delimited file lands entirely in column A.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=CSV_COLUMNS, delimiter=";", extrasaction="ignore"
        )
        writer.writeheader()
        for record in records:
            writer.writerow(_flatten_for_csv(record))
    return path
