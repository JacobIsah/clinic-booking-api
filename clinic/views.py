"""HTTP layer for the clinic booking API.

Every endpoint is a plain Django view returning JsonResponse so the wire format
matches the published contract exactly. Errors are always {"error": "..."}.
"""

import base64
import hashlib
import json
import math
import secrets
import time
from datetime import timedelta, timezone as dt_timezone
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from .models import CANCELLED, CONFIRMED, Booking, BookingAttempt, IdempotencyRecord, Slot, new_id

MAX_ATTEMPTS_PER_WINDOW = 5
RATE_LIMIT_WINDOW_SECONDS = 60


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def error(status, message, headers=None):
    response = JsonResponse({"error": message}, status=status)
    for header, value in (headers or {}).items():
        response[header] = value
    return response


def iso(value):
    """Render a datetime the way the spec examples do: UTC, trailing Z."""
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def booking_json(booking):
    return {
        "id": booking.id,
        "slot_id": booking.slot_id,
        "name": booking.name,
        "email": booking.email,
        "status": booking.status,
        "created_at": iso(booking.created_at),
    }


def slot_json(slot, available):
    return {
        "id": slot.id,
        "provider": slot.provider,
        "start": iso(slot.start),
        "end": iso(slot.end),
        "available": available,
    }


def require_api_key(view):
    """Level 2: every request needs a valid X-API-Key, or it gets a 401.

    Views reached through the bundled web page set api_key_verified first, so
    the browser never has to hold the key.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not getattr(request, "api_key_verified", False):
            provided = request.headers.get("X-API-Key") or ""
            if not provided or not any(
                secrets.compare_digest(provided, k) for k in settings.API_KEYS
            ):
                return error(401, "missing or invalid API key")
        return view(request, *args, **kwargs)

    return wrapper


def read_json_object(request):
    """Parse a JSON object body. Returns (data, error_response)."""
    if not request.body:
        return None, error(400, "a JSON body is required")
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, error(400, "body must be valid JSON")
    if not isinstance(data, dict):
        return None, error(400, "body must be a JSON object")
    return data, None


def method_not_allowed(allowed):
    joined = ", ".join(allowed)
    return error(405, "method not allowed, use " + joined, {"Allow": joined})


def encode_cursor(booking):
    raw = "{}|{}".format(iso(booking.created_at), booking.id).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor):
    """Returns (created_at, id), or None when the cursor is unusable."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        stamp, _, booking_id = raw.partition("|")
        created_at = parse_datetime(stamp)
        if created_at is None or not booking_id:
            return None
        if timezone.is_naive(created_at):
            created_at = created_at.replace(tzinfo=dt_timezone.utc)
        return created_at, booking_id
    except Exception:
        return None


def parse_slot_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = parse_datetime(value.strip())
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


# --------------------------------------------------------------------------- #
# level 5 - rate limiting
# --------------------------------------------------------------------------- #


def rate_limit_retry_after(email):
    """At most 5 attempts per email per minute, failures included.

    Returns the whole seconds to wait when the caller is over the limit, or None
    when the attempt is allowed (in which case it is recorded). A Postgres
    advisory lock keyed on the email keeps the count exact when requests arrive
    together; other emails are untouched because both the lock and the query are
    scoped to this one address.
    """
    now = timezone.now()
    cutoff = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ["rl:" + email])
        recent = list(
            BookingAttempt.objects.filter(email=email, created_at__gt=cutoff)
            .order_by("created_at")
            .values_list("created_at", flat=True)
        )
        if len(recent) >= MAX_ATTEMPTS_PER_WINDOW:
            # The attempt that has to age out before another one is allowed.
            blocking = recent[len(recent) - MAX_ATTEMPTS_PER_WINDOW]
            wait = (blocking + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS) - now).total_seconds()
            return max(1, math.ceil(wait))
        BookingAttempt.objects.create(email=email, created_at=now)
    return None


# --------------------------------------------------------------------------- #
# level 1 - reset and slots
# --------------------------------------------------------------------------- #


