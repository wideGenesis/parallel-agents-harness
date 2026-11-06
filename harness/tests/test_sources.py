"""Document review: h fetches the [T2: url] sources a change cites and puts them in
the review package; the reviewer has no network."""

import http.server
import json
import threading
import urllib.parse

import pytest
from conftest import git
from test_review import write_verdict
from test_review import wt as _wt

from harness import config, review, sources

wt = _wt  # the task worktree fixture from test_review


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if urllib.parse.unquote(self.path) == "/wiki/Straße":
            body = "Artikel über Migrationen".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        elif self.path == "/page":
            body = (b"<html><head><style>x{}</style><script>evil()</script></head>"
                    b"<body><p>Postgres 18 &amp; sqlx: migrations run in order.</p>"
                    b"</body></html>")  # fmt: skip
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/big":
            body = b"a" * (sources.MAX_BYTES * 2)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        elif self.path == "/to-file":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1/secret")  # urllib follows ftp
            self.end_headers()
            return
        else:
            body = b"nope"
            self.send_response(404)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # quiet test server
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(sources, "ALLOW_PRIVATE", True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_cited_takes_t2_urls_once():
    lines = ["a [T2: https://x.org/a] b [T2: https://x.org/a].", "[T1] no url",
             "[T2: http://y.org/b §3] and [T3: https://z.org]"]  # fmt: skip
    assert sources.cited(lines) == ["https://x.org/a", "http://y.org/b"]


def test_fetch_html_keeps_text_only(server):
    src = sources.fetch(f"{server}/page")
    assert not src.error
    assert src.text == "Postgres 18 & sqlx: migrations run in order."


def test_fetch_failures_are_named(server, monkeypatch):
    assert sources.fetch(f"{server}/missing").error == "HTTP 404"
    assert "non-http" in sources.fetch(f"{server}/to-file").error
    assert sources.fetch("file:///etc/passwd").error == "not an http(s) URL"
    monkeypatch.setattr(sources, "TIMEOUT", 0.5)
    assert sources.fetch("http://10.255.255.1/").error.startswith("unreachable")


def test_fetch_caps_the_text(server):
    assert len(sources.fetch(f"{server}/big").text) == sources.MAX_TEXT


def test_document_change_adds_the_documents_checklist(wt, server, monkeypatch):
    adr = wt / "docs" / "adr" / "0001-x.md"
    adr.parent.mkdir(parents=True)
    adr.write_text(f"# ADR\n\nMigrations run in order [T2: {server}/page].\n"
                   f"Gone [T2: {server}/missing].\n", encoding="utf-8")  # fmt: skip
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "docs: adr")
    cfg = config.load(wt)
    pkg, _, _ = review.prepare(cfg, wt)
    text = pkg.read_text(encoding="utf-8")
    assert "### documents" in text and "`docs/adr/0001-x.md`" in text
    assert "Postgres 18 & sqlx: migrations run in order." in text
    assert f"{server}/missing — **unreachable** (HTTP 404)" in text
    assert review._state(wt)["checklists"] == ["documents"]
    write_verdict(wt, "approve")  # no "checklists": the gate refuses it
    with pytest.raises(review.ReviewError, match="checklist documents"):
        review.gate(cfg, wt)
    v = json.loads((wt / review.VERDICT).read_text(encoding="utf-8"))
    (wt / review.VERDICT).write_text(json.dumps({**v, "checklists": ["documents"]}),
                                     encoding="utf-8")  # fmt: skip
    assert review.gate(cfg, wt) is not None


def test_code_change_has_no_documents_checklist(wt):
    pkg, _, _ = review.prepare(config.load(wt), wt)
    assert "### documents" not in pkg.read_text(encoding="utf-8")
    assert "checklists" not in review._state(wt)


def test_review_docs_from_config():
    assert config._review_docs({})[0] == "docs/adr/**"
    assert config._review_docs({"docs": ["notes/**"]}) == ["notes/**"]
    with pytest.raises(config.ConfigError, match="review.docs"):
        config._review_docs({"docs": "notes/**"})


