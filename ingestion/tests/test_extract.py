import json
from pathlib import Path

import pytest

from ingestion.extract import MIN_WORDS, extract, extract_all

FIXTURES = Path(__file__).with_name("fixtures")
SITE = "https://www.desu.edu"
MATRIX_URL = SITE + "/student-life/housing-dining/apply-housing/housing-comparison-matrix"
LANDING_URL = SITE + "/student-life/housing-dining"
SAT_URL = SITE + "/admissions/undergraduate-students/how-download-your-satact-score-report"

# Trimmed copies of real desu.edu pages, one per template we have seen.
PAGES = {
    "housing_comparison_matrix.html": MATRIX_URL,  # content page with a table
    "housing_dining_landing.html": LANDING_URL,  # landing page, no <article>
    "sat_act_score_report.html": SAT_URL,  # mostly a video and images
}

# Site chrome: mega-menu, mobile nav, header, footer, and repeated in-content boxes.
CHROME_TEXT = [
    "Scholarship Ball",  # mega-menu / mobile nav (Giving)
    "Faculty Senate",  # mobile nav (About)
    "Find your major or program",  # header mega-menu
    "Daytime: 302.857.6060",  # footer
    "Middle States Commission",  # footer
    "Start your journey here",  # admissions sub-footer on every page
    "Mobile Navigation",
]


def load(name):
    return (FIXTURES / name).read_text()


@pytest.fixture(scope="module")
def matrix():
    return extract(load("housing_comparison_matrix.html"), MATRIX_URL)


@pytest.fixture(scope="module")
def landing():
    return extract(load("housing_dining_landing.html"), LANDING_URL)


@pytest.fixture(scope="module")
def sat():
    return extract(load("sat_act_score_report.html"), SAT_URL)


@pytest.mark.parametrize(("name", "url"), PAGES.items())
def test_site_chrome_is_stripped(name, url):
    html = load(name)
    # Guard against a fixture trimmed so far that the assertions below prove nothing.
    assert "Scholarship Ball" in html and "Faculty Senate" in html

    result = extract(html, url)

    assert result.container == "main [role=main]"
    for text in CHROME_TEXT:
        assert text not in result.text


def test_content_page_keeps_body_and_metadata(matrix):
    assert matrix.text.startswith("# Housing Comparison Matrix\n\n")
    assert "features and amenities available at each of our traditional facilities" in matrix.text
    assert matrix.title == "Housing Comparison Matrix"
    assert matrix.canonical_url == MATRIX_URL
    assert matrix.modified_time == "2022-03-04T11:39:37-05:00"
    assert not matrix.low_text


def test_breadcrumb_is_metadata_not_text(matrix):
    assert [c["title"] for c in matrix.breadcrumb] == [
        "Home",
        "Student Life",
        "Housing & Dining",
        "New and Returning Students Housing Application",
        "Housing Comparison Matrix",
    ]
    assert matrix.breadcrumb[1]["url"] == SITE + "/student-life"
    assert matrix.breadcrumb[-1]["url"] is None
    assert "Home >" not in matrix.text


def test_tables_survive_as_markdown(matrix):
    lines = matrix.text.splitlines()
    header = next(line for line in lines if line.startswith("| Facility Name |"))
    assert f"[Tubman-Laws Hall]({LANDING_URL}/residential-halls/tubman-laws-hall)" in header
    assert lines[lines.index(header) + 1] == "|" + " --- |" * 10
    assert "| Building Capacity | 620 | 265 | 245 | 244 | 86 | 135 | 193 | 173 | 163 |" in lines
    assert "| Air Conditioning | Yes |" in matrix.text


def test_landing_page_keeps_sections_lists_and_links(landing):
    assert landing.title == "Housing & Dining"
    assert landing.modified_time == "2026-08-12T12:59:13-04:00"
    assert landing.breadcrumb == []
    assert "## Freshman Residence Halls" in landing.text
    assert "### Meta V. Jenkins Hall" in landing.text
    assert "Housing 245 coed students" in landing.text
    contract = SITE + "/sites/flagship/files/document/21/housing-dining-contract.pdf"
    assert f"- [READ ME FIRST]({contract})" in landing.text
    starrez = "https://desu.starrezhousing.com/StarRezPortalX"
    assert f"[Pay Deposit/Apply for Housing]({starrez})" in landing.text


