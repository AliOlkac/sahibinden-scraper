"""Generate synthetic sahibinden-shaped pages for the test suite.

These are NOT copies of sahibinden pages. They are hand-built markup that
reproduces the *structures* the selector config targets — the two near-identical
``searchResults*AttributeValue`` cell families, the ``local-painted-new`` /
``painted-new`` class-token trap, ``li.selected`` equipment lists, bucketed engine
specs — so the parser's logic can be tested without fetching (or redistributing)
anything from the site.

Real pages you save yourself go in ``tests/fixtures/real/``, which is gitignored.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# Padding to clear the guards.min_body_bytes floor, mimicking a real page's bulk.
_BULK = "<div class='filler'>%s</div>" % ("lorem ipsum dolor sit amet " * 1800)


def _row(
    *,
    cid: str,
    slug: str,
    title: str,
    brand: str,
    series: str,
    model: str,
    year: str,
    km: str,
    color: str,
    price: str,
    date_text: str,
    city: str,
    district: str,
    store: str | None = None,
    promo: bool = False,
) -> str:
    classes = "searchResultsItem"
    if promo:
        classes += " nativeAd searchResultsPromoSuper"
    store_attr = f' data-store_name="{store}"' if store else ""
    return f"""
      <tr class="{classes} " data-id="{cid}"{store_attr}>
        <td class="searchResultsLargeThumbnail">
          <a href="/ilan/vasita-otomobil-{slug}-{cid}/detay">
            <img src="https://i0.shbdn.com/photos/00/00/00/thmb_{cid}001.jpg">
          </a>
        </td>
        <td class="searchResultsTagAttributeValue">{brand}</td>
        <td class="searchResultsTagAttributeValue">{series}</td>
        <td class="searchResultsTagAttributeValue">{model}</td>
        <td class="searchResultsTitleValue">
          <a class="classifiedTitle" href="/ilan/vasita-otomobil-{slug}-{cid}/detay">{title}</a>
        </td>
        <td class="searchResultsAttributeValue">{year}</td>
        <td class="searchResultsAttributeValue">{km}</td>
        <td class="searchResultsAttributeValue">{color}</td>
        <td class="searchResultsPriceValue">
          <div class="classified-price-container"><span>{price}</span></div>
        </td>
        <td class="searchResultsDateValue"><span>{date_text}</span></td>
        <td class="searchResultsLocationValue">{city}<br>{district}</td>
      </tr>"""


def search_page() -> str:
    rows = [
        _row(cid="1240940267", slug="ford-focus-1-5-tdci-titanium",
             title="2018 FORD FOCUS 1.5 TDCi TITANIUM OTOMATİK",
             brand="Ford", series="Focus", model="1.5 TDCi Titanium",
             year="2018", km="96.000", color="Beyaz", price="1.250.000 TL",
             date_text="30 Ağustos 2026", city="İstanbul", district="Kadıköy",
             store="Kadıköy Oto"),
        _row(cid="1240940268", slug="ford-focus-1-6-tdci-trend-x",
             title="2017 Ford Focus 1.6 TDCi Trend X — Hatasız",
             brand="Ford", series="Focus", model="1.6 TDCi Trend X",
             year="2017", km="142.500", color="Gri", price="1.010.000 TL",
             date_text="Bugün", city="Ankara", district="Çankaya"),
        _row(cid="1240940269", slug="ford-focus-1-5-tdci-titanium-2",
             title="2018 Focus Titanium — Değişensiz",
             brand="Ford", series="Focus", model="1.5 TDCi Titanium",
             year="2018", km="88.000", color="Siyah", price="1.395.000 TL",
             date_text="Dün", city="İzmir", district="Karşıyaka"),
        # Promoted duplicate of the first row — must be skipped, not counted twice.
        _row(cid="1240940267", slug="ford-focus-1-5-tdci-titanium",
             title="2018 FORD FOCUS 1.5 TDCi TITANIUM OTOMATİK",
             brand="Ford", series="Focus", model="1.5 TDCi Titanium",
             year="2018", km="96.000", color="Beyaz", price="1.250.000 TL",
             date_text="30 Ağustos 2026", city="İstanbul", district="Kadıköy",
             promo=True),
        # A euro-priced listing: must not be silently compared against lira rows.
        _row(cid="1240940270", slug="ford-focus-st-line",
             title="2019 Ford Focus ST-Line",
             brand="Ford", series="Focus", model="1.5 EcoBoost ST-Line",
             year="2019", km="61.000", color="Mavi", price="32.500 €",
             date_text="12 Ocak", city="Bursa", district="Nilüfer"),
    ]
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>Ford Focus ilanları - sahibinden.com</title></head>
<body>
  <div class="result-text"><span>1.842 ilan bulundu</span></div>
  <table id="searchResultsTable">
    <thead><tr>
      <th></th><th>Marka</th><th>Seri</th><th>Model</th><th>İlan Başlığı</th>
      <th>Yıl</th><th>KM</th><th>Renk</th><th>Fiyat</th><th>İlan Tarihi</th><th>İl / İlçe</th>
    </tr></thead>
    <tbody class="searchResultsRowClass">{"".join(rows)}</tbody>
  </table>
  <a class="prevNextBut" title="Sonraki" href="/ford-focus?pagingOffset=50">Sonraki</a>
  <input id="currentPageValue" value="1">
  {_BULK}
</body></html>"""