@csrf_exempt
@require_api_key
def reset(request):
    if request.method != "POST":
        return method_not_allowed(["POST"])

    data, bad = read_json_object(request)
    if bad:
        return bad

    raw_slots = data.get("slots")
    if not isinstance(raw_slots, list):
        return error(400, "slots must be a list")

    # Keyed by id so a repeated id inside one payload is last-one-wins rather
    # than a database error.
    parsed = {}
    for index, item in enumerate(raw_slots):
        if not isinstance(item, dict):
            return error(400, "slots[{}] must be an object".format(index))
        slot_id = item.get("id")
        provider = item.get("provider")
        if not isinstance(slot_id, str) or not slot_id.strip():
            return error(400, "slots[{}].id is required".format(index))
        if not isinstance(provider, str) or not provider.strip():
            return error(400, "slots[{}].provider is required".format(index))
        start = parse_slot_datetime(item.get("start"))
        end = parse_slot_datetime(item.get("end"))
        if start is None:
            return error(400, "slots[{}].start must be an ISO 8601 date-time".format(index))
        if end is None:
            return error(400, "slots[{}].end must be an ISO 8601 date-time".format(index))
        slot_id = slot_id.strip()
        parsed[slot_id] = Slot(
            id=slot_id, provider=provider.strip(), start=start, end=end
        )

    with transaction.atomic():
        Booking.objects.all().delete()
        Slot.objects.all().delete()
        # A reset is a clean slate, so the retry and rate-limit bookkeeping goes
        # too. Otherwise attempts recorded before the reset would count against
        # the first booking made after it.
        IdempotencyRecord.objects.all().delete()
        BookingAttempt.objects.all().delete()
        Slot.objects.bulk_create(list(parsed.values()))

    return JsonResponse({"slots": len(parsed)}, status=200)


@require_api_key
def slots(request):
    if request.method != "GET":
        return method_not_allowed(["GET"])

    queryset = Slot.objects.all()
    provider = request.GET.get("provider")
    if provider is not None:
        queryset = queryset.filter(provider=provider)

    taken = set(Booking.objects.filter(status=CONFIRMED).values_list("slot_id", flat=True))
    payload = [slot_json(slot, slot.id not in taken) for slot in queryset.order_by("start", "id")]
    return JsonResponse({"slots": payload})


# --------------------------------------------------------------------------- #
# bookings
# --------------------------------------------------------------------------- #


@csrf_exempt
@require_api_key
def bookings(request):
    if request.method == "GET":
        return list_bookings(request)
    if request.method == "POST":
        return create_booking(request)
    return method_not_allowed(["GET", "POST"])


def _insert_booking(data):
    """Validate, then book. Returns (status_code, payload)."""
    slot_id = data.get("slot_id")
    name = data.get("name")
    email = data.get("email")

    # Level 2: missing or empty fields, and invalid emails, are turned away.
    if not isinstance(slot_id, str) or not slot_id.strip():
        return 400, {"error": "slot_id is required"}
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required"}
    if not isinstance(email, str) or not email.strip():
        return 400, {"error": "email is required"}

    slot_id = slot_id.strip()
    name = name.strip()
    email = email.strip().lower()

    try:
        validate_email(email)
    except ValidationError:
        return 400, {"error": "email is not valid"}

    slot = Slot.objects.filter(pk=slot_id).first()
    if slot is None:
        return 404, {"error": "slot not found"}
    if slot.start <= timezone.now():
        return 400, {"error": "slot has already started"}

    try:
        # Level 3: the partial unique index picks the winner when two of these
        # run at the same instant. The loser raises, and becomes a 409.
        with transaction.atomic():
            booking = Booking.objects.create(
                id=new_id(),
                slot=slot,
                name=name,
                email=email,
                status=CONFIRMED,
                created_at=timezone.now(),
            )
    except IntegrityError:
        return 409, {"error": "slot is already booked"}

    return 201, booking_json(booking)