def test_private_hosts_are_not_fetched():
    """The author picks the URL: never a loopback, private or metadata address."""
    for url in ("http://127.0.0.1:1/x", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.1/", "http://localhost/"):  # fmt: skip
        assert "not a public address" in sources.fetch(url).error


def test_slow_drip_hits_the_deadline(server, monkeypatch):
    monkeypatch.setattr(sources, "TIMEOUT", 0.3)
    monkeypatch.setattr(sources.time, "monotonic", iter([0.0, 0.0, 9.0, 9.0]).__next__)
    assert sources.fetch(f"{server}/big").error.startswith("too slow")


def test_cited_keeps_every_url_and_the_package_marks_the_rest(wt, monkeypatch):
    lines = [f"[T2: https://x.org/{i}]" for i in range(sources.MAX_URLS + 2)]
    assert len(sources.cited(lines)) == sources.MAX_URLS + 2
    adr = wt / "docs" / "adr" / "0002-y.md"
    adr.parent.mkdir(parents=True, exist_ok=True)
    adr.write_text("\n".join(lines) + "\n", encoding="utf-8")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "docs: many sources")
    monkeypatch.setattr(sources, "fetch", lambda u: sources.Source(u, "t ``` x"))
    pkg, _, _ = review.prepare(config.load(wt), wt)
    text = pkg.read_text(encoding="utf-8")
    assert (
        f"https://x.org/{sources.MAX_URLS + 1} — **unreachable** (not fetched" in text
    )
    assert (
        "````text\nt ``` x\n````" in text
    )  # the fence outlasts the source's backticks


def test_scheme_less_sources_are_fetched_as_https():
    """A project writes `[T2: host/path, date]` without a scheme."""
    lines = ["[T2: docs.github.com/en/actions/x, 2026-09-20]", "[T2: docs.github.com, 2026-09-20]",
             "[T2: docs/provenance.md]", "[T2: the user]", "[T2: crates/api/src/x.rs:12]"]  # fmt: skip
    assert sources.cited(lines) == ["https://docs.github.com/en/actions/x"]
    assert sources.cited(["[T2: https://x.org/a,b, 2026]"]) == ["https://x.org/a,b"]


def test_project_provenance_scheme_goes_into_the_package(wt, monkeypatch):
    """`[review] provenance` carries the project's scheme for criterion 1."""
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "provenance.md").write_text("E1. Unlabelled reads as [T4].\n",
                                               encoding="utf-8")  # fmt: skip
    rule = wt / "AGENTS.md"
    with rule.open("a", encoding="utf-8") as f:
        f.write("- a new rule\n")
    git(wt, "add", "docs/provenance.md")
    git(wt, "commit", "-qm", "docs: scheme")
    git(wt, "push", "-q", "origin", "HEAD:main")  # the scheme lives on the base branch
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "docs: rule")
    cfg = config.load(wt)
    cfg.review_provenance = "docs/provenance.md"
    review._git(wt, "fetch", "-q", "origin", "main")
    pkg, _, _ = review.prepare(cfg, wt)
    text = pkg.read_text(encoding="utf-8")
    assert "Provenance scheme of this project (`docs/provenance.md`)" in text
    assert "E1. Unlabelled reads as [T4]." in text
    cfg.review_provenance = "docs/missing.md"
    with pytest.raises(
        review.ReviewError, match="provenance: docs/missing.md not found"
    ):
        review.documents(cfg, wt, review._git(wt, "merge-base", "main", "HEAD").strip(),
                         ["AGENTS.md"])  # fmt: skip


def test_review_provenance_from_config():
    assert config._review_provenance({}) == ""
    assert config._review_provenance({"provenance": "docs/e.md"}) == "docs/e.md"
    for bad in ("/etc/x", "../x", 3, "C:/x", r"docs\..\x", r"\\host\x"):
        with pytest.raises(config.ConfigError, match="review.provenance"):
            config._review_provenance({"provenance": bad})


def test_non_ascii_path_is_fetched(server):
    """a non-ASCII path is percent-encoded, not a UnicodeEncodeError."""
    src = sources.fetch(f"{server}/wiki/Straße")
    assert not src.error and src.text == "Artikel über Migrationen"
    assert sources.to_uri("https://bücher.example/straße?q=ü") == (
        "https://xn--bcher-kva.example/stra%C3%9Fe?q=%C3%BC"
    )


def test_placeholder_is_not_a_source():
    """a format example in an instruction is not a citation."""
    line = "x `[T2: docs.github.com/<path>, date]` y [T2: a.org/{page}] [T2: b.org/x…]"
    assert sources.cited([line]) == []


def test_bad_host_is_an_invalid_url_not_a_crash():
    src = sources.fetch(
        "https://" + "ü" * 70 + ".example/x"
    )  # a label over 63 chars: no IDNA
    assert src.error.startswith("not a valid URL")
    assert sources.to_uri("http://[::1]:8080/x") == "http://[::1]:8080/x"
