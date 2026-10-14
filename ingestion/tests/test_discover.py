import csv

import httpx
import pytest

from ingestion.crawler import USER_AGENT, Crawler
from ingestion.discover import (
    DEFAULT_RULES_PATH,
    RulesError,
    compile_pattern,
    discover,
    load_rules,
    normalize_url,
    parse_sitemap_html,
    parse_sitemap_xml,
    write_candidates,
)
from ingestion.tests.test_crawler import ROBOTS_TXT, FakeClock

WWW = "https://www.desu.edu"
CAST = "https://cast.desu.edu"

RULES_YAML = """\
hosts: [www.desu.edu, cast.desu.edu]
exclude:
  - "**/news"
  - "**/faculty-profiles"
  - "**/*-form"
include:
  - topic: financial-aid
    change_frequency: medium
    paths: [www.desu.edu/admissions/financial-aid]
  - topic: admissions
    change_frequency: medium
    paths: [www.desu.edu/admissions]
  - topic: academics
    change_frequency: slow
    paths: ["*.desu.edu/departments/*$"]
"""


def sitemap_xml(*urls):
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    )


def sitemap_html(*links):
    items = "".join(f'<li><a href="{href}">{text}</a></li>' for href, text in links)
    return (
        "<html><body><main><div class='site-map-menus'><ul>"
        f"{items}"
        "<div class='element-invisible'><a href='https://example.com/x'>Hidden</a></div>"
        "</ul></div></main></body></html>"
    )


def page(title, canonical=None, body="Some words about this page."):
    link = f'<link rel="canonical" href="{canonical}" />' if canonical else ""
    return (
        f"<html><head><title>{title} | Delaware State University</title>{link}</head>"
        f"<body><main><div role='main'><h1>{title}</h1><p>{body}</p></div></main></body></html>"
    )


