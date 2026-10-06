import json
from datetime import datetime, timedelta

import httpx
import pytest

from ingestion import chunk, extract
from ingestion.crawler import _file_stem
from ingestion.index import LocalIndex, SyncResult
from ingestion.pipeline import DUE_SLACK, Summary, is_due, run
from ingestion.sources import Source, load_check_intervals, load_delete_after
from ingestion.tests.test_crawler import SAMPLE_PAGE, SITE, FakeSite, html, make_crawler

ETAG = '"1790963877-1"'
LAST_MODIFIED = "Fri, 02 Oct 2026 17:57:57 GMT"
INTERVALS = {"fast": timedelta(0), "medium": timedelta(days=1), "slow": timedelta(days=7)}
DELETE_AFTER = 3

# Same page, but only the site chrome differs: the extracted text is identical.
CHROME_ONLY_EDIT = SAMPLE_PAGE.replace(b"<footer>Delaware", b"<footer>Dover, Delaware")
TEXT_EDIT = SAMPLE_PAGE.replace(b"Sample fixture", b"Updated fixture")  # modified_time not bumped
DATE_EDIT = SAMPLE_PAGE.replace(b"2026-08-14T10:22:31", b"2026-09-01T09:00:00")


def source(path, change_frequency="medium"):
    return Source(url=SITE + path, topic="housing", change_frequency=change_frequency, notes="t")


def with_validators(body=SAMPLE_PAGE, etag=ETAG, last_modified=LAST_MODIFIED):
    """Answers like desu.edu's Drupal page cache: 304 only when both validators match."""

    def respond(request):
        if (
            request.headers.get("if-none-match") == etag
            and request.headers.get("if-modified-since") == last_modified
        ):
            return httpx.Response(304, headers={"etag": etag})
        headers = {"content-type": "text/html; charset=utf-8", "etag": etag}
        return httpx.Response(
            200, content=body, headers={**headers, "last-modified": last_modified}
        )

    return respond


class Pipeline:
    """Runs the pipeline on a fake site with state kept in tmp_path, like repeated cron runs."""

    def __init__(self, tmp_path):
        self.raw = tmp_path / "raw"
        self.extracted = tmp_path / "extracted"
        self.chunks = tmp_path / "chunks"
        self.index = LocalIndex(tmp_path / "index" / "chunks.json")

    def run(self, site, sources, now=None, force=False, registered=None):
        # A fresh crawler every run: nothing carries over except what is on disk.
        crawler, self.clock = make_crawler(self.raw, site, delay=0.0)
        return run(
            sources,
            crawler,
            self.extracted,
            self.chunks,
            index=self.index,
            registered=registered,
            intervals=INTERVALS,
            delete_after=DELETE_AFTER,
            force=force,
            now=now,
        )

    def page_files(self, path):
        """Every file the pipeline keeps for the page at path."""
        stem = _file_stem(SITE + path)
        return sorted(
            p.relative_to(self.raw.parent).as_posix()
            for d in (self.raw, self.extracted, self.chunks)
            for p in d.glob(f"{stem}.*")
        )

    def indexed_urls(self):
        return {c.source_url for c in self.index.chunks()}

    def raw_meta(self, path):
        [meta] = [
            json.loads(p.read_text())
            for p in self.raw.glob("*.json")
            if p.name != "manifest.json" and json.loads(p.read_text())["url"] == SITE + path
        ]
        return meta

    def chunk_file(self):
        [path] = self.chunks.glob("*.json")
        return path

    def mark_chunks(self):
        """Overwrite the chunk file with a marker, to tell whether a run rewrites it."""
        self.chunk_file().write_text('{"marker": true, "chunks": []}')

    def chunks_rewritten(self):
        return "marker" not in json.loads(self.chunk_file().read_text())

    def last_checked(self, path):
        return datetime.fromisoformat(self.raw_meta(path)["checked_at"])


@pytest.fixture
def pipeline(tmp_path):
    return Pipeline(tmp_path)


def test_first_run_processes_new_pages_and_saves_state(pipeline):
    site = FakeSite({"/housing": with_validators()})

    summary = pipeline.run(site, [source("/housing")])

    assert summary.new == [SITE + "/housing"]
    assert summary.checked == 1
    meta = pipeline.raw_meta("/housing")
    assert (meta["etag"], meta["last_modified"]) == (ETAG, LAST_MODIFIED)
    assert meta["checked_at"] == meta["fetched_at"]
    [extracted] = pipeline.extracted.glob("*.json")
    page = json.loads(extracted.read_text())
    assert page["modified_time"] == "2026-08-14T10:22:31-04:00"
    assert len(page["text_hash"]) == 64
    assert json.loads(pipeline.chunk_file().read_text())["chunks"]


