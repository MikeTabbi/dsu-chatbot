import pytest

from api.app.main import app, get_chat_retriever
from api.app.retriever import LocalKeywordRetriever
from eval.tests.test_run import CHUNKS, CSV_HEADER, CSV_ROWS


@pytest.fixture
def cases_csv(tmp_path):
    path = tmp_path / "questions.csv"
    path.write_text(CSV_HEADER + CSV_ROWS)
    return path


@pytest.fixture
def retriever():
    return LocalKeywordRetriever(CHUNKS)


@pytest.fixture
def patched(retriever):
    """Swap in the test retriever the way the /chat tests do, so the CLI and /chat both get it."""
    app.dependency_overrides[get_chat_retriever] = lambda: retriever
    yield
    app.dependency_overrides.clear()
