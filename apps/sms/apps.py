from django.apps import AppConfig


class SmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.sms'

    def ready(self) -> None:
        """Register signal handlers when app is ready."""
        from django.conf import settings
        from django.db.models.signals import post_save
        from django.dispatch import receiver

        from .models import InboundSms

        @receiver(post_save, sender=InboundSms)
        def mirror_inbound_to_device_inbox(sender, instance, created, **kwargs):
            """Mirror InboundSms to ExternalDevice inbox and optionally to remote hiDisheLink broker."""
            from apps.external_device.services import (
                inbound_ready_for_inbox_mirror,
                inbound_should_skip_modem_mirror,
                publish_inbound_to_remote,
                sync_single_inbound_to_all_devices,
            )

            if not inbound_ready_for_inbox_mirror(instance):
                return
            if inbound_should_skip_modem_mirror(instance):
                return
            
            # Mirror to local devices
            sync_single_inbound_to_all_devices(instance)
            
            # Publish to remote hiDisheLink broker if bridge mode enabled
            if getattr(settings, 'MQTT_REMOTE_BRIDGE_ENABLED', False):
                # Get remote client instance from global registry
                from apps.external_device import mqtt_client as mqtt_mod
                remote_client = getattr(mqtt_mod, '_global_remote_client', None)
                if remote_client is not None:
                    try:
                        publish_inbound_to_remote(instance, remote_client)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).exception(
                            'Failed to publish InboundSms to remote broker pk=%s',
                            instance.pk,
                        )
        
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
