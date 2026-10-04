from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from prompt import system_prompt
from pydantic import BaseModel
import sqlite3
import os
import shutil
import tempfile
import sqlglot
import sqlglot.expressions as exp
from dotenv import load_dotenv
from google import genai

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "Database.sqlite")

def connectionDB():
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    print("connnection status : ", connection)
    pointer = connection.cursor()
    tables = pointer.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()

    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)

    schema_text = ""
    real_columns = set()
    for table in tables:
        table_name = table[0]
        schema_text += f"Table: {table_name}\n"

        pointer.execute(f"PRAGMA table_info({table_name});")
        columns = pointer.fetchall()

        for column in columns:
            column_name = column[1]
            column_type = column[2]
            schema_text += f"  - {column_name} ({column_type})\n"
            real_columns.add(column_name)

        schema_text += "\n"

    # sqlite_master itself is a valid queryable table (e.g. "how many tables"),
    # but it's not part of the user schema loop above, so whitelist its columns too.
    real_columns.update({"type", "name", "tbl_name", "rootpage", "sql"})

    print(schema_text)
    return pointer, client, schema_text, real_columns

pointer, client, schema_text, real_columns = connectionDB()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "the waiter is here"}
#=======================================================
def check_columns_real(sql, real_columns):
    parsed = sqlglot.parse_one(sql)
    used_columns = [c.name for c in parsed.find_all(exp.Column)]
    alias_names = set(a.alias for a in parsed.find_all(exp.Alias) if a.alias)
    for col in used_columns:
        if col not in real_columns and col not in alias_names:
            print(f"  hallucination: '{col}' is not a real column")
            return False
    return True
def confidence_score(sql, question, results, real_columns, client):
    columns_ok = check_columns_real(sql, real_columns)   # signal 1 (strong, deterministic)
    # signal 2 (query already ran clean in ask() before this was called, so no need to re-run it here)

    judge_prompt = f"""Does this SQL result answer the question? Reply only YES or NO.
Question: {question}
SQL: {sql}
Results: {results}"""
    judge = client.models.generate_content(model="gemini-3.6-flash", contents=judge_prompt)
    judge_ok = "YES" in judge.text.upper()               # signal 3 (weak, AI vote)

    if not columns_ok:
        return "LOW", "A hard check failed (fake column reference)."
    if judge_ok:
        return "HIGH", "All checks passed and the judge agrees."
    return "MEDIUM", "Checks passed but the judge was unsure."
def is_safe_sql(sql):
    sql = sql.strip().rstrip(";")
    try:
        statements = sqlglot.parse(sql)
    except Exception:
        return False              # won't parse -> reject

    if len(statements) != 1:      # must be exactly ONE statement
        return False

    if not isinstance(statements[0], exp.Select):   # must be a SELECT
        return False

    return True

# runs a blocked query against a throwaway COPY of the database, never the real one
def preview_blocked_query(sql):
    tmp_path = tempfile.mktemp(suffix=".sqlite")
    shutil.copy(DB_PATH, tmp_path)

    try:
        sandbox_connection = sqlite3.connect(tmp_path)
        sandbox_pointer = sandbox_connection.cursor()
        try:
            sandbox_pointer.execute(sql)
            sandbox_connection.commit()
            if sandbox_pointer.rowcount is not None and sandbox_pointer.rowcount >= 0:
                effect = f"Would affect {sandbox_pointer.rowcount} row(s)."
            else:
                effect = "Executed successfully (no row count reported for this statement type)."
            return {"would_succeed": True, "effect": effect}
        except Exception as e:
            return {"would_succeed": False, "effect": f"Would fail: {e}"}
        finally:
            sandbox_connection.close()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

class AskRequest(BaseModel):
    question: str

@app.post("/ask")
def ask(request: AskRequest):
    question = request.question

    # generate SQL (your Gemini call)
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=system_prompt + "\n\n" + schema_text + "\n\n"
        + "Write a single SQLite query that answers this question:\n\n"
        + question + "\n\nOutput ONLY the raw SQL. No explanation, no markdown."
    )
    ai_query = response.text.strip()

    # guard it
    if not is_safe_sql(ai_query):
        preview = preview_blocked_query(ai_query)
        return {"question": question, "sql": ai_query, "is_safe": False,
                "message": "Query blocked — not safe to run against the real database.",
                "preview": preview}

    # run it
    pointer.execute(ai_query)
    results = pointer.fetchall()

    # score it
    level, reason = confidence_score(ai_query, question, results, real_columns, client)

    return {"question": question, "sql": ai_query, "is_safe": True,
            "results": results, "confidence": level, "reason": reason}

#=======================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
