"""Exercise every level of the contract against a running deployment.

    python verify.py https://your-service.onrender.com YOUR-API-KEY

Uses only the standard library, and talks to the API exactly the way a grader
would: over HTTP, with the API key in a header. It calls POST /reset, so point
it at your own deployment, not somebody else's.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PASS, FAIL = [], []


def call(base, key, method, path, body=None, headers=None, timeout=90):
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if key is not None:
        request.add_header("X-API-Key", key)
    for header, value in (headers or {}).items():
        request.add_header(header, value)
    # Header names are case-insensitive, and HTTP/2 lowercases them, so a proxy
    # in front of the app can hand back "retry-after" where the app sent
    # "Retry-After". Key everything in lower case and look it up that way.
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return (response.status,
                    (json.loads(raw) if raw else {}),
                    {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw[:200]}
        return exc.code, parsed, {k.lower(): v for k, v in exc.headers.items()}


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(("  ok   " if condition else "  FAIL ") + label + (" -- " + str(detail) if detail and not condition else ""))


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base, key = sys.argv[1].rstrip("/"), sys.argv[2]
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=30)

    slots = [
        {"id": "s{}".format(i), "provider": "Dr Ade" if i < 8 else "Dr Bola",
         "start": iso(soon + timedelta(hours=i)), "end": iso(soon + timedelta(hours=i, minutes=30))}
        for i in range(1, 13)
    ]
    slots.append({"id": "past", "provider": "Dr Ade",
                  "start": iso(now - timedelta(days=2)), "end": iso(now - timedelta(days=2, hours=-1))})

    stamp = str(int(time.time()))
    who = "verify-{}@example.com".format(stamp)

    print("\nWaking the service (a free instance can take ~50s to start)...")
    status, _, _ = call(base, key, "GET", "/healthz")
    print("  /healthz -> {}\n".format(status))

    # -- level 2: the key ---------------------------------------------------
    print("Level 2 - API key")
    status, body, _ = call(base, None, "GET", "/slots")
    check("no key is 401", status == 401, status)
    check("401 body has an error key", "error" in body, body)
    status, _, _ = call(base, "definitely-wrong", "GET", "/slots")
    check("wrong key is 401", status == 401, status)

    # -- level 1 ------------------------------------------------------------
    print("\nLevel 1 - list, book, look up")
    status, _, _ = call(base, key, "POST", "/reset", {"slots": slots})
    check("reset is 200", status == 200, status)

    status, body, _ = call(base, key, "GET", "/slots")
    listed = body.get("slots", [])
    check("slots is 200", status == 200, status)
    check("every slot came back", len(listed) == len(slots), len(listed))
    check("sorted by start", [s["start"] for s in listed] == sorted(s["start"] for s in listed))
    check("slots carry availability", all("available" in s for s in listed))
    check("past slots are listed too", any(s["id"] == "past" for s in listed))

    status, body, _ = call(base, key, "GET", "/slots?provider=Dr%20Bola")
    check("provider filter is exact", status == 200 and all(s["provider"] == "Dr Bola" for s in body.get("slots", [])), body)

    status, booking, _ = call(base, key, "POST", "/bookings",
                              {"slot_id": "s1", "name": "Ada Obi", "email": who})
    check("booking is 201", status == 201, booking)
    check("status is confirmed", booking.get("status") == "confirmed", booking)
    check("booking has every field",
          set(booking) >= {"id", "slot_id", "name", "email", "status", "created_at"}, booking)
    first_id = booking.get("id")

    status, fetched, _ = call(base, key, "GET", "/bookings/{}".format(first_id))
    check("look up by id is 200", status == 200, status)
    check("look up returns the same booking", fetched.get("id") == first_id, fetched)

    status, _, _ = call(base, key, "GET", "/bookings/no-such-booking")
    check("unknown booking is 404", status == 404, status)

    status, body, _ = call(base, key, "GET", "/slots")
    booked = {s["id"]: s["available"] for s in body.get("slots", [])}
    check("booked slot is unavailable", booked.get("s1") is False, booked.get("s1"))

    # -- level 2: validation ------------------------------------------------
    print("\nLevel 2 - validation")
    cases = [
        ("missing slot_id", {"name": "A", "email": "a{}@example.com".format(stamp)}, 400),
        ("empty name", {"slot_id": "s2", "name": "  ", "email": "b{}@example.com".format(stamp)}, 400),
        ("invalid email", {"slot_id": "s2", "name": "A", "email": "not-an-email"}, 400),
        ("unknown slot", {"slot_id": "nope", "name": "A", "email": "c{}@example.com".format(stamp)}, 404),
        ("slot already started", {"slot_id": "past", "name": "A", "email": "d{}@example.com".format(stamp)}, 400),
    ]
    for label, body_in, expected in cases:
        status, body, _ = call(base, key, "POST", "/bookings", body_in)
        check("{} is {}".format(label, expected), status == expected, "{} {}".format(status, body))

    trimmed_email = "  TRIM{}@Example.COM  ".format(stamp)
    status, body, _ = call(base, key, "POST", "/bookings",
                           {"slot_id": "s2", "name": "  Ada Obi  ", "email": trimmed_email})
    check("name is trimmed", body.get("name") == "Ada Obi", body)
    check("email is trimmed and lowercased",
          body.get("email") == trimmed_email.strip().lower(), body)

    # -- level 3 ------------------------------------------------------------
    print("\nLevel 3 - no double booking, and cancelling")
    status, body, _ = call(base, key, "POST", "/bookings",
                           {"slot_id": "s1", "name": "Someone Else", "email": "e{}@example.com".format(stamp)})
    check("second booking on a taken slot is 409", status == 409, "{} {}".format(status, body))

    # Genuinely simultaneous requests, each with its own email so the rate
    # limit plays no part.
    racers = 8
    barrier = threading.Barrier(racers)
    results, lock = [], threading.Lock()

    def race(index):
        payload = {"slot_id": "s9", "name": "Racer {}".format(index),
                   "email": "race{}-{}@example.com".format(index, stamp)}
        barrier.wait(timeout=60)
        status, _, _ = call(base, key, "POST", "/bookings", payload)
        with lock:
            results.append(status)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    check("exactly one of {} simultaneous requests won".format(racers),
          results.count(201) == 1, results)
    check("the rest got 409", results.count(409) == racers - 1, results)

    status, body, _ = call(base, key, "DELETE", "/bookings/{}".format(first_id))
    check("cancel is 200", status == 200, status)
    check("cancel sets status", body.get("status") == "cancelled", body)

    status, body, _ = call(base, key, "GET", "/slots")
    freed = {s["id"]: s["available"] for s in body.get("slots", [])}
    check("cancelling frees the slot", freed.get("s1") is True, freed.get("s1"))

    status, _, _ = call(base, key, "DELETE", "/bookings/{}".format(first_id))
    check("cancelling twice is 409", status == 409, status)
    status, _, _ = call(base, key, "DELETE", "/bookings/no-such-booking")
    check("cancelling an unknown booking is 404", status == 404, status)

    # -- level 4 ------------------------------------------------------------
    print("\nLevel 4 - idempotency and paging")
    idem_email = "idem-{}@example.com".format(stamp)
    payload = {"slot_id": "s3", "name": "Ada Obi", "email": idem_email}
    headers = {"Idempotency-Key": "verify-{}".format(stamp)}

    s1_, b1_, _ = call(base, key, "POST", "/bookings", payload, headers)
    s2_, b2_, _ = call(base, key, "POST", "/bookings", payload, headers)
    check("retry returns the same status", s1_ == s2_ == 201, (s1_, s2_))
    check("retry returns the same booking id", b1_.get("id") == b2_.get("id"), (b1_, b2_))

    s3_, b3_, _ = call(base, key, "POST", "/bookings",
                       {"slot_id": "s4", "name": "Ada Obi", "email": idem_email}, headers)
    check("same key with a different body is 422", s3_ == 422, "{} {}".format(s3_, b3_))

    page_email = "page-{}@example.com".format(stamp)
    made = []
    for slot_id in ["s5", "s6", "s7"]:
        status, body, _ = call(base, key, "POST", "/bookings",
                               {"slot_id": slot_id, "name": "Pager", "email": page_email})
        if status == 201:
            made.append(body["id"])
    check("made bookings to page through", len(made) == 3, made)

    seen, cursor, pages = [], None, 0
    while True:
        path = "/bookings?email={}&limit=1".format(page_email)
        if cursor:
            path += "&cursor=" + cursor
        status, body, _ = call(base, key, "GET", path)
        if status != 200:
            check("paging returned 200", False, "{} {}".format(status, body))
            break
        pages += 1
        seen.extend(b["id"] for b in body.get("bookings", []))
        cursor = body.get("next_cursor")
        if cursor is None or pages > 10:
            break
    check("paging walked every booking once", seen == made, (seen, made))
    check("last page has a null cursor", cursor is None, cursor)

    status, body, _ = call(base, key, "GET", "/bookings?email={}".format(page_email.upper()))
    check("email match ignores case", status == 200 and len(body.get("bookings", [])) == 3, body)

    for bad in ["0", "51", "abc"]:
        status, _, _ = call(base, key, "GET", "/bookings?email={}&limit={}".format(page_email, bad))
        check("limit={} is 400".format(bad), status == 400, status)

    # -- level 5 ------------------------------------------------------------
    print("\nLevel 5 - rate limiting")
    spam = "spam-{}@example.com".format(stamp)
    codes = []
    for i in range(6):
        status, _, head = call(base, key, "POST", "/bookings",
                               {"slot_id": "s{}".format(i + 1), "name": "Spammer", "email": spam})
        codes.append((status, head.get("retry-after")))
    last_status, retry_after = codes[-1]
    check("first five attempts were allowed", all(s != 429 for s, _ in codes[:5]), codes)
    check("the sixth attempt is 429", last_status == 429, codes)
    check("429 carries Retry-After", retry_after is not None, codes)
    check("Retry-After is whole seconds",
          retry_after is not None and str(retry_after).strip().isdigit(), retry_after)

    other = "calm-{}@example.com".format(stamp)
    status, body, _ = call(base, key, "POST", "/bookings",
                           {"slot_id": "s12", "name": "Calm", "email": other})
    check("a different email is unaffected", status in (201, 409), "{} {}".format(status, body))

    # -- the page -----------------------------------------------------------
    print("\nWeb page")
    try:
        with urllib.request.urlopen(base + "/", timeout=90) as response:
            html = response.read().decode()
        check("page is served at /", response.status == 200 and "Clinic Booking" in html)
    except Exception as exc:
        check("page is served at /", False, exc)

    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("\nFailed:")
        for label in FAIL:
            print("  - " + label)
        return 1
    print("Everything in the contract checks out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
