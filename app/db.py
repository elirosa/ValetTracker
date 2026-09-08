"""SQLite storage for ValetTracker.

One connection, guarded by a lock. Ticket volume at a valet stand is tiny
(hundreds per night at most), so a single writer is plenty and avoids the
whole connection-pool question.
"""

import os
import re
import sqlite3
import threading
import time

DB_PATH = os.environ.get("DB_PATH", "/data/valettracker.db")

_conn = None
_lock = threading.RLock()

STATUSES = ("parked", "requested", "ready", "delivered")
OPEN_STATUSES = ("parked", "requested", "ready")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no    TEXT NOT NULL,
    plate        TEXT NOT NULL DEFAULT '',
    make         TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    color        TEXT NOT NULL DEFAULT '',
    spot         TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'parked',
    created_at   INTEGER NOT NULL,
    requested_at INTEGER,
    ready_at     INTEGER,
    closed_at    INTEGER,
    created_by   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    actor     TEXT NOT NULL DEFAULT '',
    at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at);
CREATE INDEX IF NOT EXISTS idx_events_ticket   ON events(ticket_id);

-- Ticket numbers must be unique among cars still on the lot, but numbered
-- tags get reused night after night, so uniqueness cannot be global.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_open_no
    ON tickets(ticket_no) WHERE status != 'delivered';
"""


def now() -> int:
    return int(time.time())


def connect():
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        parent = os.path.dirname(DB_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(SCHEMA)
        _conn.commit()
        return _conn


def query(sql, params=()):
    with _lock:
        return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    with _lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def log_event(ticket_id, kind, actor="", detail=""):
    execute(
        "INSERT INTO events (ticket_id, kind, detail, actor, at) VALUES (?,?,?,?,?)",
        (ticket_id, kind, detail, actor, now()),
    )


# --- tickets ---------------------------------------------------------------

def next_ticket_no() -> str:
    """Suggest the next number after the highest numeric tag in use today.

    Stands that use pre-printed tags will type their own number over this;
    it is only a convenience for stands that don't.
    """
    rows = query(
        "SELECT ticket_no FROM tickets WHERE created_at > ? ORDER BY id DESC LIMIT 500",
        (now() - 86400,),
    )
    highest = 0
    width = 4
    for row in rows:
        m = re.fullmatch(r"\s*(\d+)\s*", row["ticket_no"])
        if m:
            value = int(m.group(1))
            if value > highest:
                highest = value
                width = max(4, len(m.group(1)))
    return str(highest + 1).zfill(width)


def get_ticket(ticket_id):
    return query_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))


def open_ticket_with_number(ticket_no):
    return query_one(
        "SELECT * FROM tickets WHERE ticket_no = ? AND status != 'delivered'",
        (ticket_no,),
    )


def create_ticket(fields, actor=""):
    ts = now()
    cur = execute(
        """INSERT INTO tickets
           (ticket_no, plate, make, model, color, spot, phone, notes,
            status, created_at, created_by)
           VALUES (?,?,?,?,?,?,?,?,'parked',?,?)""",
        (
            fields["ticket_no"], fields.get("plate", ""), fields.get("make", ""),
            fields.get("model", ""), fields.get("color", ""), fields.get("spot", ""),
            fields.get("phone", ""), fields.get("notes", ""), ts, actor,
        ),
    )
    log_event(cur.lastrowid, "parked", actor)
    return get_ticket(cur.lastrowid)


def list_tickets(scope="open", q="", limit=200):
    sql = "SELECT * FROM tickets WHERE "
    params = []
    if scope == "open":
        sql += "status != 'delivered'"
    elif scope == "closed":
        sql += "status = 'delivered'"
    else:
        sql += "1=1"

    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        sql += (" AND (ticket_no LIKE ? OR plate LIKE ? OR make LIKE ? OR model LIKE ?"
                " OR color LIKE ? OR spot LIKE ? OR notes LIKE ?)")
        params.extend([like] * 7)

    # Cars whose owner is waiting float to the top; then oldest first, because
    # the guest who has waited longest is the one about to get annoyed.
    # Waiting guests first, and within them the longest wait on top — that is
    # the guest about to walk over and ask. Cars still parked fall back to
    # newest first, since a valet checking the list just parked one.
    sql += """ ORDER BY CASE status
                    WHEN 'requested' THEN 0
                    WHEN 'ready'     THEN 1
                    WHEN 'parked'    THEN 2
                    ELSE 3 END,
               CASE WHEN status IN ('requested','ready') THEN requested_at END ASC,
               created_at DESC
               LIMIT ?"""
    params.append(int(limit))
    return query(sql, params)


def set_status(ticket_id, status, actor=""):
    ts = now()
    if status == "requested":
        execute(
            "UPDATE tickets SET status='requested', requested_at=?, ready_at=NULL,"
            " closed_at=NULL WHERE id=?", (ts, ticket_id))
    elif status == "ready":
        execute(
            "UPDATE tickets SET status='ready', ready_at=?, requested_at=COALESCE(requested_at,?),"
            " closed_at=NULL WHERE id=?", (ts, ts, ticket_id))
    elif status == "delivered":
        execute("UPDATE tickets SET status='delivered', closed_at=? WHERE id=?", (ts, ticket_id))
    elif status == "parked":
        execute(
            "UPDATE tickets SET status='parked', requested_at=NULL, ready_at=NULL,"
            " closed_at=NULL WHERE id=?", (ticket_id,))
    else:
        raise ValueError("unknown status")
    log_event(ticket_id, status, actor)
    return get_ticket(ticket_id)


def update_ticket(ticket_id, fields, actor=""):
    allowed = ("ticket_no", "plate", "make", "model", "color", "spot", "phone", "notes")
    sets, params = [], []
    for key in allowed:
        if key in fields:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if sets:
        params.append(ticket_id)
        execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id = ?", params)
        log_event(ticket_id, "edited", actor, ", ".join(f.split(" ")[0] for f in sets))
    return get_ticket(ticket_id)


def delete_ticket(ticket_id):
    execute("DELETE FROM events WHERE ticket_id = ?", (ticket_id,))
    execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))


def ticket_events(ticket_id):
    return query("SELECT * FROM events WHERE ticket_id = ? ORDER BY at ASC, id ASC", (ticket_id,))


def stats():
    counts = {s: 0 for s in STATUSES}
    for row in query("SELECT status, COUNT(*) n FROM tickets GROUP BY status"):
        counts[row["status"]] = row["n"]

    since = now() - 86400
    row = query_one(
        """SELECT COUNT(*) n, AVG(closed_at - requested_at) avg_wait
           FROM tickets
           WHERE status='delivered' AND closed_at >= ? AND requested_at IS NOT NULL""",
        (since,),
    ) or {}
    avg_wait = row.get("avg_wait")
    return {
        "on_lot": counts["parked"],
        "requested": counts["requested"],
        "ready": counts["ready"],
        "delivered_24h": row.get("n") or 0,
        # An average of 0 is a real number, not a missing one.
        "avg_retrieval_seconds": int(avg_wait) if avg_wait is not None else None,
    }
