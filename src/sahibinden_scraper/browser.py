"""Playwright session: a real, visible browser that a human can take over.

Deliberate design constraints
-----------------------------
sahibinden.com sits behind a bot check. This tool does **not** try to defeat it and
contains no CAPTCHA solving, fingerprint spoofing or detection-evasion. Instead:

* The browser runs **headed** with a **persistent profile**, so your own normal session
  (cookies, any login) is what visits the site — the same session you would browse with.
* When a verification / rate-limit interstitial appears, the scraper **stops and hands
  the window to you**, waits until you have cleared it yourself, then resumes.
* Requests are **sequential and paced with jitter**, with backoff on 429/403. There is
  no concurrency: one tab, human speed, so the crawl stays within what a person
  browsing the same filter would generate.

If you would not be comfortable clicking through these pages by hand, do not run this.
"""

from __future__ import annotations

import random
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Sequence

__all__ = ["BrowserConfig", "Pacer", "BlockedError", "Session", "InterstitialError"]


class BlockedError(RuntimeError):
    """Raised when the site returns a hard block we will not attempt to bypass."""


class InterstitialError(BlockedError):
    """A verification page appeared and could not be cleared by the human in time."""


# --------------------------------------------------------------------------- #
# Pacing
# --------------------------------------------------------------------------- #

@dataclass
class Pacer:
    """Randomised, backing-off delay between requests.

    Jitter is not an evasion trick — it is what keeps a long sequential crawl from
    hammering the origin at a fixed rate. Backoff doubles on every soft failure and
    resets on success.
    """

    min_delay: float = 3.0
    max_delay: float = 7.0
    backoff: float = 0.0
    max_backoff: float = 120.0
    _rng: random.Random = field(default_factory=random.Random)

    def wait(self, *, label: str = "") -> None:
        delay = self._rng.uniform(self.min_delay, self.max_delay) + self.backoff
        if delay <= 0:
            return
        suffix = f" ({label})" if label else ""
        print(f"    … {delay:.1f} sn bekleniyor{suffix}", flush=True)
        time.sleep(delay)

    def penalise(self) -> float:
        self.backoff = min(self.max_backoff, (self.backoff or 5.0) * 2)
        return self.backoff

    def reset(self) -> None:
        self.backoff = 0.0


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class BrowserConfig:
    profile_dir: Path
    headless: bool = False
    channel: Optional[str] = "chrome"
    locale: str = "tr-TR"
    timezone: str = "Europe/Istanbul"
    viewport_width: int = 1440
    viewport_height: int = 900
    nav_timeout_ms: int = 45_000
    block_media: bool = True
    manual_timeout_s: int = 600
    slow_mo_ms: int = 0


# Substrings that identify sahibinden's interstitials. Matched case-insensitively
# against the page title and the visible body text.
INTERSTITIAL_MARKERS: "tuple[str, ...]" = (
    "tarayıcınızı kontrol ediyoruz",
    "tarayicinizi kontrol ediyoruz",
    "güvenlik doğrulaması",
    "guvenlik dogrulamasi",
    "yükleniyor",
    "just a moment",
    "checking your browser",
    "erişim engellendi",
    "erisim engellendi",
    "access denied",
    "unusual traffic",
    "olağan dışı",
    "robot değilim",
    "captcha",
    "attention required",
)

BLOCK_RESOURCE_TYPES = {"image", "media", "font"}


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #

