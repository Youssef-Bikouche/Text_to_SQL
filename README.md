# Text-to-SQL Query Engine

Ask a database a question in plain English and get back the SQL, the results, and an honest confidence score — not just a translation, but a pipeline that catches when the AI got it wrong before you ever trust the answer.

## Why this exists

Letting an LLM write SQL directly against a real database fails in three distinct ways:

1. **The query can be dangerous.** A destructive or malformed statement (`DELETE`, `DROP`, stacked statements) must never touch the real data, whether or not it's "correct."
2. **The query can be wrong.** It runs cleanly and returns believable numbers, but silently answers a different question than the one asked — the dangerous kind of wrong, because nothing *looks* broken.
3. **The query can be right but untrusted.** Even a correct answer is worthless in production if the user has to blindly believe it.

This project addresses all three with layered guardrails instead of trusting the model's output directly.

## How it works

```
question --> Gemini generates SQL --> structural safety check
                                            |
                          ok? ----------------------------- not ok?
                           |                                    |
                   run against real DB              run against a disposable
                           |                          copy of the DB instead,
                   confidence scoring                 report what *would*
                   (schema + execution +               have happened
                    LLM-as-judge)
                           |
                        response
```

1. **Schema-aware generation** — the live database schema (tables + columns) is loaded at startup and included in the prompt, so the model writes SQL grounded in what actually exists.
2. **Structural safety check** (`is_safe_sql`) — the generated SQL is parsed with `sqlglot` and only allowed to execute if it parses as exactly **one `SELECT` statement**. This is stronger than a keyword blocklist, which smart injections (comments, stacked statements) can slip past.
3. **Sandboxed preview for blocked queries** — if a query fails the safety check, it isn't just rejected. It's run against a **disposable copy** of the database so the user can still see what it *would* have done (e.g. "Would affect 8 row(s)") without the real data ever being at risk.
4. **Confidence scoring** — for queries that do run, three signals combine into a `LOW` / `MEDIUM` / `HIGH` verdict:
   - **Schema check** — does the SQL only reference real tables/columns (including SQLite's own `sqlite_master` metadata columns for introspection queries)?
   - **Execution check** — does the query actually run without erroring?
   - **LLM-as-judge** — a second model call looks at the question, SQL, and results and votes on whether they actually answer what was asked.

   A failed hard check (schema or execution) always forces `LOW`, regardless of what the judge thinks — deterministic checks outrank an AI's opinion.

## Project structure

```
backend/
  main2.py          FastAPI backend — the whole pipeline above
  prompt.py         System prompt for SQL generation
  requirements.txt
  .env              Gemini API key (not committed)
frontend/
  index.html        Single-page vanilla JS frontend
data/
  Database.sqlite   Sample SQLite database
```

## Setup

**1. Install dependencies**

```bash
pip install -r backend/requirements.txt
```

**2. Add your Gemini API key**

Create a `.env` file in `backend/`:

```
GOOGLE_API_KEY=your_key_here
```

**3. Run the backend**

```bash
cd backend
python main2.py
```

The API starts at `http://127.0.0.1:8002`.

**4. Open the frontend**

Open `frontend/index.html` directly in a browser. It talks to the API at `127.0.0.1:8002` by default.

## API

`POST /ask`

```json
{ "question": "How many tables are in the db?" }
```

Response (safe query):

```json
{
  "question": "How many tables are in the db?",
  "sql": "SELECT COUNT(*) FROM sqlite_master WHERE type='table';",
  "is_safe": true,
  "results": [[11]],
  "confidence": "HIGH",
  "reason": "All checks passed and the judge agrees."
}
```

Response (blocked query):

```json
{
  "question": "delete all employees",
  "sql": "DELETE FROM Employee;",
  "is_safe": false,
  "message": "Query blocked — not safe to run against the real database.",
  "preview": { "would_succeed": true, "effect": "Would affect 8 row(s)." }
}
```

## Limitations

The confidence score flags *likely* problems — it doesn't prove correctness. It's a signal for when to double-check an answer, not a guarantee.
