"""Django app configuration for external device gateway."""

from django.apps import AppConfig


class ExternalDeviceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.external_device'
    verbose_name = 'External Device Gateway'

    def ready(self) -> None:
        # Register OpenAPI auth extension for ApiKeyAuthentication (import side effect).
        from . import spectacular_auth  # noqa: F401
