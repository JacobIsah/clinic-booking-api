"""Data model for the clinic booking API.

The important piece here is ``Booking.uniq_confirmed_slot``: a partial unique
index over ``slot_id`` restricted to confirmed rows. The database, not the
application, is what makes double booking impossible, so it holds no matter how
many workers or processes race on the same slot (level 3). Cancelling a booking
takes the row out of the index and frees the slot again.
"""

import uuid

from django.db import models

CONFIRMED = "confirmed"
CANCELLED = "cancelled"


def new_id():
    return uuid.uuid4().hex


class Slot(models.Model):
    id = models.CharField(primary_key=True, max_length=255)
    provider = models.CharField(max_length=255)
    start = models.DateTimeField()
    end = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["start"]),
            models.Index(fields=["provider"]),
        ]

    def __str__(self):
        return f"{self.id} {self.provider} {self.start:%Y-%m-%d %H:%M}"


class Booking(models.Model):
    STATUS_CHOICES = [(CONFIRMED, CONFIRMED), (CANCELLED, CANCELLED)]

    id = models.CharField(primary_key=True, max_length=64, default=new_id)
    slot = models.ForeignKey(Slot, on_delete=models.CASCADE, related_name="bookings")
    name = models.CharField(max_length=255)
    email = models.CharField(max_length=320)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=CONFIRMED)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["slot"],
                condition=models.Q(status=CONFIRMED),
                name="uniq_confirmed_slot",
            )
        ]
        indexes = [
            models.Index(fields=["email", "created_at", "id"]),
        ]

    def __str__(self):
        return f"{self.id} {self.slot_id} {self.status}"


class IdempotencyRecord(models.Model):
    """One row per Idempotency-Key seen (level 4).

    ``request_hash`` lets us tell a genuine retry from a different body reusing
    the key, and the stored status/body let a retry replay the original response
    byte for byte instead of booking a second time.
    """

    key = models.CharField(primary_key=True, max_length=255)
    request_hash = models.CharField(max_length=64)
    status_code = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.key} -> {self.status_code}"


class BookingAttempt(models.Model):
    """One row per booking attempt, used for the per-email rate limit (level 5).

    Kept in the database rather than in memory so the limit is shared by every
    worker process.
    """

    email = models.CharField(max_length=320)
    created_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["email", "created_at"]),
        ]

    def __str__(self):
        return f"{self.email} @ {self.created_at:%H:%M:%S}"