def create_booking(request):
    data, bad = read_json_object(request)
    if bad:
        return bad

    # Level 5 runs before anything that can fail, so failed attempts count too.
    raw_email = data.get("email")
    if isinstance(raw_email, str) and raw_email.strip():
        retry_after = rate_limit_retry_after(raw_email.strip().lower())
        if retry_after is not None:
            return error(
                429,
                "too many booking attempts for this email, try again later",
                {"Retry-After": str(retry_after)},
            )

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        status, payload = _insert_booking(data)
        return JsonResponse(payload, status=status)

    # Level 4: canonical JSON, so key order and whitespace do not read as a
    # different body.
    request_hash = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()

    record, created = IdempotencyRecord.objects.get_or_create(
        key=idempotency_key, defaults={"request_hash": request_hash}
    )

    if not created:
        if not secrets.compare_digest(record.request_hash, request_hash):
            return error(422, "this Idempotency-Key was already used with a different body")
        if record.status_code is None:
            # The first request holding this key is still in flight, so wait for
            # it and replay its response rather than racing it.
            for _ in range(50):
                time.sleep(0.1)
                record.refresh_from_db()
                if record.status_code is not None:
                    break
        if record.status_code is None:
            return error(409, "a request with this Idempotency-Key is still in progress")
        return JsonResponse(record.response_body, status=record.status_code)

    status, payload = _insert_booking(data)
    IdempotencyRecord.objects.filter(key=idempotency_key).update(
        status_code=status, response_body=payload
    )
    return JsonResponse(payload, status=status)


def list_bookings(request):
    """Level 4: one person's bookings, oldest first, cursor paginated."""
    email = request.GET.get("email")
    if email is None or not email.strip():
        return error(400, "email is required")
    email = email.strip().lower()

    raw_limit = request.GET.get("limit")
    if raw_limit is None or not str(raw_limit).strip():
        limit = 10
    else:
        try:
            limit = int(str(raw_limit).strip())
        except (TypeError, ValueError):
            return error(400, "limit must be an integer between 1 and 50")
        if limit < 1 or limit > 50:
            return error(400, "limit must be an integer between 1 and 50")

    queryset = Booking.objects.filter(email__iexact=email).order_by("created_at", "id")

    raw_cursor = request.GET.get("cursor")
    if raw_cursor is not None and raw_cursor.strip():
        decoded = decode_cursor(raw_cursor.strip())
        if decoded is None:
            return error(400, "invalid cursor")
        last_created, last_id = decoded
        # Keyset pagination, so a page stays stable even if rows arrive while
        # the caller is still working through the pages.
        queryset = queryset.filter(
            Q(created_at__gt=last_created) | Q(created_at=last_created, id__gt=last_id)
        )

    rows = list(queryset[: limit + 1])
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1])

    return JsonResponse({"bookings": [booking_json(b) for b in rows], "next_cursor": next_cursor})


@csrf_exempt
@require_api_key
def booking_detail(request, booking_id):
    if request.method == "GET":
        booking = Booking.objects.filter(pk=booking_id).first()
        if booking is None:
            return error(404, "booking not found")
        return JsonResponse(booking_json(booking))

    if request.method == "DELETE":
        with transaction.atomic():
            booking = Booking.objects.select_for_update().filter(pk=booking_id).first()
            if booking is None:
                return error(404, "booking not found")
            if booking.status == CANCELLED:
                return error(409, "booking is already cancelled")
            booking.status = CANCELLED
            # Leaving the partial unique index is what frees the slot.
            booking.save(update_fields=["status"])
        return JsonResponse(booking_json(booking))

    return method_not_allowed(["GET", "DELETE"])


# --------------------------------------------------------------------------- #
# web page and health
# --------------------------------------------------------------------------- #


def index(request):
    return render(request, "clinic/index.html")


def healthz(request):
    return JsonResponse({"status": "ok", "time": iso(timezone.now())})


def from_web_page(view):
    """Mount an API view for the bundled page without exposing the API key.

    The page calls these under /web/, the server supplies the credential, and
    the browser never sees it. Same view, same validation, same limits.
    """

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        request.api_key_verified = True
        return view(request, *args, **kwargs)

    return wrapper
