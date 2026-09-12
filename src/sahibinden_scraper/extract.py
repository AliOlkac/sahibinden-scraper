"""A small, forgiving extraction engine driven by a YAML selector config.

sahibinden.com is bot-protected, so its markup could not be verified against a live
page while this was written, and it drifts over time regardless. Every rule is
therefore an *ordered list of fallbacks*, a miss is a ``None`` rather than an
exception, and the whole selector map lives in ``config/selectors.yaml`` so it can be
repaired without touching Python.

Two lookup styles are supported:

``css``    - ordered CSS selectors, first non-empty wins.
``label``  - find a label/value pair by matching Turkish label text (``Motor Gücü``)
             anywhere in an info list. Immune to column reordering, which positional
             selectors are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from bs4 import BeautifulSoup, Tag

from .textnorm import collapse_ws, tr_key

__all__ = [
    "Rule",
    "Extractor",
    "parse_html",
    "node_text",
    "label_value",
    "coverage",
]


def parse_html(html: str) -> BeautifulSoup:
    """Parse with lxml when available, falling back to the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - only when lxml is missing
        return BeautifulSoup(html, "html.parser")


def node_text(node: Optional[Tag], separator: str = " ") -> str:
    """Visible text of a node with script/style stripped and whitespace collapsed."""
    if node is None:
        return ""
    if not isinstance(node, Tag):
        return collapse_ws(str(node))
    clone = node
    text = clone.get_text(separator=separator, strip=True)
    return collapse_ws(text)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

@dataclass
class Rule:
    """One extraction rule: try each selector in order, take the first hit.

    Attributes
    ----------
    css       Ordered CSS selectors.
    labels    Turkish label aliases for ``label`` mode (e.g. ``["Motor Gücü"]``).
    attr      Attribute to read instead of text (``"href"``, ``"data-id"``, ...).
    regex     Optional pattern; if it has a group, group(1) is taken.
    many      Collect every match instead of the first.
    default   Returned when nothing matched.
    """

    css: Sequence[str] = field(default_factory=tuple)
    labels: Sequence[str] = field(default_factory=tuple)
    attr: Optional[str] = None
    regex: Optional[str] = None
    many: bool = False
    default: Any = None

    @classmethod
    def from_config(cls, spec: Any) -> "Rule":
        """Accept either a bare string / list of strings, or a full mapping."""
        if spec is None:
            return cls()
        if isinstance(spec, str):
            return cls(css=(spec,))
        if isinstance(spec, (list, tuple)):
            return cls(css=tuple(str(s) for s in spec))
        if isinstance(spec, dict):
            css = spec.get("css") or spec.get("selectors") or []
            if isinstance(css, str):
                css = [css]
            labels = spec.get("labels") or spec.get("label") or []
            if isinstance(labels, str):
                labels = [labels]
            return cls(
                css=tuple(str(s) for s in css),
                labels=tuple(str(s) for s in labels),
                attr=spec.get("attr"),
                regex=spec.get("regex"),
                many=bool(spec.get("many", False)),
                default=spec.get("default"),
            )
        raise TypeError(f"unsupported selector spec: {spec!r}")


