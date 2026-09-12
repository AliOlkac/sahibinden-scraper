"""``doctor`` — tell me which selector broke, before I waste a crawl on it.

The failure mode of a scraper against a site it cannot test against is not a crash.
It is a run that "succeeds" with 200 OK and N rows while one selector quietly
returns empty for every listing. This command is the antidote: point it at a saved
page and it prints, field by field, what was found and what was not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .extract import Extractor, label_pairs, node_text, parse_html
from .search import detect_page_kind, parse_search_page
from .detail import parse_detail_page
from .textnorm import istanbul_today, tr_key

__all__ = ["diagnose_file", "print_report"]

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# Fields without which the export is not worth producing.
CRITICAL_SEARCH = ("id", "url", "title", "price")
CRITICAL_DETAIL = ("id", "year", "km", "fuel", "transmission", "price")


def diagnose_file(path: Path, config: "dict[str, Any]") -> "dict[str, Any]":
    """Parse one saved page and report per-field coverage."""
    path = Path(path)
    html = path.read_text(encoding="utf-8", errors="replace")
    kind = detect_page_kind(html, config)
    now = istanbul_today()

    report: "dict[str, Any]" = {
        "file": str(path),
        "kind": kind,
        "bytes": len(html),
        "fields": [],
        "critical_missing": [],
        "notes": [],
    }

    if kind == "search":
        page = parse_search_page(html, config, now=now)
        report["row_count"] = len(page.listings)
        report["skipped_promo"] = page.skipped_promo
        report["notes"].extend(page.warnings)
        sample = page.listings[0] if page.listings else None
        for name in (
            "id", "url", "title", "price", "currency", "year", "km", "color",
            "brand", "series", "model", "listed_date", "city", "district",
        ):
            value = getattr(sample, name, None) if sample else None
            report["fields"].append(
                {"field": name, "found": value not in (None, "", []), "sample": _short(value)}
            )
        report["critical_missing"] = [
            f["field"] for f in report["fields"]
            if f["field"] in CRITICAL_SEARCH and not f["found"]
        ]

    elif kind == "detail":
        listing = parse_detail_page(html, config, now=now)
        report["row_count"] = 1
        for name in (
            "id", "title", "price", "currency", "year", "km", "brand", "series",
            "model", "fuel", "transmission", "body_type", "engine_power_hp_min",
            "engine_volume_cc_min", "drivetrain", "color", "condition", "warranty",
            "seller_type", "seller_name", "city", "district", "listed_date",
            "description", "thumbnail_url",
        ):
            value = getattr(listing, name, None)
            report["fields"].append(
                {"field": name, "found": value not in (None, "", []), "sample": _short(value)}
            )
        damage = listing.damage
        report["fields"].append(
            {
                "field": "damage.status_by_panel",
                "found": bool(damage.status_by_panel),
                "sample": _short(f"{len(damage.status_by_panel)} panel"),
            }
        )
        report["fields"].append(
            {
                "field": "features",
                "found": bool(listing.features or listing.features_absent),
                "sample": _short(f"{listing.feature_count} seçili"),
            }
        )
        report["critical_missing"] = [
            f["field"] for f in report["fields"]
            if f["field"] in CRITICAL_DETAIL and not f["found"]
        ]
        report["notes"].extend(listing.parse_warnings)
        if damage.crosscheck_ok is False:
            report["notes"].append(
                "boya panel sayımı sitenin kendi sayacıyla uyuşmuyor — "
                "local-painted-new / painted-new ayrımını kontrol edin"
            )
        # Show what the page actually offered, so a failed alias is obvious.
        soup = parse_html(html)
        extractor = Extractor(config)
        scope = extractor.node(soup, "detail", "scope") or soup
        pairs = label_pairs(scope)
        mapped = {
            tr_key(a)
            for aliases in ((config.get("detail") or {}).get("labels") or {}).values()
            for a in (aliases or ())
        }
        report["unmapped_labels"] = sorted(set(pairs) - mapped)
        report["all_labels_found"] = sorted(pairs)
    else:
        report["row_count"] = 0
        report["notes"].append(
            f"Bu dosya bir arama ya da ilan sayfası değil ({kind}). "
            "Doğrulama ekranı kaydetmiş olabilirsiniz."
        )

    found = sum(1 for f in report["fields"] if f["found"])
    total = len(report["fields"]) or 1
    report["coverage_pct"] = round(100.0 * found / total, 1)
    return report


def _short(value: Any, limit: int = 58) -> Optional[str]:
    if value in (None, "", []):
        return None
    text = str(value).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def print_report(report: "dict[str, Any]", *, colour: bool = True) -> None:
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if colour else text

    print()
    print(f"  {report['file']}")
    print(
        f"  tür: {report['kind']}   satır: {report.get('row_count', 0)}   "
        f"boyut: {report['bytes'] // 1024} KB   kapsam: {report['coverage_pct']}%"
    )
    print(f"  {'alan':<28} {'durum':<10} örnek")
    for entry in report["fields"]:
        if entry["found"]:
            status, code = "BULUNDU", _GREEN
        elif entry["field"] in CRITICAL_SEARCH or entry["field"] in CRITICAL_DETAIL:
            status, code = "EKSİK", _RED
        else:
            status, code = "yok", _DIM
        line = f"  {entry['field']:<28} {status:<10} {entry['sample'] or ''}"
        print(paint(line, code))

    if report.get("unmapped_labels"):
        print(paint("\n  Eşlenmemiş etiketler (config'e eklenebilir):", _YELLOW))
        for label in report["unmapped_labels"][:25]:
            print(f"    - {label}")

    for note in report.get("notes", []):
        print(paint(f"  ! {note}", _YELLOW))

    if report["critical_missing"]:
        print(
            paint(
                f"\n  KRİTİK: {', '.join(report['critical_missing'])} okunamadı. "
                "config/selectors.yaml içindeki ilgili seçiciyi düzeltin.",
                _RED,
            )
        )
