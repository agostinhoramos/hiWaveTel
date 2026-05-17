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
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
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
            template_name_js='apps_external_device/swagger_ui_persist.js',
        ),
        name='swagger-ui',
    ),
    path('api/auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/v1/', include('apps.external_device.urls')),
    path('api/sms/device/', include('apps.external_device.device_urls')),
    path('api/', include('apps.sms.urls')),
]
