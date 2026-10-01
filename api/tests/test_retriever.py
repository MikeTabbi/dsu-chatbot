import json
from dataclasses import asdict

import pytest

from api.app.config import Settings
from api.app.retriever import (
    SYNONYMS_PATH,
    LocalKeywordRetriever,
    SynonymsError,
    get_retriever,
    load_synonyms,
    main,
)
from ingestion.chunk import Chunk

SITE = "https://www.desu.edu"


def chunk(heading_path, text, title="Housing & Dining", low_text=False, url="/housing"):
    return Chunk(
        chunk_id=heading_path,
        chunk_index=0,
        source_url=SITE + url,
        title=title,
        heading_path=heading_path,
        modified_time="2025-01-01T00:00:00-05:00",
        topic="housing",
        low_text=low_text,
        word_count=len(text.split()),
        text=text,
    )


CHUNKS = [
    chunk(
        "Housing & Dining > Freshman Residence Halls > Meta V. Jenkins Hall",
        "Traditional double rooms with a community bathroom on each floor.",
    ),
    chunk(
        "Housing & Dining > Upperclassman Residence Halls > Living & Learning Commons",
        "Carpeted suites with a shared kitchen and a study lounge.",
    ),
    chunk(
        "Housing & Dining > Living on Campus > Dining on Campus",
        "Choose the meal plan that best suits your needs and use flex dollars.",
    ),
    chunk(
        "Admissions > Download your SAT Score",
        "Watch the video to download your SAT score report from [College Board](https://cb.org).",
        title="How to download your SAT/ACT score report",
        low_text=True,
        url="/sat",
    ),
    chunk(
        "Admissions > Sending scores",
        "Ask the testing agency to send your score report to the admissions office.",
        title="Test scores",
        url="/scores",
    ),
]


@pytest.fixture(scope="module")
def retriever():
    return LocalKeywordRetriever(CHUNKS)


def paths(results):
    return [r.chunk.heading_path for r in results]


def test_clear_query_ranks_the_right_chunk_first(retriever):
    results = retriever.search("Which halls have a meal plan?", k=3)
    assert results[0].chunk.heading_path.endswith("Dining on Campus")
    assert results[0].score > 0


def test_results_carry_score_and_all_chunk_metadata(retriever):
    [top] = retriever.search("meal plan", k=1)
    assert top.chunk == CHUNKS[2]
    assert top.chunk.source_url == SITE + "/housing"
    assert top.chunk.modified_time and top.chunk.low_text is False


def test_heading_path_and_title_are_searched(retriever):
    assert paths(retriever.search("Jenkins", k=1)) == [CHUNKS[0].heading_path]  # heading only
    assert paths(retriever.search("ACT", k=1)) == [CHUNKS[3].heading_path]  # title only


@pytest.mark.parametrize("question", ["CARPET", "carpeted?", "Carpets!", "the carpeting"])
def test_case_punctuation_and_word_variants_match(retriever, question):
    assert paths(retriever.search(question, k=1)) == [CHUNKS[1].heading_path]


def test_plural_query_matches_singular_text(retriever):
    assert CHUNKS[1].heading_path in paths(retriever.search("kitchens", k=5))


@pytest.mark.parametrize(
    "question", ["", "   ", "?!", "asdfgh qwerty", "what is the", 'AND OR NOT "*', "col:x ^y"]
)
def test_empty_or_nonsense_query_returns_nothing(retriever, question):
    assert retriever.search(question, k=5) == []


def test_k_limits_results(retriever):
    assert len(retriever.search("score report", k=1)) == 1
    assert retriever.search("score report", k=0) == []


def test_low_text_chunk_ranks_below_a_close_normal_chunk():
    text = "Send your score report to the admissions office."
    low = chunk("Low", text, low_text=True, url="/low")
    normal = chunk("Normal", text, url="/normal")
    others = [chunk(f"Other {i}", "Dining hall hours and menus.") for i in range(4)]
    for order in ([low, normal], [normal, low]):
        results = LocalKeywordRetriever([*order, *others]).search("send score report", k=2)
        assert paths(results) == ["Normal", "Low"]


def test_low_text_chunk_is_still_returned_when_it_is_the_only_match(retriever):
    results = retriever.search("video", k=5)
    assert paths(results) == [CHUNKS[3].heading_path]


def test_word_in_every_chunk_is_below_the_threshold():
    chunks = [chunk(f"Page {i}", f"Housing note number {i}.") for i in range(4)]
    assert LocalKeywordRetriever(chunks).search("housing", k=5) == []


def test_loads_chunk_files_and_is_selected_by_config(tmp_path):
    page = {"url": SITE + "/housing", "chunks": [asdict(c) for c in CHUNKS]}
    (tmp_path / "housing.json").write_text(json.dumps(page))
    retriever = get_retriever(Settings(retriever="local", chunks_dir=str(tmp_path)))
    assert len(retriever.chunks) == len(CHUNKS)
    assert paths(retriever.search("carpet", k=1)) == [CHUNKS[1].heading_path]

    with pytest.raises(NotImplementedError):
        get_retriever(Settings(retriever="azure"))
    with pytest.raises(ValueError):
        get_retriever(Settings(retriever="elastic"))


