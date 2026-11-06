"""Sources a document cites: `h review prepare` fetches every `[T2: <url>]` the
change adds, so the reviewer can judge a claim against the text without network access.

The reviewer reads untrusted text; it gets no network tool. h fetches only the URLs the
author wrote, with fixed limits, and hands the text over as data.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

# `[T2: https://…]`, also with text after the URL: `[T2: https://… §3]`.
# `[T2: https://host/path]`, also scheme-less `[T2: host.tld/path, 2026-09-20]`: a
# host with a dot and a letters-only TLD, then a path — not `docs/x.md`, a word or a bare host.
CITED = re.compile(
    r"\[T2:\s*((?:https?://[^\s\]]+)|(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/[^\s\],]*))"
)
MAX_URLS = 20
TIMEOUT = 10.0  # per socket operation and, for the whole read, a deadline
MAX_BYTES = 512 * 1024  # read at most this much of a response
MAX_TEXT = 20 * 1024  # keep at most this much text per source
AGENT = "harness-review (+https://github.com/wideGenesis/parallel-agents-harness)"

_SCRIPT = re.compile(
    r"<(script|style|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_LINES = re.compile(r"\n\s*\n+")


@dataclass
class Source:
    url: str
    text: str = ""
    error: str = ""  # set when the source could not be read


# `[T2: docs.github.com/<path>, date]` shows the format; such a URL names no page.
TEMPLATE = re.compile(r"[<>{}…]")
# Kept as they are when encoding a path or query: URL syntax and existing %-escapes.
_SAFE = "/%:@!$&'()*+,;=-._~?#[]"


def to_uri(url: str) -> str:
    """An IRI as written (`de.wikipedia.org/wiki/Straße`) → a URI http can send: IDNA host,
    UTF-8 percent-encoded path and query."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as e:  # fetch reports it as an invalid URL, not a crash
        raise ValueError(f"host {host!r} is not a valid domain name") from e
    if ":" in host:  # IPv6 keeps its brackets
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, quote(parts.path, safe=_SAFE),
                       quote(parts.query, safe=_SAFE), ""))  # fmt: skip


def cited(added: list[str]) -> list[str]:
    """The distinct URLs cited as [T2: url] in these added lines, in order."""
    seen: dict[str, None] = {}
    for line in added:
        for url in CITED.findall(line):
            url = url.rstrip(".,;)")
            if TEMPLATE.search(url):  # a format example, not a source
                continue
            if not url.lower().startswith(("http://", "https://")):
                url = f"https://{url}"
            seen.setdefault(url, None)
    return list(seen)


# Tests serve sources from 127.0.0.1; everything else gets public hosts only.
ALLOW_PRIVATE = False


def _private(url: str) -> str:
    """Why `url` must not be fetched: a host that resolves to a loopback, private,
    link-local (cloud metadata) or otherwise non-public address. "" when it is public."""
    host = urlsplit(url).hostname
    if not host:
        return "no host"
    if ALLOW_PRIVATE:
        return ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"unreachable: {e}"
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not ip.is_global:
            return f"not a public address ({ip})"
    return ""


class _HttpOnly(urllib.request.HTTPRedirectHandler):
    """A redirect may go to another http(s) page, never to file:, ftp: or the like."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith(("http://", "https://")):
            raise urllib.error.URLError(f"redirect to a non-http URL: {newurl}")
        if why := _private(newurl):
            raise urllib.error.URLError(f"redirect to {newurl}: {why}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpOnly)


def text_of(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    if m := re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE):
        charset = m.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower() or body.lstrip()[:15].lower().startswith(
        ("<!doctype", "<html")
    ):
        body = _TAG.sub(" ", _SCRIPT.sub(" ", body))
        body = html.unescape(body)
    body = _LINES.sub("\n\n", _SPACE.sub(" ", body)).strip()
    return body[:MAX_TEXT]


def fetch(url: str) -> Source:
    if not url.lower().startswith(("http://", "https://")):
        return Source(url, error="not an http(s) URL")
    try:
        uri = to_uri(url)
    except ValueError as e:  # a bad port and the like
        return Source(url, error=f"not a valid URL: {e}")
    if why := _private(uri):
        return Source(url, error=why)
    req = urllib.request.Request(uri, headers={"User-Agent": AGENT})
    deadline = time.monotonic() + TIMEOUT
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            kind = r.headers.get("Content-Type", "")
            raw = b""
            # In pieces against a deadline: TIMEOUT alone bounds each socket read, and a
            # server sending a byte at a time would keep a single read going for minutes.
            while len(raw) < MAX_BYTES:
                if time.monotonic() > deadline:
                    return Source(url, error=f"too slow: over {TIMEOUT:g}s")
                piece = r.read(min(16384, MAX_BYTES - len(raw)))
                if not piece:
                    break
                raw += piece
    except urllib.error.HTTPError as e:
        return Source(url, error=f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        return Source(url, error=f"unreachable: {reason}")
    text = text_of(raw, kind)
    if not text:
        return Source(url, error="no text in the response")
    return Source(url, text)
