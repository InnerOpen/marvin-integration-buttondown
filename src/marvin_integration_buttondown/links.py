"""Make an issue body's relative links absolute, so they still work in a subscriber's inbox.

A site renders `[a work](/works/x)` against its own origin; an email has no origin, so the same link
is dead there. Markdown inline links and images, reference definitions, and HTML ``href``/``src``
attributes are rewritten against the site's base URL. Anything that already names a scheme
(``https:``, ``mailto:``, ``tel:``), a protocol-relative ``//host`` URL, an in-page ``#anchor`` and
a template tag (``{{ … }}``) is left exactly as written.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# ](url  or  ](<url  — inline links and images; the title, if any, follows after whitespace.
_MD_INLINE = re.compile(r"(\]\(\s*<?)([^\s)>]+)")
# [label]: url  — reference-style definitions, at the start of a line.
_MD_REFERENCE = re.compile(r"^(\s{0,3}\[[^\]\n]+\]:\s*<?)(\S+?)(?=>|\s|$)", re.MULTILINE)
_HTML_ATTR = re.compile(r"(\b(?:href|src)\s*=\s*)([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)


def normalise_base_url(value: str | None) -> str:
    """``https://example.com/`` for a usable http(s) URL; "" for blank. Raises on anything else."""
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"The site URL must be an http(s) address like https://example.com, not {text!r}.")
    return text if text.endswith("/") else f"{text}/"


def is_relative(url: str) -> bool:
    url = url.strip()
    return bool(url) and not (_SCHEME.match(url) or url.startswith(("//", "#", "{", "$")))


def absolutize_url(url: str, base: str) -> str:
    """One URL made absolute against ``base`` (normalised); absolute and special URLs are returned as-is."""
    # Relative to the site root, whatever page the link was written on: `works/x` and `/works/x` agree.
    return urljoin(base, url.lstrip("/")) if is_relative(url) else url


def absolutize_links(body: str, base_url: str) -> str:
    """``body`` with every relative link made absolute against ``base_url`` (already normalised)."""
    if not body or not base_url:
        return body
    body = _MD_INLINE.sub(lambda m: m.group(1) + absolutize_url(m.group(2), base_url), body)
    body = _MD_REFERENCE.sub(lambda m: m.group(1) + absolutize_url(m.group(2), base_url), body)
    return _HTML_ATTR.sub(lambda m: f"{m.group(1)}{m.group(2)}{absolutize_url(m.group(3), base_url)}{m.group(2)}", body)


def has_relative_links(body: str) -> bool:
    """Whether ``body`` holds a link that would be dead in an email without a base URL."""
    found = [m.group(2) for m in _MD_INLINE.finditer(body or "")]
    found += [m.group(2) for m in _MD_REFERENCE.finditer(body or "")]
    found += [m.group(3) for m in _HTML_ATTR.finditer(body or "")]
    return any(is_relative(url) for url in found)
