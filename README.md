# sahibinden-scraper

Filtrelediğiniz sahibinden.com otomobil ilanlarını, yapay zekâya tek seferde
kıyaslatabileceğiniz **tek bir JSON** (veya PDF/CSV) dosyasına dönüştürür.

Yüzlerce ilanı tek tek açıp aklınızda tutmak yerine, hepsini aynı tabloya koyar:
fiyat, yıl, KM, konum, ilan tarihi; detaylı modda ayrıca açıklama, boya/değişen
beyanı, donanım listesi ve teknik özellikler.

---

## Önce okuyun: bu araç sitenin kurallarına aykırı

Bunu gizlemenin anlamı yok, karar sizin olmalı:

- **robots.txt** (en güncel arşiv kopyası: 2025-11-08) tam olarak bu aracın
  hedeflediği iki adres kalıbını `User-agent: *` için kapatıyor:
  - `Disallow: /ilan/vasita*/detay` → ilan detay sayfaları
  - `Disallow: */arama/flt/*`, `*pagingOffset`, `*sorting=`, `*viewType=` →
    filtreli arama ve sayfalama, yani sizin vereceğiniz link
- **Kullanım Koşulları 4.11** "web crawler, veri madenciliği, screen scraping"
  ifadelerini adıyla yasaklıyor — üstelik "manuel süreçler" de kapsamda.
- **Kullanım Koşulları 4.10** ilanlara toplu erişmeyi, kopyalamayı ve derlemeyi
  yasaklıyor. **2. madde** ilan veritabanının 5846 sayılı FSEK kapsamında
  korunduğunu belirtiyor.
- Hesabınızla giriş yapmışken çalıştırmak durumu **iyileştirmez**, kötüleştirir:
  üyelik sözleşmesini kabul etmiş olursunuz.

Olası sonuçlar: IP/hesap engeli, hesabın kapatılması, hukuki talep. Bu riski sizin
adınıza değerlendiremem. `scrape` komutu bu yüzden `--kabul-ediyorum` bayrağı
olmadan çalışmaz.

**Daha düşük riskli yol — `parse-local`:** sayfaları kendi tarayıcınızda normal
şekilde gezip `Ctrl+S` ile kaydedin, sonra bu araca klasörü gösterin. Siteye tek
bir otomatik istek gitmez, çıktı birebir aynı olur. Az sayıda ilan için gerçekten
pratiktir.

Ayrıca: bu araç **CAPTCHA/bot doğrulaması aşmaz**. Doğrulama ekranı çıkarsa durur
ve pencereyi size devreder; siz geçtikten sonra kaldığı yerden devam eder.

---

## Kurulum

```bash
pip install -e .
python -m playwright install chromium
```

`parse-local`, `doctor` ve `export` komutları için Playwright gerekmez; sadece
`scrape` ve PDF çıktısı için lazım.

Kurmadan denemek isterseniz `pip install -r requirements.txt` + `PYTHONPATH=src`
da çalışır, ama `pip install -e .` her dizinden çalışan bir kurulum verir.

---

## Kullanım

### 1) Kendi filtrenizi uygulayın
sahibinden.com'da marka, yıl, fiyat, KM neyse seçin ve adres çubuğundaki linki
kopyalayın.

### 2) Çalıştırın

```bash
python -m sahibinden_scraper scrape "FILTRE_LINKINIZ" --kabul-ediyorum --format json,pdf
```

Ne yapacağını görmek için önce (hiç istek atmaz):

```bash
python -m sahibinden_scraper scrape "FILTRE_LINKINIZ" --dry-run
```

### 3) JSON'u yapay zekâya verin
`out/<ad>.json` dosyasını olduğu gibi yükleyin. **`meta` bloğunu silmeyin** — alan
sözlüğü, hangi alanların hiç okunamadığı ve sonuçların eksik olup olmadığı orada.

---

## Komutlar

| Komut | Ne yapar |
|---|---|
| `scrape <url>` | Filtreyi gezer, ilanları toplar, dışa aktarır |
| `parse-local <klasör>` | Kaydettiğiniz HTML dosyalarını işler — **siteye istek atmaz** |
| `doctor <dosya>` | Kaydedilmiş bir sayfada hangi alanların okunabildiğini gösterir |
| `export <ad>` | Tamamlanmış bir çalışmayı yeniden dışa aktarır (yeniden gezmeden) |

### Sık kullanılan bayraklar

