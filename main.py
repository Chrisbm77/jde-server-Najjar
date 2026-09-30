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
import urllib.request
import urllib.error
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
import ipaddress
from pydantic import BaseModel

import config  # your existing table/schema/business-rule definitions — move the real file here unchanged

# On Render, set CLIENTS_PATH=/etc/secrets/clients.json and upload the real
# file as a Secret File in the dashboard (Environment -> Secret Files) —
# that keeps real DB passwords out of your git repo entirely. Locally,
# it just falls back to a plain file next to this script.
CLIENTS_PATH = os.environ.get(
    "CLIENTS_PATH", os.path.join(os.path.dirname(__file__), "clients.json")
)

# Files the app WRITES to at runtime (query log, device bindings) need a
# genuinely persistent disk, not Render's default ephemeral one — a
# Secret File (like CLIENTS_PATH above) is read-only at runtime and isn't
# the right mechanism for this. Set PERSISTENT_DATA_DIR to the mount path
# of a Render Persistent Disk (e.g. /var/data) to make these survive
# every redeploy. Left unset, both fall back to a plain local file next
# to this script — fine for local testing, but will silently reset on
# every Render redeploy if left this way in production.
PERSISTENT_DATA_DIR = os.environ.get("PERSISTENT_DATA_DIR", "").strip()
_data_dir = PERSISTENT_DATA_DIR or os.path.dirname(__file__)
os.makedirs(_data_dir, exist_ok=True)

LOG_PATH = os.path.join(_data_dir, "query_log.jsonl")
MOCK_DB_PATH = os.path.join(os.path.dirname(__file__), "jde_mock.db")

# Optional: mirror every log entry to a Google Sheet via a webhook (Apps
# Script web app URL), as an easy, free, zero-infrastructure way to browse
# the query log without needing a Render Persistent Disk. Leave unset to
# skip this entirely — nothing else changes if you don't set it.
GOOGLE_SHEETS_LOG_URL = os.environ.get("GOOGLE_SHEETS_LOG_URL", "").strip()
GOOGLE_SHEETS_LOG_SECRET = os.environ.get("GOOGLE_SHEETS_LOG_SECRET", "").strip()

# Device binding store — maps API key -> the device ID that first claimed
# it. Uses the same PERSISTENT_DATA_DIR as LOG_PATH above, for the same
# reason: this file is written by the app at runtime, so it needs a real
# persistent disk to survive a redeploy, not Render's default ephemeral one.
DEVICE_BINDINGS_PATH = os.path.join(_data_dir, "device_bindings.json")
_bindings_lock = threading.Lock()

# --- Manual export/restore workflow (only needed if NOT using a Render
# Persistent Disk for PERSISTENT_DATA_DIR above) ---
#
# ADMIN_SECRET gates a separate /admin/export-bindings endpoint that dumps
# the current device_bindings.json content — copy that output BEFORE
# triggering a redeploy, then paste it into a Secret File at the path
# below (e.g. upload it as DEVICE_BINDINGS_SEED_PATH=/etc/secrets/device_bindings_seed.json
# in Render's dashboard) BEFORE the redeploy finishes starting up. On
# startup, if the live bindings file doesn't exist yet but a seed file
# does, the seed content becomes the starting bindings.
#
# This ONLY helps if you remember to do the export-then-paste dance
# before every single deploy — miss it once and that deploy's bindings
# reset anyway. A Persistent Disk needs this zero times, ever.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "").strip()
DEVICE_BINDINGS_SEED_PATH = os.environ.get("DEVICE_BINDINGS_SEED_PATH", "").strip()


def seed_device_bindings_if_needed() -> None:
    """Called once at startup. If the live bindings file doesn't exist yet
    (a fresh/ephemeral disk) but a seed file is configured and present,
    use the seed content as the starting bindings. Never overwrites an
    already-existing live bindings file."""
    if os.path.exists(DEVICE_BINDINGS_PATH):
        return  # already has real data (e.g. a genuine persistent disk) — never clobber it
    if not DEVICE_BINDINGS_SEED_PATH or not os.path.exists(DEVICE_BINDINGS_SEED_PATH):
        return  # nothing to seed from
    try:
        with open(DEVICE_BINDINGS_SEED_PATH, "r", encoding="utf-8") as f:
            seed_data = json.load(f)
        with open(DEVICE_BINDINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(seed_data, f, indent=2)
    except (json.JSONDecodeError, OSError):
        pass  # a bad seed file shouldn't crash startup — just start with no bindings


def load_device_bindings() -> dict:
    if not os.path.exists(DEVICE_BINDINGS_PATH):
        return {}
    try:
        with open(DEVICE_BINDINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}  # corrupted/unreadable bindings file — fail open on THIS file only,
        # since losing device bindings just means re-claiming, not a security bypass


def save_device_binding(api_key: str, device_id: str) -> None:
    with _bindings_lock:
        bindings = load_device_bindings()
        bindings[api_key] = device_id
        serialized = json.dumps(bindings, indent=2)
        tmp_path = DEVICE_BINDINGS_PATH + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path, DEVICE_BINDINGS_PATH)
        except OSError:
            pass  # a failed write here shouldn't break the request that triggered it


