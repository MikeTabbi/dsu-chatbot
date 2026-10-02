# Evaluation

`questions.csv` holds real student questions and what a good answer looks like. Grow this to ~50
questions before launch and run it whenever prompts, retrieval, or the model change.

Good sources for questions: office FAQ pages, common emails to admissions/financial aid/registrar,
front desk logs.

## The eval set

One row per question:

| Column | What it holds |
|---|---|
| `question` | The question as a student would type it |
| `expected_answer` | A short note on what a good answer says, for people reading results |
| `source_url` | The page or pages that answer it (any one counts). Empty for non-answer categories |
| `category` | `answer`, `personal`, `gap`, or `off_topic` (below) |
| `expected_behavior` | Must match the category: `answer_with_source`, `redirect_personal`, `say_not_found`, `decline` |
| `must_contain` | Phrases the answer must include (case-insensitive) |
| `must_not_contain` | Phrases the answer must not include |

List columns are separated by `|`. A phrase starting with `re:` is a case-insensitive regex, for
guesses that a plain phrase can't tell from a good answer: `your advisor is` also matches "I can't
tell you who your advisor is", so the advisor case forbids `re:your advisor is (dr|mr|mrs|ms|prof)\b`.
A `|` inside parentheses belongs to the regex.

Categories and how full mode grades them:

- **answer** (`answer_with_source`): answers from the sources, links one of the expected URLs, and
  marks at least one source as cited (so `/chat` returns at least one source).
- **personal** (`redirect_personal`): says it can't see the student's records and says where to
  check (DegreeWorks, Navigate, Banner, the Registrar, an advisor, Student Accounts, or Financial
  Aid).
- **gap** (`say_not_found`): DSU content we know is missing (see
  [ingestion/README.md](../ingestion/README.md#known-content-gaps)). Says it couldn't find it and
  points to an office, without guessing.
- **off_topic** (`decline`): politely says it only helps with DSU questions.

The behavior checks look for marker phrases ("couldn't find", "can't see", "only help", ...) in
[run.py](run.py). They are heuristics, so read the saved answers before trusting a pass or fail.

Claude words the same answer differently from run to run ("I couldn't find directions" one time,
"the pages I have don't give directions" the next), so a check can fail on a good answer. Loosen a
check (a marker, a phrase, an expected URL, or a case's category) only after reading the saved
answer and confirming it was genuinely correct, and write that reason in the PR. Never loosen a
check to turn a wrong or guessed answer into a pass.

## Running it

```bash
python -m eval.run                         # retrieval only: no Claude calls, free
python -m eval.run -k 3 --category answer  # top 3 chunks, answer cases only
python -m eval.run --full                  # real answers through /chat; asks before calling Claude
python -m eval.run --full --yes            # no prompt (e.g. in a script)
python -m eval.run --full --client fake    # dry run of the full path with the fake client
```

- **Retrieval only** searches each question with the configured retriever and reports whether an
  expected URL is in the top `k` chunks (default `CHAT_TOP_K`) and at what rank. Cases without
  expected URLs show `-`.
- **Full** posts each question to `/chat` in-process (FastAPI's `TestClient`, no server needed),
  so it runs the same validation, retrieval, prompt, and Claude call as the real endpoint. It
  grades the answer on HTTP status, the expected behavior, and the phrases. The `src` column shows
  whether an expected URL was among the sources the answer cites (the `sources` `/chat` returns).
  A `no` with a passing retrieval run means Claude had the page but didn't mark it as used. It prints the number of questions and asks before making real Claude
  calls unless `--yes` is passed. It uses `CLAUDE_MODEL` and `ANTHROPIC_API_KEY` from the
  environment or `.env`.

Both modes print a per-case table and an overall score, and save every result (including full
answers) to `eval/results/<timestamp>-<mode>.json`, which is gitignored.
