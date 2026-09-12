"""The human-readable report: an HTML file, optionally printed to PDF.

Chromium is already a dependency (Playwright drives the scrape), so the PDF is
produced by printing this HTML with ``page.pdf()`` rather than pulling in a second
rendering stack. That also means Turkish glyphs come from the system fonts and
simply work, which is not true of every Python PDF library.

Print CSS notes that are easy to get wrong:
* ``print_background`` defaults to **False** in Playwright, so severity colours
  vanish unless it is switched on (the caller does).
* Playwright's ``margin=`` argument is ignored whenever the CSS declares
  ``@page { margin: ... }``, so margins are declared in exactly one place — here.
* ``break-inside: avoid`` does nothing for a card taller than the page, so the
  description is clamped rather than trusted to fit.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Optional, Sequence

__all__ = ["render_html_report"]

_CSS = """
@page { size: A4 landscape; margin: 12mm 10mm; }
* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 9.5px; color: #16181d; margin: 0; background: #fff;
}
h1 { font-size: 18px; margin: 0 0 2px; }
h2 { font-size: 12px; margin: 18px 0 6px; page-break-after: avoid; }
.sub { color: #5b6270; font-size: 9px; margin-bottom: 10px; }
.meta-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px 14px;
  border: 1px solid #dfe3ea; border-radius: 4px; padding: 8px 10px; margin-bottom: 12px;
}
.meta-grid div { font-size: 9px; }
.meta-grid b { display: block; color: #5b6270; font-weight: 600; font-size: 8px;
  text-transform: uppercase; letter-spacing: .03em; }
