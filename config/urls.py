from django.urls import include, path

from clinic import urls as clinic_urls

urlpatterns = [
    path("", include(clinic_urls)),
]
