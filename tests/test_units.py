"""Unit tests for the rules that real-site testing showed to matter."""
import gzip
import sys

import pytest

from site_scraper import helpers as H
from site_scraper.crawler import Scraper


@pytest.fixture()
def scraper(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["site-scraper", "https://www.example.com", "--out", str(tmp_path)])
    from site_scraper.cli import parse_args
    sc = Scraper(parse_args())
    sc.scheme, sc.host, sc.domain, sc.root = "https", "www.example.com", "example.com", "https://www.example.com"
    sc.section_prefix, sc.region = "", None
    return sc


def test_csv_safe_defuses_formulas():
    assert H.csv_row(["=HYPERLINK(1)", "+1", "-x", "@a", "ok", 5]) == ["'=HYPERLINK(1)", "'+1", "'-x", "'@a", "ok", 5]


def test_private_addresses_are_refused():
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "[::1]", "localhost"):
        assert H.is_public_host(host) is False, host
    assert H.is_public_host("8.8.8.8") is True


def test_gzip_bomb_is_capped():
    bomb = gzip.compress(b"0" * 50_000_000)
    assert len(H.gunzip_capped(bomb, 1_000_000)) == 1_000_000


def test_malformed_links_do_not_raise():
    assert H.safe_join("https://example.com/a", "http://[broken") == ""
    assert H.safe_join("https://example.com/a/", "b.html") == "https://example.com/a/b.html"


def test_fit_name_respects_windows_path_limit(tmp_path):
    name = H.fit_name(tmp_path, "x" * 400, ".md")
    assert len(str(tmp_path / name)) <= 250 and name.endswith(".md")


@pytest.mark.parametrize("domain,path,ok", [
    ("github.com", "/example-co", True), ("github.com", "/example-co/tool/issues/12", False),
    ("github.com", "/example-co/tool", False), ("linkedin.com", "/company/example", True),
    ("linkedin.com", "/feed", False), ("youtube.com", "/@example", True), ("youtube.com", "/watch", False),
    ("x.com", "/example", True), ("x.com", "/intent/tweet", False), ("facebook.com", "/sharer/sharer.php", False),
])
def test_social_profiles(domain, path, ok):
    assert Scraper.is_profile(domain, path) is ok


def test_url_normalisation_drops_tracking_and_fragments(scraper):
    assert scraper.norm("/a/?utm_source=x&b=2&a=1#top") == "https://www.example.com/a?a=1&b=2"
    assert scraper.norm("mailto:x@example.com") is None


def test_crawler_traps_are_skipped(scraper):
    scraper.read_robots = None
    scraper.robots = type("R", (), {"can_fetch": lambda *a: True})()
    assert scraper.skip_reason("https://www.example.com/a/b/a/b/a/b", 1).startswith("trap")
    assert scraper.skip_reason("https://www.example.com/" + "x" * 400, 1).startswith("trap")
    assert scraper.skip_reason("https://other.org/page", 1) == "out of scope"


def test_section_under_locale_path(scraper):
    scraper.section_prefix = "en_in"
    assert scraper.section_of("https://www.example.com/en_in/insights/ai") == "insights"
    scraper.section_prefix = ""
    assert scraper.section_of("https://www.example.com/blog/post") == "blog"


def test_extract_titles_dates_and_placeholders(scraper, tmp_path):
    scraper.out = tmp_path
    (tmp_path / "tables").mkdir()
    (tmp_path / "api").mkdir()
    html = ("<html><head><title>Shop | Example</title></head><body><h1>Card one</h1><h1>Card two</h1>"
            "<article><p>" + "Product text. " * 40 + "</p></article></body></html>")
    rec, links, render = scraper.extract("https://www.example.com/shop", html)
    assert rec["title"] == "Shop"          # several <h1>s: the <title> wins
    assert rec["date_published"] == ""     # no article markup: no guessed date
    # a page that is mostly template placeholder text gets sent to the browser (when one is available)
    shell = "<html><body>" + "<div>This is some text inside of a div block.</div>" * 8 + "</body></html>"
    needs = scraper.needs_render(__import__("bs4").BeautifulSoup(shell, "lxml"),
                                 "This is some text inside of a div block. " * 8)
    assert needs is scraper.can_render()
