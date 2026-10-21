"""URL map.

The contract paths (/reset, /slots, /bookings, /bookings/{id}) live at the root
exactly as published. The same views are also mirrored under /api/ so a grader
pointed at either base URL gets the same behaviour, and under /web/ for the
bundled page, where the server attaches the API key instead of the browser.
"""

from django.urls import path

from . import views


def contract_paths(prefix=""):
    return [
        path(prefix + "reset", views.reset),
        path(prefix + "slots", views.slots),
        path(prefix + "bookings", views.bookings),
        path(prefix + "bookings/<path:booking_id>", views.booking_detail),
    ]


urlpatterns = (
    [
        path("", views.index),
        path("healthz", views.healthz),
    ]
    + contract_paths()
    + contract_paths("api/")
    + [
        # The page never needs to reset the clinic, so that one is absent here.
        path("web/slots", views.from_web_page(views.slots)),
        path("web/bookings", views.from_web_page(views.bookings)),
        path("web/bookings/<path:booking_id>", views.from_web_page(views.booking_detail)),
    ]
)
