"""Contract tests, grouped by the five levels of the assessment."""

import json
import threading
from datetime import timedelta, timezone as dt_timezone

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from clinic.models import Booking, BookingAttempt, IdempotencyRecord, Slot

KEY = "test-key"
HEADERS = {"x-api-key": KEY}


def iso(value):
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def slot_payload(slot_id, provider, start, minutes=30):
    return {
        "id": slot_id,
        "provider": provider,
        "start": iso(start),
        "end": iso(start + timedelta(minutes=minutes)),
    }


@override_settings(API_KEYS=[KEY])
class ApiTestCase(TestCase):
    """Shared helpers. Every request carries the API key unless stated."""

    def setUp(self):
        self.now = timezone.now()
        self.future = self.now + timedelta(days=7)

    # -- request helpers ---------------------------------------------------
    def post(self, path, body, headers=None, **kwargs):
        return self.client.post(
            path,
            data=json.dumps(body),
            content_type="application/json",
            headers={**HEADERS, **(headers or {})},
            **kwargs,
        )

    def get(self, path, headers=None):
        return self.client.get(path, headers={**HEADERS, **(headers or {})})

    def delete(self, path, headers=None):
        return self.client.delete(path, headers={**HEADERS, **(headers or {})})

    def reset_slots(self, slots):
        response = self.post("/reset", {"slots": slots})
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def seed(self, count=3, provider="Dr Ade", base=None):
        base = base or (timezone.now() + timedelta(days=1))
        slots = [
            slot_payload("s{}".format(i + 1), provider, base + timedelta(hours=i))
            for i in range(count)
        ]
        self.reset_slots(slots)
        return slots

    def book(self, slot_id="s1", name="Ada Obi", email="ada@example.com", headers=None):
        return self.post(
            "/bookings", {"slot_id": slot_id, "name": name, "email": email}, headers=headers
        )


# --------------------------------------------------------------------------- #
# Level 1
# --------------------------------------------------------------------------- #