def remove_device_binding(api_key: str) -> bool:
    """Clear one key's device binding on the LIVE running server (not a
    local file — this is the actual state that matters). Returns True if
    a binding existed and was removed, False if there was nothing to
    remove."""
    with _bindings_lock:
        bindings = load_device_bindings()
        if api_key not in bindings:
            return False
        del bindings[api_key]
        serialized = json.dumps(bindings, indent=2)
        tmp_path = DEVICE_BINDINGS_PATH + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(serialized)
            os.replace(tmp_path, DEVICE_BINDINGS_PATH)
        except OSError:
            pass
        return True

MAX_ROWS = 200
ORACLE_CALL_TIMEOUT_MS = 15000

app = FastAPI(title="JDE Connector API")


@app.on_event("startup")
def _on_startup():
    seed_device_bindings_if_needed()

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
    try:
        with open(CLIENTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        # A malformed clients.json (e.g. content pasted on top of old
        # content instead of replacing it, leaving two JSON objects
        # concatenated together) previously crashed EVERY request with an
        # opaque 500 and a raw traceback — every client, not just one.
        # Fail clearly and controlled instead: one specific, actionable
        # error, not a stack trace.
        raise HTTPException(
            status_code=500,
            detail=(
                f"clients.json is not valid JSON ({e}). This blocks ALL "
                f"clients, not just one — check Render's Secret File for "
                f"clients.json; a common cause is pasting new content "
                f"without fully clearing the old content first."
            ),
        )


def get_client_ip(request: Request) -> str:
    """Get the real originating client IP, not the reverse proxy's own
    address. Render (like most hosted platforms) sits in front of this
    app — the raw TCP connection appears to come from Render's internal
    infrastructure, not the actual client. The real IP is in the
    X-Forwarded-For header instead, which can be a comma-separated chain
    if multiple proxies were involved; the FIRST entry is the original
    client, later ones are intermediate hops. Falls back to the raw
    connection IP only when there's no such header (e.g. running this
    locally with no proxy in front of it at all)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _ip_allowed(client_ip: str, allowed: list) -> bool:
    """Check client_ip against a list that can mix plain IPs and CIDR
    ranges (e.g. ["203.0.113.5", "198.51.100.0/24"])."""
    try:
        ip_obj = ipaddress.ip_address(client_ip)
    except ValueError:
        return False  # couldn't even parse the client IP — fail closed
    for entry in allowed:
        entry = entry.strip()
        try:
            if "/" in entry:
                if ip_obj in ipaddress.ip_network(entry, strict=False):
                    return True
            elif client_ip == entry:
                return True
        except ValueError:
            continue  # a malformed entry in clients.json shouldn't crash auth — just skip it
    return False


def authenticate(authorization: Optional[str], client_ip: Optional[str] = None, device_id: Optional[str] = None) -> dict:
    """Validate the Authorization header (and, if configured for this
    deployment, the source IP and/or device binding) and return the
    deployment's config dict, or raise a 401/403 with a clear reason.
    This check is the real enforcement point now — it can't be edited
    away by a client, because it never runs on their machine."""
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

    # IP allowlisting — optional, per deployment. A deployment with no
    # "allowed_ips" set has no IP restriction at all (backward compatible
    # with every existing client). One set explicitly rejects any request
    # from outside that list, REGARDLESS of whether the API key itself is
    # valid — this is what actually stops a shared key from working
    # outside the expected network, not just something that gets noticed
    # after the fact in the log.
    allowed_ips = deployment.get("allowed_ips")
    if allowed_ips:
        if client_ip is None or not _ip_allowed(client_ip, allowed_ips):
            log_query(
                deployment.get("client_name", "unknown"),
                "(auth check)",
                "refused_ip",
                error=f"request from {client_ip}, not in allowed list",
                ip=client_ip,
                device_id=device_id,
            )
            raise HTTPException(
                status_code=403,
                detail="Access denied from this network. Contact your vendor if this is unexpected.",
            )

    # Device binding — optional, per deployment, enabled via
    # "device_binding_enabled": true in clients.json. Complements IP
    # allowlisting rather than replacing it: this works regardless of
    # network location (covers remote/hybrid workers), by tying a key to
    # whichever specific client installation used it FIRST, rather than
    # to a network. The first successful request with a given key claims
    # that key's device slot; any later request with the right key but a
    # DIFFERENT device ID is refused, even though the key itself is
    # valid — the key alone stops being sufficient on its own.
    if deployment.get("device_binding_enabled"):
        if not device_id:
            log_query(
                deployment.get("client_name", "unknown"),
                "(auth check)",
                "refused_no_device_id",
                error="device binding is enabled for this deployment but the request carried no device ID",
                ip=client_ip,
                device_id=None,
            )
            raise HTTPException(
                status_code=403,
                detail="This deployment requires device binding, but no device ID was sent. Update your client.",
            )

        bindings = load_device_bindings()
        existing = bindings.get(api_key)
        if existing is None:
            save_device_binding(api_key, device_id)  # first use — claim this device slot
        elif existing != device_id:
            log_query(
                deployment.get("client_name", "unknown"),
                "(auth check)",
                "refused_device_mismatch",
                error=f"request from device {device_id}, bound to a different device",
                ip=client_ip,
                device_id=device_id,
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    "This API key is already bound to a different device. "
                    "Contact your vendor if you need it reset (e.g. after a "
                    "new computer)."
                ),
            )

    return deployment


# ---------------------------------------------------------------------------
# Logging — same shape as the old local log, now centralized across every
# client instead of scattered on their individual machines. That's a
# useful side effect of this move: one place to see usage across your
# whole client base, including refused/attempted access.
# ---------------------------------------------------------------------------


def _send_to_google_sheet(entry: dict) -> None:
    """Best-effort mirror of one log entry to the Google Sheet webhook.
    Runs in its own background thread (see log_query below) so a slow or
    unreachable sheet never adds latency to an actual query, and any
    failure here is silent — the local LOG_PATH write already happened
    regardless of whether this succeeds."""
    if not GOOGLE_SHEETS_LOG_URL:
        return
    payload = dict(entry)
    if GOOGLE_SHEETS_LOG_SECRET:
        payload["secret"] = GOOGLE_SHEETS_LOG_SECRET
    try:
        req = urllib.request.Request(
            GOOGLE_SHEETS_LOG_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass  # never let a Sheet hiccup affect anything else


def log_query(deployment_name: str, sql: str, status: str, row_count: Optional[int] = None, error: Optional[str] = None, ip: Optional[str] = None, device_id: Optional[str] = None) -> None:
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "deployment": deployment_name,
        "sql": sql,
        "status": status,
        "row_count": row_count,
        "error": error,
        "ip": ip,
        "device_id": device_id,
    }
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never break an actual query

    if GOOGLE_SHEETS_LOG_URL:
        threading.Thread(target=_send_to_google_sheet, args=(entry,), daemon=True).start()


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
        return conn, True, pool

    # No DSN configured for this deployment yet — fall back to the shared
    # mock DB. Useful for standing up and testing this server before any
    # tunnel is live.
    if not os.path.exists(MOCK_DB_PATH):
        raise RuntimeError("No DSN configured for this deployment, and no mock database found.")
    return sqlite3.connect(MOCK_DB_PATH), False, None


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


def _table_allowed_for_department(table: str, department: str) -> bool:
    """Check one table against a department's boundary: exact catalog
    membership first (precise, sourced from the real 382-table catalog),
    falling back to prefix matching only for a table not yet in that
    catalog — keeps discovery mode useful for uncatalogued tables without
    losing the department wall."""
    exact = getattr(config, "DEPARTMENT_TABLES", {}).get(department)
    if exact and table in exact:
        return True
    prefixes = getattr(config, "DEPARTMENT_TABLE_PREFIXES", {}).get(department, [])
    return any(table.startswith(p.upper()) for p in (pp.upper() for pp in prefixes))


def department_prefixes(deployment: dict) -> Optional[bool]:
    """Return False if this deployment has no department set (no
    restriction applies), or True if it does (restriction applies —
    checked per-table via _table_allowed_for_department)."""
    return deployment.get("department") is not None


def effective_allowed_tables(deployment: dict) -> set:
    """The curated-list subset visible to this deployment — used only for
    display purposes (e.g. listing tables in an error message, or in
    get_jde_schema)."""
    allowed_tables = set(config.ALLOWED_TABLES)
    department = deployment.get("department")
    if not department:
        return allowed_tables
    return {t for t in allowed_tables if _table_allowed_for_department(t, department)}


def uses_only_allowed_tables(sql: str, deployment: dict) -> bool:
    tables = referenced_tables(sql)
    if not tables:
        return False

    # Department boundary — enforced ALWAYS when a department is set,
    # regardless of discovery mode. Checked against DEPARTMENT_TABLES
    # (exact, from the real catalog) first, then DEPARTMENT_TABLE_PREFIXES
    # as a fallback for real tables not yet in that catalog.
    department = deployment.get("department")
    if department:
        has_exact_or_prefix_entry = (
            department in getattr(config, "DEPARTMENT_TABLES", {})
            or department in getattr(config, "DEPARTMENT_TABLE_PREFIXES", {})
        )
        if not has_exact_or_prefix_entry:
            return False  # department set but misconfigured — fail closed
        if not all(_table_allowed_for_department(t, department) for t in tables):
            return False

    if not config.RESTRICT_TO_APPROVED_TABLES:
        return True  # discovery mode, within whatever department boundary applied above
    return tables.issubset(config.ALLOWED_TABLES)  # curated mode: must also be reviewed/verified


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


class ResetBindingRequest(BaseModel):
    target: str  # a client_name (or partial match), or the last 8 chars of an API key


# ---------------------------------------------------------------------------
# Endpoints — these mirror the tool names the thin client exposes to Claude
# ---------------------------------------------------------------------------


def _execute_with_retry(deployment: dict, sql: str):
    """Run a query, retrying ONCE with a genuinely fresh connection if the
    first attempt fails. This specifically handles a real failure mode
    seen in production: a pooled connection that's been sitting idle can
    get silently killed by an intermediate firewall's idle-connection
    timeout (seen as DPY-4011 'the database or network closed the
    connection' / 'Connection reset by peer'), and the pool has no way to
    know that until it's actually used and fails.

    On failure, if the connection came from a pool, it's explicitly
    dropped (pool.drop) rather than left to be silently reused — this
    guarantees the retry gets a genuinely different, live connection
    rather than risking the same dead one being handed out again.

    Returns (cols, rows) on success, or raises the second attempt's
    exception if both attempts fail (a real SQL error will fail
    identically both times and surface normally; a transient dead-connection
    error has a real chance of succeeding on the retry)."""
    last_exception = None
    for attempt in range(2):
        conn = None
        pool = None
        try:
            conn, _is_oracle, pool = get_connection(deployment)
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(MAX_ROWS + 1)
            conn.close()  # releases back to the pool on success
            return cols, rows
        except Exception as e:
            last_exception = e
            if conn is not None:
                if pool is not None:
                    try:
                        pool.drop(conn)  # force it out — never let a connection that just failed go back in
                    except Exception:
                        pass
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass
    raise last_exception


@app.post("/v1/query")
def query_jde_database(req: QueryRequest, request: Request, authorization: Optional[str] = Header(default=None), x_device_id: Optional[str] = Header(default=None)):
    client_ip = get_client_ip(request)
    deployment = authenticate(authorization, client_ip, x_device_id)
    name = deployment.get("client_name", "unknown")
    sql = req.sql

    if not is_read_only(sql):
        log_query(name, sql, "refused_write", ip=client_ip, device_id=x_device_id)
        return {"result": "REFUSED: only single SELECT statements are permitted."}

    if not uses_only_allowed_tables(sql, deployment):
        log_query(name, sql, "refused_table", ip=client_ip, device_id=x_device_id)
        return {"result": "REFUSED: you don't have access to that data."}

    try:
        cols, rows = _execute_with_retry(deployment, sql)
    except Exception as e:
        log_query(name, sql, "error", error=str(e), ip=client_ip, device_id=x_device_id)
        return {"result": f"DATABASE ERROR: {e}"}

    if not rows:
        log_query(name, sql, "executed", row_count=0, ip=client_ip, device_id=x_device_id)
        return {"result": "No matching records were found."}

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    log_query(name, sql, "executed", row_count=len(rows), ip=client_ip, device_id=x_device_id)

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
def get_jde_schema(request: Request, authorization: Optional[str] = Header(default=None), x_device_id: Optional[str] = Header(default=None)):
    deployment = authenticate(authorization, get_client_ip(request), x_device_id)
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
def get_reference(topic: str, request: Request, authorization: Optional[str] = Header(default=None), x_device_id: Optional[str] = Header(default=None)):
    authenticate(authorization, get_client_ip(request), x_device_id)  # same active/expiry/IP/device gate as the DB endpoints

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


@app.get("/admin/export-bindings")
def export_device_bindings(format: Optional[str] = None, x_admin_secret: Optional[str] = Header(default=None)):
    """Dump the current device bindings, with client names attached for
    readability. Requires ADMIN_SECRET to be set as an environment
    variable on this server AND sent as the X-Admin-Secret header — this
    is a completely separate credential from any client API key, since
    no regular client key should ever be able to see every deployment's
    device bindings, only their own key's enforcement.

    Add ?format=table to the URL for a genuinely readable plain-text
    table (real line breaks, not JSON-escaped \\n) — better for reading
    directly in a terminal. Without it, returns the usual JSON.

    Manual export/restore workflow (only needed without a real Persistent
    Disk):
      1. Before a redeploy, call this endpoint (WITHOUT ?format=table —
         you need the real JSON "bindings" object for this step) and copy
         the full response.
      2. Paste it into the Secret File at DEVICE_BINDINGS_SEED_PATH in
         Render's dashboard, replacing whatever was there.
      3. Trigger the redeploy. On startup, the seed file becomes the
         starting bindings (see seed_device_bindings_if_needed above).
      4. Repeat before every future deploy — this step is not automatic.
    """
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=404,
            detail="Admin export is not configured on this server (ADMIN_SECRET is unset).",
        )
    if not x_admin_secret or not secrets.compare_digest(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing admin secret.")

    bindings = load_device_bindings()
    clients = load_clients()
    key_to_name = {k: v.get("client_name", "unknown") for k, v in clients.items()}

    readable = [
        {"client_name": key_to_name.get(k, "(key not in clients.json)"), "api_key": k, "device_id": v}
        for k, v in bindings.items()
    ]

    # Build a clean, aligned plain-text table — easy to read directly from
    # a terminal, rather than parsing the JSON by eye.
    headers = ("Name", "API Key", "Device ID")
    rows = [(r["client_name"], r["api_key"], r["device_id"]) for r in readable]
    col_widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(3)
    ]
    def fmt_row(cells):
        return "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(cells))
    table_lines = [fmt_row(headers), fmt_row(["-" * w for w in col_widths])]
    table_lines += [fmt_row(row) for row in rows]
    if not rows:
        table_lines.append("(no active bindings)")
    table = "\n".join(table_lines)

    if format == "table":
        return PlainTextResponse(table)

    return {
        "bindings": bindings,  # paste THIS whole object into the seed file's content
        "readable": readable,
        "table": table,  # same data, formatted as an aligned text table for easy reading
    }


@app.post("/admin/reset-binding")
def reset_device_binding(req: ResetBindingRequest, x_admin_secret: Optional[str] = Header(default=None)):
    """Clear one client's device binding on the LIVE server — this is the
    real fix for 'this person got a new laptop' or 'I need to test with
    their key myself first,' since it acts on the server's actual current
    state, not a local file that has no effect on what's actually
    deployed. Same ADMIN_SECRET gate as the export endpoint.

    target can be a client_name (or partial, case-insensitive match) or
    the last 8 characters of an API key — same convenience matching as
    the local reset_device.py script, so you can use whichever you have
    on hand.
    """
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=404,
            detail="Admin reset is not configured on this server (ADMIN_SECRET is unset).",
        )
    if not x_admin_secret or not secrets.compare_digest(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing admin secret.")

    bindings = load_device_bindings()
    clients = load_clients()
    key_to_name = {k: v.get("client_name", "unknown") for k, v in clients.items()}

    target = req.target.strip()
    matches = [
        api_key for api_key in bindings
        if target.lower() in key_to_name.get(api_key, "").lower() or api_key.endswith(target)
    ]

    if not matches:
        raise HTTPException(status_code=404, detail=f"No active binding found matching '{target}'.")
    if len(matches) > 1:
        names = [key_to_name.get(k, "unknown") for k in matches]
        raise HTTPException(
            status_code=409,
            detail=f"'{target}' matches more than one binding ({', '.join(names)}) — be more specific.",
        )

    api_key = matches[0]
    name = key_to_name.get(api_key, "unknown")
    remove_device_binding(api_key)

    return {"result": f"Device binding for '{name}' has been reset. Their next request will claim a new device."}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
