# Command cheat sheet

The commands you use most, in one place. Run everything from the project folder
(`~/dev/dsu-chatbot`). The READMEs have the details behind each one.

## First time on a machine

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # then set CLAUDE_CLIENT=anthropic and ANTHROPIC_API_KEY
```

Every new terminal: `source .venv/bin/activate`

## Run the bot

```bash
./scripts/run-demo.sh                       # backend + widget demo page; opens http://localhost:8080/demo.html
```

Ctrl+C stops both. With `CLAUDE_CLIENT=fake` in `.env`, answers start with "[fake answer]".

Type the `./` (or the `scripts/` path): the shell doesn't look in the current folder, so a bare
`run-demo.sh` says "command not found". From inside `scripts/`, use `./run-demo.sh`. If it says
"permission denied", run `chmod +x scripts/run-demo.sh` once, or start it with
`bash scripts/run-demo.sh`.

Other ways:

```bash
uvicorn api.app.main:app --reload           # backend only, restarts when code changes
python -m http.server 8080 --directory widget   # widget only; open http://localhost:8080/demo.html?mock=1 (no backend, saved answers)
```

Is the backend up? http://localhost:8000/health

Ask one question from the terminal (backend running):

```bash
python -c "import httpx; print(httpx.post('http://localhost:8000/chat', json={'question': 'How do I register for classes?'}, timeout=60).json())"
```

## Check a single piece

```bash
python -m api.app.retriever "Which halls have carpeted rooms?"   # what search finds (score, page, URL)
python -m api.app.retriever -k 3 "How do I send my SAT scores?"  # top 3 only
python -m api.app.prompt "Which dorms have carpeted rooms?"      # the exact prompt Claude would get
python -m api.app.prompt --ask "Which dorms have carpeted rooms?"  # ...and Claude's answer (CLAUDE_CLIENT=anthropic)
python -m api.app.claude_client "Reply with: hello from DSU"     # is the Claude connection working?
```

## Update the DSU content (the index)

```bash
python -m ingestion.pipeline                # crawl pages that are due, then extract, chunk, sync the index
python -m ingestion.pipeline --force        # re-check every page
python -m ingestion.pipeline --topic housing --limit 3   # a small test run
python -m ingestion.sources                 # check sources.yaml (counts per topic)
```

Adding new pages: `python -m ingestion.discover`, approve rows, `python -m ingestion.approve`, then
the pipeline. See [ingestion/README.md](ingestion/README.md).

The running server picks up a new index by itself; no restart needed.

## Tests and lint (what CI runs)

```bash
pytest                                  # all Python tests
ruff check .                            # lint
ruff format .                           # fix formatting (CI runs: ruff format --check .)
node --test widget/tests/*.test.js      # widget tests, no npm install
```

All four before opening a PR:

```bash
ruff check . && ruff format --check . && pytest -q && node --test widget/tests/*.test.js
```

## Eval (is the bot still answering well?)

```bash
python -m eval.run                          # retrieval only: free, no Claude calls
python -m eval.run --rewrite --rerank       # retrieval the way /chat does it (small Claude cost)
python -m eval.run --full                   # real answers through /chat; asks before spending money
python -m eval.run --full --yes             # same, no prompt
python -m eval.run --full --client fake     # dry run, free
python -m eval.run --full --category answer # one category only
python -m eval.run --compare                # re-grade the latest full run with the Claude judge
python -m eval.run --calibrate              # check the judge against hand-labeled answers
```

Results (full answers included) are saved in `eval/results/`. A few source misses from run to
run is normal; a wrong or made-up answer is not. See [eval/README.md](eval/README.md).

## What students asked (exchange log)

```bash
python -m api.app.exchange_log review       # thumbs-down and "couldn't find" answers, newest first
python -m api.app.exchange_log unanswered   # questions search found nothing for (missing content)
python -m api.app.exchange_log purge        # delete exchanges older than EXCHANGE_RETENTION_DAYS
```

## Git and GitHub workflow

```bash
git checkout main && git pull               # start from the latest main
git checkout -b feature/short-name          # one branch per issue
git status                                  # what changed
git add -A && git commit -m "What changed (#issue)"
git push -u origin feature/short-name
gh pr create --fill                         # open the PR
gh pr checks                                # CI status of the current branch's PR
gh pr checks --watch                        # wait for CI to finish
gh issue list                               # open issues
gh issue view 76                            # one issue
```

Roadmap board: `gh project item-list 14 --owner MikeTabbi` (project "DSU Chatbot Roadmap").

## When something's off

| Problem | Try |
|---|---|
| Every answer is "couldn't find anything" | No index yet: `python -m ingestion.pipeline` |
| Answers start with "[fake answer]" | Set `CLAUDE_CLIENT=anthropic` and `ANTHROPIC_API_KEY` in `.env` |
| `ModuleNotFoundError` | Forgot `source .venv/bin/activate`, or run `pip install -e ".[dev]"` |
| Widget says it can't reach the assistant | Backend not running, or the page's origin isn't in `ALLOWED_ORIGINS` |
| `command not found: run-demo.sh` | Add the path: `./scripts/run-demo.sh` (or `./run-demo.sh` inside `scripts/`) |
| "Address already in use" | Something still runs on 8000/8080: `lsof -i :8000` then `kill <PID>` |
| "You're sending questions faster..." | Local rate limit; restart the backend or set `RATE_LIMITER=none` in `.env` |