.warn {
  border-left: 3px solid #c2410c; background: #fff7ed; padding: 7px 10px;
  margin: 10px 0; font-size: 9px; border-radius: 0 3px 3px 0;
}
table { width: 100%; border-collapse: collapse; }
thead { display: table-header-group; }
th {
  text-align: left; font-size: 8px; text-transform: uppercase; letter-spacing: .04em;
  color: #5b6270; border-bottom: 1.5px solid #c8cdd6; padding: 4px 5px; font-weight: 600;
}
td { padding: 4px 5px; border-bottom: 1px solid #eef0f4; vertical-align: top; }
tr { break-inside: avoid; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.cohort td { background: #f3f5f9; font-weight: 600; font-size: 9px; }
.flag { color: #b91c1c; font-size: 8px; }
.muted { color: #8990a0; }
.chip {
  display: inline-block; padding: 0 4px; border-radius: 3px; font-size: 8px;
  background: #eef0f4; margin-right: 3px;
}
.chip.bad { background: #fee2e2; color: #991b1b; }
.chip.ok  { background: #dcfce7; color: #14532d; }
.card { break-inside: avoid; border: 1px solid #dfe3ea; border-radius: 4px;
  padding: 8px 10px; margin-bottom: 8px; }
.card h3 { font-size: 10.5px; margin: 0 0 3px; }
.desc {
  color: #4b5160; font-size: 8.5px; margin-top: 4px;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
  overflow: hidden;
}
a { color: #1d4ed8; text-decoration: none; }
"""


def _fmt_int(value: Optional[Any]) -> str:
    if value is None:
        return "<span class='muted'>—</span>"
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return html.escape(str(value))


def _fmt(value: Optional[Any]) -> str:
    if value is None or value == "":
        return "<span class='muted'>—</span>"
    if isinstance(value, bool):
        return "Evet" if value else "Hayır"
    return html.escape(str(value))


def _damage_chip(record: "dict[str, Any]") -> str:
    damage = record.get("damage") or {}
    if not damage.get("declared"):
        if damage.get("heavy_damage_record"):
            return "<span class='chip bad'>ağır hasar kayıtlı</span>"
        return "<span class='muted'>beyan yok</span>"
    painted = damage.get("painted_count") or 0
    replaced = damage.get("replaced_count") or 0
    if painted == 0 and replaced == 0:
        return "<span class='chip ok'>orijinal</span>"
    return f"<span class='chip'>{painted} boyalı</span><span class='chip'>{replaced} değişen</span>"


def render_html_report(
    records: Sequence["dict[str, Any]"],
    meta: "dict[str, Any]",
    out_path: Path,
) -> Path:
    """Write the HTML report and return its path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: "list[str]" = []
    current_cohort: Optional[str] = None
    for record in records:
        cohort = record.get("cohort_key")
        if cohort != current_cohort:
            current_cohort = cohort
            label = html.escape(str(cohort or "—")).replace("|", " · ")
            size = record.get("cohort_size")
            widened = " (yıl genişletildi)" if record.get("cohort_widened") else ""
            rows.append(
                f"<tr class='cohort'><td colspan='12'>{label} — {size} ilan{widened}</td></tr>"
            )

        # Flags are snake_case identifiers so they stay stable for the model; the
        # printed report is for a person, so they get spaced out here.
        flags = [str(f).replace("_", " ") for f in (record.get("red_flags") or [])]
        flag_html = (
            "<div class='flag'>" + html.escape(" · ".join(flags)) + "</div>" if flags else ""
        )
        title = html.escape(record.get("title") or record.get("digest") or "—")
        url = record.get("url")
        title_html = f"<a href='{html.escape(url)}'>{title}</a>" if url else title

        vs_median = record.get("price_vs_cohort_median_pct")
        vs_median_html = (
            f"{vs_median:+.1f}%" if isinstance(vs_median, (int, float)) else "<span class='muted'>—</span>"
        )

        rows.append(
            "<tr>"
            f"<td>{title_html}{flag_html}</td>"
            f"<td class='num'>{_fmt(record.get('year'))}</td>"
            f"<td class='num'>{_fmt_int(record.get('km'))}</td>"
            f"<td class='num'>{_fmt_int(record.get('km_per_year'))}</td>"
            f"<td class='num'>{_fmt_int(record.get('price'))} {_fmt(record.get('currency'))}</td>"
            f"<td class='num'>{vs_median_html}</td>"
            f"<td>{_fmt(record.get('fuel'))}</td>"
            f"<td>{_fmt(record.get('transmission'))}</td>"
            f"<td>{_damage_chip(record)}</td>"
            f"<td>{_fmt(record.get('seller_type'))}</td>"
            f"<td>{_fmt(record.get('city'))}</td>"
            f"<td class='num'>{_fmt(record.get('listed_date'))}</td>"
            "</tr>"
        )

    empty_fields = meta.get("fields_empty_across_all_listings") or []
    warnings: "list[str]" = []
    if empty_fields:
        warnings.append(
            "Hiçbir ilanda okunamayan alanlar: <b>"
            + html.escape(", ".join(empty_fields))
            + "</b>. Bu alanların boş olması araç hakkında bilgi değil, scraper eksiğidir."
        )
    if meta.get("truncated"):
        warnings.append(
            f"Filtre <b>{_fmt(meta.get('total_count'))}</b> ilan eşleşti ama sayfalama "
            f"<b>{_fmt(meta.get('collected_count'))}</b> tanesine ulaşabildi. "
            "Bu liste eksiktir; 'en ucuz' gibi ifadeler yanıltıcı olur."
        )
    if meta.get("listings_with_parse_warnings"):
        warnings.append(
            f"<b>{meta['listings_with_parse_warnings']}</b> ilanda ayrıştırma uyarısı var; "
            "JSON'daki parse_warnings alanına bakın."
        )

    meta_cells = "".join(
        f"<div><b>{html.escape(label)}</b>{_fmt(value)}</div>"
        for label, value in (
            ("İlan sayısı", meta.get("collected_count")),
            ("Filtrede toplam", meta.get("total_count")),
            ("Detay okunan", meta.get("detail_pages_fetched")),
            ("Uyarılı ilan", meta.get("listings_with_red_flags")),
            ("Çalışma tarihi", meta.get("scraped_at")),
            ("Şema", meta.get("schema_version")),
        )
    )

    document = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<title>sahibinden otomobil karşılaştırma</title>
<style>{_CSS}</style></head>
<body>
<h1>Otomobil ilan karşılaştırması</h1>
<div class="sub">{html.escape(str(meta.get('source_url') or ''))}</div>
<div class="meta-grid">{meta_cells}</div>
{"".join(f"<div class='warn'>{w}</div>" for w in warnings)}
<h2>Kohorta göre sıralı liste</h2>
<table>
  <thead><tr>
    <th>İlan</th><th class="num">Yıl</th><th class="num">KM</th><th class="num">KM/yıl</th>
    <th class="num">Fiyat</th><th class="num">Kohort ort.</th><th>Yakıt</th><th>Vites</th>
    <th>Boya/Değişen</th><th>Kimden</th><th>İl</th><th class="num">İlan tarihi</th>
  </tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
</body></html>
"""
    out_path.write_text(document, encoding="utf-8")
    return out_path
