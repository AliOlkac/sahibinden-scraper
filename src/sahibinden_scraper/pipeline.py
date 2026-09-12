"""Orchestration: paginate, fetch details, resume, export.

Failure handling is the interesting part. A long polite crawl gets interrupted, so
every page is cached to disk and every finished record is appended to a JSONL
ledger the moment it is parsed. A re-run picks up where it stopped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .browser import BlockedError, BrowserConfig, Pacer, Session
from .config import Settings
from .detail import merge_listing, parse_detail_page
from .exporters import build_dataset, write_csv, write_json
from .models import Listing
from .report import render_html_report
from .search import build_page_url, detect_page_kind, normalise_search_url, parse_search_page
from .store import RunStore, slugify, stable_key, utc_now_iso
from .textnorm import istanbul_now_iso, istanbul_today

__all__ = ["run_scrape", "export_run", "parse_local"]

# Stop the run when this many navigations in a row come back blocked. Repeated
# blocks mean an IP- or account-level decision, not a challenge a human can clear,
# and summoning the person at the keyboard over and over is worse than stopping.
CONSECUTIVE_BLOCK_LIMIT = 3

# After the first search page, if the load-bearing fields are empty for more than
# this share of rows, the selectors have drifted. Failing here costs one page;
# failing at the end costs the whole crawl and produces a plausible empty file.
DRIFT_THRESHOLD = 0.30


class RunAborted(RuntimeError):
    """The run stopped early but whatever was collected is still on disk."""


def _drift_ratio(listings: "list[Listing]") -> float:
    if not listings:
        return 1.0
    bad = 0
    for listing in listings:
        core = (listing.id, listing.url, listing.price, listing.year, listing.km)
        if sum(1 for value in core if value in (None, "")) >= 2:
            bad += 1
    return bad / len(listings)


def run_scrape(settings: Settings, config: "dict[str, Any]") -> RunStore:
    """Crawl the supplied filter URL and write the run ledger. Returns the store."""
    now_date = settings.now_date or istanbul_today()
    settings.now_date = now_date
    settings.reference_year = settings.reference_year or now_date.year

    run_name = settings.run_name or slugify(urlparse(settings.url).path or "arama")
    store = RunStore(settings.run_dir, name=run_name).open(
        meta={
            "source_url": settings.url,
            "normalised_url": normalise_search_url(settings.url, config),
            "started_at": istanbul_now_iso(),
            "now_date": now_date.isoformat(),
            "detail_mode": settings.detail,
        }
    )

    pacer = Pacer(min_delay=settings.min_delay, max_delay=settings.max_delay)
    browser = BrowserConfig(
        profile_dir=settings.profile_path(),
        headless=settings.headless,
        channel=settings.channel,
        manual_timeout_s=settings.manual_timeout_s,
    )

    guards = config.get("guards") or {}
    pagination = config.get("pagination") or {}
    page_size = int(pagination.get("page_size", 50))
    hard_cap = int(pagination.get("hard_cap", 1000))

    # A stub row (search data only) is "recorded", not "finished": when detail mode
    # is on, only rows that actually got a detail page count as done, or a --fast or
    # budget-capped run would block detail fetching forever on every later resume.
    done = store.completed_ids(require_detail=settings.detail) if settings.resume else set()
    if done:
        print(f"  ↻ {len(done)} ilan zaten tamamlanmış, atlanıyor (--no-resume ile baştan alın)")

    stubs: "dict[str, Listing]" = {}
    selected: "list[Listing]" = []
    total_count: Optional[int] = None
    truncated = False
    # Carried across resumes: a per-process budget would let three resumed runs make
    # three times the requests the user asked to cap.
    requests_used = int(store.meta.get("requests_used") or 0) if settings.resume else 0
    if requests_used:
        print(f"  ↻ önceki çalışmalarda {requests_used} istek yapılmış (bütçeye dahil)")
    consecutive_blocks = 0

    session = Session(browser, pacer)
    try:
        session.start()

        # ---------------- search pages ---------------- #
        offset = 0
        page_index = 0
        while True:
            if settings.max_pages is not None and page_index >= settings.max_pages:
                # Every early exit below is a *possible* truncation. Flagging it here
                # rather than only when total_count parsed keeps the report's "this
                # list is incomplete" warning on a page whose counter we could not read.
                truncated = True
                break
            if offset >= hard_cap:
                truncated = True
                print(f"  ! sayfalama üst sınırına ulaşıldı ({hard_cap}); sonuçlar eksik")
                break
            if settings.request_budget is not None and requests_used >= settings.request_budget:
                truncated = True
                print("  ! istek bütçesi doldu")
                break

            page_url = build_page_url(settings.url, offset, config)
            # Keyed on the full resolved URL, not the page number: different query
            # slices share page numbers and would otherwise overwrite each other.
            cache_key = f"{page_index:04d}-{stable_key(page_url)}"
            html = None if settings.refetch else store.read_page("search", cache_key)

            if html is None:
                print(f"\n  → arama sayfası {page_index + 1} (offset {offset})")
                before = session.request_count
                try:
                    html = session.goto(page_url, ready_selectors=guards.get("search_ready") or ())
                    consecutive_blocks = 0
                except BlockedError as exc:
                    consecutive_blocks += 1
                    store.record_error(page_url, str(exc))
                    print(f"  ✗ {exc}")
                    truncated = True
                    if consecutive_blocks >= CONSECUTIVE_BLOCK_LIMIT:
                        raise RunAborted(
                            "Üst üste engellendi. Bu bir doğrulama ekranı değil, "
                            "IP/hesap düzeyinde bir karar gibi görünüyor; durduruldu."
                        ) from exc
                    break
                # Real navigations only: a cache hit costs the site nothing and must
                # not consume the budget, or a resumed run would exhaust its allowance
                # without making a single request.
                requests_used += max(0, session.request_count - before)
                store.write_page("search", cache_key, html)
                pacer.wait(label="sayfalar arası")

            kind = detect_page_kind(html, config)
            if kind != "search":
                truncated = True
                print(f"  ! beklenen arama sayfası değil ({kind}); sayfalama durduruldu")
                break

            page = parse_search_page(html, config, now=now_date, base_url=config.get("base_url"))
            if page.total_count and total_count is None:
                total_count = page.total_count
                print(f"  filtrede toplam {total_count} ilan")

            if page_index == 0:
                ratio = _drift_ratio(page.listings)
                if ratio > DRIFT_THRESHOLD:
                    raise RunAborted(
                        f"İlk sayfadaki satırların %{ratio * 100:.0f}'inde temel alanlar boş. "
                        "Seçiciler kaymış olabilir — 'doctor' komutunu çalıştırın; "
                        "20 sayfa boşuna gezmeden durduruldu."
                    )

            new_this_page = 0
            for listing in page.listings:
                key = listing.id or listing.url
                if not key or key in stubs:
                    continue
                listing.first_seen_at = utc_now_iso()
                stubs[key] = listing
                new_this_page += 1

            print(
                f"    {len(page.listings)} satır ({new_this_page} yeni, "
                f"{page.skipped_promo} doping atlandı)"
            )
            for warning in page.warnings:
                print(f"    ! {warning}")

            if settings.limit is not None and len(stubs) >= settings.limit:
                break
            if not page.listings or (total_count and len(stubs) >= total_count):
                break

            offset += page_size
            page_index += 1

        if total_count and len(stubs) < total_count:
            truncated = True

        selected = list(stubs.values())
        if settings.limit is not None:
            selected = selected[: settings.limit]
        print(f"\n  {len(selected)} ilan toplandı.")

        # ---------------- detail pages ---------------- #
        # A block during pagination says nothing about the detail pages, and a run
        # that limped through the search must not start the detail phase already
        # one strike from aborting.
        consecutive_blocks = 0
        if settings.detail:
            for index, stub in enumerate(selected, start=1):
                key = stub.id or stub.url
                if key in done:
                    continue
                if settings.request_budget is not None and requests_used >= settings.request_budget:
                    truncated = True
                    print("  ! istek bütçesi doldu; kalan ilanlar sadece liste verisiyle kaydediliyor")
                    break
                before = session.request_count
                record = _fetch_detail(
                    session, store, stub, settings, config, index, len(selected)
                )
                if record is None:
                    consecutive_blocks += 1
                    if consecutive_blocks >= CONSECUTIVE_BLOCK_LIMIT:
                        raise RunAborted("Üst üste engellendi; durduruldu.")
                    continue
                consecutive_blocks = 0
                # Count real navigations only: a cache hit costs the site nothing and
                # must not consume the budget, or a resumed run would exhaust its
                # allowance without making a single request.
                requests_used += max(0, session.request_count - before)
                store.append_listing(
                    record.to_dict(
                        reference_year=settings.reference_year,
                        include_images=settings.include_images,
                    )
                )
                done.add(key)

    finally:
        # The stub flush lives in `finally` so an abort mid-detail still persists
        # every row already collected. Without it a RunAborted threw away the whole
        # search phase while the CLI told the user the data had been saved.
        _flush_stubs(store, selected, done, settings)
        session.close()

    store.set_meta(
        finished_at=istanbul_now_iso(),
        total_count=total_count,
        truncated=truncated,
        requests_used=requests_used,
        reference_year=settings.reference_year,
    )
    return store


def _flush_stubs(
    store: RunStore,
    selected: "list[Listing]",
    done: "set[str]",
    settings: Settings,
) -> None:
    """Persist every listing that never got a detail page, clearly marked as such.

    Called from a ``finally``: an aborted run must still leave behind what it had
    rather than reporting success over an empty ledger.
    """
    for stub in selected:
        key = stub.id or stub.url
        if not key or key in done:
            continue
        stub.scraped_at = istanbul_now_iso()
        stub.last_seen_at = stub.scraped_at
        store.append_listing(
            stub.to_dict(
                reference_year=settings.reference_year,
                include_images=settings.include_images,
            )
        )
        done.add(key)


def _fetch_detail(
    session: Session,
    store: RunStore,
    stub: Listing,
    settings: Settings,
    config: "dict[str, Any]",
    index: int,
    total: int,
) -> Optional[Listing]:
    """Fetch and parse one detail page. ``None`` means "blocked, try again later"."""
    guards = config.get("guards") or {}
    key = stub.id or stub.url or str(index)
    html = None if settings.refetch else store.read_page("detail", key)

    if html is None:
        if not stub.url:
            return stub
        print(f"  → [{index}/{total}] {stub.url}")
        try:
            html = session.goto(stub.url, ready_selectors=guards.get("detail_ready") or ())
        except BlockedError as exc:
            store.record_error(stub.url, str(exc))
            print(f"    ✗ {exc}")
            return None
        store.write_page("detail", key, html)
        session.pacer.wait(label="ilanlar arası")

    kind = detect_page_kind(html, config)
    if kind == "gone":
        # A removed listing is not a block. Keep the row, mark it, move on.
        stub.status = "removed"
        stub.scraped_at = istanbul_now_iso()
        stub.last_seen_at = stub.scraped_at
        print("    · ilan yayından kaldırılmış")
        return stub
    if kind not in ("detail", "unknown"):
        store.record_error(stub.url or key, f"beklenmeyen sayfa türü: {kind}")
        return None

    detail = parse_detail_page(
        html,
        config,
        now=settings.now_date,
        url=stub.url,
        description_chars=settings.description_chars,
    )
    merged = merge_listing(stub, detail)
    merged.scraped_at = istanbul_now_iso()
    merged.last_seen_at = merged.scraped_at
    return merged


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def export_run(
    store: RunStore,
    settings: Settings,
    *,
    session_for_pdf: Optional[Session] = None,
) -> "list[Path]":
    """Write the requested output formats from a run's ledger."""
    records = store.load_listings()
    if not records:
        raise RunAborted("Kaydedilmiş ilan yok; dışa aktarılacak bir şey bulunamadı.")

    meta = {
        "source_url": store.meta.get("source_url"),
        "scraped_at": store.meta.get("finished_at") or store.meta.get("started_at"),
        "total_count": store.meta.get("total_count"),
        "truncated": bool(store.meta.get("truncated")),
        "reference_year": store.meta.get("reference_year") or settings.reference_year,
        "run_dir": str(store.dir),
        "errors": len(store.errors),
    }

    payload = build_dataset(records, meta=meta)
    ordered = payload.get("listings")
    if ordered is None:
        columns = payload["columns"]
        ordered = [dict(zip(columns, row)) for row in payload["rows"]]

    out_dir = Path(settings.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = store.name
    written: "list[Path]" = []

    if "json" in settings.formats:
        written.append(write_json(out_dir / f"{stem}.json", payload))
    if "csv" in settings.formats:
        written.append(write_csv(out_dir / f"{stem}.csv", ordered))
    if "pdf" in settings.formats or "html" in settings.formats:
        html_path = render_html_report(ordered, payload["meta"], out_dir / f"{stem}.html")
        written.append(html_path)
        if "pdf" in settings.formats:
            pdf_path = out_dir / f"{stem}.pdf"
            try:
                session = session_for_pdf or Session(
                    BrowserConfig(profile_dir=settings.profile_path(), headless=True)
                )
                session.html_to_pdf(html_path, pdf_path, landscape=True)
                written.append(pdf_path)
            except Exception as exc:
                # Playwright's "browser not installed" error is a 20-line ASCII
                # banner; printing it whole buries the one line that matters.
                first_line = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else type(exc).__name__
                print(f"\n  ! PDF üretilemedi: {first_line}")
                if "install" in str(exc).lower() or "executable" in str(exc).lower():
                    print("    Tarayıcı motoru kurulu değil. Şunu çalıştırın:")
                    print("        python -m playwright install chromium")
                print(f"    HTML raporu yazıldı, tarayıcıdan yazdırabilirsiniz: {html_path}")
    return written


# --------------------------------------------------------------------------- #
# Offline parsing
# --------------------------------------------------------------------------- #

def parse_local(
    paths: "list[Path]",
    settings: Settings,
    config: "dict[str, Any]",
) -> RunStore:
    """Parse HTML files you saved from your own browser — no requests at all.

    This is the zero-traffic path: browse the filter yourself, save the pages
    (Ctrl+S), point this at the folder. Everything downstream is identical.
    """
    now_date = settings.now_date or istanbul_today()
    settings.now_date = now_date
    settings.reference_year = settings.reference_year or now_date.year

    store = RunStore(settings.run_dir, name=settings.run_name or "yerel").open(
        meta={
            "source_url": "yerel dosyalar",
            "started_at": istanbul_now_iso(),
            "now_date": now_date.isoformat(),
            "detail_mode": True,
        }
    )

    files: "list[Path]" = []
    for path in paths:
        path = Path(path)
        files.extend(sorted(path.rglob("*.htm*")) if path.is_dir() else [path])

    stubs: "dict[str, Listing]" = {}
    details: "dict[str, Listing]" = {}

    for path in files:
        html = path.read_text(encoding="utf-8", errors="replace")
        kind = detect_page_kind(html, config)
        if kind == "search":
            page = parse_search_page(html, config, now=now_date)
            for listing in page.listings:
                key = listing.id or listing.url
                if key:
                    stubs.setdefault(key, listing)
            print(f"  · {path.name}: arama sayfası, {len(page.listings)} ilan")
        elif kind == "detail":
            listing = parse_detail_page(
                html, config, now=now_date, description_chars=settings.description_chars
            )
            key = listing.id or listing.url or path.stem
            details[key] = listing
            print(f"  · {path.name}: ilan detayı ({key})")
        else:
            print(f"  · {path.name}: tanınmadı ({kind}), atlandı")

    for key, listing in details.items():
        stub = stubs.pop(key, None)
        record = merge_listing(stub, listing) if stub else listing
        record.scraped_at = istanbul_now_iso()
        store.append_listing(
            record.to_dict(
                reference_year=settings.reference_year,
                include_images=settings.include_images,
            )
        )
    for listing in stubs.values():
        listing.scraped_at = istanbul_now_iso()
        store.append_listing(
            listing.to_dict(
                reference_year=settings.reference_year,
                include_images=settings.include_images,
            )
        )

    store.set_meta(finished_at=istanbul_now_iso(), reference_year=settings.reference_year)
    return store
