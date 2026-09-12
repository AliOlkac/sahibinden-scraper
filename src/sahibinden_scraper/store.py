"""On-disk run state: raw HTML cache, append-only progress log, resume support.

A scrape of several hundred listings is slow by design (it is paced politely), so it
has to survive being interrupted. Everything lands under a single run directory:

    runs/<slug>/
        pages/search-0001.html      raw search result pages
        pages/detail-<id>.html      raw detail pages
        listings.jsonl              one record per line, appended as it completes
        state.json                  run metadata + which ids are already done
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

__all__ = ["RunStore", "slugify", "utc_now_iso", "stable_key"]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_key(value: str, *, length: int = 12) -> str:
    """A short digest that is the same in every process.

    Python randomises ``hash()`` for str per interpreter run (PYTHONHASHSEED), so a
    cache filename built from it changes on every launch — the resumed run would
    miss every cached page and re-fetch the whole search, which is precisely the
    traffic this tool is trying not to generate.
    """
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def slugify(value: str, *, max_len: int = 60) -> str:
    """Filesystem-safe slug, ASCII only (Windows path rules are unforgiving)."""
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _SLUG_STRIP.sub("-", text).strip("-")
    return (text[:max_len].strip("-") or "run")


class RunStore:
    """Owns one run directory and everything written into it."""

    def __init__(self, root: Path, *, name: Optional[str] = None) -> None:
        self.root = Path(root)
        self.name = name or "run"
        self.dir = self.root / self.name
        self.pages_dir = self.dir / "pages"
        self.listings_path = self.dir / "listings.jsonl"
        self.state_path = self.dir / "state.json"
        self._state: "dict[str, Any]" = {}

    # -- lifecycle ---------------------------------------------------------- #

    def open(self, *, meta: Optional["dict[str, Any]"] = None) -> "RunStore":
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self._state = self._read_state()
        self._state.setdefault("created_at", utc_now_iso())
        self._state["updated_at"] = utc_now_iso()
        if meta:
            self._state.setdefault("meta", {}).update(meta)
        self._write_state()
        return self

    def _read_state(self) -> "dict[str, Any]":
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    @property
    def meta(self) -> "dict[str, Any]":
        return self._state.setdefault("meta", {})

    def set_meta(self, **kwargs: Any) -> None:
        self.meta.update(kwargs)
        self._state["updated_at"] = utc_now_iso()
        self._write_state()

    # -- raw page cache ----------------------------------------------------- #

    def page_path(self, kind: str, key: str) -> Path:
        """Cache filename for one page.

        A readable slug plus a digest of the full key: the slug keeps the directory
        browsable, the digest keeps two long keys that share a 40-character prefix
        (two listing URLs for the same model, say) from overwriting each other.
        """
        key = str(key)
        slug = slugify(key, max_len=40)
        return self.pages_dir / f"{kind}-{slug}-{stable_key(key, length=10)}.html"

    # A page shorter than this is a torn write or an error stub, never real markup.
    MIN_PAGE_BYTES = 2048

    def has_page(self, kind: str, key: str) -> bool:
        path = self.page_path(kind, key)
        return path.is_file() and path.stat().st_size >= self.MIN_PAGE_BYTES

    def read_page(self, kind: str, key: str) -> Optional[str]:
        """Cached HTML, or ``None`` when there is nothing usable.

        A run killed mid-write leaves a truncated or zero-byte file. Returning its
        empty contents would count as a cache *hit*, and the resumed run would then
        "parse" nothing and record a listing with every field missing — so an
        implausibly short file is treated as absent and refetched.
        """
        path = self.page_path(kind, key)
        if not path.is_file():
            return None
        try:
            if path.stat().st_size < self.MIN_PAGE_BYTES:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def write_page(self, kind: str, key: str, html: str) -> Path:
        """Write atomically, so an interrupt cannot leave a half-written page."""
        path = self.page_path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(path)
        return path

    # -- listing records ---------------------------------------------------- #

    def completed_ids(self, *, require_detail: bool = False) -> "set[str]":
        """Ids already in listings.jsonl, so a resumed run can skip them.

        ``require_detail`` matters more than it looks. A ``--fast`` run, or one that
        hit its request budget, writes stub rows carrying only search-page data. If
        those counted as finished, every later resume would skip them forever and
        the detail pages would never be fetched — the run would look complete while
        permanently missing every technical field.
        """
        ids: "set[str]" = set()
        for record in self.iter_listings():
            identifier = record.get("id")
            if not identifier:
                continue
            if require_detail and not record.get("detail_fetched"):
                continue
            ids.add(identifier)
        return ids

    def iter_listings(self) -> Iterator["dict[str, Any]"]:
        if not self.listings_path.exists():
            return iter(())

        def _gen() -> Iterator["dict[str, Any]"]:
            with self.listings_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # A run killed mid-write can leave one torn line; skip it.
                        continue

        return _gen()

    def append_listing(self, record: "dict[str, Any]") -> None:
        self.listings_path.parent.mkdir(parents=True, exist_ok=True)
        with self.listings_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_listings(self, *, dedupe: bool = True) -> "list[dict[str, Any]]":
        """All records, with later writes for the same id winning."""
        records = list(self.iter_listings())
        if not dedupe:
            return records
        by_id: "dict[str, dict[str, Any]]" = {}
        order: "list[str]" = []
        for rec in records:
            key = str(rec.get("id") or rec.get("url") or len(order))
            if key not in by_id:
                order.append(key)
            by_id[key] = rec
        return [by_id[k] for k in order]

    # -- errors ------------------------------------------------------------- #

    def record_error(self, key: str, message: str) -> None:
        errors = self._state.setdefault("errors", [])
        errors.append({"key": key, "message": message, "at": utc_now_iso()})
        self._state["updated_at"] = utc_now_iso()
        self._write_state()

    @property
    def errors(self) -> "list[dict[str, Any]]":
        return list(self._state.get("errors", []))


def write_json(path: Path, payload: Any) -> Path:
    """Write pretty UTF-8 JSON, creating parents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