def test_landing_page_drops_quick_link_bar_and_inline_css(landing):
    assert "CARES Act" not in landing.text
    assert "Title IX" not in landing.text
    assert "color:#00549A" not in landing.text


def test_section_menu_is_stripped(sat):
    # Sidebar nav lists the section's sibling pages; only the page itself should remain.
    assert "Accepted Students" not in sat.text


def test_page_with_little_text_is_flagged(sat):
    assert sat.low_text
    assert sat.word_count < MIN_WORDS
    assert sat.title == "How to download your SAT/ACT score report"
    assert "## Download your SAT Score" in sat.text
    assert "[Embedded video](https://www.youtube.com/embed/YqWr29cdk9s" in sat.text


def test_falls_back_to_body_without_chrome_when_content_region_is_missing():
    html = """<html><head><title>Plain Page</title></head><body>
      <header><nav><a href="/giving">Scholarship Ball</a></nav></header>
      <div id="sliding-popup">We use cookies</div>
      <div class="content"><h1>Plain Page</h1><p>The real content.</p></div>
      <footer>Faculty Senate</footer>
    </body></html>"""

    result = extract(html, SITE + "/plain")

    assert result.container == "fallback"
    assert result.text == "# Plain Page\n\nThe real content."
    assert result.title == "Plain Page"
    assert result.canonical_url is None
    assert result.modified_time is None


def test_renders_lists_links_and_line_breaks():
    html = """<main><div role="main">
      <ol><li>First <a href="/apply">apply</a></li><li>Then<ul><li>nested</li></ul></li></ol>
      <p>Line one<br>line two <a href="#top">top</a> <a href="mailto:x@desu.edu">email</a></p>
      <p>Write to <span class="spamspan"><span class="u">housing</span> [at]
         <span class="d">desu.edu</span></span><!-- a comment --></p>
    </div></main>"""

    result = extract(html, SITE + "/a/b")

    assert result.text == (
        f"1. First [apply]({SITE}/apply)\n2. Then\n   - nested\n\n"
        "Line one\nline two top [email](mailto:x@desu.edu)\n\n"
        "Write to housing@desu.edu"
    )


def test_modified_time_falls_back_to_og_updated_time():
    html = """<head><meta property="og:updated_time" content="2025-01-02T03:04:05-05:00">
      <link rel="canonical" href="/canonical"></head><main><p>x</p></main>"""

    result = extract(html, SITE + "/page?utm=1")

    assert result.modified_time == "2025-01-02T03:04:05-05:00"
    assert result.canonical_url == SITE + "/canonical"


def test_extract_all_writes_one_file_per_crawled_page(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "extracted"
    raw.mkdir()
    for stem, (name, url) in zip(["matrix-1", "sat-2"], [*PAGES.items()][::2], strict=True):
        (raw / f"{stem}.html").write_text(load(name))
        meta = {
            "url": url,
            "final_url": url,
            "status": 200,
            "fetched_at": "2026-09-30T21:05:18+00:00",
            "topic": "housing",
            "encoding": "utf-8",
            "html_file": f"{stem}.html",
        }
        (raw / f"{stem}.json").write_text(json.dumps(meta))
    (raw / "manifest.json").write_text(json.dumps({"pages": []}))

    pages = extract_all(raw, out)

    assert sorted(p.name for p in out.iterdir()) == ["matrix-1.json", "sat-2.json"]
    assert [p.raw_file for p in pages] == ["matrix-1.html", "sat-2.html"]
    saved = json.loads((out / "matrix-1.json").read_text())
    assert saved["url"] == MATRIX_URL
    assert saved["title"] == "Housing Comparison Matrix"
    assert saved["modified_time"] == "2022-03-04T11:39:37-05:00"
    assert saved["fetched_at"] == "2026-09-30T21:05:18+00:00"
    assert saved["topic"] == "housing"
    assert saved["warnings"] == []
    assert "| Building Capacity | 620 |" in saved["text"]
    sat = json.loads((out / "sat-2.json").read_text())
    assert sat["low_text"] is True
    assert sat["warnings"] and "words of text" in sat["warnings"][0]
