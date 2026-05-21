"""Django app configuration for external device gateway."""

from django.apps import AppConfig


class ExternalDeviceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.external_device'
    verbose_name = 'External Device Gateway'

    def ready(self) -> None:
        # Register OpenAPI auth extension for ApiKeyAuthentication (import side effect).
        from . import spectacular_auth  # noqa: F401
        
        # Register signal handlers to clear device ID cache when devices change
        from django.db.models.signals import post_delete, post_save
        from django.dispatch import receiver
        
        from .models import ExternalDevice, HiDishelinkDevice
        from .mqtt_client import clear_device_id_cache
        
        @receiver(post_save, sender=ExternalDevice)
        @receiver(post_delete, sender=ExternalDevice)
        @receiver(post_save, sender=HiDishelinkDevice)
        @receiver(post_delete, sender=HiDishelinkDevice)
        def invalidate_device_cache(sender, **kwargs):
            """Clear device ID sanitization cache when devices change."""
            clear_device_id_cache()

