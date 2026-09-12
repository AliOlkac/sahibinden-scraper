"""Loading and validating the selector config, plus run settings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .textnorm import tr_key

__all__ = [
    "load_selectors",
    "lint_config",
    "Settings",
    "DEFAULT_CONFIG_PATH",
    "LOCAL_OVERRIDE_PATH",
    "resolve_config_path",
]

# The shipped map lives inside the package so it survives `pip install`; a
# repo-relative path would only work from a source checkout.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "selectors.yaml"

# A copy here, in the working directory, wins over the shipped one — so the site's
# HTML can be repaired without touching an installed package.
LOCAL_OVERRIDE_PATH = Path("config") / "selectors.yaml"

_ENTITY = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")

# Letters that tr_fold() collapses away, so they cannot appear in a pattern that
# runs against folded text.
_TURKISH_LETTERS = set("ıİşŞğĞüÜöÖçÇâÂîÎûÛ")


def resolve_config_path(path: Optional[Path] = None) -> Path:
    """Which selector file a run will actually use."""
    if path:
        return Path(path)
    if LOCAL_OVERRIDE_PATH.is_file():
        return LOCAL_OVERRIDE_PATH
    return DEFAULT_CONFIG_PATH


def load_selectors(path: Optional[Path] = None) -> "dict[str, Any]":
    """Read the YAML selector map. Raises with a useful message when it is bad."""
    path = resolve_config_path(path)
    # is_file(), not exists(): a directory passed to --config would otherwise reach
    # read_text() and surface as a raw PermissionError traceback.
    if not path.is_file():
        raise FileNotFoundError(f"Seçici dosyası bulunamadı (ya da dosya değil): {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{path} okunamadı: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} okunamadı (geçersiz YAML): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} bir sözlük döndürmeli, {type(data).__name__} döndü")
    problems = lint_config(data)
    if problems:
        joined = "\n  - ".join(problems)
        raise ValueError(f"{path} tutarsız:\n  - {joined}")
    return data


def lint_config(config: "dict[str, Any]") -> "list[str]":
    """Cheap structural checks that catch the mistakes that fail *silently*.

    An HTML entity in a vocabulary key is the motivating example: values are
    entity-decoded before lookup, so a key written ``Benzin &amp; LPG`` can never
    match and every LPG-converted car quietly falls through to "unmapped".
    """
    problems: "list[str]" = []

    for section in ("search", "detail", "guards", "pagination"):
        if section not in config:
            problems.append(f"'{section}' bölümü eksik")

    for name, table in (config.get("vocab") or {}).items():
        if not isinstance(table, dict):
            problems.append(f"vocab.{name} bir sözlük olmalı")
            continue
        for key in table:
            if isinstance(key, str) and _ENTITY.search(key):
                problems.append(
                    f"vocab.{name}: '{key}' HTML entity içeriyor; "
                    "değerler çözümlenmiş halde geldiği için asla eşleşmez"
                )

    # Two aliases folding to the same key would make label lookup order-dependent.
    labels = (config.get("detail") or {}).get("labels") or {}
    seen: "dict[str, str]" = {}
    for field_name, aliases in labels.items():
        for alias in aliases or ():
            key = tr_key(alias)
            if key in seen and seen[key] != field_name:
                problems.append(
                    f"detail.labels: '{alias}' hem {seen[key]} hem {field_name} "
                    "alanına bağlı; hangisinin kazanacağı belirsiz"
                )
            seen[key] = field_name

    for field_name in (config.get("detail") or {}).get("required_labels") or ():
        if field_name not in labels:
            problems.append(f"detail.required_labels'daki '{field_name}' için alias yok")

    for name, pattern in (config.get("text_mining") or {}).items():
        if not isinstance(pattern, str):
            # A YAML scalar that parsed as a number or bool would otherwise reach
            # re.compile and raise TypeError out of the linter itself.
            problems.append(f"text_mining.{name} bir metin olmalı, {type(pattern).__name__} geldi")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"text_mining.{name} geçersiz regex: {exc}")
            continue
        # The haystack is tr_fold()-ed before matching, so a pattern containing a
        # Turkish character can never match. Caught here because the symptom is a
        # finding that silently never fires, not an error.
        offenders = sorted({ch for ch in pattern if ch in _TURKISH_LETTERS})
        if offenders:
            problems.append(
                f"text_mining.{name} Türkçe karakter içeriyor ({''.join(offenders)}); "
                "metin eşleşmeden önce ASCII'ye katlandığı için asla eşleşmez — "
                "deseni ASCII yazın (ör. 'ö'->'o', 'ı'->'i', 'ş'->'s')"
            )

    return problems


# --------------------------------------------------------------------------- #
# Run settings
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    """Everything one run needs, assembled from CLI arguments."""

    url: str = ""
    out_dir: Path = Path("out")
    run_dir: Path = Path("runs")
    run_name: Optional[str] = None
    config_path: Optional[Path] = None

    detail: bool = True
    limit: Optional[int] = None
    max_pages: Optional[int] = None
    formats: "tuple[str, ...]" = ("json",)

    headless: bool = False
    channel: Optional[str] = "chrome"
    profile_dir: Optional[Path] = None
    min_delay: float = 3.0
    max_delay: float = 7.0
    manual_timeout_s: int = 600
    request_budget: Optional[int] = None

    resume: bool = True
    refetch: bool = False
    fail_under: float = 0.6
    description_chars: int = 400
    include_images: bool = False
    accepted_terms: bool = False

    # Populated at run start so every parse in the run shares one clock.
    now_date: Any = None
    reference_year: Optional[int] = None

    def profile_path(self) -> Path:
        return self.profile_dir or (self.run_dir / "_browser_profile")