def _read(node: Tag, rule: Rule) -> Optional[str]:
    """Pull the configured attribute or text out of one matched node."""
    if rule.attr:
        raw = node.get(rule.attr)
        if isinstance(raw, (list, tuple)):
            raw = " ".join(str(x) for x in raw)
        value = collapse_ws(raw) if raw else ""
    else:
        value = node_text(node)
    if not value:
        return None
    if rule.regex:
        m = re.search(rule.regex, value, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        value = collapse_ws(m.group(1) if m.groups() else m.group(0))
    return value or None


def _select(root: Tag, selector: str) -> "list[Tag]":
    """CSS select that never raises on a malformed or unsupported selector."""
    try:
        return list(root.select(selector))
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Label-based lookup
# --------------------------------------------------------------------------- #

# The container shapes sahibinden uses for label/value rows. ``div`` is included
# because some layouts render the info block as a CSS grid of divs — but including
# it means a *wrapper* div can also match, so _iter_label_rows filters to leaf rows.
_HOLDER_SELECTOR = "li, tr, dl > div, .classifiedInfoItem, div"


def _iter_label_rows(root: Tag):
    """Yield ``(folded_label, value)`` for every leaf label/value row under ``root``.

    Shared by :func:`label_value` and :func:`label_pairs` so the two can never
    disagree about what counts as a row — when they did, ``extras`` came back empty
    on exactly the markup ``label_value`` could read.

    A candidate is rejected when its value half still contains another label row:
    a wrapper div would otherwise match on its first descendant's label and hand
    back the entire block as the value ("2019 Model 1.6 TDI ...").
    """
    for holder in root.select(_HOLDER_SELECTOR):
        children = [c for c in holder.find_all(recursive=False) if isinstance(c, Tag)]
        if len(children) < 2:
            continue
        # Reject non-leaf holders: either half containing a nested row means this is
        # a wrapper, and the real row will be visited later in document order.
        if any(child.find(["li", "tr", "dt"]) for child in children):
            continue
        if any(_looks_like_row(descendant) for descendant in children[1].find_all(True)):
            continue
        key = tr_key(node_text(children[0]))
        value = node_text(children[1])
        if key and value:
            yield key, value

    for dt in root.select("dt"):
        key = tr_key(node_text(dt))
        value = node_text(dt.find_next_sibling("dd"))
        if key and value:
            yield key, value


def _looks_like_row(node: Tag) -> bool:
    """True when ``node`` is itself a plausible label/value row."""
    if not isinstance(node, Tag):
        return False
    children = [c for c in node.find_all(recursive=False) if isinstance(c, Tag)]
    return len(children) >= 2


def label_value(root: Optional[Tag], aliases: Iterable[str]) -> Optional[str]:
    """Find a value by its Turkish label inside a definition-list-ish container.

    Covers the shapes sahibinden is known to use:

    ``<li><strong>Yıl</strong><span>2019</span></li>``
    ``<li><span>Yıl</span><span>2019</span></li>``
    ``<tr><th>Yıl</th><td>2019</td></tr>``
    ``<dt>Yıl</dt><dd>2019</dd>``
    and a last-resort ``"Yıl: 2019"`` split on the same line.
    """
    if root is None:
        return None
    wanted = {tr_key(a) for a in aliases if a}
    if not wanted:
        return None

    for key, value in _iter_label_rows(root):
        if key in wanted:
            return value

    # Last resort: "Label: value" rendered as flat text.
    for line in node_text(root, separator="\n").split("\n"):
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        if tr_key(label) in wanted:
            value = collapse_ws(value)
            if value:
                return value
    return None


def label_pairs(root: Optional[Tag]) -> "dict[str, str]":
    """Every label -> value pair found in a container, keyed by folded label.

    Feeds ``extras`` (labels the config does not map) and the ``doctor`` report,
    which shows what the page actually offered when an alias failed to match.
    """
    out: "dict[str, str]" = {}
    if root is None:
        return out
    for key, value in _iter_label_rows(root):
        out.setdefault(key, value)
    return out


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #

class Extractor:
    """Applies a config subtree to a DOM node, recording which rules missed."""

    def __init__(self, config: "dict[str, Any]", *, strict: bool = False) -> None:
        self.config = config or {}
        self.strict = strict
        self.misses: "list[str]" = []
        self.hits: "list[str]" = []

    # -- rule resolution ---------------------------------------------------- #

    def rule(self, *path: str) -> Rule:
        """Look up ``config[a][b][c]`` and coerce it into a :class:`Rule`."""
        node: Any = self.config
        for part in path:
            if not isinstance(node, dict) or part not in node:
                return Rule()
            node = node[part]
        return Rule.from_config(node)

    # -- extraction --------------------------------------------------------- #

    def one(self, root: Optional[Tag], *path: str, scope: Optional[Tag] = None) -> Optional[str]:
        """First value produced by the rule at ``path``; ``None`` when all miss."""
        rule = self.rule(*path)
        value = self._apply(root, rule, scope=scope)
        name = ".".join(path)
        if value in (None, ""):
            self.misses.append(name)
            return rule.default
        self.hits.append(name)
        return value

    def many(self, root: Optional[Tag], *path: str) -> "list[str]":
        """Every distinct value the rule at ``path`` yields, order preserved."""
        rule = self.rule(*path)
        rule.many = True
        values = self._apply_many(root, rule)
        name = ".".join(path)
        (self.hits if values else self.misses).append(name)
        return values

    def nodes(self, root: Optional[Tag], *path: str) -> "list[Tag]":
        """Matched elements (not text) for rules that select containers or rows."""
        if root is None:
            return []
        rule = self.rule(*path)
        for selector in rule.css:
            found = _select(root, selector)
            if found:
                self.hits.append(".".join(path))
                return found
        self.misses.append(".".join(path))
        return []

    def node(self, root: Optional[Tag], *path: str) -> Optional[Tag]:
        found = self.nodes(root, *path)
        return found[0] if found else None

    # -- internals ---------------------------------------------------------- #

    def _apply(self, root: Optional[Tag], rule: Rule, scope: Optional[Tag] = None) -> Optional[str]:
        if root is None:
            return None
        for selector in rule.css:
            for node in _select(root, selector):
                value = _read(node, rule)
                if value:
                    return value
        if rule.labels:
            # Prefer a narrow scope (the info list) but fall back to the whole page,
            # because sahibinden moves these blocks between layouts.
            for container in (scope, root):
                value = label_value(container, rule.labels)
                if value:
                    return value
        return None

    def _apply_many(self, root: Optional[Tag], rule: Rule) -> "list[str]":
        if root is None:
            return []
        out: "list[str]" = []
        seen: "set[str]" = set()
        for selector in rule.css:
            for node in _select(root, selector):
                value = _read(node, rule)
                if value and value not in seen:
                    seen.add(value)
                    out.append(value)
            if out:
                break
        return out


def coverage(extractor: Extractor) -> "dict[str, Any]":
    """Field-coverage report used by the ``doctor`` self-test."""
    hits = sorted(set(extractor.hits))
    misses = sorted(set(extractor.misses) - set(hits))
    total = len(hits) + len(misses)
    return {
        "found": hits,
        "missing": misses,
        "found_count": len(hits),
        "missing_count": len(misses),
        "coverage_pct": round(100.0 * len(hits) / total, 1) if total else 0.0,
    }