class LevelOneTests(ApiTestCase):
    def test_reset_replaces_slots_and_clears_bookings(self):
        self.seed(2)
        self.book("s1")
        self.assertEqual(Booking.objects.count(), 1)

        self.reset_slots([slot_payload("x1", "Dr Bola", self.future)])

        self.assertEqual(Booking.objects.count(), 0)
        self.assertEqual(list(Slot.objects.values_list("id", flat=True)), ["x1"])

    def test_reset_accepts_slots_in_the_past(self):
        past = self.now - timedelta(days=3)
        self.reset_slots([slot_payload("old", "Dr Ade", past)])
        body = self.get("/slots").json()
        self.assertEqual(len(body["slots"]), 1)
        self.assertEqual(body["slots"][0]["id"], "old")

    def test_slots_are_listed_earliest_first_with_all_fields(self):
        base = self.now + timedelta(days=2)
        self.reset_slots(
            [
                slot_payload("late", "Dr Ade", base + timedelta(hours=5)),
                slot_payload("early", "Dr Ade", base),
                slot_payload("mid", "Dr Ade", base + timedelta(hours=2)),
            ]
        )
        body = self.get("/slots").json()
        self.assertEqual([s["id"] for s in body["slots"]], ["early", "mid", "late"])
        for slot in body["slots"]:
            self.assertEqual(
                set(slot), {"id", "provider", "start", "end", "available"}
            )
            self.assertTrue(slot["start"].endswith("Z"))
            self.assertTrue(slot["available"])

    def test_slots_filter_by_provider_is_an_exact_match(self):
        base = self.now + timedelta(days=1)
        self.reset_slots(
            [
                slot_payload("a", "Dr Ade", base),
                slot_payload("b", "Dr Bola", base + timedelta(hours=1)),
            ]
        )
        self.assertEqual([s["id"] for s in self.get("/slots?provider=Dr Ade").json()["slots"]], ["a"])
        self.assertEqual(self.get("/slots?provider=Dr").json()["slots"], [])

    def test_booking_a_slot_returns_201_and_a_confirmed_booking(self):
        self.seed(1)
        response = self.book("s1")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(
            set(body), {"id", "slot_id", "name", "email", "status", "created_at"}
        )
        self.assertEqual(body["slot_id"], "s1")
        self.assertEqual(body["name"], "Ada Obi")
        self.assertEqual(body["email"], "ada@example.com")
        self.assertEqual(body["status"], "confirmed")
        self.assertTrue(body["id"])
        self.assertTrue(body["created_at"].endswith("Z"))

    def test_booked_slot_is_no_longer_available(self):
        self.seed(2)
        self.book("s1")
        by_id = {s["id"]: s for s in self.get("/slots").json()["slots"]}
        self.assertFalse(by_id["s1"]["available"])
        self.assertTrue(by_id["s2"]["available"])

    def test_booking_can_be_looked_up_by_id(self):
        self.seed(1)
        created = self.book("s1").json()
        fetched = self.get("/bookings/{}".format(created["id"]))
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), created)

    def test_unknown_booking_id_is_404(self):
        response = self.get("/bookings/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json())


# --------------------------------------------------------------------------- #
# Level 2
# --------------------------------------------------------------------------- #


class LevelTwoAuthTests(ApiTestCase):
    def test_every_endpoint_rejects_a_missing_key(self):
        cases = [
            ("post", "/reset"),
            ("get", "/slots"),
            ("post", "/bookings"),
            ("get", "/bookings?email=a@b.com"),
            ("get", "/bookings/anything"),
            ("delete", "/bookings/anything"),
        ]
        for method, path in cases:
            with self.subTest(path=path, method=method):
                call = getattr(self.client, method)
                if method == "post":
                    response = call(path, data="{}", content_type="application/json")
                else:
                    response = call(path)
                self.assertEqual(response.status_code, 401, path)
                self.assertIn("error", response.json())

    def test_wrong_key_is_401(self):
        response = self.client.get("/slots", headers={"x-api-key": "nope"})
        self.assertEqual(response.status_code, 401)

    def test_empty_key_is_401(self):
        response = self.client.get("/slots", headers={"x-api-key": ""})
        self.assertEqual(response.status_code, 401)

    def test_correct_key_is_accepted(self):
        self.assertEqual(self.get("/slots").status_code, 200)


class LevelTwoValidationTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed(2)

    def test_missing_fields_are_400(self):
        bodies = [
            {},
            {"name": "Ada", "email": "a@b.com"},
            {"slot_id": "s1", "email": "a@b.com"},
            {"slot_id": "s1", "name": "Ada"},
        ]
        for index, body in enumerate(bodies):
            with self.subTest(body=body):
                response = self.post("/bookings", body, headers={"x-test": str(index)})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("error", response.json())

    def test_empty_and_whitespace_fields_are_400(self):
        bodies = [
            {"slot_id": "", "name": "Ada", "email": "a@b.com"},
            {"slot_id": "   ", "name": "Ada", "email": "a@b.com"},
            {"slot_id": "s1", "name": "", "email": "a@b.com"},
            {"slot_id": "s1", "name": "   ", "email": "a@b.com"},
            {"slot_id": "s1", "name": "Ada", "email": ""},
            {"slot_id": "s1", "name": "Ada", "email": "   "},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.post("/bookings", body)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Booking.objects.count(), 0)

    def test_invalid_emails_are_400(self):
        for bad in ["not-an-email", "a@", "@b.com", "a b@c.com", "a@b", "a@@b.com"]:
            with self.subTest(email=bad):
                # A distinct slot keeps the rate limiter out of the way.
                response = self.post(
                    "/bookings", {"slot_id": "s1", "name": "Ada", "email": bad}
                )
                self.assertEqual(response.status_code, 400, (bad, response.content))
        self.assertEqual(Booking.objects.count(), 0)

    def test_unknown_slot_is_404(self):
        response = self.book("missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json())

    def test_slot_that_already_started_is_400(self):
        self.reset_slots(
            [
                slot_payload("past", "Dr Ade", self.now - timedelta(hours=1)),
                slot_payload("now", "Dr Ade", self.now - timedelta(seconds=1)),
            ]
        )
        for slot_id in ["past", "now"]:
            with self.subTest(slot=slot_id):
                response = self.book(slot_id, email="a{}@example.com".format(slot_id))
                self.assertEqual(response.status_code, 400, response.content)

    def test_validation_runs_before_the_slot_lookup(self):
        # A bad body plus a bad slot is a 400, not a 404.
        response = self.post("/bookings", {"slot_id": "missing", "name": "", "email": "a@b.com"})
        self.assertEqual(response.status_code, 400)

    def test_name_and_email_are_trimmed_and_email_is_lowercased(self):
        response = self.post(
            "/bookings",
            {"slot_id": " s1 ", "name": "  Ada Obi  ", "email": "  ADA@Example.COM  "},
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["name"], "Ada Obi")
        self.assertEqual(body["email"], "ada@example.com")
        self.assertEqual(body["slot_id"], "s1")

        stored = Booking.objects.get(pk=body["id"])
        self.assertEqual(stored.email, "ada@example.com")
        self.assertEqual(stored.name, "Ada Obi")

    def test_malformed_json_is_400(self):
        response = self.client.post(
            "/bookings", data="{not json", content_type="application/json", headers=HEADERS
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())


# --------------------------------------------------------------------------- #
# Level 3
# --------------------------------------------------------------------------- #


class LevelThreeTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed(2)

    def test_second_booking_on_the_same_slot_is_409(self):
        self.assertEqual(self.book("s1", email="one@example.com").status_code, 201)
        response = self.book("s1", email="two@example.com")
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())
        self.assertEqual(Booking.objects.filter(slot_id="s1").count(), 1)

    def test_cancelling_sets_status_and_frees_the_slot(self):
        created = self.book("s1", email="one@example.com").json()

        response = self.delete("/bookings/{}".format(created["id"]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["id"], created["id"])

        by_id = {s["id"]: s for s in self.get("/slots").json()["slots"]}
        self.assertTrue(by_id["s1"]["available"])

    def test_slot_can_be_rebooked_after_a_cancellation(self):
        first = self.book("s1", email="one@example.com").json()
        self.delete("/bookings/{}".format(first["id"]))
        second = self.book("s1", email="two@example.com")
        self.assertEqual(second.status_code, 201, second.content)
        self.assertNotEqual(second.json()["id"], first["id"])

    def test_cancelling_twice_is_409(self):
        created = self.book("s1", email="one@example.com").json()
        self.assertEqual(self.delete("/bookings/{}".format(created["id"])).status_code, 200)
        response = self.delete("/bookings/{}".format(created["id"]))
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())

    def test_cancelling_an_unknown_booking_is_404(self):
        response = self.delete("/bookings/nope")
        self.assertEqual(response.status_code, 404)

    def test_cancelled_booking_is_still_readable(self):
        created = self.book("s1", email="one@example.com").json()
        self.delete("/bookings/{}".format(created["id"]))
        fetched = self.get("/bookings/{}".format(created["id"]))
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["status"], "cancelled")