def test_cli_prints_score_title_heading_path_and_url(monkeypatch, capsys):
    monkeypatch.setattr("api.app.retriever.get_retriever", lambda: LocalKeywordRetriever(CHUNKS))
    main(["carpeted suites", "-k", "1"])
    out = capsys.readouterr().out
    assert "Housing & Dining" in out
    assert CHUNKS[1].heading_path in out and SITE + "/housing" in out

    main(["video"])
    assert "[low text]" in capsys.readouterr().out

    main(["qwertyuiop"])
    assert "No relevant chunks found." in capsys.readouterr().out


SYNONYMS = [["club", "student organization"], ["dorm", "residence hall"]]
ORG_CHUNKS = [
    chunk("Student Life > Organizations", "Join a registered student organization.", url="/orgs"),
    chunk("Student Life > Clubs", "Club sports meet on Fridays.", url="/clubs"),
    *(
        chunk(f"Admissions > Step {i}", f"Application step {i}.", url=f"/apply/{i}")
        for i in range(6)
    ),
]


def test_synonym_query_finds_the_chunk_in_dsu_wording():
    plain = LocalKeywordRetriever(ORG_CHUNKS)
    expanded = LocalKeywordRetriever(ORG_CHUNKS, synonyms=SYNONYMS)
    assert "/orgs" not in [r.chunk.source_url[len(SITE) :] for r in plain.search("dorms", k=5)]
    assert expanded.expand("Which dorms are open?") == ["residence hall"]
    assert expanded.expand("What clubs can I join?") == ["student organization"]
    results = expanded.search("What organizations can I join?", k=5)
    assert paths(results)[0] == "Student Life > Organizations"
    results = LocalKeywordRetriever(CHUNKS, synonyms=[["dorm", "residence halls"]]).search(
        "dorms", k=3
    )
    assert results and all("Residence Halls" in p for p in paths(results))


def test_students_own_word_outranks_a_synonym_only_match():
    retriever = LocalKeywordRetriever(ORG_CHUNKS, synonyms=SYNONYMS)
    clubs, orgs = (
        next(r.score for r in retriever.search("clubs", k=5) if r.chunk.heading_path.endswith(e))
        for e in ("Clubs", "Organizations")
    )
    assert clubs > orgs > 0
    assert paths(retriever.search("student organization", k=1)) == ["Student Life > Organizations"]


def test_phrases_match_whole_words_only():
    retriever = LocalKeywordRetriever([], synonyms=[["hall", "dorm"]])
    assert retriever.expand("challenge") == []
    assert retriever.expand("Residence Halls") == ["dorm"]


@pytest.mark.parametrize("content", [None, "", "# only comments\n", "groups:\n"])
def test_empty_or_missing_synonyms_file_falls_back_to_plain_search(tmp_path, content):
    path = tmp_path / "synonyms.yaml"
    if content is not None:
        path.write_text(content)
    assert load_synonyms(path) == []
    page = {"url": SITE + "/orgs", "chunks": [asdict(c) for c in ORG_CHUNKS]}
    (tmp_path / "orgs.json").write_text(json.dumps(page))
    retriever = get_retriever(Settings(chunks_dir=str(tmp_path), synonyms_file=str(path)))
    plain = LocalKeywordRetriever(ORG_CHUNKS)
    assert retriever.expand("clubs") == []
    assert retriever.search("clubs", k=5) == plain.search("clubs", k=5)


def test_synonyms_file_accepts_comma_lines_and_lists(tmp_path):
    path = tmp_path / "synonyms.yaml"
    path.write_text("groups:\n  - Club,  Student Organization\n  - [dorm, residence-hall]\n")
    assert load_synonyms(path) == [["club", "student organization"], ["dorm", "residence hall"]]


@pytest.mark.parametrize(
    "content, message",
    [
        ("groups: [club, org", "not valid YAML"),
        ("- club, organization\n", "one top-level key, 'groups:'"),
        ("synonyms:\n  - club, organization\n", "one top-level key, 'groups:'"),
        ("groups: club, organization\n", "'groups' must be a list"),
        ("groups:\n  - club\n", "group 1 .* needs at least two"),
        ("groups:\n  - club, , organization\n", "group 1 .* empty entry"),
        ("groups:\n  - {club: organization}\n", "group 1 .* separated by commas"),
        ("groups:\n  - club, organization\n  - dorm, clubs\n", "group 2 .* already in group 1"),
    ],
)
def test_bad_synonyms_file_gives_a_clear_error(tmp_path, content, message):
    path = tmp_path / "synonyms.yaml"
    path.write_text(content)
    with pytest.raises(SynonymsError, match=message):
        load_synonyms(path)


def test_repo_synonyms_file_loads():
    groups = load_synonyms(SYNONYMS_PATH)
    assert ["club", "organization", "student organization"] in groups
    assert Settings(_env_file=None).synonyms_file == "api/app/synonyms.yaml"
