import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from api.app.config import Settings
from api.app.exchange_log import (
    Exchange,
    NullExchangeLog,
    SqliteExchangeLog,
    UnknownRequestError,
    get_exchange_log,
    main,
)


def exchange(request_id, days_ago=0, outcome="answered", question="Where is DSU?"):
    moment = datetime.now(UTC) - timedelta(days=days_ago)
    return Exchange(
        request_id=request_id,
        timestamp=moment.isoformat(timespec="seconds"),
        question=question,
        answer="DSU is in Dover.",
        outcome=outcome,
        prompt_version="abc123def456",
        latency_ms=120,
        source_urls=["https://www.desu.edu/about"],
        chunk_ids=["c1", "c2"],
        model="fake",
        input_tokens=50,
        output_tokens=5,
    )


@pytest.fixture
def store(tmp_path):
    return SqliteExchangeLog(tmp_path / "logs" / "exchanges.sqlite")


def test_add_and_read_back(store):
    added = exchange("r1")
    store.add(added)
    [item] = store.recent()
    assert item.exchange == added
    assert (item.rating, item.comment) == (None, None)


def test_feedback_is_created_then_updated_not_duplicated(store):
    store.add(exchange("r1"))
    store.set_feedback("r1", "up", None)
    store.set_feedback("r1", "down", "Wrong office hours")
    [item] = store.recent()
    assert (item.rating, item.comment) == ("down", "Wrong office hours")
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback").fetchone() == (1,)


def test_feedback_for_an_unknown_request_raises(store):
    with pytest.raises(UnknownRequestError):
        store.set_feedback("nope", "up", None)


def test_recent_filters_by_rating_and_outcome_newest_first(store):
    store.add(exchange("old", days_ago=2))
    store.add(exchange("new"))
    store.add(exchange("none", outcome="no_sources"))
    store.set_feedback("old", "down", None)
    store.set_feedback("new", "down", None)
    store.set_feedback("none", "up", None)
    assert [r.exchange.request_id for r in store.recent(rating="down")] == ["new", "old"]
    assert [r.exchange.request_id for r in store.recent(outcome="no_sources")] == ["none"]
    assert len(store.recent(limit=1)) == 1


def test_retention_deletes_older_exchanges_and_their_feedback(store):
    store.add(exchange("old", days_ago=100))
    store.add(exchange("recent", days_ago=10))
    store.set_feedback("old", "down", None)
    assert store.delete_older_than(datetime.now(UTC) - timedelta(days=90)) == 1
    assert [r.exchange.request_id for r in store.recent()] == ["recent"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM feedback").fetchone() == (0,)


def test_null_log_keeps_nothing():
    store = NullExchangeLog()
    store.add(exchange("r1"))
    assert store.recent() == []
    with pytest.raises(UnknownRequestError):
        store.set_feedback("r1", "up", None)


def test_setting_picks_the_store(tmp_path):
    path = tmp_path / "x.sqlite"
    assert isinstance(get_exchange_log(Settings(exchange_log_path=str(path))), SqliteExchangeLog)
    assert isinstance(get_exchange_log(Settings(exchange_log="none")), NullExchangeLog)
    with pytest.raises(ValueError, match="EXCHANGE_LOG"):
        get_exchange_log(Settings(exchange_log="cosmos"))


def test_purge_command_uses_the_retention_setting(tmp_path, capsys):
    config = Settings(exchange_log_path=str(tmp_path / "x.sqlite"), exchange_retention_days=30)
    store = get_exchange_log(config)
    store.add(exchange("old", days_ago=31))
    store.add(exchange("recent", days_ago=29))
    main(["purge"], config=config)
    assert "Deleted 1 exchange(s)" in capsys.readouterr().out
    assert [r.exchange.request_id for r in store.recent()] == ["recent"]
    main(["purge", "--days", "0"], config=config)
    assert store.recent() == []


def test_review_command_lists_thumbs_down_with_comment(tmp_path, capsys):
    config = Settings(exchange_log_path=str(tmp_path / "x.sqlite"))
    store = get_exchange_log(config)
    store.add(exchange("liked", question="Liked question"))
    store.add(exchange("disliked", question="Disliked question"))
    store.set_feedback("liked", "up", None)
    store.set_feedback("disliked", "down", "Out of date")
    main(["review"], config=config)
    out = capsys.readouterr().out
    assert "1 thumbs-down exchange(s)" in out
    assert "Question: Disliked question" in out
    assert "DSU is in Dover." in out
    assert "Sources:  https://www.desu.edu/about" in out
    assert "Comment:  Out of date" in out
    assert "Liked question" not in out


def test_unanswered_command_lists_no_sources_questions(tmp_path, capsys):
    config = Settings(exchange_log_path=str(tmp_path / "x.sqlite"))
    store = get_exchange_log(config)
    store.add(exchange("a", question="Where is DSU?"))
    store.add(exchange("b", outcome="no_sources", question="Where is the MLK Building?"))
    main(["unanswered"], config=config)
    out = capsys.readouterr().out
    assert "Where is the MLK Building?" in out
    assert "Where is DSU?" not in out
