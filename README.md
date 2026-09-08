# ValetTracker

Ticket tracking for a valet stand. Park a car against a tag number, mark it
when the guest turns up, mark it again when the keys go back. Built to be used
one-handed on a phone in the dark, which is where valet work actually happens.

No database server, no pip dependencies, no build step. One container, one
SQLite file on a volume.

## Deploying in Komodo

1. Push this repo to GitHub. The included workflow builds `linux/amd64` and
   `linux/arm64` and pushes to `ghcr.io/<owner>/valettracker:latest` on every
   commit to `main`.
2. If you want Komodo to pull a private package, make the package public under
   **Packages → valettracker → Package settings**, or add a GHCR registry
   credential in Komodo.
3. In Komodo, create a **Stack**, paste in `docker-compose.yml`, and paste
   `.env.example` into the Environment box with your own values filled in.
4. Deploy, then open `http://<host>:8080` and sign in with `STAFF_PIN`.

Generate the secret before you deploy:

```bash
openssl rand -hex 32
```

### Running it locally instead

```bash
cp .env.example .env          # edit SECRET_KEY and the PINs
docker compose up -d
```

To build from source rather than pull, add `build: .` to the service in
`docker-compose.yml`, or:

```bash
docker build -t valettracker .
docker run -p 8080:8000 -v valet-data:/data \
  -e SECRET_KEY=$(openssl rand -hex 32) -e STAFF_PIN=1234 valettracker
```

### Without Docker

```bash
SECRET_KEY=dev STAFF_PIN=1234 DB_PATH=./valet.db python3 app/server.py
```

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `STAFF_PIN` | — | Valet sign-in. Required; the app refuses to start without it or `ADMIN_PIN`. |
| `ADMIN_PIN` | empty | Adds a manager who can also delete tickets. |
| `SECRET_KEY` | random | Signs session cookies. Random means everyone is signed out on restart. |
| `LOT_NAME` | `Main Lot` | Shown in the header. |
| `SESSION_HOURS` | `12` | How long a sign-in lasts. |
| `COOKIE_SECURE` | `false` | Set true when served over HTTPS. |
| `HOST_PORT` | `8080` | Host port. The container always listens on 8000. |
| `DB_PATH` | `/data/valettracker.db` | SQLite file. Keep it on the volume. |

## How a ticket moves

```
parked ──"Guest is here"──> requested ──"Car is out front"──> ready
   ^                             │                              │
   └────────── "Undo" ───────────┴──── "Handed over" ──────> delivered
```

Tickets sort so that whoever has waited longest sits at the top, because that
is the guest about to walk over and ask. Tag numbers are unique among cars
still on the lot but reusable once a car is delivered, which is how numbered
tags actually get used night after night.

## Data

Everything lives in one SQLite file on the `valettracker-data` volume. Every
status change is written to an `events` table, so a ticket carries its own
timeline. Back it up by copying the file:

```bash
docker compose exec valettracker \
  python -c "import sqlite3;sqlite3.connect('/data/valettracker.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp valettracker:/data/backup.db ./backup.db
```

`Export CSV` in the header pulls the same data with wait times worked out per
ticket.

## API

Everything under `/api` needs the session cookie from `POST /api/session`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Liveness, no auth. Returns open ticket count. |
| `POST` | `/api/session` | Sign in with `{"pin": "1234"}`. |
| `DELETE` | `/api/session` | Sign out. |
| `GET` | `/api/tickets?scope=open\|closed\|all&q=` | List with stats and next tag number. |
| `POST` | `/api/tickets` | Park a car. Auto-numbers if `ticket_no` is omitted. |
| `GET` | `/api/tickets/{id}` | One ticket plus its event timeline. |
| `PATCH` | `/api/tickets/{id}` | Edit details. |
| `POST` | `/api/tickets/{id}/status` | `{"status": "requested\|ready\|delivered\|parked"}`. |
| `DELETE` | `/api/tickets/{id}` | Admin PIN only. |
| `GET` | `/api/export.csv` | CSV with wait times. |

## Security notes

PINs are shared credentials, which suits a shared podium but means this is not
something to expose to the open internet as-is. Put it behind your reverse
proxy or a VPN, and set `COOKIE_SECURE=true` once it is on HTTPS. Sign-in is
rate limited to 10 wrong PINs per IP, then a five minute lockout.

## Worth adding next

- A guest-facing link or QR code so the guest can request their own car
- SMS when the car is out front
- Per-valet sign-in, so the event log names a person rather than a role
- Multiple lots in one instance
