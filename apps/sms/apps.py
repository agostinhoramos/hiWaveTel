from django.apps import AppConfig


class SmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.sms'

    def ready(self) -> None:
        """Register signal handlers when app is ready."""
        from django.db.models.signals import post_save
        from django.dispatch import receiver

        from .models import InboundSms

        @receiver(post_save, sender=InboundSms)
        def mirror_inbound_to_device_inbox(sender, instance, created, **kwargs):
            """Mirror InboundSms to all active ExternalDevice inbox on create and update."""
            from apps.external_device.services import sync_single_inbound_to_all_devices

            sync_single_inbound_to_all_devices(instance)
        
        # Register shutdown handler for queue
        import atexit
        from . import queue_processor as _qp
        
        def shutdown_queue():
            q = _qp._global_queue
            if q is not None:
                try:
                    q.stop(timeout=10.0)
                except Exception:
                    pass
        
        atexit.register(shutdown_queue)
