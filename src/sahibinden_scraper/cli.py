"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings, load_selectors, resolve_config_path
from .doctor import diagnose_file, print_report
from .exporters import write_json
from .pipeline import RunAborted, export_run, parse_local, run_scrape
from .store import RunStore, slugify
from .textnorm import istanbul_today

VALID_FORMATS = ("json", "csv", "pdf", "html")

TERMS_NOTICE = """
  ────────────────────────────────────────────────────────────────────
  ÖNCE ŞUNU BİLİN

  sahibinden.com'un robots.txt dosyası tam olarak bu aracın hedeflediği
  iki adres kalıbını kapatıyor:
      Disallow: /ilan/vasita*/detay        (ilan detay sayfaları)
      Disallow: */arama/flt/*  ve  *pagingOffset  *sorting=  *viewType=
  Kullanım Koşulları'nın 4.11. maddesi "web crawler / veri madenciliği /
  screen scraping" ifadelerini adıyla yasaklıyor; 4.10 ilanların toplu
  olarak alınmasını ve derlenmesini yasaklıyor; 2. madde ilan veritabanının
  5846 sayılı FSEK kapsamında korunduğunu belirtiyor.

  Yani: bu aracı çalıştırmak sitenin kendi kurallarına aykırı. Hesabınızın
  kapatılması ya da hukuki talep riski size aittir. Ben bu riski sizin
  yerinize değerlendiremem; kararı vererek devam ediyorsunuz.

  DAHA DÜŞÜK RİSKLİ YOL: sayfaları kendi tarayıcınızda gezip Ctrl+S ile
  kaydedin, sonra `parse-local` komutunu çalıştırın. Siteye tek bir
  otomatik istek gitmez, çıktı birebir aynı olur.

  Devam etmek için --kabul-ediyorum bayrağını ekleyin.
  ────────────────────────────────────────────────────────────────────
"""