@override_settings(API_KEYS=[KEY])
class LevelThreeConcurrencyTests(TransactionTestCase):
    """The real test of level 3: simultaneous requests for one slot.

    Each thread gets its own database connection, so these are genuinely
    concurrent transactions rather than a simulated race.
    """

    reset_sequences = True
    THREADS = 12

    def _reset(self, start):
        return self.client.post(
            "/reset",
            data=json.dumps(
                {
                    "slots": [
                        {
                            "id": "s1",
                            "provider": "Dr Ade",
                            "start": iso(start),
                            "end": iso(start + timedelta(minutes=30)),
                        }
                    ]
                }
            ),
            content_type="application/json",
            headers=HEADERS,
        )

    def test_only_one_of_many_simultaneous_requests_wins_the_slot(self):
        start = timezone.now() + timedelta(days=1)
        self.assertEqual(self._reset(start).status_code, 200)

        barrier = threading.Barrier(self.THREADS)
        results = []
        lock = threading.Lock()

        def attempt(index):
            # Distinct emails so the per-email rate limit plays no part here.
            body = json.dumps(
                {
                    "slot_id": "s1",
                    "name": "Racer {}".format(index),
                    "email": "racer{}@example.com".format(index),
                }
            )
            try:
                barrier.wait(timeout=30)
                response = self.client.post(
                    "/bookings", data=body, content_type="application/json", headers=HEADERS
                )
                with lock:
                    results.append(response.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(results), self.THREADS, "some threads did not report")
        self.assertEqual(results.count(201), 1, "expected exactly one winner, got {}".format(results))
        self.assertEqual(results.count(409), self.THREADS - 1, results)
        self.assertEqual(Booking.objects.filter(slot_id="s1", status="confirmed").count(), 1)

    def test_simultaneous_retries_of_one_idempotency_key_create_one_booking(self):
        start = timezone.now() + timedelta(days=1)
        self.assertEqual(self._reset(start).status_code, 200)

        threads_count = 5
        barrier = threading.Barrier(threads_count)
        results = []
        lock = threading.Lock()
        body = json.dumps({"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"})

        def attempt():
            try:
                barrier.wait(timeout=30)
                response = self.client.post(
                    "/bookings",
                    data=body,
                    content_type="application/json",
                    headers={**HEADERS, "idempotency-key": "same-key"},
                )
                with lock:
                    results.append((response.status_code, response.json().get("id")))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(len(results), threads_count)
        self.assertEqual(Booking.objects.count(), 1, results)
        statuses = {status for status, _ in results}
        self.assertEqual(statuses, {201}, results)
        ids = {booking_id for _, booking_id in results}
        self.assertEqual(len(ids), 1, results)


