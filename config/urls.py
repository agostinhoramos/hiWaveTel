"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. See:
https://docs.djangoproject.com/en/stable/topics/http/urls/
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
)

from apps.sms.views_health import health_modem_manager

urlpatterns = [
    path('admin/', admin.site.urls),
    path(
        'api/schema/',
        SpectacularAPIView.as_view(),
        name='schema',
    ),
    path('api/health/', health_modem_manager, name='api-health-mm'),
    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(
            url_name='schema',
            template_name_js='drf_spectacular/swagger_ui.js',
        ),
        name='swagger-ui',
    ),
    path('api/', include('apps.sms.urls')),
]
