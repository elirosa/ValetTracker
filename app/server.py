"""ValetTracker — HTTP layer.

Standard library only. ThreadingHTTPServer handles a valet stand's traffic
(a few tablets and phones) without breaking a sweat, and it keeps the
container image dependency-free.
"""

import csv
import io
import json
import mimetypes
import os
import re
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
import db  # noqa: E402

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
LOT_NAME = os.environ.get("LOT_NAME", "Main Lot")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "ValetTracker"
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", content_type="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code=200, extra=None):
        self._send(code, json.dumps(data), "application/json; charset=utf-8", extra)

    def _error(self, code, message):
        self._json({"error": message}, code)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("body must be JSON")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _session(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(auth.COOKIE_NAME)
        return auth.read_token(morsel.value) if morsel else None

    # --- routing ----------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        try:
            if path == "/healthz":
                return self._json({"status": "ok", "tickets_open": len(db.list_tickets("open"))})

            if path == "/api/session" and method == "POST":
                return self.login()
            if path == "/api/session" and method == "DELETE":
                return self._json({"ok": True}, extra={"Set-Cookie": auth.clear_cookie_header()})
            if path == "/api/session" and method == "GET":
                session = self._session()
                if not session:
                    return self._json({"authenticated": False, "lot": LOT_NAME}, 200)
                return self._json({
                    "authenticated": True,
                    "role": session["role"],
                    "lot": LOT_NAME,
                    "expires": session["exp"],
                })

            if path.startswith("/api/"):
                session = self._session()
                if not session:
                    return self._error(401, "Sign in to continue.")
                return self.api(method, path, params, session)

            if method == "GET":
                return self.static(path)
            return self._error(405, "Method not allowed.")

        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.log_message("unhandled error on %s %s: %r", method, path, exc)
            return self._error(500, "Something broke on the server. Try again.")

    # --- endpoints --------------------------------------------------------

    def login(self):
        ip = self._client_ip()
        if auth.locked_out(ip):
            return self._error(429, "Too many wrong PINs. Wait five minutes and try again.")
        body = self._body()
        role = auth.role_for_pin(str(body.get("pin", "")))
        if not role:
            auth.record_failure(ip)
            return self._error(401, "That PIN doesn't match.")
        auth.clear_failures(ip)
        return self._json(
            {"authenticated": True, "role": role, "lot": LOT_NAME},
            extra={"Set-Cookie": auth.cookie_header(auth.make_token(role))},
        )

    def api(self, method, path, params, session):
        actor = session["role"]

        if path == "/api/tickets" and method == "GET":
            scope = (params.get("scope", ["open"])[0] or "open").lower()
            if scope not in ("open", "closed", "all"):
                scope = "open"
            search = params.get("q", [""])[0]
            limit = min(int(params.get("limit", ["200"])[0] or 200), 1000)
            return self._json({
                "tickets": db.list_tickets(scope, search, limit),
                "stats": db.stats(),
                "next_ticket_no": db.next_ticket_no(),
                "server_time": db.now(),
            })

        if path == "/api/tickets" and method == "POST":
            fields = self._clean(self._body())
            if not fields.get("ticket_no"):
                fields["ticket_no"] = db.next_ticket_no()
            if not fields.get("plate") and not fields.get("model"):
                raise ValueError("Add a plate or a model so the car can be identified.")
            if db.open_ticket_with_number(fields["ticket_no"]):
                raise ValueError(f"Ticket {fields['ticket_no']} is already on the lot.")
            return self._json({"ticket": db.create_ticket(fields, actor)}, 201)

        if path == "/api/stats" and method == "GET":
            return self._json(db.stats())

        if path == "/api/export.csv" and method == "GET":
            return self.export_csv(params)

        match = re.fullmatch(r"/api/tickets/(\d+)", path)
        if match:
            ticket_id = int(match.group(1))
            ticket = db.get_ticket(ticket_id)
            if not ticket:
                return self._error(404, "That ticket no longer exists.")
            if method == "GET":
                return self._json({"ticket": ticket, "events": db.ticket_events(ticket_id)})
            if method == "PATCH":
                fields = self._clean(self._body())
                new_no = fields.get("ticket_no")
                if new_no and new_no != ticket["ticket_no"]:
                    clash = db.open_ticket_with_number(new_no)
                    if clash and clash["id"] != ticket_id:
                        raise ValueError(f"Ticket {new_no} is already on the lot.")
                return self._json({"ticket": db.update_ticket(ticket_id, fields, actor)})
            if method == "DELETE":
                if actor != "admin":
                    return self._error(403, "Only the admin PIN can delete tickets.")
                db.delete_ticket(ticket_id)
                return self._json({"ok": True})
            return self._error(405, "Method not allowed.")

        match = re.fullmatch(r"/api/tickets/(\d+)/status", path)
        if match and method == "POST":
            ticket_id = int(match.group(1))
            if not db.get_ticket(ticket_id):
                return self._error(404, "That ticket no longer exists.")
            status = str(self._body().get("status", "")).lower()
            if status not in db.STATUSES:
                raise ValueError("Unknown status.")
            return self._json({"ticket": db.set_status(ticket_id, status, actor)})

        return self._error(404, "No such endpoint.")

    def export_csv(self, params):
        scope = (params.get("scope", ["all"])[0] or "all").lower()
        rows = db.list_tickets(scope if scope in ("open", "closed", "all") else "all",
                               params.get("q", [""])[0], 5000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ticket", "plate", "color", "make", "model", "spot", "status",
                         "parked_at", "requested_at", "ready_at", "delivered_at",
                         "minutes_on_lot", "minutes_to_retrieve", "notes"])
        for t in rows:
            end = t["closed_at"] or db.now()
            on_lot = round((end - t["created_at"]) / 60, 1)
            retrieve = ""
            if t["requested_at"] and t["closed_at"]:
                retrieve = round((t["closed_at"] - t["requested_at"]) / 60, 1)
            writer.writerow([
                t["ticket_no"], t["plate"], t["color"], t["make"], t["model"], t["spot"],
                t["status"], iso(t["created_at"]), iso(t["requested_at"]),
                iso(t["ready_at"]), iso(t["closed_at"]), on_lot, retrieve, t["notes"],
            ])
        return self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="valettracker.csv"'})

    @staticmethod
    def _clean(body):
        fields = {}
        for key in ("ticket_no", "plate", "make", "model", "color", "spot", "phone", "notes"):
            if key in body:
                value = str(body[key] if body[key] is not None else "").strip()[:200]
                if key in ("ticket_no", "plate", "spot"):
                    value = value.upper()
                fields[key] = value
        return fields

    # --- static files -----------------------------------------------------

    def static(self, path):
        rel = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as handle:
            body = handle.read()
        cache = "no-cache" if full.endswith(".html") else "public, max-age=300"
        return self._send(200, body, ctype, {"Cache-Control": cache})


def iso(ts):
    if not ts:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def main():
    if not auth.STAFF_PIN and not auth.ADMIN_PIN:
        sys.stderr.write(
            "ValetTracker will not start: set STAFF_PIN (and ideally ADMIN_PIN) "
            "in the environment.\n")
        sys.exit(1)
    if auth.secret_is_ephemeral():
        sys.stderr.write(
            "Warning: SECRET_KEY is unset, so a random one was generated. "
            "Everyone gets signed out whenever the container restarts.\n")

    db.connect()
    sys.stderr.write(f"ValetTracker listening on {HOST}:{PORT} for lot '{LOT_NAME}' "
                     f"(database at {db.DB_PATH})\n")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