_PANELS = [
    ("front-bumper", "painted-new"),
    ("front-hood", "original-new"),
    ("roof", "original-new"),
    # The trap: this panel is LOCALLY painted. A substring match on "painted-new"
    # would misread it as fully painted and mis-price the car.
    ("front-right-mudguard", "local-painted-new"),
    ("front-right-door", "original-new"),
    ("rear-right-door", "original-new"),
    ("rear-right-mudguard", "original-new"),
    ("front-left-mudguard", "original-new"),
    ("front-left-door", "changed-new"),
    ("rear-left-door", "original-new"),
    ("rear-left-mudguard", "original-new"),
    ("rear-hood", "original-new"),
    ("rear-bumper", "original-new"),
]


def detail_page() -> str:
    panels = "".join(
        f'<div class="{name} {state}"></div>' for name, state in _PANELS
    )
    info_rows = [
        ("İlan No", "1240940267"),
        ("İlan Tarihi", "30 Ağustos 2026"),
        ("Marka", "Ford"),
        ("Seri", "Focus"),
        ("Model", "1.5 TDCi Titanium"),
        ("Yıl", "2018"),
        ("Yakıt", "Dizel"),
        ("Vites", "Otomatik"),
        ("Araç Durumu", "İkinci El"),
        ("KM", "96.000"),
        ("Kasa Tipi", "Sedan"),
        # Buckets, not exact numbers.
        ("Motor Gücü", "101 - 125 hp"),
        ("Motor Hacmi", "1401 - 1600 cm3"),
        ("Çekiş", "Önden Çekiş"),
        ("Renk", "Beyaz"),
        ("Garanti", "Hayır"),
        ("Ağır Hasar Kayıtlı", "Hayır"),
        ("Plaka / Uyruk", "Türkiye (TR) Plakalı"),
        ("Kimden", "Galeriden"),
        ("Takas", "Evet"),
        # An unmapped label, to prove it lands in extras{} rather than vanishing.
        ("Muayene", "06/2027"),
    ]
    info_html = "".join(
        f"<li><strong>{label}</strong><span>{value}</span></li>" for label, value in info_rows
    )
    spec_html = "".join(
        f'<tr><td class="title">{label}</td><td class="value">{value}</td></tr>'
        for label, value in [
            ("Ort. Yakıt Tüketimi", "4,6 lt"),
            ("Yakıt Deposu", "52 lt"),
            ("Hızlanma 0-100 km/saat", "10,5 sn"),
        ]
    )
    safety = ["ABS", "ESP / VSA", "Yokuş Kalkış Desteği", "Şerit Takip Sistemi", "Gece Görüş"]
    interior = ["Deri Koltuk", "Hız Sabitleyici", "Elektrikli Ön Camlar", "Sunroof"]
    selected = {"ABS", "ESP / VSA", "Yokuş Kalkış Desteği", "Şerit Takip Sistemi",
                "Deri Koltuk", "Hız Sabitleyici", "Elektrikli Ön Camlar"}

    def _items(options: list[str]) -> str:
        return "".join(
            f'<li class="{"selected" if o in selected else ""}">{o}</li>' for o in options
        )

    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>2018 Ford Focus - sahibinden.com</title>
