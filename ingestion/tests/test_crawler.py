import json
from pathlib import Path

import httpx
import pytest

from ingestion.crawler import USER_AGENT, Crawler, _file_stem
from ingestion.sources import Source

FIXTURES = Path(__file__).with_name("fixtures")
ROBOTS_TXT = (FIXTURES / "robots.txt").read_text()
SAMPLE_PAGE = (FIXTURES / "sample_page.html").read_bytes()
SITE = "https://www.desu.edu"


def source(path, topic="housing"):
    return Source(url=SITE + path, topic=topic, change_frequency="medium", notes="test")


class FakeClock:
    """Stands in for time.monotonic/time.sleep so delay tests run instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSite:
    """Serves canned responses by path and records every request."""

    def __init__(self, routes):
        self.routes = {"/robots.txt": httpx.Response(200, text=ROBOTS_TXT), **routes}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        route = self.routes.get(request.url.path, httpx.Response(404))
        if callable(route):
            return route(request)
        return route

    @property
    def paths(self):
        return [r.url.path for r in self.requests]


def html(body=SAMPLE_PAGE):
    return httpx.Response(200, content=body, headers={"content-type": "text/html; charset=utf-8"})


def make_crawler(tmp_path, site, delay=0.0, retries=2):
    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(site), headers={"User-Agent": USER_AGENT})
    crawler = Crawler(
        tmp_path, delay=delay, retries=retries, client=client, sleep=clock.sleep, clock=clock
    )
    return crawler, clock


def test_saves_raw_page_with_metadata(tmp_path):
    site = FakeSite({"/housing": html()})
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/housing")])

    assert result.ok
    assert (tmp_path / result.html_file).read_bytes() == SAMPLE_PAGE
    meta = json.loads((tmp_path / result.html_file).with_suffix(".json").read_text())
    assert meta["url"] == SITE + "/housing"
    assert meta["final_url"] == SITE + "/housing"
    assert meta["status"] == 200
    assert meta["fetched_at"].endswith("+00:00")
    assert meta["content_type"] == "text/html; charset=utf-8"
    assert meta["topic"] == "housing"
    assert "error" not in meta


def test_manifest_lists_successes_and_failures(tmp_path):
    site = FakeSite({"/housing": html()})
    crawler, _ = make_crawler(tmp_path, site)

    crawler.crawl([source("/housing"), source("/gone")])

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["user_agent"] == USER_AGENT
    assert [(p["url"], p["status"], p["error"]) for p in manifest["pages"]] == [
        (SITE + "/housing", 200, None),
        (SITE + "/gone", 404, "HTTP 404"),
    ]


def test_sends_identifying_user_agent(tmp_path):
    site = FakeSite({"/housing": html()})
    crawler, _ = make_crawler(tmp_path, site)

    crawler.crawl([source("/housing")])

    assert USER_AGENT.startswith("DSU-Chatbot-Crawler/")
    assert "github.com/MikeTabbi/dsu-chatbot" in USER_AGENT
    assert all(r.headers["user-agent"] == USER_AGENT for r in site.requests)


def test_robots_txt_is_fetched_once_and_disallowed_paths_are_skipped(tmp_path):
    site = FakeSite({"/housing": html(), "/admissions": html()})
    crawler, _ = make_crawler(tmp_path, site)

    results = crawler.crawl([source("/housing"), source("/includes/secret"), source("/admissions")])

    assert [r.ok for r in results] == [True, False, True]
    assert "robots.txt" in results[1].error
    assert site.paths == ["/robots.txt", "/housing", "/admissions"]


def test_honors_robots_crawl_delay_between_requests(tmp_path):
    site = FakeSite({"/a": html(), "/b": html()})
    crawler, clock = make_crawler(tmp_path, site, delay=1.0)

    crawler.crawl([source("/a"), source("/b")])

    # robots.txt, /a, /b: each request after the first waits out the 10s Crawl-delay.
    assert clock.sleeps == [10.0, 10.0]


def test_configured_delay_wins_when_longer_than_crawl_delay(tmp_path):
    site = FakeSite({"/a": html()})
    crawler, clock = make_crawler(tmp_path, site, delay=15.0)

    crawler.crawl([source("/a")])

    assert clock.sleeps == [15.0]


def test_404_is_skipped_and_crawl_continues(tmp_path):
    site = FakeSite({"/housing": html()})
    crawler, _ = make_crawler(tmp_path, site)

    missing, found = crawler.crawl([source("/missing"), source("/housing")])

    assert (missing.ok, missing.status, missing.error) == (False, 404, "HTTP 404")
    assert missing.html_file is None
    assert found.ok
    assert site.paths.count("/missing") == 1  # 4xx is not retried


def test_timeout_is_retried_then_skipped(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    site = FakeSite({"/slow": timeout, "/housing": html()})
    crawler, _ = make_crawler(tmp_path, site, retries=2)

    slow, found = crawler.crawl([source("/slow"), source("/housing")])

    assert not slow.ok
    assert slow.status is None
    assert slow.error.startswith("timeout")
    assert site.paths.count("/slow") == 3
    assert found.ok


def test_server_error_is_retried_until_success(tmp_path):
    responses = iter([httpx.Response(503), html()])
    site = FakeSite({"/flaky": lambda request: next(responses)})
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/flaky")])

    assert result.ok
    assert result.status == 200
    assert site.paths.count("/flaky") == 2


def test_same_site_redirect_is_followed_and_final_url_recorded(tmp_path):
    site = FakeSite(
        {
            "/old": httpx.Response(301, headers={"location": "/new"}),
            "/new": html(),
        }
    )
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/old")])

    assert result.ok
    assert result.url == SITE + "/old"
    assert result.final_url == SITE + "/new"
    assert result.redirects == [SITE + "/new"]
    meta = json.loads((tmp_path / result.html_file).with_suffix(".json").read_text())
    assert meta["final_url"] == SITE + "/new"


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("https://example.com/elsewhere", "off-domain"),
        ("/includes/private", "robots.txt"),
    ],
)
def test_redirect_to_forbidden_target_is_skipped(tmp_path, location, reason):
    site = FakeSite({"/old": httpx.Response(302, headers={"location": location})})
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/old")])

    assert not result.ok
    assert reason in result.error
    assert site.paths == ["/robots.txt", "/old"]


def test_redirect_loop_is_skipped(tmp_path):
    site = FakeSite({"/loop": httpx.Response(302, headers={"location": "/loop"})})
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/loop")])

    assert not result.ok
    assert "redirects" in result.error


def test_unreachable_robots_txt_skips_the_host(tmp_path):
    site = FakeSite({"/robots.txt": httpx.Response(503), "/housing": html()})
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/housing")])

    assert not result.ok
    assert "/housing" not in site.paths


def test_missing_robots_txt_allows_everything(tmp_path):
    site = FakeSite({"/robots.txt": httpx.Response(404), "/includes/page": html()})
    crawler, _ = make_crawler(tmp_path, site)

    [result] = crawler.crawl([source("/includes/page")])

    assert result.ok


def test_file_stem_is_readable_and_unique():
    a = _file_stem(SITE + "/student-life/housing-dining")
    b = _file_stem(SITE + "/student-life/housing-dining?page=2")
    assert a.startswith("student_life_housing_dining-")
    assert a != b
    assert _file_stem(SITE + "/").startswith("index-")