class Session:
    """One browser window, driven sequentially."""

    def __init__(self, config: BrowserConfig, pacer: Optional[Pacer] = None) -> None:
        self.config = config
        self.pacer = pacer or Pacer()
        self._pw = None
        self._context = None
        self.page = None
        self.request_count = 0

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> "Session":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright kurulu değil. Kurulum:\n"
                "    pip install -r requirements.txt\n"
                "    python -m playwright install chromium"
            ) from exc

        cfg = self.config
        cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()

        launch_kwargs = dict(
            user_data_dir=str(cfg.profile_dir),
            headless=cfg.headless,
            locale=cfg.locale,
            timezone_id=cfg.timezone,
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
            slow_mo=cfg.slow_mo_ms or 0,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if cfg.channel:
            launch_kwargs["channel"] = cfg.channel

        try:
            self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            # Falling back covers two common cases: no Google Chrome installed
            # (channel="chrome" fails) and a profile already locked by a running Chrome.
            message = str(exc)
            if cfg.channel:
                print(
                    f"  ! Chrome kanalı açılamadı ({message.splitlines()[0][:120]}); "
                    "Playwright'in kendi Chromium'una geçiliyor.",
                    flush=True,
                )
                launch_kwargs.pop("channel", None)
                self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
            else:
                raise

        self._context.set_default_navigation_timeout(cfg.nav_timeout_ms)
        self._context.set_default_timeout(cfg.nav_timeout_ms)

        if cfg.block_media:
            self._context.route("**/*", self._route)

        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self

    def _route(self, route, request) -> None:
        """Skip images/fonts/media: this scraper reads text, not pixels."""
        try:
            if request.resource_type in BLOCK_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def close(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._context = None
        self._pw = None
        self.page = None

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- navigation --------------------------------------------------------- #

    def goto(
        self,
        url: str,
        *,
        ready_selectors: Sequence[str] = (),
        attempts: int = 3,
        settle_ms: int = 1200,
    ) -> str:
        """Navigate and return page HTML once real content is on screen.

        Raises :class:`BlockedError` when the page never becomes readable.
        """
        last_error: Optional[str] = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                delay = self.pacer.penalise()
                print(f"  ! yeniden deneniyor ({attempt}/{attempts}), {delay:.0f} sn sonra", flush=True)
                time.sleep(delay)
            try:
                response = self.page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                last_error = f"gezinme hatası: {exc}"
                continue

            self.request_count += 1
            status = response.status if response else 0
            if status in (403, 429, 503):
                last_error = f"HTTP {status}"
                if not self._hand_over_to_human(url, ready_selectors, reason=f"HTTP {status}"):
                    continue

            self.page.wait_for_timeout(settle_ms)

            if self._content_ready(ready_selectors):
                self.pacer.reset()
                return self.page.content()

            if self._looks_like_interstitial():
                if self._hand_over_to_human(url, ready_selectors, reason="doğrulama sayfası"):
                    self.pacer.reset()
                    return self.page.content()
                last_error = "doğrulama sayfası aşılamadı"
                continue

            # No ready selector matched and it does not look like a block: the page
            # may simply have a layout we do not recognise. Return it and let the
            # parser decide — a coverage warning is more useful than a hard failure.
            html = self.page.content()
            if len(html) > 20_000:
                self.pacer.reset()
                return html
            last_error = "sayfa beklenenden kısa (muhtemelen boş/engelli)"

        raise BlockedError(f"{url} okunamadı — {last_error or 'bilinmeyen sebep'}")

    def _content_ready(self, ready_selectors: Sequence[str]) -> bool:
        if not ready_selectors:
            return True
        for selector in ready_selectors:
            try:
                if self.page.query_selector(selector):
                    return True
            except Exception:
                continue
        return False

    def _looks_like_interstitial(self) -> bool:
        try:
            title = (self.page.title() or "").lower()
        except Exception:
            title = ""
        try:
            body = (self.page.inner_text("body") or "")[:2000].lower()
        except Exception:
            body = ""
        haystack = f"{title}\n{body}"
        return any(marker in haystack for marker in INTERSTITIAL_MARKERS)

    # -- human handoff ------------------------------------------------------ #

    def _hand_over_to_human(
        self,
        url: str,
        ready_selectors: Sequence[str],
        *,
        reason: str,
    ) -> bool:
        """Pause and wait for the person at the keyboard to clear the page.

        This is the whole anti-bot story: we never solve the check, we ask you to.
        Returns True once the expected content appears, False on timeout.
        """
        if self.config.headless:
            raise InterstitialError(
                f"{reason} çıktı ve tarayıcı headless. --headed ile çalıştırın ki "
                "doğrulamayı kendiniz geçebilesiniz."
            )

        deadline = time.monotonic() + self.config.manual_timeout_s
        print("", flush=True)
        print("  " + "=" * 68, flush=True)
        print(f"  DURDU: {reason}", flush=True)
        print(f"  Adres: {url}", flush=True)
        print("  Açılan tarayıcı penceresinde doğrulamayı kendiniz tamamlayın.", flush=True)
        print("  Sayfa normal içeriğe dönünce scraper kaldığı yerden devam eder.", flush=True)
        print(f"  (en fazla {self.config.manual_timeout_s // 60} dk beklenecek, Ctrl+C ile iptal)", flush=True)
        print("  " + "=" * 68, flush=True)

        while time.monotonic() < deadline:
            time.sleep(2.0)
            if self._looks_like_interstitial():
                continue
            if not self._content_ready(ready_selectors):
                continue
            # Clearing a challenge usually lands the browser on the homepage or on
            # whatever the person clicked next — NOT necessarily the listing we
            # asked for. Without this check the scraper would take that page's HTML
            # and file it under the requested listing's id, silently recording one
            # car's data against another car's ad.
            if not self._on_requested_page(url):
                print("  … doğrulama geçildi, istenen sayfaya dönülüyor", flush=True)
                try:
                    self.page.goto(url, wait_until="domcontentloaded")
                    self.request_count += 1
                    self.page.wait_for_timeout(1200)
                except Exception:
                    continue
                if self._looks_like_interstitial() or not self._content_ready(ready_selectors):
                    continue
            print("  ✓ Devam ediliyor.\n", flush=True)
            return True
        print("  ✗ Süre doldu.\n", flush=True)
        return False

    def _on_requested_page(self, url: str) -> bool:
        """Compare paths only: the site adds and reorders query parameters freely."""
        from urllib.parse import urlparse

        current = self.current_url()
        if not current:
            return False
        return urlparse(current).path.rstrip("/") == urlparse(url).path.rstrip("/")

    # -- misc --------------------------------------------------------------- #

    def current_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    def html_to_pdf(self, html_path: Path, pdf_path: Path, *, landscape: bool = False) -> Path:
        """Render a local HTML file to PDF.

        ``page.pdf()`` is Chromium-only and requires headless, so this opens a
        throwaway headless browser rather than reusing the visible one.
        """
        from playwright.sync_api import sync_playwright

        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
                page.pdf(
                    path=str(pdf_path),
                    format="A4",
                    landscape=landscape,
                    print_background=True,
                    margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                )
            finally:
                browser.close()
        return pdf_path


@contextmanager
def session(config: BrowserConfig, pacer: Optional[Pacer] = None) -> Iterator[Session]:
    sess = Session(config, pacer)
    try:
        yield sess.start()
    finally:
        sess.close()