# --------------------------------------------------------------------------- #
# Level 4
# --------------------------------------------------------------------------- #


class LevelFourIdempotencyTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed(3)

    def test_same_key_and_body_replays_the_same_response(self):
        body = {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}
        first = self.post("/bookings", body, headers={"idempotency-key": "abc-123"})
        second = self.post("/bookings", body, headers={"idempotency-key": "abc-123"})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(second.json(), first.json())
        self.assertEqual(Booking.objects.count(), 1)

    def test_replay_is_unaffected_by_key_order_in_the_body(self):
        first = self.post(
            "/bookings",
            {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
            headers={"idempotency-key": "order-test"},
        )
        second = self.post(
            "/bookings",
            {"email": "ada@example.com", "name": "Ada Obi", "slot_id": "s1"},
            headers={"idempotency-key": "order-test"},
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(Booking.objects.count(), 1)

    def test_same_key_with_a_different_body_is_422(self):
        self.post(
            "/bookings",
            {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
            headers={"idempotency-key": "reused"},
        )
        response = self.post(
            "/bookings",
            {"slot_id": "s2", "name": "Ada Obi", "email": "ada@example.com"},
            headers={"idempotency-key": "reused"},
        )
        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn("error", response.json())
        self.assertEqual(Booking.objects.count(), 1)

    def test_different_keys_create_different_bookings(self):
        self.post(
            "/bookings",
            {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
            headers={"idempotency-key": "k1"},
        )
        second = self.post(
            "/bookings",
            {"slot_id": "s2", "name": "Ada Obi", "email": "ada@example.com"},
            headers={"idempotency-key": "k2"},
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(Booking.objects.count(), 2)

    def test_a_replayed_failure_returns_the_same_status(self):
        body = {"slot_id": "missing", "name": "Ada Obi", "email": "ada@example.com"}
        first = self.post("/bookings", body, headers={"idempotency-key": "fail-key"})
        second = self.post("/bookings", body, headers={"idempotency-key": "fail-key"})
        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(Booking.objects.count(), 0)

    def test_booking_without_a_key_still_works(self):
        self.assertEqual(self.book("s1").status_code, 201)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)


class LevelFourPaginationTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        base = self.now + timedelta(days=1)
        self.reset_slots(
            [slot_payload("s{}".format(i), "Dr Ade", base + timedelta(hours=i)) for i in range(12)]
        )
        # Created directly so the per-email rate limit does not block the setup.
        self.created = []
        for i in range(12):
            booking = Booking.objects.create(
                slot_id="s{}".format(i),
                name="Ada Obi",
                email="ada@example.com",
                status="confirmed",
                created_at=self.now + timedelta(seconds=i),
            )
            self.created.append(booking)
        Booking.objects.create(
            slot_id="s0",
            name="Other Person",
            email="someone@example.com",
            status="cancelled",
            created_at=self.now,
        )

    def test_bookings_are_listed_oldest_first(self):
        body = self.get("/bookings?email=ada@example.com&limit=50").json()
        self.assertEqual(len(body["bookings"]), 12)
        self.assertIsNone(body["next_cursor"])
        self.assertEqual(
            [b["id"] for b in body["bookings"]], [b.id for b in self.created]
        )

    def test_only_that_persons_bookings_come_back(self):
        body = self.get("/bookings?email=ada@example.com&limit=50").json()
        self.assertTrue(all(b["email"] == "ada@example.com" for b in body["bookings"]))

    def test_email_match_ignores_case(self):
        body = self.get("/bookings?email=ADA@Example.COM&limit=50").json()
        self.assertEqual(len(body["bookings"]), 12)

    def test_paging_with_the_cursor_walks_every_booking_once(self):
        seen = []
        cursor = None
        pages = 0
        while True:
            path = "/bookings?email=ada@example.com&limit=5"
            if cursor:
                path += "&cursor={}".format(cursor)
            body = self.get(path).json()
            pages += 1
            seen.extend(b["id"] for b in body["bookings"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 10, "cursor did not terminate")

        self.assertEqual(pages, 3)
        self.assertEqual(seen, [b.id for b in self.created])
        self.assertEqual(len(set(seen)), 12)

    def test_last_page_has_a_null_cursor(self):
        body = self.get("/bookings?email=ada@example.com&limit=12").json()
        self.assertEqual(len(body["bookings"]), 12)
        self.assertIsNone(body["next_cursor"])

    def test_default_limit_is_ten(self):
        body = self.get("/bookings?email=ada@example.com").json()
        self.assertEqual(len(body["bookings"]), 10)
        self.assertIsNotNone(body["next_cursor"])

    def test_bad_limits_are_400(self):
        for bad in ["0", "-1", "51", "999", "abc", "1.5", ""]:
            with self.subTest(limit=bad):
                response = self.get("/bookings?email=ada@example.com&limit={}".format(bad))
                if bad == "":
                    # An empty limit falls back to the default.
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 400, (bad, response.content))
                    self.assertIn("error", response.json())

    def test_limit_bounds_are_accepted(self):
        for good in ["1", "50"]:
            with self.subTest(limit=good):
                response = self.get("/bookings?email=ada@example.com&limit={}".format(good))
                self.assertEqual(response.status_code, 200)

    def test_missing_email_is_400(self):
        self.assertEqual(self.get("/bookings").status_code, 400)
        self.assertEqual(self.get("/bookings?email=").status_code, 400)

    def test_unknown_email_returns_an_empty_page(self):
        body = self.get("/bookings?email=nobody@example.com").json()
        self.assertEqual(body["bookings"], [])
        self.assertIsNone(body["next_cursor"])

    def test_cancelled_bookings_are_included(self):
        booking = self.created[0]
        self.delete("/bookings/{}".format(booking.id))
        body = self.get("/bookings?email=ada@example.com&limit=50").json()
        statuses = {b["id"]: b["status"] for b in body["bookings"]}
        self.assertEqual(statuses[booking.id], "cancelled")
        self.assertEqual(len(body["bookings"]), 12)

    def test_a_broken_cursor_is_400(self):
        response = self.get("/bookings?email=ada@example.com&cursor=!!!not-base64!!!")
        self.assertEqual(response.status_code, 400)


# --------------------------------------------------------------------------- #
# Level 5
# --------------------------------------------------------------------------- #


class LevelFiveRateLimitTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed(10)

    def test_sixth_attempt_in_a_minute_is_429_with_retry_after(self):
        for i in range(5):
            response = self.book("s{}".format(i + 1), email="spam@example.com")
            self.assertEqual(response.status_code, 201, (i, response.content))

        response = self.book("s6", email="spam@example.com")
        self.assertEqual(response.status_code, 429, response.content)
        self.assertIn("error", response.json())

        retry_after = response.headers.get("Retry-After")
        self.assertIsNotNone(retry_after, "Retry-After header is missing")
        self.assertRegex(retry_after, r"^\d+$", "Retry-After must be whole seconds")
        self.assertGreaterEqual(int(retry_after), 1)
        self.assertLessEqual(int(retry_after), 60)

        self.assertEqual(Booking.objects.filter(email="spam@example.com").count(), 5)

    def test_failed_attempts_count_towards_the_limit(self):
        # Five failures, no booking made, and the sixth attempt is still blocked.
        for _ in range(5):
            response = self.book("no-such-slot", email="spam@example.com")
            self.assertEqual(response.status_code, 404)

        response = self.book("s1", email="spam@example.com")
        self.assertEqual(response.status_code, 429, response.content)
        self.assertEqual(Booking.objects.count(), 0)

    def test_validation_failures_count_too(self):
        for _ in range(5):
            response = self.post(
                "/bookings", {"slot_id": "", "name": "Ada", "email": "spam@example.com"}
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.book("s1", email="spam@example.com").status_code, 429)

    def test_other_emails_are_not_affected(self):
        for i in range(5):
            self.book("s{}".format(i + 1), email="spam@example.com")
        self.assertEqual(self.book("s6", email="spam@example.com").status_code, 429)

        response = self.book("s7", email="calm@example.com")
        self.assertEqual(response.status_code, 201, response.content)

    def test_limit_is_per_email_regardless_of_case_or_spacing(self):
        for i in range(5):
            self.book("s{}".format(i + 1), email="spam@example.com")
        response = self.book("s6", email="  SPAM@Example.com  ")
        self.assertEqual(response.status_code, 429, response.content)

    def test_window_slides_so_old_attempts_stop_counting(self):
        for i in range(5):
            self.assertEqual(self.book("s{}".format(i + 1), email="spam@example.com").status_code, 201)
        self.assertEqual(self.book("s6", email="spam@example.com").status_code, 429)

        # Age the recorded attempts past the window instead of sleeping a minute.
        BookingAttempt.objects.filter(email="spam@example.com").update(
            created_at=timezone.now() - timedelta(seconds=61)
        )
        response = self.book("s6", email="spam@example.com")
        self.assertEqual(response.status_code, 201, response.content)

    def test_rate_limit_is_checked_before_idempotent_replay(self):
        body = {"slot_id": "s1", "name": "Ada Obi", "email": "spam@example.com"}
        first = self.post("/bookings", body, headers={"idempotency-key": "rl-key"})
        self.assertEqual(first.status_code, 201)
        for _ in range(4):
            self.post("/bookings", body, headers={"idempotency-key": "rl-key"})
        response = self.post("/bookings", body, headers={"idempotency-key": "rl-key"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(Booking.objects.count(), 1)


# --------------------------------------------------------------------------- #
# Plumbing: the bundled page, mirrors and health
# --------------------------------------------------------------------------- #


class SupportingSurfaceTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed(2)

    def test_web_page_is_served_at_the_root(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Clinic Booking", response.content)

    def test_health_endpoint_needs_no_key(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_page_endpoints_work_without_the_key(self):
        response = self.client.get("/web/slots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["slots"]), 2)

        booked = self.client.post(
            "/web/bookings",
            data=json.dumps({"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}),
            content_type="application/json",
        )
        self.assertEqual(booked.status_code, 201, booked.content)

    def test_page_cannot_reset_the_clinic(self):
        response = self.client.post(
            "/web/reset", data=json.dumps({"slots": []}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)

    def test_api_prefix_mirrors_the_contract(self):
        self.assertEqual(self.get("/api/slots").status_code, 200)
        created = self.post(
            "/api/bookings", {"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"}
        )
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(
            self.get("/api/bookings/{}".format(created.json()["id"])).status_code, 200
        )

    def test_wrong_methods_are_rejected(self):
        self.assertEqual(self.get("/reset").status_code, 405)
        self.assertEqual(self.delete("/slots").status_code, 405)
        self.assertEqual(self.delete("/bookings").status_code, 405)

    def test_error_bodies_always_have_a_single_error_key(self):
        response = self.get("/bookings/missing")
        self.assertEqual(set(response.json()), {"error"})
        self.assertIsInstance(response.json()["error"], str)


class DatabaseGuaranteeTests(TestCase):
    def test_partial_unique_index_exists_on_confirmed_bookings(self):
        constraints = connection.introspection.get_constraints(
            connection.cursor(), Booking._meta.db_table
        )
        self.assertIn("uniq_confirmed_slot", constraints)
