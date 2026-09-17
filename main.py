#!/usr/bin/env python3
"""
JDE Connector — Hosted API
===========================

This is the server-side counterpart to the thin client that runs on each
client's machine. Every piece of logic that used to be shipped to the
client — table allowlists, SQL validation, schema descriptions, department
scoping, and license/entitlement checks — lives here instead, where a
client can never read or edit it, because it never reaches their machine.

The thin client authenticates with a per-deployment API key and forwards
its two requests (get schema, run a query) here. This server then reaches
into that specific client's Oracle/JDE database over whichever tunnel is
set up for them (see the setup steps your vendor gave you — an SSH reverse
tunnel or a Cloudflare Tunnel, forwarding their Oracle port to a port on
this server) and returns the result.

Run locally with:
    pip install -r requirements.txt
    uvicorn main:app --host 127.0.0.1 --port 8000

On Render (or a similar platform-as-a-service), the platform terminates
HTTPS for you — the start command there is:
    uvicorn main:app --host 0.0.0.0 --port $PORT

No reverse proxy (Caddy/nginx) is needed on Render; that's only for a
self-managed VPS where nothing else provides TLS for you. Either way, API
keys must never travel over plain HTTP, so don't skip whichever of the two
applies to where this ends up running.
"""

import os
import re
import sqlite3
import json
import datetime
import secrets
import threading
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import config  # your existing table/schema/business-rule definitions — move the real file here unchanged

# On Render, set CLIENTS_PATH=/etc/secrets/clients.json and upload the real
# file as a Secret File in the dashboard (Environment -> Secret Files) —
# that keeps real DB passwords out of your git repo entirely. Locally,
# it just falls back to a plain file next to this script.
CLIENTS_PATH = os.environ.get(
    "CLIENTS_PATH", os.path.join(os.path.dirname(__file__), "clients.json")
)
LOG_PATH = os.path.join(os.path.dirname(__file__), "query_log.jsonl")
MOCK_DB_PATH = os.path.join(os.path.dirname(__file__), "jde_mock.db")

MAX_ROWS = 200
ORACLE_CALL_TIMEOUT_MS = 15000

app = FastAPI(title="JDE Connector API")

# ---------------------------------------------------------------------------
# Client/deployment registry — one entry per API key you've issued (one
# per department deployment, typically). This file contains real database
# credentials, so unlike the earlier public license Gist, THIS FILE MUST
# NEVER BE PUBLIC. Keep it on the server's disk with restricted
# permissions (chmod 600 clients.json), or move it to a real secrets
# manager once you have more than a handful of clients.
# ---------------------------------------------------------------------------