```
--format json,csv,pdf,html   çıktı biçimleri (varsayılan: json)
--limit 50                   en fazla 50 ilan
--fast                       detay sayfalarını açma (hızlı ama teknik alanlar boş kalır)
--min-delay / --max-delay    istekler arası bekleme (varsayılan 3-7 sn)
--budget 200                 bu çalışmada en fazla 200 istek
--name kiraz-2024            çalışma adı; aynı adla tekrar çalıştırınca kaldığı yerden devam eder
--dry-run                    hiçbir istek atmadan planı yazdır
```

---

## Nasıl çalışıyor

```
filtre linki
   │
   ├─ browser.py    görünür Chrome, kalıcı profil, istek başına 3-7 sn bekleme
   │                doğrulama ekranı → DURUR, pencereyi size devreder
   ├─ search.py     sonuç tablosunu gezer, satırları çıkarır, doping satırlarını atar
   ├─ detail.py     her ilanı açar: künye, boya/değişen, donanım, açıklama
   ├─ store.py      her sayfayı diske yazar, her kaydı anında JSONL'e ekler
   │                → çalışma yarıda kesilirse kaldığı yerden devam eder
   └─ exporters.py  kohort/sıralama hesapları + alan sözlüğü → JSON / CSV / PDF
```

Bütün CSS seçicileri tek bir YAML dosyasında. Site HTML'ini değiştirdiğinde Python'a
dokunmanız gerekmez: `doctor` hangi seçicinin bozulduğunu söyler, siz doğru seçiciyi
ilgili listenin başına eklersiniz.

Dosya paketin içinde (`src/sahibinden_scraper/selectors.yaml`) ve `doctor` çıktısının
sonunda tam yolu yazar. Kurulu paketi kurcalamak istemiyorsanız çalıştığınız klasöre
`config/selectors.yaml` koyun — varsa o kullanılır. Ya da `--config kendi-dosyam.yaml`.

### Seçicilerin durumu — dürüst uyarı

sahibinden otomatik istemcilere 403 döndüğü için **bu depodaki hiçbir seçici canlı
sayfada doğrulanamadı.** Hepsi bu siteyi okuyan açık kaynak projelerden derlendi ve
`selectors.yaml` içinde tek tek işaretlendi:

- `[C]` en az iki kaynakta birebir görüldü (ama bu projeler birbirinden kopyalamış olabilir)
- `[1]` tek kaynak
- `[?]` tamamen çıkarım — kanıt yok

**İlk çalıştırmadan önce** bir arama ve bir ilan sayfasını tarayıcınızdan kaydedip
şunu çalıştırın:

```bash
python -m sahibinden_scraper doctor kaydettiginiz-sayfa.html
```

Ne bulunduğunu, ne bulunamadığını ve config'e eklenebilecek eşlenmemiş etiketleri
listeler. `scrape` ayrıca ilk arama sayfasından sonra otomatik bir kayma kontrolü
yapar: satırların %30'undan fazlasında temel alanlar boşsa, 20 sayfa boşuna gezmeden
durur.

---

## Çıktı JSON'unda ne var

```jsonc
{
  "meta": {
    "collected_count": 143,
    "total_count": 1842,
    "truncated": true,              // filtre 1842 eşleşti, 143'üne ulaşılabildi
    "fields_empty_across_all_listings": ["trim"],
    "field_dictionary": { ... },    // her alanın ne demek olduğu
    "reading_notes": [ ... ]        // modelin bilmesi gereken tuzaklar
  },
  "listings": [ { ... } ]           // 120'den fazlaysa otomatik sütunlu biçim
}
```

Şema kararlarından birkaçı ve nedenleri:

**`null` "bilinmiyor" demek, "yok" demek değil.** Tramer beyanı olmayan araçla
"0 TL tramer" yazan araç aynı şey değildir.

**`fields_empty_across_all_listings`** — hiçbir ilanda okunamayan alanlar. Bu liste
olmadan model her boş alanı "araçta bu özellik yok" diye okur. Bu, dosyadaki en
değerli dürüstlük alanı.

**Motor gücü ve hacmi aralık olarak gelir.** sahibinden bunları `101 - 125 hp` gibi
kutular hâlinde yayınlıyor. Ortalamasını almak, sitede hiç olmayan bir kesinlik
uydurmak olurdu; bu yüzden `_min`/`_max` ve `is_bucket` olarak saklanır ve
`engine_power_hp` boş bırakılır.

**Boya, değişen ve hasar kaydı üç ayrı eksendir.** "Boyasız değişensiz ama ağır
hasar kayıtlı" gerçek ve yaygın bir kombinasyondur (profesyonelce onarılmış araç).
Tek bir "durum puanı"na indirgemek, tam da satın alırken baktığınız bilgiyi yok eder.