class FakeHosts:
    """Serves canned responses keyed by host and path, and records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        key = request.url.host + request.url.path
        route = self.routes.get(key)
        if route is None and request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_TXT)
        if route is None:
            return httpx.Response(404)
        if isinstance(route, str):
            return httpx.Response(200, text=route, headers={"content-type": "text/html"})
        return route

    def fetched(self, host):
        return [r.url.path for r in self.requests if r.url.host == host]


def make_crawler(tmp_path, site, delay=0.0):
    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(site), headers={"User-Agent": USER_AGENT})
    crawler = Crawler(tmp_path, delay=delay, client=client, sleep=clock.sleep, clock=clock)
    return crawler, clock


@pytest.fixture
def rules(tmp_path):
    path = tmp_path / "discovery.yaml"
    path.write_text(RULES_YAML)
    return load_rules(path)


# Sitemap parsing


def test_parse_sitemap_xml_urlset_and_index():
    pages, children = parse_sitemap_xml(sitemap_xml(WWW + "/a", WWW + "/b"))
    assert pages == [WWW + "/a", WWW + "/b"] and children == []

    index = (
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{WWW}/sitemap.xml?page=2</loc></sitemap></sitemapindex>"
    )
    assert parse_sitemap_xml(index) == ([], [WWW + "/sitemap.xml?page=2"])


def test_parse_sitemap_xml_without_namespace_and_invalid():
    assert parse_sitemap_xml(f"<urlset><url><loc> {WWW}/a </loc></url></urlset>")[0] == [WWW + "/a"]
    assert parse_sitemap_xml("<html>not a sitemap") == ([], [])


def test_parse_sitemap_html_resolves_links_and_skips_hidden_menus():
    html = sitemap_html(("/admissions", "Admissions"), ("#top", "Top"), ("mailto:a@desu.edu", "x"))
    assert parse_sitemap_html(html, WWW + "/sitemap") == [(WWW + "/admissions", "Admissions")]


# Include and exclude rules


def test_patterns():
    assert compile_pattern("www.desu.edu/admissions").fullmatch("www.desu.edu/admissions/apply")
    assert compile_pattern("www.desu.edu/admissions").fullmatch("www.desu.edu/admissions")
    assert not compile_pattern("www.desu.edu/admissions").fullmatch("www.desu.edu/admissionsx")
    assert compile_pattern("*.desu.edu/departments/*$").fullmatch("cast.desu.edu/departments/bio")
    assert not compile_pattern("*.desu.edu/departments/*$").fullmatch(
        "cast.desu.edu/departments/bio/faculty"
    )
    assert compile_pattern("**/news").fullmatch("www.desu.edu/about/news/2026")


def test_rules_exclude_wins_and_first_include_wins(rules):
    rule, _ = rules.classify(WWW + "/admissions/financial-aid/loans")
    assert rule.topic == "financial-aid"
    assert rules.classify(WWW + "/admissions/apply-now")[0].topic == "admissions"
    assert rules.classify(WWW + "/admissions/news") == (None, "excluded by **/news")
    assert rules.classify(CAST + "/about/faculty-profiles/jane")[1] == (
        "excluded by **/faculty-profiles"
    )
    assert rules.classify(WWW + "/giving") == (None, "no include rule")


def test_shipped_rules_load_and_keep_student_pages():
    rules = load_rules(DEFAULT_RULES_PATH)
    assert rules.classify(WWW + "/admissions/financial-aid")[0].topic == "financial-aid"
    assert rules.classify(WWW + "/student-life/campus-health-services/counseling-services")[0]
    for excluded in (
        "/news",
        "/about/administration/board-trustees/board-agendas-minutes-notices",
        "/about/administration/human-resources/job-opportunities",
        "/about/police-department/daily-crime-fire-log",
        "/giving/give-now",
    ):
        assert rules.classify(WWW + excluded)[0] is None, excluded
    assert rules.classify(CAST + "/about/faculty-profiles/someone")[0] is None


@pytest.mark.parametrize(
    "text, message",
    [
        ("hosts: [example.com]\ninclude: []", "hosts"),
        ("hosts: [www.desu.edu]\ninclude: []", "include must be"),
        (
            "hosts: [www.desu.edu]\ninclude:\n"
            "  - {topic: aid, change_frequency: daily, paths: [a]}",
            "change_frequency",
        ),
        ("hosts: [www.desu.edu]\ninclude:\n  - {topic: aid, paths: [a]}", "exactly"),
        ("hosts: [www.desu.edu]\nexclude: [1]\ninclude: []", "exclude"),
    ],
)
def test_invalid_rules_rejected(tmp_path, text, message):
    path = tmp_path / "rules.yaml"
    path.write_text(text)
    with pytest.raises(RulesError, match=message):
        load_rules(path)


# Canonical deduping


def test_normalize_url():
    assert normalize_url("http://DESU.edu//admissions/?utm=1#x") == WWW + "/admissions"
    assert normalize_url("https://cast.desu.edu/") == CAST
    assert normalize_url("https://example.com/admissions") is None


def test_discover_dedupes_by_canonical_url(tmp_path, rules):
    site = FakeHosts(
        {
            "www.desu.edu/sitemap.xml": sitemap_xml(
                WWW + "/admissions/apply-now",
                WWW + "/admissions/apply-now/",  # same page, trailing slash
                WWW + "/admissions/apply",  # same page, another path; canonical says so
                WWW + "/admissions/visit",
                WWW + "/admissions/financial-aid",  # already registered
                WWW + "/news",
                WWW + "/files/guide.pdf",
            ),
            "www.desu.edu/sitemap": sitemap_html(("/admissions/visit", "Visit")),
            "www.desu.edu/admissions/apply-now": page("Apply Now"),
            "www.desu.edu/admissions/apply": page(
                "Apply Now", canonical=WWW + "/admissions/apply-now"
            ),
            "www.desu.edu/admissions/visit": page(
                "Visit DSU", body='<a href="/sites/flagship/files/map.pdf">Map</a>'
            ),
            "cast.desu.edu/sitemap.xml": httpx.Response(404),
            "cast.desu.edu/sitemap": httpx.Response(404),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    report = discover(crawler, rules, {WWW + "/admissions/financial-aid"})

    by_url = {c.url: c for c in report.candidates}
    assert set(by_url) == {WWW + "/admissions/apply-now", WWW + "/admissions/visit"}
    assert by_url[WWW + "/admissions/apply-now"].duplicate_urls == [WWW + "/admissions/apply"]
    assert by_url[WWW + "/admissions/apply-now"].title == "Apply Now"
    assert by_url[WWW + "/admissions/visit"].found_in == {"sitemap.xml", "sitemap"}
    assert report.registered == [WWW + "/admissions/financial-aid"]
    assert report.dropped["excluded by **/news"] == 1
    # PDFs are listed, never fetched
    assert set(report.pdfs) == {WWW + "/files/guide.pdf", WWW + "/sites/flagship/files/map.pdf"}
    assert not [p for p in site.fetched("www.desu.edu") if p.endswith(".pdf")]
    # The pages after filtering are fetched once each; excluded and registered ones never
    assert site.fetched("www.desu.edu").count("/admissions/apply-now") == 1
    assert "/news" not in site.fetched("www.desu.edu")
    assert "/admissions/financial-aid" not in site.fetched("www.desu.edu")

    assert report.hosts["www.desu.edu"][1:] == ["/sitemap.xml: found, 7 URLs", "/sitemap: found"]
    assert report.hosts["cast.desu.edu"][1:] == ["/sitemap.xml: HTTP 404", "/sitemap: HTTP 404"]

    out = tmp_path / "candidates.csv"
    write_candidates(report.candidates, out)
    rows = list(csv.DictReader(out.open()))
    assert [r["url"] for r in rows] == [WWW + "/admissions/apply-now", WWW + "/admissions/visit"]
    assert rows[0]["approve"] == "" and rows[0]["topic"] == "admissions"


def test_canonical_pointing_at_registered_page_is_dropped(tmp_path, rules):
    site = FakeHosts(
        {
            "www.desu.edu/sitemap.xml": sitemap_xml(WWW + "/admissions/old-apply"),
            "www.desu.edu/admissions/old-apply": page("Apply", canonical=WWW + "/admissions/apply"),
            "cast.desu.edu/robots.txt": httpx.Response(404),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)
    report = discover(crawler, rules, {WWW + "/admissions/apply"})
    assert report.candidates == []
    assert report.registered == [WWW + "/admissions/old-apply"]


# Subdomain robots.txt handling


def test_each_subdomain_robots_and_crawl_delay_respected(tmp_path, rules):
    site = FakeHosts(
        {
            "cast.desu.edu/robots.txt": httpx.Response(
                200, text="User-agent: *\nCrawl-delay: 30\nDisallow: /departments/private\n"
            ),
            "cast.desu.edu/sitemap.xml": sitemap_xml(
                CAST + "/departments/biology", CAST + "/departments/private"
            ),
            "cast.desu.edu/departments/biology": page("Biology"),
            "cast.desu.edu/departments/private": page("Private"),
            "www.desu.edu/robots.txt": httpx.Response(503),
        }
    )
    crawler, clock = make_crawler(tmp_path, site, delay=1.0)
    report = discover(crawler, rules, set())

    # www's robots.txt is unavailable, so nothing else is requested from it
    assert site.fetched("www.desu.edu") == ["/robots.txt"] * 3  # retried twice
    assert "host skipped" in report.hosts["www.desu.edu"][0]
    # cast's own robots.txt applies: the disallowed page is never fetched
    assert "/departments/private" not in site.fetched("cast.desu.edu")
    assert report.failed == [
        f"{CAST}/departments/private: disallowed by robots.txt: {CAST}/departments/private"
    ]
    assert [c.url for c in report.candidates] == [CAST + "/departments/biology"]
    assert report.hosts["cast.desu.edu"][0] == "robots.txt read; 30s between requests"
    # cast's Crawl-delay (30s), not --delay (1s), spaces its requests
    assert 30 in [round(s) for s in clock.sleeps]
    assert all(round(s) in (1, 30) for s in clock.sleeps)


def test_no_fetch_skips_page_requests(tmp_path, rules):
    site = FakeHosts({"www.desu.edu/sitemap.xml": sitemap_xml(WWW + "/admissions/apply-now")})
    crawler, _ = make_crawler(tmp_path, site)
    report = discover(crawler, rules, set(), fetch_pages=False)
    assert [c.url for c in report.candidates] == [WWW + "/admissions/apply-now"]
    assert "/admissions/apply-now" not in site.fetched("www.desu.edu")