<meta property="og:image" content="https://i0.shbdn.com/photos/12/40/94/x5_1240940267001.jpg"></head>
<body>
<div class="classifiedDetail" id="classifiedDetail">
  <div id="classifiedId" data-classifiedid="1240940267"></div>
  <div class="classifiedDetailTitle"><h1>2018 FORD FOCUS 1.5 TDCi TITANIUM OTOMATİK</h1></div>
  <div class="classifiedInfo">
    <h2><a href="/istanbul">İstanbul</a> <a href="/kadikoy">Kadıköy</a> <a href="/caferaga">Caferağa Mah.</a></h2>
    <h3>1.250.000 TL</h3>
    <ul class="classifiedInfoList">{info_html}</ul>
  </div>
  <input type="hidden" id="favoriteClassifiedPrice" value="1250000">
  <table class="classifiedInfo"><tbody>{spec_html}</tbody></table>

  <div class="car-parts">{panels}</div>
  <div class="car-damage-info-list">
    <ul>
      <li class="pair-title">Boyalı</li>
      <li class="selected-damage">Ön Tampon</li>
      <li class="pair-title">Değişen</li>
      <li class="selected-damage">Sol Ön Kapı</li>
    </ul>
  </div>

  <div id="classifiedProperties">
    <h3>Güvenlik</h3><ul>{_items(safety)}</ul>
    <h3>İç Donanım</h3><ul>{_items(interior)}</ul>
    <h3>Boyalı / Değişen</h3><ul><li class="selected">Ön Tampon</li></ul>
  </div>

  <div class="classifiedDetailMainPhoto">
    <label><img src="https://i0.shbdn.com/photos/12/40/94/x5_1240940267001.jpg"
                data-source="https://i0.shbdn.com/photos/12/40/94/big_1240940267001.jpg"></label>
  </div>
  <ul class="classifiedDetailThumbList">
    <li><label><img class="thmbImg" src="https://i0.shbdn.com/photos/12/40/94/thmb_1240940267002.jpg"
        data-source="https://i0.shbdn.com/photos/12/40/94/big_1240940267002.jpg"></label></li>
  </ul>

  <div class="storeBox"><a href="/kadikoy-oto"><span class="storeInfo">Kadıköy Oto Galeri</span></a></div>

  <div id="classifiedDescription">
    Aracımız hatasız boyasız değişensiz, tramer kaydı yoktur.
    Bakımları yetkili serviste yapılmıştır. Muayenesi 2027 yılına kadar vardır.
    İkinci anahtar mevcuttur. Krediye uygundur. Takas olur, pazarlık payı vardır.
  </div>
</div>
{_BULK}
</body></html>"""


def detail_otv_page() -> str:
    """A listing whose prose carries the facts with no structured field: ÖTV
    exemption, a Tramer figure, a pert record — and which contradicts itself."""
    return detail_page().replace(
        "Aracımız hatasız boyasız değişensiz, tramer kaydı yoktur.",
        "ÖTV muafiyetli engelli aracıdır. Tramer kaydı 45.000 TL'dir. "
        "Araç pert kayıtlıdır. Hatasız boyasızdır.",
    ).replace("1240940267", "1240940271")


def detail_removed_page() -> str:
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>sahibinden.com</title></head>
<body>
  <div class="error-page">
    <h1>Aradığınız ilan yayından kaldırılmıştır.</h1>
    <p>Benzer ilanlara göz atabilirsiniz.</p>
  </div>
</body></html>"""


def interstitial_page() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>sahibinden.com Yükleniyor</title></head>
<body><p>Tarayıcınızı kontrol ediyoruz...</p>
<p>Devam Et butonuna tıklayarak kaldığınız yerden devam edebilirsiniz.</p></body></html>"""


PAGES = {
    "search_sample.html": search_page,
    "detail_sample.html": detail_page,
    "detail_otv.html": detail_otv_page,
    "detail_removed.html": detail_removed_page,
    "interstitial.html": interstitial_page,
}


def write_all() -> "list[Path]":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = []
    for name, builder in PAGES.items():
        path = FIXTURES / name
        path.write_text(builder(), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write_all():
        print(f"{path}  ({path.stat().st_size // 1024} KB)")