**`text_findings` altındaki her şey satıcının kendi metnidir.** Tramer tutarı, ÖTV
muafiyeti, pert kaydı, servis geçmişi — hepsi açıklamadan regex ile çıkarılır,
resmî sorgu değildir. Bu yüzden ayrı bir blokta ve `_provenance: description_regex`
etiketiyle durur.

**`red_flags`** — çelişkiler ve dikkat noktaları tek listede: ilan "hatasız" derken
boya beyanı varsa, yıllık km anormalse, ÖTV muafiyetli araçsa burada görürsünüz.

**`fields_null_by_design`** — `fields_empty_across_all_listings`'ten farklıdır. İlki
"tasarım gereği boş" (hiçbir ilan TL değilse `price_try`, aralık verilmişse
`engine_power_hp`), ikincisi "scraper okuyamadı" demektir. Karıştırılmasınlar diye
ayrı tutulur.

**`cohort_size` ile `cohort_priced_count` farklıdır.** Euro fiyatlı bir ilan kohortun
üyesidir ama fiyat karşılaştırmasına giremez. "Kohort medyanının %4 altında" ifadesinin
kaç araç üzerinden hesaplandığını görebilmeniz için ikisi de yazılır.

**`listed_date` son güncelleme tarihidir.** sahibinden'in ücretli "doping" ürünü
bunu bugüne çeker, yani yeniliğe göre sıralama manipüle edilebilir.

**`cohort_*` alanları sadece bu çalışmadaki ilanlara göredir**, piyasanın tamamına
göre değil. Kohort `marka|seri|yıl|yakıt|vites`; 5'ten az üye varsa yıl kaldırılarak
genişletilir ve `cohort_widened: true` olur.

---

## Testler

```bash
python -m pytest -q
```

81 test; `tests/fixtures/` altındaki sentetik sayfalar üzerinde çalışır.
`test_parsers.py` davranışı, `test_regressions.py` ise çok ajanlı bir kod
incelemesinin bulup doğruladığı 26 hatanın geri gelmemesini test eder. Bunlar
sahibinden'den alınmış sayfalar **değildir** — seçicilerin hedeflediği yapıları
yeniden üreten elle yazılmış HTML'lerdir. Kendi kaydettiğiniz gerçek sayfaları
`tests/fixtures/real/` altına koyun; orası `.gitignore`'da.

En kritik testler sessiz hataları koruyor:

- `test_locally_painted_panel_is_not_read_as_fully_painted` — `local-painted-new`
  sınıfı `painted-new` sınıfını **içerir**. Alt dize eşleşmesi yapan bir parser her
  lokal boyalı paneli tam boyalı sayar; araç yanlış fiyatlanır ve hiçbir hata çıkmaz.
- `test_taxonomy_and_attribute_columns_are_not_swapped` — `TagAttributeValue`
  (marka/seri) ile `AttributeValue` (yıl/km/renk) dört karakter farkla aynı satırda
  durur. Karıştırınca yıl sütununda "Ford" yazar; makul göründüğü için gözden kaçar.
- `test_parse_tr_date_requires_an_explicit_clock` — "Bugün" İstanbul saatiyle
  bugündür. 01:30'da başlayan bir çalışma UTC'de dünde olduğu için tarihleri bir gün
  kaydırırdı.
- `test_turkish_labels_fold_to_the_same_key` — `"İ".lower()` iki kod noktası döner
  ve `"YIL".lower() != "Yıl".lower()`. `str.lower()` ile etiket eşleştiren bir
  parser sessizce eşleşmeyi bırakır.

---

## Bilinen sınırlar

- **1000 ilan sınırı.** sahibinden derin sayfalamayı ~1000 sonuçta kesiyor. Filtre
  daha fazlasını eşleştirirse `truncated: true` yazılır ve PDF'te uyarı çıkar.
  Daha fazlası için filtreyi yıl/fiyat aralıklarına bölüp ayrı çalıştırın.
- **Sadece otomobil.** Arazi/SUV/pickup kategorilerinin sütun düzeni ve filtre
  parametreleri hiçbir kaynakta belgelenmemiş.
- **Telefon numarası toplanmaz.** Kişisel veri, ilan başına ekstra tıklama gerektirir
  ve fiyat/donanım karşılaştırmasına hiçbir şey katmaz.
- **Farklı para birimleri.** USD/EUR ilanlarda `price_try` boş bırakılır; kur
  uydurmaktansa kıyaslama dışı bırakmak doğru.