def _non_negative(raw: str) -> int:
    """argparse type for the caps. 0 is a legitimate value ("do nothing"), so the
    code paths test `is not None`; negatives are simply rejected here."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"tam sayı bekleniyordu: {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"negatif olamaz: {value}")
    return value


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None,
                        help="seçici dosyası (varsayılan: ./config/selectors.yaml varsa o, "
                             "yoksa paketle gelen dosya)")
    parser.add_argument("--out", dest="out_dir", type=Path, default=Path("out"),
                        help="çıktı klasörü (varsayılan: out)")
    parser.add_argument("--runs", dest="run_dir", type=Path, default=Path("runs"),
                        help="ham sayfa ve ilerleme klasörü (varsayılan: runs)")
    parser.add_argument("--name", dest="run_name", default=None,
                        help="çalışma adı; aynı adla tekrar çalıştırınca kaldığı yerden devam eder")
    parser.add_argument("--format", dest="formats", default="json",
                        help="virgülle: json,csv,pdf,html (varsayılan: json)")
    parser.add_argument("--description-chars", type=int, default=400,
                        help="açıklama metninin kırpılacağı karakter sayısı (varsayılan: 400)")
    parser.add_argument("--with-images", action="store_true",
                        help="tüm fotoğraf adreslerini JSON'a ekle (dosyayı büyütür)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sahibinden-scraper",
        description="sahibinden.com otomobil ilanlarını tek bir karşılaştırılabilir veri setine dönüştürür.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Örnekler:\n"
            "  python -m sahibinden_scraper scrape \"<filtre-linkiniz>\" --format json,pdf --limit 50\n"
            "  python -m sahibinden_scraper parse-local ./kaydedilen-sayfalar --format json\n"
            "  python -m sahibinden_scraper doctor ./runs/<ad>/pages/detail-123.html\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- scrape ---- #
    scrape = sub.add_parser("scrape", help="filtre linkini gezip veri setini üretir")
    scrape.add_argument("url", help="sahibinden filtre linkiniz (tırnak içinde)")
    _add_common(scrape)
    scrape.add_argument("--kabul-ediyorum", dest="accepted_terms", action="store_true",
                        help="robots.txt / Kullanım Koşulları uyarısını okudum ve sorumluluğu alıyorum")
    scrape.add_argument("--limit", type=_non_negative, default=None, help="en fazla kaç ilan işlensin")
    scrape.add_argument("--max-pages", type=_non_negative, default=None,
                        help="en fazla kaç arama sayfası gezilsin")
    scrape.add_argument("--fast", dest="detail", action="store_false",
                        help="detay sayfalarını açma; sadece liste verisi (çok daha hızlı, çok daha az alan)")
    scrape.add_argument("--headless", action="store_true",
                        help="pencereyi gizle (doğrulama ekranı çıkarsa çalışma durur)")
    scrape.add_argument("--channel", default="chrome",
                        help="tarayıcı kanalı: chrome | msedge | boş bırakmak için ''")
    scrape.add_argument("--profile", dest="profile_dir", type=Path, default=None,
                        help="tarayıcı profili klasörü (varsayılan: runs/_browser_profile)")
    scrape.add_argument("--min-delay", type=float, default=3.0, help="istekler arası en az saniye")
    scrape.add_argument("--max-delay", type=float, default=7.0, help="istekler arası en fazla saniye")
    scrape.add_argument("--budget", dest="request_budget", type=_non_negative, default=None,
                        help="bu çalışmada yapılacak en fazla istek sayısı")
    scrape.add_argument("--manual-timeout", dest="manual_timeout_s", type=int, default=600,
                        help="doğrulama ekranında sizi kaç saniye bekleyeyim (varsayılan: 600)")
    scrape.add_argument("--no-resume", dest="resume", action="store_false",
                        help="önceki ilerlemeyi yok say")
    scrape.add_argument("--refetch", action="store_true",
                        help="diskteki önbelleği yok sayıp sayfaları yeniden indir")
    scrape.add_argument("--dry-run", action="store_true",
                        help="hiçbir istek atmadan ne yapacağını yazdır")

    # ---- parse-local ---- #
    local = sub.add_parser(
        "parse-local",
        help="tarayıcınızdan kaydettiğiniz HTML dosyalarını işler (siteye istek atmaz)",
    )
    local.add_argument("paths", nargs="+", type=Path, help="HTML dosyaları veya klasörler")
    _add_common(local)

    # ---- export ---- #
    export = sub.add_parser("export", help="tamamlanmış bir çalışmayı yeniden dışa aktarır")
    export.add_argument("run_name", help="runs/ altındaki çalışma adı")
    _add_common(export)

    # ---- doctor ---- #
    doctor = sub.add_parser(
        "doctor",
        help="kaydedilmiş bir sayfada hangi alanların okunabildiğini gösterir",
    )
    doctor.add_argument("paths", nargs="+", type=Path, help="HTML dosyaları veya klasörler")
    doctor.add_argument("--config", type=Path, default=None)
    doctor.add_argument("--json-out", type=Path, default=Path("doctor-report.json"),
                        help="--json ile birlikte: raporun yazılacağı dosya")
    doctor.add_argument("--json", dest="as_json", action="store_true", help="rapor yerine JSON yaz")
    doctor.add_argument("--fail-under", type=float, default=0.0,
                        help="kapsam bu oranın (0-1) altındaysa hata koduyla çık")

    return parser


def _settings_from(args: argparse.Namespace) -> Settings:
    formats = tuple(
        f.strip().lower() for f in str(getattr(args, "formats", "json")).split(",") if f.strip()
    )
    # Slugified here and nowhere else. It becomes a directory name and an output
    # filename, so a raw --name could contain path separators or characters Windows
    # rejects; and slugifying on read but not on write made `export` unable to name
    # any run created with --name.
    raw_name = getattr(args, "run_name", None)
    run_name = slugify(str(raw_name)) if raw_name else None

    settings = Settings(
        url=getattr(args, "url", "") or "",
        out_dir=getattr(args, "out_dir", Path("out")),
        run_dir=getattr(args, "run_dir", Path("runs")),
        run_name=run_name,
        config_path=getattr(args, "config", None),
        detail=getattr(args, "detail", True),
        limit=getattr(args, "limit", None),
        max_pages=getattr(args, "max_pages", None),
        formats=formats or ("json",),
        headless=getattr(args, "headless", False),
        channel=(getattr(args, "channel", "chrome") or None),
        profile_dir=getattr(args, "profile_dir", None),
        min_delay=getattr(args, "min_delay", 3.0),
        max_delay=getattr(args, "max_delay", 7.0),
        manual_timeout_s=getattr(args, "manual_timeout_s", 600),
        request_budget=getattr(args, "request_budget", None),
        resume=getattr(args, "resume", True),
        refetch=getattr(args, "refetch", False),
        description_chars=getattr(args, "description_chars", 400),
        include_images=getattr(args, "with_images", False),
        accepted_terms=getattr(args, "accepted_terms", False),
    )
    settings.now_date = istanbul_today()
    settings.reference_year = settings.now_date.year
    return settings


def _force_utf8_streams() -> None:
    """Turkish output must survive a redirect or a pipe.

    On Windows a piped stdout defaults to the ANSI code page, so the first "ş" or
    "→" in any message raised UnicodeEncodeError — every command crashed under
    ``... > out.txt``. ``errors="replace"`` keeps it alive even on a console that
    cannot render the glyphs.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: "list[str] | None" = None) -> int:
    _force_utf8_streams()
    args = _build_parser().parse_args(argv)

    raw_formats = getattr(args, "formats", None)
    if raw_formats is not None:
        unknown = [
            token.strip() for token in str(raw_formats).split(",")
            if token.strip() and token.strip().lower() not in VALID_FORMATS
        ]
        if unknown:
            # Previously an unknown format wrote nothing and reported success.
            print(
                f"HATA: bilinmeyen çıktı biçimi: {', '.join(unknown)}. "
                f"Geçerli olanlar: {', '.join(VALID_FORMATS)}",
                file=sys.stderr,
            )
            return 2

    try:
        config = load_selectors(getattr(args, "config", None))
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        return _cmd_doctor(args, config)

    settings = _settings_from(args)

    if args.command == "scrape":
        return _cmd_scrape(args, settings, config)
    if args.command == "parse-local":
        return _cmd_parse_local(args, settings, config)
    if args.command == "export":
        return _cmd_export(args, settings)
    return 2


