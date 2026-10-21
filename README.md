# Clinic Booking API

A booking API for a clinic — patients list open appointment slots, book one, and
look the booking up again — plus a small web page that uses it. Built to the
contract published at <https://koyaacademy.com/assessment-api>.

Python · Django 5 · PostgreSQL · deployed on Render with a Neon database.

- **Live API:** _see DEPLOY.md once deployed_
- **Live page:** the same base URL, at `/`

## How it works

| Piece | File |
| --- | --- |
| Endpoints, validation, limits | [`clinic/views.py`](clinic/views.py) |
| Tables and the anti-double-booking index | [`clinic/models.py`](clinic/models.py) |
| URL map | [`clinic/urls.py`](clinic/urls.py) |
| Web page | [`clinic/templates/clinic/index.html`](clinic/templates/clinic/index.html) |
| Tests, grouped by level | [`clinic/tests/test_api.py`](clinic/tests/test_api.py) |

The contract paths live at the root (`/slots`, `/bookings`, …) and are also
mirrored under `/api/`, so either base URL behaves the same.

## Endpoints

Every request needs an `X-API-Key` header. Missing or wrong gives `401`. Every
error body is `{"error": "<message>"}`.

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/reset` | Replace all slots, delete every booking |
| `GET` | `/slots` | All slots, earliest first; `?provider=` filters exactly |
| `POST` | `/bookings` | Book a slot |
| `GET` | `/bookings?email=` | One person's bookings, paged |
| `GET` | `/bookings/{id}` | One booking |
| `DELETE` | `/bookings/{id}` | Cancel a booking |

`GET /healthz` needs no key and is what Render's health check uses.

## The five levels

**Level 1 — list, book, look up.** `/slots` returns every slot sorted by
`start`, each with an `available` flag. `POST /bookings` returns `201` and the
booking with status `confirmed`. `GET /bookings/{id}` returns it again, `404` if
there is no such booking.

**Level 2 — key and validation.** A `require_api_key` decorator guards every
endpoint and compares the key with `secrets.compare_digest`. Bookings are
rejected with `400` when `slot_id`, `name` or `email` is missing, empty or
whitespace, or when the email does not pass Django's email validator; `404` when
the slot does not exist; `400` when the slot has already started. `name` and
`email` are trimmed, and `email` is stored lower case.

**Level 3 — one booking per slot, ever.** The guarantee is a partial unique
index in Postgres:

```python
models.UniqueConstraint(
    fields=["slot"], condition=models.Q(status="confirmed"), name="uniq_confirmed_slot"
)
```

Two simultaneous requests both try to insert; the database lets exactly one
through and the other raises `IntegrityError`, which becomes a `409`. Because the
rule lives in the database rather than in a check-then-write in Python, it holds
across every worker and process — there is no window between checking and
writing for a second request to slip through. Cancelling sets `status` to
`cancelled`, which takes the row out of the index and frees the slot for
somebody else.

**Level 4 — safe retries and paging.** An optional `Idempotency-Key` header
gets a row storing a SHA-256 of the canonicalised body along with the response
that was sent. The same key with the same body replays that stored response —
same status, same booking `id`, no second booking. The same key with a different
body is `422`. Canonical JSON means re-ordered keys still count as the same
body. If a retry arrives while the first request is still in flight, it waits
for the original rather than racing it.

`GET /bookings?email=` uses keyset (cursor) pagination ordered by
`(created_at, id)`: the cursor is a base64 of the last row's sort key, so pages
stay correct even when bookings are added while someone is paging. `limit` is 1
to 50, default 10, anything else `400`. The last page has `next_cursor: null`.

**Level 5 — no spamming.** At most 5 attempts per email per minute, failures
included, so a caller cannot probe for free by sending requests that fail. The
6th gets `429` with a `Retry-After` header in whole seconds, computed from when
the oldest attempt in the window ages out. Attempts are rows in the database, so
the limit is shared by every worker; a Postgres advisory lock keyed on the email
keeps the count exact under simultaneous requests, and only ever blocks that one
email.

## The web page

`/` serves a page that lists slots, books one, shows the confirmation with its
reference, and lists and cancels a person's bookings.

It calls the API under `/web/`, which is the same views with the key attached on
the server. That way the browser never holds the API key — shipping it in
JavaScript would hand a working credential to anyone who opened the page. The
page also sends a fresh `Idempotency-Key` per attempt, so a double click or a
retried request cannot produce two bookings.

## Running it locally

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
cp .env.example .env
.venv/Scripts/python.exe manage.py migrate
.venv/Scripts/python.exe manage.py runserver
```

Without `DATABASE_URL` it uses SQLite, which is fine for everything except the
two concurrency tests — SQLite allows only one writer, so those need Postgres.

With conda (no Docker needed, which is how this was verified):

```bash
conda create -n pgtest -c conda-forge postgresql -y
PG=~/anaconda3/envs/pgtest/Library/bin
"$PG/initdb.exe" -D ./pgdata -U postgres --pwfile=<(echo postgres) -A md5
"$PG/pg_ctl.exe" -D ./pgdata -l pg.log -o "-p 5433 -c listen_addresses=127.0.0.1" start
```

Or with Docker:

```bash
docker run -d --name clinic-pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 postgres:16
```

Either way:

```bash
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/postgres DATABASE_NO_SSL=1 \
  .venv/Scripts/python.exe manage.py test clinic
```

## Environment variables

| Name | Required | Notes |
| --- | --- | --- |
| `DATABASE_URL` | in production | Postgres URL; SQLite is used when unset |
| `API_KEYS` | in production | Comma-separated; the first is the one the page uses |
| `DJANGO_SECRET_KEY` | in production | Render generates one |
| `DJANGO_DEBUG` | no | Off unless set to `1` |
| `DATABASE_NO_SSL` | no | Set to `1` for a local Postgres container |

## Tests

```bash
.venv/Scripts/python.exe manage.py test clinic
```

66 tests, grouped by level, including two that run a dozen genuinely concurrent
requests at one slot and assert that exactly one wins.

All 66 pass against PostgreSQL 18. On SQLite the two concurrency tests fail with
"database is locked" — SQLite allows only one writer, so they need Postgres.

There is also `verify.py`, which exercises the whole contract over HTTP against
a deployed URL the way a grader would:

```bash
python verify.py https://your-service.onrender.com YOUR-API-KEY
```