def load_clients() -> dict:
    if not os.path.exists(CLIENTS_PATH):
        return {}
    with open(CLIENTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def authenticate(authorization: Optional[str]) -> dict:
    """Validate the Authorization header and return the deployment's
    config dict, or raise a 401/403 with a clear reason. This check is the
    real enforcement point now — it can't be edited away by a client,
    because it never runs on their machine."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed API key.")

    api_key = authorization[len("Bearer "):].strip()

    deployment = None
    for key, entry in load_clients().items():
        if secrets.compare_digest(key, api_key):
            deployment = entry
            break

    if deployment is None:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    if not deployment.get("active", False):
        raise HTTPException(
            status_code=403,
            detail="This deployment has been disabled. Contact your vendor.",
        )

    expires_on = deployment.get("expires_on")
    if expires_on:
        try:
            if datetime.date.today() > datetime.date.fromisoformat(expires_on):
                raise HTTPException(
                    status_code=403,
                    detail=f"Access expired on {expires_on}. Contact your vendor to renew.",
                )
        except ValueError:
            pass  # malformed date in clients.json — don't crash on it, just skip expiry

    return deployment


# ---------------------------------------------------------------------------
# Logging — same shape as the old local log, now centralized across every
# client instead of scattered on their individual machines. That's a
# useful side effect of this move: one place to see usage across your
# whole client base, including refused/attempted access.
# ---------------------------------------------------------------------------


def log_query(deployment_name: str, sql: str, status: str, row_count: Optional[int] = None, error: Optional[str] = None) -> None:
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "deployment": deployment_name,
        "sql": sql,
        "status": status,
        "row_count": row_count,
        "error": error,
    }
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never break an actual query


# ---------------------------------------------------------------------------
# Per-deployment database connection pooling.
#
# Previously this opened a brand new Oracle connection (a full TCP
# handshake + auth round-trip to the Oracle listener) on every single
# query. That's the single most expensive part of handling a request,
# and it was being paid in full every time.
#
# Now: one connection pool per distinct (dsn, user) pair, created lazily
# on first use and reused for every subsequent request from any
# deployment that shares those credentials. Acquiring a connection from
# an existing pool is fast (just handing out an already-open session);
# only the very first request for a given deployment pays the full
# connection-setup cost.
#
# oracledb's pooled connections behave specially with .close(): calling
# it on a connection that came from a pool releases it back to the pool
# instead of actually tearing down the session. That means the rest of
# this file (conn.cursor(), conn.close(), etc.) needs no changes at all
# — the speedup is entirely contained in get_connection().
# ---------------------------------------------------------------------------

_pools: dict = {}
_pools_lock = threading.Lock()

# Pool sizing — deliberately conservative defaults, since this server may
# serve several deployments and each pool holds its own real Oracle
# sessions. Tune via environment variables if a specific deployment needs
# more concurrency.
POOL_MIN = int(os.environ.get("ORACLE_POOL_MIN", "1"))
POOL_MAX = int(os.environ.get("ORACLE_POOL_MAX", "5"))
POOL_INCREMENT = int(os.environ.get("ORACLE_POOL_INCREMENT", "1"))
# How long an idle pooled connection is kept open before Oracle-side or
# pool-side cleanup may reclaim it. Matches ORACLE_CALL_TIMEOUT_MS's spirit
# — don't hold idle sessions open indefinitely.
POOL_TIMEOUT_SECONDS = int(os.environ.get("ORACLE_POOL_TIMEOUT_SECONDS", "300"))


def _get_or_create_pool(user: str, password: str, dsn: str):
    """Return the shared pool for this (dsn, user) pair, creating it if
    this is the first request that's needed it. Thread-safe: FastAPI runs
    synchronous path operations in a thread pool, so concurrent first
    requests for the same new deployment are a real possibility."""
    import oracledb  # imported lazily so mock-only testing never needs it

    pool_key = (dsn, user)
    pool = _pools.get(pool_key)
    if pool is not None:
        return pool

    with _pools_lock:
        # Re-check inside the lock — another thread may have created it
        # while this one was waiting.
        pool = _pools.get(pool_key)
        if pool is not None:
            return pool

        try:
            pool = oracledb.create_pool(
                user=user,
                password=password,
                dsn=dsn,
                min=POOL_MIN,
                max=POOL_MAX,
                increment=POOL_INCREMENT,
                timeout=POOL_TIMEOUT_SECONDS,
            )
        except Exception as e:
            # Don't cache a failed pool — the next request should retry
            # cleanly rather than being stuck with a broken pool forever
            # (e.g. if this failed because the DB was briefly unreachable).
            raise RuntimeError(f"Could not create connection pool: {e}") from e

        _pools[pool_key] = pool
        return pool


def get_connection(deployment: dict):
    db = deployment.get("db", {})
    dsn = db.get("dsn", "").strip()

    if dsn:
        pool = _get_or_create_pool(db["user"], db["password"], dsn)
        conn = pool.acquire()
        conn.call_timeout = ORACLE_CALL_TIMEOUT_MS
        return conn, True

    # No DSN configured for this deployment yet — fall back to the shared
    # mock DB. Useful for standing up and testing this server before any
    # tunnel is live.
    if not os.path.exists(MOCK_DB_PATH):
        raise RuntimeError("No DSN configured for this deployment, and no mock database found.")
    return sqlite3.connect(MOCK_DB_PATH), False


# ---------------------------------------------------------------------------
# SQL guardrails — identical logic to the old local script, just living
# here now where a client can't read or edit it.
# ---------------------------------------------------------------------------

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
TABLE_REF_PATTERN = re.compile(r"\b(?:FROM|JOIN)\s+(?:\w+\.)?(\w+)", re.IGNORECASE)


def is_read_only(sql: str) -> bool:
    stripped = sql.strip().rstrip(";")
    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        return False
    if WRITE_KEYWORDS.search(stripped):
        return False
    return True


def referenced_tables(sql: str) -> set:
    return {m.upper() for m in TABLE_REF_PATTERN.findall(sql)}


def effective_allowed_tables(deployment: dict) -> set:
    allowed_tables = set(config.ALLOWED_TABLES)
    department = deployment.get("department")
    if not department:
        return allowed_tables
    prefixes = getattr(config, "DEPARTMENT_TABLE_PREFIXES", {}).get(department)
    if prefixes is None:
        return set()
    return {t for t in allowed_tables if any(t.upper().startswith(p.upper()) for p in prefixes)}


def uses_only_allowed_tables(sql: str, deployment: dict) -> bool:
    tables = referenced_tables(sql)
    if not tables:
        return False
    if not config.RESTRICT_TO_APPROVED_TABLES:
        return True
    return tables.issubset(effective_allowed_tables(deployment))


def table_ref(table: str, oracle: bool) -> str:
    if not oracle:
        return table
    schema = config.TABLE_SCHEMAS.get(table)
    return f"{schema}.{table}" if schema else table


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    sql: str


# ---------------------------------------------------------------------------
# Endpoints — these mirror the tool names the thin client exposes to Claude
# ---------------------------------------------------------------------------


@app.post("/v1/query")
def query_jde_database(req: QueryRequest, authorization: Optional[str] = Header(default=None)):
    deployment = authenticate(authorization)
    name = deployment.get("client_name", "unknown")
    sql = req.sql

    if not is_read_only(sql):
        log_query(name, sql, "refused_write")
        return {"result": "REFUSED: only single SELECT statements are permitted."}

    if not uses_only_allowed_tables(sql, deployment):
        log_query(name, sql, "refused_table")
        allowed_list = ", ".join(sorted(effective_allowed_tables(deployment))) or (
            "(no tables configured for this deployment)"
        )
        return {
            "result": (
                f"REFUSED: this query references a table outside the approved "
                f"list. Approved tables are: {allowed_list}."
            )
        }

    try:
        conn, _is_oracle = get_connection(deployment)
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
        conn.close()  # for pooled connections, this releases back to the pool, not a real close
    except Exception as e:
        log_query(name, sql, "error", error=str(e))
        return {"result": f"DATABASE ERROR: {e}"}

    if not rows:
        log_query(name, sql, "executed", row_count=0)
        return {"result": "No matching records were found."}

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    log_query(name, sql, "executed", row_count=len(rows))

    lines = [" | ".join(cols)]
    for row in rows:
        lines.append(" | ".join(str(v) for v in row))
    result = "\n".join(lines)

    if truncated:
        result += (
            f"\n\n[Results truncated at {MAX_ROWS} rows. Narrow your "
            f"question to see a complete answer.]"
        )

    return {"result": result}


@app.post("/v1/schema")
def get_jde_schema(authorization: Optional[str] = Header(default=None)):
    deployment = authenticate(authorization)
    visible_tables = effective_allowed_tables(deployment) if config.RESTRICT_TO_APPROVED_TABLES else None
    oracle = bool(deployment.get("db", {}).get("dsn", "").strip())

    if oracle:
        example_table = table_ref(config.TABLES[0]["name"], oracle)
        dialect_note = (
            f"SQL DIALECT: Oracle SQL. Use Oracle syntax — e.g. FETCH FIRST n "
            f"ROWS ONLY instead of LIMIT, TO_DATE()/date literals for date "
            f"comparisons, and Oracle string concatenation with ||. ALWAYS use "
            f"each table's full schema-qualified name exactly as shown below "
            f"(e.g. {example_table}, not {config.TABLES[0]['name']})."
        )
    else:
        dialect_note = "SQL DIALECT: SQLite (mock data mode). Table names have no schema prefix."

    sections = [dialect_note, ""]

    if visible_tables is not None and not visible_tables:
        sections.append("NOTE: no tables are configured for this deployment — contact your vendor.")

    for table in config.TABLES:
        if visible_tables is not None and table["name"] not in visible_tables:
            continue
        ref = table_ref(table["name"], oracle)
        sections.append(f"TABLE {ref} ({table['description']})")
        col_lines = [(col, dtype, comment) for col, dtype, comment in table["columns"]]
        max_col_len = max(len(c) for c, _, _ in col_lines)
        max_type_len = max(len(t) for _, t, _ in col_lines)
        for col, dtype, comment in col_lines:
            sections.append(f"  {col.ljust(max_col_len)}  {dtype.ljust(max_type_len)}  -- {comment}")
        for note in table.get("notes", []):
            sections.append(f"  -- {note}")
        sections.append("")

    sections.append("RULES:")
    for rule in config.RULES:
        sections.append(f"- {rule}")
    sections.append("")

    for question, sql_template in config.EXAMPLES:
        referenced = {name for name in config.ALLOWED_TABLES if "{" + name + "}" in sql_template}
        if visible_tables is not None and not referenced.issubset(visible_tables):
            continue  # example touches a table outside this deployment's access — omit it, don't just leave the name in
        sql = sql_template
        for table in config.TABLES:
            placeholder = "{" + table["name"] + "}"
            if placeholder in sql:
                sql = sql.replace(placeholder, table_ref(table["name"], oracle))
        sections.append(f'Example question: "{question}"')
        sections.append(f"Example SQL: {sql}")
        sections.append("")

    return {"result": "\n".join(sections)}


# ---------------------------------------------------------------------------
# Reference document serving — the jde-development skill's actual knowledge
# content (table design rules, event rule syntax, etc.) lives here instead
# of being shipped as files to the client. The client's local skill only
# has a thin fetch script; this is what makes disabling a deployment's key
# actually stop the skill from working, not just stop future git pulls.
#
# REFERENCE_TOPICS is an explicit allowlist, not just "serve whatever's in
# the folder" — this is what prevents a topic value like "../main.py" (or
# anything else not on this list) from ever reaching the filesystem.
# ---------------------------------------------------------------------------

REFERENCES_DIR = os.path.join(os.path.dirname(__file__), "references")
REFERENCE_TOPICS = {
    "par-file-structure": "par-file-structure.md",
    "master-reference": "master-reference.md",
    "table-design": "table-design.md",
    "business-view-design": "business-view-design.md",
    "data-dictionary": "data-dictionary.md",
    "data-structure-design": "data-structure-design.md",
    "event-rules": "event-rules.md",
    "form-design-aid": "form-design-aid.md",
    "report-design-aid": "report-design-aid.md",
    "application-design": "application-design.md",
    "business-function-programming": "business-function-programming.md",
    "development-tools-overview": "development-tools-overview.md",
    "core-rules": "core-rules.md",
}


@app.get("/v1/reference/{topic}")
def get_reference(topic: str, authorization: Optional[str] = Header(default=None)):
    authenticate(authorization)  # same active/expiry gate as the DB endpoints

    filename = REFERENCE_TOPICS.get(topic)
    if filename is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown reference topic '{topic}'. Valid topics: {', '.join(sorted(REFERENCE_TOPICS))}",
        )

    path = os.path.join(REFERENCES_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Reference file for '{topic}' is missing on the server.")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    return {"content": content}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