def _cmd_scrape(args: argparse.Namespace, settings: Settings, config: dict) -> int:
    from .search import build_page_url, normalise_search_url

    if args.dry_run:
        print("\n  Yapılacaklar (hiçbir istek atılmadı):")
        print(f"    kaynak link : {settings.url}")
        print(f"    normalize   : {normalise_search_url(settings.url, config)}")
        print(f"    ilk sayfa   : {build_page_url(settings.url, 0, config)}")
        print(f"    detay modu  : {'açık' if settings.detail else 'kapalı (--fast)'}")
        print(f"    biçimler    : {', '.join(settings.formats)}")
        print(f"    çıktı       : {settings.out_dir.resolve()}")
        print(f"    çalışma     : {settings.run_dir.resolve() / (settings.run_name or 'otomatik')}")
        print(f"    bekleme     : {settings.min_delay}-{settings.max_delay} sn/istek")
        return 0

    if not settings.accepted_terms:
        print(TERMS_NOTICE)
        return 3

    try:
        store = run_scrape(settings, config)
    except RunAborted as exc:
        print(f"\n  DURDURULDU: {exc}")
        print("  Toplanan veriler diskte duruyor; 'export' ile dışa aktarabilirsiniz.")
        return 4
    except KeyboardInterrupt:
        print("\n  İptal edildi. İlerleme kaydedildi; aynı --name ile devam edebilirsiniz.")
        return 130

    return _finish(store, settings)


def _cmd_parse_local(args: argparse.Namespace, settings: Settings, config: dict) -> int:
    files, problems = collect_html_files(list(args.paths))
    for problem in problems:
        print(f"HATA: {problem}", file=sys.stderr)
    if not files:
        return 2
    settings.run_name = settings.run_name or "yerel"
    store = parse_local(files, settings, config)
    return _finish(store, settings)


def _cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    name = slugify(str(args.run_name))
    store = RunStore(settings.run_dir, name=name)
    # Checked BEFORE open(), which would otherwise create an empty directory for a
    # name that does not exist and leave phantom runs behind.
    if not store.listings_path.is_file():
        print(f"HATA: {store.listings_path} bulunamadı.", file=sys.stderr)
        existing = sorted(
            d.name for d in Path(settings.run_dir).glob("*")
            if d.is_dir() and (d / "listings.jsonl").is_file()
        )
        if existing:
            print("  Mevcut çalışmalar: " + ", ".join(existing), file=sys.stderr)
        return 2
    store.open()
    return _finish(store, settings)


def _finish(store: RunStore, settings: Settings) -> int:
    try:
        written = export_run(store, settings)
    except RunAborted as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 4
    print("\n  Yazıldı:")
    for path in written:
        print(f"    {path.resolve()}")
    print(
        "\n  JSON'u yapay zekâya verirken 'meta' bloğunu da birlikte gönderin: "
        "alan sözlüğü, eksik alan listesi ve eksiklik uyarıları orada."
    )
    return 0


def collect_html_files(paths: "list[Path]") -> "tuple[list[Path], list[str]]":
    """Expand files and directories into a file list, naming whatever is missing.

    A path that does not exist previously fell through to ``read_text`` and
    surfaced as a bare FileNotFoundError traceback.
    """
    files: "list[Path]" = []
    problems: "list[str]" = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            found = sorted(path.rglob("*.htm*"))
            if not found:
                problems.append(f"klasörde HTML dosyası yok: {path}")
            files.extend(found)
        elif path.is_file():
            files.append(path)
        else:
            problems.append(f"bulunamadı: {path}")
    return files, problems


def _cmd_doctor(args: argparse.Namespace, config: dict) -> int:
    files, problems = collect_html_files(list(args.paths))
    for problem in problems:
        print(f"HATA: {problem}", file=sys.stderr)
    if not files:
        return 2

    reports = [diagnose_file(path, config) for path in files]
    worst = min((r["coverage_pct"] for r in reports), default=0.0)

    if args.as_json:
        print(write_json(args.json_out, {"reports": reports}).resolve())
    else:
        for report in reports:
            print_report(report)
        print(f"\n  Kullanılan seçici dosyası: {resolve_config_path(args.config)}")
        print(
            "  İpucu: EKSİK çıkan alanlar için bu dosyadaki ilgili seçici listesine "
            "doğru seçiciyi EN BAŞA ekleyin; Python'a dokunmanız gerekmez."
        )

    # Checked in BOTH paths. A coverage gate that silently passes under --json is
    # worse than no gate: CI goes green on a broken parser.
    if args.fail_under and worst < args.fail_under * 100:
        print(f"\n  Kapsam %{worst} < eşik %{args.fail_under * 100:.0f}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
