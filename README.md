# DSU Chatbot

A chatbot that answers general questions about Delaware State University using public DSU information, with answers grounded in (and linked to) official sources.

## How it works

- **Question path:** chat widget on dsu.edu → backend API → retriever finds relevant DSU content → Claude writes an answer with source links.
- **Data path:** ingestion job crawls DSU sources on a schedule, splits pages into chunks, and stores them in a search index.
- **Feedback:** answers are logged with thumbs up/down so gaps in the knowledge base can be fixed.

See [docs/architecture.md](docs/architecture.md) for details.

## Repo layout

| Folder | What's in it |
|---|---|
| `api/` | Backend (FastAPI): receives questions, retrieves sources, calls Claude |
| `ingestion/` | Crawler, chunking, and indexing jobs |
| `widget/` | Embeddable chat UI for dsu.edu |
| `prompts/` | System prompt and templates, versioned like code |
| `eval/` | Test questions with expected answers |
| `infra/` | Azure setup as code |
| `docs/` | Architecture and decision records |

## Run locally

Requires Python 3.11+.

```bash
cp .env.example .env          # then fill in your own values
pip install -e ".[dev]"
uvicorn api.app.main:app --reload
```

Check it's running: http://localhost:8000/health

## Retrieval

`/chat` will depend only on the `Retriever` interface in [api/app/retriever.py](api/app/retriever.py):
`search(question, k)` returns the top `k` chunks, best first, each with a `score` and the full
chunk (`source_url`, `title`, `heading_path`, `modified_time`, `low_text`, `text`, ...). An empty
list means nothing relevant was found. The `RETRIEVER` setting picks the implementation: `local`
(the default) or `azure` (Azure AI Search, #22, not built yet).

```bash
python -m ingestion.chunk                                     # build data/chunks first
python -m api.app.retriever "Which halls have carpeted rooms?"   # prints score, title, heading path, URL
python -m api.app.retriever -k 3 "How do I send my SAT scores?"
```

The local retriever loads every file in `CHUNKS_DIR` (default `data/chunks`) into an in-memory
**SQLite FTS5** index and ranks with FTS5's built-in **BM25**. Why FTS5:

- It ships with Python's `sqlite3`, so there is no new dependency, no model download, and no
  cloud account. A few hundred chunks index in milliseconds at startup.
- Its `porter` tokenizer lowercases, drops punctuation, and stems, so "Rooms", "room", and
  "carpeted"/"carpet" match. A hand-rolled BM25 would need its own tokenizer and stemmer.
- BM25 is the same keyword ranking Azure AI Search uses, so local results are a fair preview of
  its keyword mode when choosing keyword vs. vector vs. hybrid (#18).

Details:

- `title`, `heading_path`, and `text` are indexed together (headings aren't in `text`). Link URLs
  are dropped from the indexed text; link text is kept.
- The question is split into words, common stopwords ("what", "the", "do", ...) are dropped, and
  the rest are ORed. Each word is quoted, so user input is never parsed as FTS5 query syntax.
- **Relevance threshold:** a result needs a score above `RETRIEVER_MIN_SCORE` (default 0.1).
  Words that appear in nearly every chunk (e.g. "housing") score about 0, so a question matching
  only those returns nothing.
- **Low-text chunks** (from near-empty pages) keep 80% of their score, so they still show up but
  lose to a normal chunk with a similar score.
- Keyword search has no synonyms ("dorm" doesn't match "hall") and favors short chunks, so a long
  table that mentions a word once can rank below a short chunk that repeats other query words.

## Tests and linting

```bash
pytest
ruff check .
ruff format .
```

## Contributing

1. Branch off `main` (`feature/short-description`).
2. Open a pull request. CI must pass before merging.
3. Never commit secrets. Use `.env` locally; it's gitignored.
