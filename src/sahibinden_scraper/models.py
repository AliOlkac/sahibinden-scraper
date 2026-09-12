"""The listing record — the shape an LLM actually reads.

Design notes, because this schema *is* the product:

* **Flat where possible.** Nesting is reserved for the genuinely repeating
  structures (damage panels, feature groups).
* **Units live in field names** (``km``, ``engine_power_hp_min``,
  ``avg_consumption_l_per_100km``) so no unit ambiguity survives into the prompt.
* **Explicit nulls.** Missing is ``None``, never ``0`` or ``""``. A car with no
  declared Tramer record and one with a declared 0 TL record are different facts.
* **Ranges stay ranges.** sahibinden publishes Motor Gücü and Motor Hacmi as
  dropdown *buckets* ("101 - 125 hp"). Emitting a midpoint invents precision the
  site never had and invites a model to rank two indistinguishable cars.
* **Provenance is explicit.** Anything mined out of the seller's free text is
  tagged ``description_regex`` and never presented as a registry fact.
* **Derived arithmetic is precomputed** — ranking 300 rows by km/year is exactly
  what a model does badly and a scraper does for free.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

__all__ = [
    "DamageReport",
    "TextFindings",
    "Listing",
    "FIELD_DICTIONARY",
    "PROVENANCE",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = "1.1"

# The three panel states that count as "not original", kept in one place because
# the painted/local-painted distinction is easy to lose track of.
PAINTED_STATES = ("boyali", "lokal_boyali")


@dataclass
class DamageReport:
    """The "Boya, değişen" panel declaration plus anything mined about Tramer.

    ``status_by_panel`` is the single source of truth: the per-state lists are
    derived from it, never authored alongside it.

    Damage record, paint status and Tramer are **three independent axes**.
    "Boyasız değişensiz, ağır hasar kayıtlı" is a legitimate and common
    combination (a professionally restored car), so they are never merged into one
    condition score.
    """

    status_by_panel: "dict[str, str]" = field(default_factory=dict)
    summary_text: Optional[str] = None
    heavy_damage_record: Optional[bool] = None
    declared: bool = False

    # The site renders its own counters; parsing them separately gives a free
    # crosscheck against our class-token parse of the diagram.
    painted_count_css: Optional[int] = None
    replaced_count_css: Optional[int] = None

    def _panels(self, *states: str) -> "list[str]":
        return sorted(name for name, state in self.status_by_panel.items() if state in states)

    @property
    def original(self) -> "list[str]":
        return self._panels("orijinal")

    @property
    def painted(self) -> "list[str]":
        return self._panels("boyali")

    @property
    def locally_painted(self) -> "list[str]":
        return self._panels("lokal_boyali")

    @property
    def replaced(self) -> "list[str]":
        return self._panels("degismis")

    @property
    def painted_count(self) -> int:
        """Fully painted + locally painted. Stated explicitly because the site's
        own counter counts only ``painted-new`` and the two disagree by design."""
        return len(self._panels(*PAINTED_STATES))

    @property
    def replaced_count(self) -> int:
        return len(self.replaced)

    @property
    def crosscheck_ok(self) -> Optional[bool]:
        """Does our diagram parse agree with the site's own counters?

        A mismatch is the signature of the ``local-painted-new`` /
        ``painted-new`` substring trap, so it is surfaced rather than swallowed.
        """
        if not self.declared or self.painted_count_css is None:
            return None
        return len(self.painted) == self.painted_count_css and (
            self.replaced_count_css is None or self.replaced_count == self.replaced_count_css
        )

    def to_dict(self) -> "dict[str, Any]":
        return {
            "declared": self.declared,
            "heavy_damage_record": self.heavy_damage_record,
            "painted_count": self.painted_count if self.declared else None,
            "locally_painted_count": len(self.locally_painted) if self.declared else None,
            "replaced_count": self.replaced_count if self.declared else None,
            "original": self.original,
            "painted": self.painted,
            "locally_painted": self.locally_painted,
            "replaced": self.replaced,
            "status_by_panel": self.status_by_panel,
            "summary_text": self.summary_text,
            "panel_crosscheck_ok": self.crosscheck_ok,
        }


@dataclass
class TextFindings:
    """Facts mined from the seller's own prose.

    Every one of these is an **unverified seller assertion**, not a registry
    lookup. They live in their own block, and the codebook marks the whole block
    ``description_regex``, so a model cannot mistake them for structured data.
    """

    tramer_amount: Optional[int] = None
    tramer_declared_none: Optional[bool] = None
    claims_clean: Optional[bool] = None
    pert_record: Optional[bool] = None
    otv_exempt: Optional[bool] = None
    lpg_fitted: Optional[bool] = None
    service_history: Optional[bool] = None
    second_key: Optional[bool] = None
    credit_eligible: Optional[bool] = None
    inspection_valid_year: Optional[int] = None
    negotiable: Optional[bool] = None
    no_negotiation: Optional[bool] = None

    def to_dict(self) -> "dict[str, Any]":
        data = asdict(self)
        data["_provenance"] = "description_regex"
        return data


@dataclass
class Listing:
    """One sahibinden car classified, normalised."""

    # --- identity -------------------------------------------------------- #
    id: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    status: str = "active"           # active | removed | error

    # --- money ----------------------------------------------------------- #
    price: Optional[int] = None
    currency: Optional[str] = None
    price_try: Optional[int] = None  # only set when it can be stated honestly
    price_try_source: Optional[str] = None  # "native" | "fx:<rate>" | None

    # --- the numbers every buyer sorts on -------------------------------- #
    year: Optional[int] = None
    km: Optional[int] = None

    # --- what car is it -------------------------------------------------- #
    brand: Optional[str] = None
    series: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None

    # --- mechanicals ----------------------------------------------------- #
    fuel: Optional[str] = None
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    engine_power_hp_min: Optional[int] = None
    engine_power_hp_max: Optional[int] = None
    engine_power_is_bucket: bool = False
    engine_volume_cc_min: Optional[int] = None
    engine_volume_cc_max: Optional[int] = None
    engine_volume_is_bucket: bool = False
    drivetrain: Optional[str] = None
    color: Optional[str] = None
    condition: Optional[str] = None
    avg_consumption_l_per_100km: Optional[float] = None
    fuel_tank_litres: Optional[int] = None

    # --- condition / history --------------------------------------------- #
    damage: DamageReport = field(default_factory=DamageReport)
    text_findings: TextFindings = field(default_factory=TextFindings)
    warranty: Optional[bool] = None
    plate_nationality: Optional[str] = None

    # --- seller ---------------------------------------------------------- #
    seller_type: Optional[str] = None
    seller_name: Optional[str] = None
    is_dealer: Optional[bool] = None
    exchange_accepted: Optional[bool] = None

    # --- where / when ---------------------------------------------------- #
    city: Optional[str] = None
    district: Optional[str] = None
    neighborhood: Optional[str] = None
    listed_date: Optional[str] = None

    # --- content --------------------------------------------------------- #
    description: Optional[str] = None
    description_truncated: bool = False
    features: "dict[str, list[str]]" = field(default_factory=dict)
    features_absent: "dict[str, list[str]]" = field(default_factory=dict)
    thumbnail_url: Optional[str] = None
    image_urls: "list[str]" = field(default_factory=list)
    image_count: Optional[int] = None

    # --- provenance / health --------------------------------------------- #
    scraped_at: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    detail_fetched: bool = False
    parse_warnings: "list[str]" = field(default_factory=list)
    field_conflicts: "list[str]" = field(default_factory=list)
    extras: "dict[str, str]" = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Derived
    # ------------------------------------------------------------------ #

    @property
    def engine_power_hp(self) -> Optional[int]:
        """A single number *only* when the site gave one. Buckets stay None."""
        if self.engine_power_is_bucket:
            return None
        return self.engine_power_hp_min

    @property
    def engine_volume_cc(self) -> Optional[int]:
        if self.engine_volume_is_bucket:
            return None
        return self.engine_volume_cc_min

    @property
    def tax_band_ambiguous(self) -> Optional[bool]:
        """True when the engine-volume bucket straddles a Turkish tax boundary.

        1600cc and 2000cc are the ÖTV/MTV band edges and move the price more than
        almost anything else, so a bucket like "1401 - 1600" that sits right on one
        must not be treated as settled.
        """
        low, high = self.engine_volume_cc_min, self.engine_volume_cc_max
        if low is None or high is None or low == high:
            return None
        return any(low < edge < high for edge in (1600, 2000))

    def age_years(self, reference_year: Optional[int]) -> Optional[int]:
        if self.year is None or reference_year is None:
            return None
        return max(0, reference_year - self.year)

    def km_per_year(self, reference_year: Optional[int]) -> Optional[int]:
        age = self.age_years(reference_year)
        if self.km is None or not age:
            return None
        return int(round(self.km / age))

    @property
    def feature_count(self) -> int:
        return sum(len(v) for v in self.features.values())

    def red_flags(self, reference_year: Optional[int] = None) -> "list[str]":
        """One array of reason strings beats booleans scattered across the record —
        it survives compaction and reads as a checklist."""
        flags: "list[str]" = []
        d, t = self.damage, self.text_findings

        if t.claims_clean and d.declared and (d.painted_count or d.replaced_count):
            flags.append(
                f"ilan_metni_hatasiz_diyor_ama_{d.painted_count}_boyali_{d.replaced_count}_degisen"
            )
        if d.heavy_damage_record:
            flags.append("agir_hasar_kayitli")
        if t.pert_record:
            flags.append("pert_kaydi_metinde_geciyor")
        if t.otv_exempt:
            flags.append("otv_muafiyetli_arac_devir_kisiti_olabilir")
        if d.crosscheck_ok is False:
            flags.append("boya_panel_sayimi_tutmuyor")
        kmy = self.km_per_year(reference_year)
        if kmy is not None and kmy > 40000:
            flags.append(f"yillik_km_cok_yuksek_{kmy}")
        if kmy is not None and kmy < 3000 and (self.age_years(reference_year) or 0) >= 3:
            flags.append(f"yillik_km_supheli_dusuk_{kmy}")
        if self.currency and self.currency != "TRY":
            flags.append(f"fiyat_{self.currency}_cinsinden")
        if not self.detail_fetched:
            flags.append("detay_sayfasi_okunmadi_teknik_alanlar_eksik")
        if self.field_conflicts:
            flags.append("liste_ve_detay_sayfasi_celisiyor")
        if self.status != "active":
            flags.append(f"ilan_durumu_{self.status}")
        return flags

    def digest(self, reference_year: Optional[int] = None) -> str:
        """One dense line summarising the car, for cheap scanning."""

        def _tr_num(value: int) -> str:
            return f"{value:,}".replace(",", ".")

        bits = [
            " ".join(p for p in (self.brand, self.series, self.model) if p)
            or (self.title or "?"),
            str(self.year) if self.year else "yıl yok",
            f"{_tr_num(self.km)} km" if self.km is not None else "km yok",
            f"{_tr_num(self.price)} {self.currency}" if self.price else "fiyat yok",
        ]
        mech = "/".join(p for p in (self.fuel, self.transmission) if p)
        if mech:
            bits.append(mech)
        if self.damage.declared:
            bits.append(
                f"{len(self.damage.painted)} boyalı, "
                f"{len(self.damage.locally_painted)} lokal, "
                f"{self.damage.replaced_count} değişen"
            )
        elif self.damage.heavy_damage_record:
            bits.append("ağır hasar kayıtlı")
        if self.text_findings.tramer_amount:
            bits.append(f"tramer~{_tr_num(self.text_findings.tramer_amount)} (metinden)")
        where = ", ".join(p for p in (self.city, self.district) if p)
        if where:
            bits.append(where)
        if self.seller_type:
            bits.append(self.seller_type)
        return " | ".join(bits)

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def to_dict(
        self,
        *,
        reference_year: Optional[int] = None,
        include_images: bool = False,
    ) -> "dict[str, Any]":
        """Ordered, LLM-facing dict: identity, then decision-driving numbers, then
        detail, then provenance last."""
        record: "dict[str, Any]" = {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "status": self.status,
            "digest": self.digest(reference_year),
            "red_flags": self.red_flags(reference_year),
            "price": self.price,
            "currency": self.currency,
            "price_try": self.price_try,
            "price_try_source": self.price_try_source,
            "year": self.year,
            "km": self.km,
            "age_years": self.age_years(reference_year),
            "km_per_year": self.km_per_year(reference_year),
            "brand": self.brand,
            "series": self.series,
            "model": self.model,
            "trim": self.trim,
            "fuel": self.fuel,
            "transmission": self.transmission,
            "body_type": self.body_type,
            "engine_power_hp": self.engine_power_hp,
            "engine_power_hp_min": self.engine_power_hp_min,
            "engine_power_hp_max": self.engine_power_hp_max,
            "engine_power_is_bucket": self.engine_power_is_bucket,
            "engine_volume_cc": self.engine_volume_cc,
            "engine_volume_cc_min": self.engine_volume_cc_min,
            "engine_volume_cc_max": self.engine_volume_cc_max,
            "engine_volume_is_bucket": self.engine_volume_is_bucket,
            "tax_band_ambiguous": self.tax_band_ambiguous,
            "drivetrain": self.drivetrain,
            "color": self.color,
            "condition": self.condition,
            "avg_consumption_l_per_100km": self.avg_consumption_l_per_100km,
            "fuel_tank_litres": self.fuel_tank_litres,
            "damage": self.damage.to_dict(),
            "text_findings": self.text_findings.to_dict(),
            "warranty": self.warranty,
            "plate_nationality": self.plate_nationality,
            "seller_type": self.seller_type,
            "seller_name": self.seller_name,
            "is_dealer": self.is_dealer,
            "exchange_accepted": self.exchange_accepted,
            "city": self.city,
            "district": self.district,
            "neighborhood": self.neighborhood,
            "listed_date": self.listed_date,
            "feature_count": self.feature_count,
            "features": self.features,
            "features_absent": self.features_absent,
            "description": self.description,
            "description_truncated": self.description_truncated,
            "thumbnail_url": self.thumbnail_url,
            "image_count": self.image_count,
            "scraped_at": self.scraped_at,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "detail_fetched": self.detail_fetched,
            "parse_warnings": self.parse_warnings,
            "field_conflicts": self.field_conflicts,
            "extras": self.extras,
        }
        if include_images:
            record["image_urls"] = self.image_urls
        return record


PROVENANCE: "dict[str, str]" = {
    "structured": "Sayfadaki etiketli alandan birebir okundu.",
    "derived": "Scraper hesapladı (yaş, km/yıl, sıralamalar).",
    "seller_text": "Satıcının serbest metninden regex ile çıkarıldı; DOĞRULANMAMIŞ beyandır.",
    "cohort": "Sadece BU çalışmadaki ilanlar üzerinden hesaplandı; piyasanın tamamı değildir.",
}

FIELD_DICTIONARY: "dict[str, str]" = {
    "id": "sahibinden ilan numarası (benzersiz). [structured]",
    "url": "İlanın tam adresi. [structured]",
    "status": "active | removed | error. removed = ilan çalışma sırasında yayından kalkmış. [derived]",
    "digest": "Tek satırlık özet; yapılandırılmış alanların insan okunur tekrarı. [derived]",
    "red_flags": "Dikkat edilmesi gereken durumların listesi. Boş liste = tespit edilen sorun yok. [derived]",
    "price": "İlandaki fiyat, kendi para biriminde. Pazarlık payı dahil değildir. [structured]",
    "currency": "TRY | USD | EUR | GBP. Farklı para birimleri BİREBİR KIYASLANAMAZ. [structured]",
    "price_try": "TL karşılığı. Sadece ilan zaten TL ise ya da kur verildiyse dolar. null ise price/currency kullanın. [derived]",
    "price_try_source": "native = ilan zaten TL; fx:<kur> = verilen kurla çevrildi. [derived]",
    "year": "Model yılı. [structured]",
    "km": "Kilometre (gösterge değeri, satıcı beyanı). [structured]",
    "age_years": "reference_year - year. [derived]",
    "km_per_year": "km / age_years. Yılı bu yıl olan araçlarda null. [derived]",
    "brand": "Marka. [structured]",
    "series": "Seri. [structured]",
    "model": "Model / motor-donanım satırı. [structured]",
    "fuel": "Benzin | Dizel | LPG | Benzin & LPG | Hibrit | Elektrik. [structured]",
    "transmission": "Manuel | Otomatik | Yarı Otomatik. [structured]",
    "engine_power_hp": "Kesin motor gücü. sahibinden bunu ARALIK olarak yayınladıysa null olur - o durumda min/max kullanın. [structured]",
    "engine_power_hp_min/max": "Motor gücü aralığı (HP). is_bucket=true ise kesin değer YOKTUR. [structured]",
    "engine_volume_cc_min/max": "Motor hacmi aralığı (cc). is_bucket=true ise kesin değer YOKTUR. [structured]",
    "tax_band_ambiguous": "true = motor hacmi aralığı 1600/2000cc ÖTV-MTV sınırını kesiyor; vergi sınıfı belirsiz. [derived]",
    "damage.declared": "Satıcı panel panel boya/değişen beyanı girmiş mi. false ise sayımlar null'dur. [structured]",
    "damage.heavy_damage_record": "'Ağır Hasar Kayıtlı' alanı. Boya/değişen ve Tramer'den BAĞIMSIZ bir eksendir. [structured]",
    "damage.painted_count": "Boyalı + lokal boyalı panel sayısı. [derived]",
    "damage.replaced_count": "Değişmiş panel sayısı. [derived]",
    "damage.status_by_panel": "Panel adı -> orijinal | boyali | lokal_boyali | degismis. Tek doğruluk kaynağı. [structured]",
    "damage.panel_crosscheck_ok": "false = bizim panel sayımımız sitenin kendi sayacıyla uyuşmuyor; bu kayda güvenmeyin. [derived]",
    "text_findings": "TAMAMI satıcının serbest metninden regex ile çıkarıldı. Doğrulanmamış beyandır. [seller_text]",
    "text_findings.tramer_amount": "Metinde geçen Tramer tutarı. Resmî sorgu DEĞİLDİR. null = metinde bulunamadı.",
    "text_findings.otv_exempt": "ÖTV muafiyetli / engelli aracı. Fiyatı %20-40 etkiler ve devir kısıtı taşır.",
    "text_findings.pert_record": "'Pert' / 'hasarlı araç' ifadesi geçiyor. Ağır hasar kaydından farklı bir şeydir.",
    "warranty": "Garanti var mı. [structured]",
    "seller_type": "Sahibinden | Galeriden | Yetkili Bayiden. [structured]",
    "listed_date": "İlan tarihi (ISO). DİKKAT: bu SON GÜNCELLEME tarihidir - sahibinden'in 'doping' ürünü bunu bugüne çeker, yani yeniliğe göre sıralama manipüle edilebilir. [structured]",
    "feature_count": "Seçili donanım sayısı; donanım zenginliği için kaba bir vekil. [derived]",
    "features": "Grup -> araçta OLAN donanımlar. [structured]",
    "features_absent": "Grup -> araçta OLMAYAN donanımlar. Site tüm seçenekleri listelediği için bu bilgi bedava gelir. [structured]",
    "description": "Satıcının serbest metni; uzunsa kırpılır. [structured]",
    "detail_fetched": "false ise SADECE liste sayfası verisi vardır; teknik alanların tamamı eksiktir. [derived]",
    "parse_warnings": "Bu kayıtta bulunamayan/şüpheli alanlar. Boş olmayan liste = temkinli yaklaşın. [derived]",
    "field_conflicts": "Liste sayfası ile detay sayfasının çeliştiği alanlar (ör. fiyat düşmüş olabilir). [derived]",
    "extras": "Eşlenemeyen ama sayfada bulunan ham etiket -> değer çiftleri. [structured]",
    "cohort_key": "marka|seri|yıl|yakıt|vites. Kıyaslanabilir araç grubu. [cohort]",
    "cohort_size": "Kohorttaki ilan sayısı. [cohort]",
    "cohort_priced_count": "Kohortta TL fiyatı OLAN ilan sayısı. Fiyat karşılaştırmaları yalnızca bunlar üzerinden yapılır. [cohort]",
    "cohort_km_count": "Kohortta KM bilgisi olan ilan sayısı. [cohort]",
    "cohort_widened": "true = kohortta 5'ten az ilan vardı, yıl kaldırılarak genişletildi; kıyas daha gevşektir. [cohort]",
    "cohort_scope": "Her zaman 'this_run_only': kohort yalnızca bu çalışmadaki ilanlardır, piyasanın tamamı değil. [cohort]",
    "price_pct_rank_in_cohort": "Kohort içinde fiyat yüzdelik sırası (0 = en ucuz). Tek fiyatlı üye varsa null. [cohort]",
    "km_pct_rank_in_cohort": "Kohort içinde KM yüzdelik sırası (0 = en düşük KM). [cohort]",
    "price_vs_cohort_median_pct": "Kohort medyanına göre fark, yüzde. En az 2 fiyatlı üye gerekir, yoksa null. [cohort]",
    "index": "Dosyadaki sıra. Kayıtlar kohorta, sonra fiyata göre sıralıdır. [derived]",
}
