import json
from dataclasses import asdict
from pathlib import Path

import pytest

from ingestion.chunk import MAX_WORDS, chunk_all, chunk_page
from ingestion.extract import _word_count, extract

FIXTURES = Path(__file__).with_name("fixtures")
SITE = "https://www.desu.edu"
MATRIX_URL = SITE + "/student-life/housing-dining/apply-housing/housing-comparison-matrix"
LANDING_URL = SITE + "/student-life/housing-dining"
SAT_URL = SITE + "/admissions/undergraduate-students/how-download-your-satact-score-report"


def extracted(name, url):
    """The extractor's output for a fixture, as chunk_page reads it from data/extracted/."""
    html = (FIXTURES / name).read_text()
    return {**asdict(extract(html, url)), "url": url, "final_url": url, "topic": "housing"}


@pytest.fixture(scope="module")
def matrix():
    return extracted("housing_comparison_matrix.html", MATRIX_URL)


@pytest.fixture(scope="module")
def landing():
    return extracted("housing_dining_landing.html", LANDING_URL)


@pytest.fixture(scope="module")
def sat():
    return extracted("sat_act_score_report.html", SAT_URL)


def test_chunks_follow_headings_and_keep_their_path(landing):
    chunks = chunk_page(landing)
    paths = [c.heading_path for c in chunks]

    assert paths[0] == "Housing & Dining > More than a Place to Sleep"
    assert "Housing & Dining > Freshman Residence Halls > Meta V. Jenkins Hall" in paths
    jenkins = chunks[
        paths.index("Housing & Dining > Freshman Residence Halls > Meta V. Jenkins Hall")
    ]
    assert "Housing 245 coed students" in jenkins.text
    assert "Medgar W. Evers" not in jenkins.text
    for chunk in chunks:
        assert not chunk.text.startswith("#")  # headings live in heading_path, not the text


def test_every_chunk_carries_source_metadata(landing):
    chunks = chunk_page(landing)

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.source_url == LANDING_URL
        assert chunk.title == "Housing & Dining"
        assert chunk.modified_time == "2026-08-12T12:59:13-04:00"
        assert chunk.topic == "housing"
        assert chunk.low_text is False
        assert chunk.chunk_id and chunk.text
        assert chunk.word_count <= MAX_WORDS


def test_small_table_stays_in_one_chunk(matrix):
    (chunk,) = chunk_page(matrix)

    assert chunk.heading_path == "Housing Comparison Matrix"
    assert "| Building Capacity | 620 | 265 |" in chunk.text
    assert "| Air Conditioning | Yes |" in chunk.text


def test_split_table_repeats_header_row_in_every_piece(matrix):
    table = [line for line in matrix["text"].splitlines() if line.startswith("|")]
    header, separator, rows = table[0], table[1], table[2:]
    assert header.startswith("| Facility Name | [Tubman-Laws Hall]")

    chunks = chunk_page(matrix, max_words=80)

    pieces = [c.text.splitlines() for c in chunks if "|" in c.text]
    assert len(pieces) >= 3
    seen_rows = []
    for lines in pieces:
        table_lines = [line for line in lines if line.startswith("|")]
        assert table_lines[:2] == [header, separator]
        for row in table_lines[2:]:
            assert row in rows  # never cut in half
            seen_rows.append(row)
    assert seen_rows == rows  # every row appears once, in order


def test_long_section_is_split_by_size_with_overlap():
    sentences = [f"Sentence {i} is about residence hall number {i}." for i in range(60)]
    page = {"url": SITE + "/long", "title": "Long", "text": "# Long\n\n" + " ".join(sentences)}

    chunks = chunk_page(page, max_words=100, overlap=20)

    assert len(chunks) > 1
    assert all(c.word_count <= 100 for c in chunks)
    assert all(c.heading_path == "Long" for c in chunks)
    for before, after in zip(chunks, chunks[1:], strict=False):
        first_sentence = after.text.split(". ", 1)[0] + "."
        repeated = before.text[before.text.index(first_sentence) :]  # the end of the last piece
        assert after.text.startswith(repeated)
        assert 0 < _word_count(repeated) <= 20
    assert chunks[-1].text.endswith("Sentence 59 is about residence hall number 59.")


def test_low_text_flag_carries_over_to_chunks(sat):
    assert sat["low_text"]

    chunks = chunk_page(sat)

    assert chunks
    assert all(c.low_text for c in chunks)
    assert chunks[0].heading_path.startswith("How to download your SAT/ACT score report > ")


def test_chunk_ids_are_stable_across_runs(tmp_path, matrix, landing, sat):
    first, second = tmp_path / "first", tmp_path / "second"
    for run in (first, second):
        src = run / "extracted"
        src.mkdir(parents=True)
        # Extract afresh each run so nothing is shared between them.
        for stem, name, url in [
            ("matrix", "housing_comparison_matrix.html", MATRIX_URL),
            ("landing", "housing_dining_landing.html", LANDING_URL),
            ("sat", "sat_act_score_report.html", SAT_URL),
        ]:
            (src / f"{stem}.json").write_text(json.dumps(extracted(name, url)))
        chunk_all(src, run / "chunks")

    for name in ("matrix.json", "landing.json", "sat.json"):
        a = json.loads((first / "chunks" / name).read_text())
        b = json.loads((second / "chunks" / name).read_text())
        assert [c["chunk_id"] for c in a["chunks"]] == [c["chunk_id"] for c in b["chunks"]]
        assert a == b

    ids = [c.chunk_id for page in (matrix, landing, sat) for c in chunk_page(page)]
    assert len(ids) == len(set(ids))


def test_editing_one_section_only_changes_that_chunks_id(landing):
    before = chunk_page(landing)
    edited = {**landing, "text": landing["text"].replace("245 coed", "250 coed")}

    after = chunk_page(edited)

    changed = [
        a.heading_path for a, b in zip(before, after, strict=True) if a.chunk_id != b.chunk_id
    ]
    assert changed == ["Housing & Dining > Freshman Residence Halls > Meta V. Jenkins Hall"]


def test_chunk_all_writes_one_file_per_extracted_page(tmp_path, matrix, sat):
    src, out = tmp_path / "extracted", tmp_path / "chunks"
    src.mkdir()
    (src / "matrix-1.json").write_text(json.dumps(matrix))
    (src / "sat-2.json").write_text(json.dumps(sat))

    result = chunk_all(src, out)

    assert sorted(p.name for p in out.iterdir()) == ["matrix-1.json", "sat-2.json"]
    saved = json.loads((out / "matrix-1.json").read_text())
    assert saved["url"] == MATRIX_URL
    assert saved["chunks"] == [asdict(c) for c in result["matrix-1.json"]]
    assert _word_count(saved["chunks"][0]["text"]) == saved["chunks"][0]["word_count"]