def test_304_skips_extract_and_chunk_and_records_the_check(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    first = pipeline.raw_meta("/housing")
    pipeline.mark_chunks()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.unchanged == [SITE + "/housing"]
    request = site.requests[-1]
    assert request.headers["if-none-match"] == ETAG
    assert request.headers["if-modified-since"] == LAST_MODIFIED
    assert not pipeline.chunks_rewritten()
    meta = pipeline.raw_meta("/housing")
    assert meta["status"] == 304
    assert meta["fetched_at"] == first["fetched_at"]  # the saved body is still the first one
    assert meta["checked_at"] >= first["checked_at"]
    assert meta["html_file"] == first["html_file"]


def test_crawl_delay_applies_to_requests_that_return_304(pipeline):
    site = FakeSite({"/a": with_validators(), "/b": with_validators()})
    sources = [source("/a"), source("/b")]
    pipeline.run(site, sources)

    pipeline.run(site, sources, force=True)

    # robots.txt, /a (304), /b (304): each request after the first waits out the 10s Crawl-delay.
    assert pipeline.clock.sleeps == [10.0, 10.0]
    assert [r.headers.get("if-none-match") for r in site.requests[-2:]] == [ETAG, ETAG]


def test_without_validators_an_identical_text_hash_skips_rechunking(pipeline):
    pages = iter([SAMPLE_PAGE, CHROME_ONLY_EDIT])
    site = FakeSite({"/housing": lambda request: html(next(pages))})
    pipeline.run(site, [source("/housing")])
    pipeline.mark_chunks()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert "if-none-match" not in site.requests[-1].headers
    assert summary.unchanged == [SITE + "/housing"]
    assert not pipeline.chunks_rewritten()


@pytest.mark.parametrize(
    "edit",
    [TEXT_EDIT, DATE_EDIT],
    ids=["text changed, same modified_time", "new modified_time, same text"],
)
def test_changed_text_or_modified_time_is_rechunked(pipeline, edit):
    pages = iter([SAMPLE_PAGE, edit])
    site = FakeSite({"/housing": lambda request: html(next(pages))})
    pipeline.run(site, [source("/housing")])
    pipeline.mark_chunks()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.changed == [SITE + "/housing"]
    assert pipeline.chunks_rewritten()


def test_changed_page_behind_validators_is_rechunked(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    site.routes["/housing"] = with_validators(TEXT_EDIT, etag='"1790999999-1"')

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.changed == [SITE + "/housing"]
    assert pipeline.raw_meta("/housing")["etag"] == '"1790999999-1"'
    assert "Updated fixture" in pipeline.chunk_file().read_text()


@pytest.mark.parametrize(
    ("elapsed", "checked"),
    [
        (timedelta(hours=2), ["fast"]),
        (timedelta(hours=23, minutes=30), ["fast", "medium"]),  # within DUE_SLACK of a day
        (timedelta(days=2), ["fast", "medium"]),
        (timedelta(days=7), ["fast", "medium", "slow"]),
    ],
)
def test_only_due_pages_are_checked(pipeline, elapsed, checked):
    site = FakeSite({f"/{f}": with_validators() for f in INTERVALS})
    sources = [source(f"/{f}", f) for f in INTERVALS]
    pipeline.run(site, sources)
    site.requests.clear()
    now = pipeline.last_checked("/slow") + elapsed

    summary = pipeline.run(site, sources, now=now)

    assert site.paths == ["/robots.txt"] + [f"/{f}" for f in checked]
    assert summary.unchanged == [SITE + f"/{f}" for f in checked]
    assert summary.not_due == [SITE + f"/{f}" for f in INTERVALS if f not in checked]


def test_nothing_due_makes_no_requests(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing", "slow")])
    site.requests.clear()

    summary = pipeline.run(site, [source("/housing", "slow")])

    assert site.requests == []
    assert (summary.checked, summary.not_due) == (0, [SITE + "/housing"])


def test_force_checks_pages_that_are_not_due(pipeline):
    site = FakeSite({"/medium": with_validators(), "/slow": with_validators()})
    sources = [source("/medium", "medium"), source("/slow", "slow")]
    pipeline.run(site, sources)
    site.requests.clear()

    summary = pipeline.run(site, sources, force=True)

    assert site.paths == ["/robots.txt", "/medium", "/slow"]
    assert (summary.checked, summary.not_due) == (2, [])


def test_failed_page_keeps_its_previous_state_and_stays_due(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    before = pipeline.raw_meta("/housing")
    site.routes["/housing"] = httpx.Response(404)

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.failed == [SITE + "/housing: HTTP 404 on 1 check(s) in a row (deleted after 3)"]
    assert pipeline.raw_meta("/housing") == {**before, "missing_checks": 1}
    assert json.loads(pipeline.chunk_file().read_text())["chunks"]
    assert pipeline.indexed_urls() == {SITE + "/housing"}
    assert summary.deleted == []


def test_new_source_alongside_unchanged_ones(pipeline):
    site = FakeSite({"/a": with_validators(), "/b": with_validators()})
    pipeline.run(site, [source("/a")])

    summary = pipeline.run(site, [source("/a"), source("/b")], force=True)

    assert (summary.unchanged, summary.new) == ([SITE + "/a"], [SITE + "/b"])


def test_304_for_a_page_never_chunked_is_processed_from_the_saved_copy(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    pipeline.chunk_file().unlink()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert site.requests[-1].headers["if-none-match"] == ETAG
    assert summary.unchanged == [SITE + "/housing"]  # the page didn't change; its chunks are back
    assert json.loads(pipeline.chunk_file().read_text())["chunks"]


@pytest.mark.parametrize(
    ("meta", "due"),
    [
        (None, True),
        ({}, True),
        ({"checked_at": "not a time"}, True),
        ({"fetched_at": "2026-10-01T00:00:00+00:00"}, True),  # older metadata has no checked_at
        (
            {"checked_at": "2026-10-01T12:00:00+00:00", "fetched_at": "2026-09-01T00:00:00+00:00"},
            False,
        ),
    ],
)
def test_is_due(meta, due):
    now = datetime.fromisoformat("2026-10-02T00:00:00+00:00")
    assert is_due(meta, "medium", INTERVALS, now) is due


def test_due_slack_is_shorter_than_every_configured_interval():
    intervals = load_check_intervals()
    assert intervals["fast"] == timedelta(0)
    assert all(DUE_SLACK < interval for interval in intervals.values() if interval)


def test_summary_reports_every_count():
    summary = Summary(
        unchanged=["a", "b"],
        changed=["c"],
        new=["d"],
        reprocessed=["g"],
        failed=["e: HTTP 404 on 1 check(s) in a row (deleted after 3)"],
        not_due=["f"],
        deleted=["h: no longer in sources.yaml"],
        redirects=["i -> j"],
        checked=5,
        index=SyncResult(added=["1", "2"], updated=["3"], removed=["4", "5", "6"]),
    )
    assert str(summary).splitlines() == [
        "Checked 5: 2 unchanged, 1 changed, 1 new, 1 failed. Skipped 1 not due. "
        "Reprocessed 1 for a new extractor or chunker version.",
        "Chunks: 2 added, 1 updated, 3 removed.",
        "Pages deleted: 1. Pages failing: 1. Redirects to review: 1.",
        "  deleted: h: no longer in sources.yaml",
        "  failing: e: HTTP 404 on 1 check(s) in a row (deleted after 3)",
        "  redirect: i -> j (update sources.yaml)",
    ]


def test_delete_after_setting_is_in_the_registry():
    assert load_delete_after() == 3


# Deleted and moved pages


def test_page_removed_from_sources_is_deleted_with_its_chunks(pipeline):
    site = FakeSite({"/a": with_validators(), "/b": with_validators()})
    pipeline.run(site, [source("/a"), source("/b")])
    assert pipeline.page_files("/b") and pipeline.indexed_urls() == {SITE + "/a", SITE + "/b"}

    summary = pipeline.run(site, [source("/a")])

    assert summary.deleted == [SITE + "/b: no longer in sources.yaml"]
    assert pipeline.page_files("/b") == []
    assert pipeline.page_files("/a")
    assert pipeline.indexed_urls() == {SITE + "/a"}
    assert summary.index.removed and not summary.index.added


def test_checking_part_of_the_registry_keeps_the_other_pages(pipeline):
    site = FakeSite({"/a": with_validators(), "/b": with_validators()})
    pipeline.run(site, [source("/a"), source("/b")])

    summary = pipeline.run(site, [source("/a")], registered=[SITE + "/a", SITE + "/b"])

    assert summary.deleted == []
    assert pipeline.page_files("/b")
    assert pipeline.indexed_urls() == {SITE + "/a", SITE + "/b"}


def test_404_deletes_the_page_only_after_n_checks_in_a_row(pipeline):
    site = FakeSite({"/a": with_validators(), "/gone": with_validators()})
    sources = [source("/a"), source("/gone")]
    pipeline.run(site, sources)
    files = pipeline.page_files("/gone")
    site.routes["/gone"] = httpx.Response(404)

    for check in (1, 2):
        summary = pipeline.run(site, sources, force=True)
        assert summary.failed == [
            f"{SITE}/gone: HTTP 404 on {check} check(s) in a row (deleted after 3)"
        ]
        assert summary.deleted == []
        assert pipeline.page_files("/gone") == files
        assert SITE + "/gone" in pipeline.indexed_urls()

    summary = pipeline.run(site, sources, force=True)

    assert summary.deleted == [f"{SITE}/gone: HTTP 404 on 3 checks in a row"]
    assert summary.failed == [f"{SITE}/gone: HTTP 404 on 3 checks in a row (deleted)"]
    assert pipeline.page_files("/gone") == [f"raw/{_file_stem(SITE + '/gone')}.json"]
    assert pipeline.indexed_urls() == {SITE + "/a"}
    assert summary.index.removed

    # Still listed in sources.yaml: reported as failing every run, but deleted only once.
    summary = pipeline.run(site, sources, force=True)
    assert summary.deleted == []
    assert summary.failed == [f"{SITE}/gone: HTTP 404 on 4 checks in a row (deleted)"]


def test_410_counts_like_404_and_other_errors_do_not_count(pipeline):
    site = FakeSite({"/gone": with_validators()})
    pipeline.run(site, [source("/gone")])
    site.routes["/gone"] = httpx.Response(410)
    pipeline.run(site, [source("/gone")], force=True)
    site.routes["/gone"] = httpx.Response(503)

    summary = pipeline.run(site, [source("/gone")], force=True)

    assert summary.failed == [f"{SITE}/gone: HTTP 503"]
    assert pipeline.raw_meta("/gone")["missing_checks"] == 1


def test_a_200_resets_the_failure_count(pipeline):
    site = FakeSite({"/flaky": with_validators()})
    pipeline.run(site, [source("/flaky")])
    site.routes["/flaky"] = httpx.Response(404)
    pipeline.run(site, [source("/flaky")], force=True)
    pipeline.run(site, [source("/flaky")], force=True)
    assert pipeline.raw_meta("/flaky")["missing_checks"] == 2

    site.routes["/flaky"] = with_validators()
    summary = pipeline.run(site, [source("/flaky")], force=True)
    assert summary.failed == [] and summary.unchanged == [SITE + "/flaky"]
    assert pipeline.raw_meta("/flaky")["missing_checks"] == 0

    site.routes["/flaky"] = httpx.Response(404)
    summary = pipeline.run(site, [source("/flaky")], force=True)
    assert summary.failed == [f"{SITE}/flaky: HTTP 404 on 1 check(s) in a row (deleted after 3)"]
    assert pipeline.page_files("/flaky")


def test_deleted_page_that_comes_back_is_fetched_in_full(pipeline):
    site = FakeSite({"/back": with_validators()})
    pipeline.run(site, [source("/back")])
    site.routes["/back"] = httpx.Response(404)
    for _ in range(DELETE_AFTER):
        pipeline.run(site, [source("/back")], force=True)
    site.routes["/back"] = with_validators()

    summary = pipeline.run(site, [source("/back")], force=True)

    assert "if-none-match" not in site.requests[-1].headers
    assert summary.new == [SITE + "/back"]
    assert pipeline.indexed_urls() == {SITE + "/back"}


def test_permanent_redirect_is_reported_and_sources_are_not_rewritten(pipeline):
    site = FakeSite(
        {
            "/old": httpx.Response(301, headers={"location": "/new"}),
            "/new": with_validators(),
            "/temp": httpx.Response(302, headers={"location": "/new"}),
        }
    )

    summary = pipeline.run(site, [source("/old"), source("/temp")])

    assert summary.redirects == [f"{SITE}/old -> {SITE}/new"]  # a 302 is not a move
    assert summary.new == [SITE + "/old", SITE + "/temp"]  # still processed meanwhile
    assert "redirect: " + f"{SITE}/old -> {SITE}/new (update sources.yaml)" in str(summary)


def test_redirect_off_desu_edu_is_not_reported_as_a_move(pipeline):
    site = FakeSite({"/out": httpx.Response(301, headers={"location": "https://example.com/"})})
    summary = pipeline.run(site, [source("/out")])
    assert summary.redirects == []
    assert summary.failed == [f"{SITE}/out: off-domain url: https://example.com/"]


# Index sync


def test_index_follows_the_chunk_files_and_a_quiet_run_writes_nothing(pipeline):
    site = FakeSite({"/housing": with_validators()})
    first = pipeline.run(site, [source("/housing")])
    ids = {json.loads(pipeline.chunk_file().read_text())["chunks"][0]["chunk_id"]}
    assert len(first.index.added) == len(pipeline.index.chunks()) > 0
    assert ids <= set(first.index.added)
    index_mtime = pipeline.index.path.stat().st_mtime_ns
    chunks_mtime = pipeline.chunk_file().stat().st_mtime_ns

    second = pipeline.run(site, [source("/housing")], force=True)

    assert second.index == SyncResult()
    assert pipeline.index.path.stat().st_mtime_ns == index_mtime
    assert pipeline.chunk_file().stat().st_mtime_ns == chunks_mtime


def test_changed_page_replaces_its_chunks_in_the_index(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    before = {c.chunk_id for c in pipeline.index.chunks()}
    site.routes["/housing"] = with_validators(TEXT_EDIT, etag='"1790999999-1"')

    summary = pipeline.run(site, [source("/housing")], force=True)

    after = {c.chunk_id for c in pipeline.index.chunks()}
    assert set(summary.index.added) == after - before
    assert set(summary.index.removed) == before - after
    assert summary.index.added and summary.index.removed  # no orphans left behind
    assert any("Updated fixture" in c.text for c in pipeline.index.chunks())


def test_new_modified_time_updates_chunks_in_place(pipeline):
    pages = iter([SAMPLE_PAGE, DATE_EDIT])
    site = FakeSite({"/housing": lambda request: html(next(pages))})
    pipeline.run(site, [source("/housing")])

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.index.updated and not (summary.index.added or summary.index.removed)
    assert {c.modified_time for c in pipeline.index.chunks()} == {"2026-09-01T09:00:00-04:00"}


# Processing versions


@pytest.mark.parametrize(
    ("module", "name"),
    [(extract, "EXTRACTOR_VERSION"), (chunk, "CHUNKER_VERSION")],
)
def test_version_bump_reprocesses_pages_even_when_not_due(pipeline, monkeypatch, module, name):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing", "slow")])
    assert pipeline.raw_meta("/housing")[name.lower()] == getattr(module, name)
    pipeline.mark_chunks()
    site.requests.clear()

    monkeypatch.setattr(module, name, getattr(module, name) + 1)
    summary = pipeline.run(site, [source("/housing", "slow")])

    assert site.requests == []  # not due: reprocessed from the saved copy
    assert summary.reprocessed == [SITE + "/housing"]
    assert summary.not_due == []
    assert pipeline.chunks_rewritten()
    assert pipeline.raw_meta("/housing")[name.lower()] == getattr(module, name)

    pipeline.mark_chunks()
    summary = pipeline.run(site, [source("/housing", "slow")])
    assert summary.not_due == [SITE + "/housing"] and not pipeline.chunks_rewritten()


def test_version_bump_reprocesses_a_page_that_answers_304(pipeline, monkeypatch):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    pipeline.mark_chunks()
    monkeypatch.setattr(chunk, "CHUNKER_VERSION", chunk.CHUNKER_VERSION + 1)

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert pipeline.raw_meta("/housing")["status"] == 304
    assert summary.reprocessed == [SITE + "/housing"]
    assert pipeline.chunks_rewritten()


def test_cli_exits_non_zero_when_a_page_fails(tmp_path, monkeypatch, capsys):
    from ingestion import pipeline as pipeline_module

    registry = tmp_path / "sources.yaml"
    registry.write_text(
        "check_interval_hours: {fast: 0, medium: 24, slow: 168}\n"
        "delete_after_missing_checks: 3\n"
        "sources:\n"
        f"  - {{url: '{SITE}/gone', topic: t, change_frequency: fast, notes: n}}\n"
    )
    site = FakeSite({})
    monkeypatch.setattr(
        pipeline_module, "Crawler", lambda raw, delay: make_crawler(raw, site, delay=0.0)[0]
    )
    args = ["--sources", str(registry), "--index", str(tmp_path / "index.json")]
    args += [f"--{d}={tmp_path / d}" for d in ("raw", "extracted", "chunks")]

    with pytest.raises(SystemExit) as exit_info:
        pipeline_module.main(args)

    assert exit_info.value.code == 1
    assert "Pages failing: 1." in capsys.readouterr().out
