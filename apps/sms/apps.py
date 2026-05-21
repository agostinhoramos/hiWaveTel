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
            import logging

            from django.db import transaction
            from apps.external_device.services import (
                inbound_ready_for_inbox_mirror,
                inbound_should_skip_modem_mirror,
            )
            from apps.sms.inbound_processor import get_inbound_processor

            logger = logging.getLogger(__name__)

            if not inbound_ready_for_inbox_mirror(instance):
                return
            if inbound_should_skip_modem_mirror(instance):
                return

            # On updates, only re-queue when multipart text/sender was patched (mmcli race).
            if not created:
                update_fields = kwargs.get('update_fields')
                if update_fields is not None:
                    patched = set(update_fields)
                    if not (patched & {'text', 'from_number', 'mm_state'}):
                        return
                # update_fields=None → full save; proceed if content is ready.

            def _publish_remote_fallback(inbound_obj):
                if not getattr(settings, 'MQTT_REMOTE_BRIDGE_ENABLED', False):
                    return
                from apps.external_device import mqtt_client as mqtt_mod
                from apps.external_device.services import (
                    publish_inbound_to_remote,
                    publish_inbound_to_remote_ephemeral,
                )

                remote_client = getattr(mqtt_mod, '_global_remote_client', None)
                try:
                    if remote_client is not None:
                        publish_inbound_to_remote(inbound_obj, remote_client)
                    else:
                        publish_inbound_to_remote_ephemeral(inbound_obj)
                except Exception:
                    logger.exception(
                        'Failed to publish InboundSms to remote broker pk=%s',
                        inbound_obj.pk,
                    )

            def enqueue_processing():
                processor = get_inbound_processor()
                if processor is not None:
                    success = processor.enqueue(instance.pk)
                    if not success:
                        from apps.external_device.services import sync_single_inbound_to_all_devices

                        logger.error(
                            'Inbound processor queue full for pk=%s, falling back to sync processing',
                            instance.pk,
                        )
                        sync_single_inbound_to_all_devices(instance)
                        _publish_remote_fallback(instance)
                else:
                    from apps.external_device.services import sync_single_inbound_to_all_devices

                    sync_single_inbound_to_all_devices(instance)
                    _publish_remote_fallback(instance)

            transaction.on_commit(enqueue_processing)

        # Start inbound processor eagerly in long-running processes (watcher / gateway / gunicorn).
        import os

        if int(os.environ.get('INBOUND_PROCESSOR_WORKERS', '2')) > 0:
            try:
                from .inbound_processor import get_inbound_processor

                get_inbound_processor()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    'Failed to start inbound processor queue at app ready',
                )
        
        # Register shutdown handlers for queues
        import atexit
        from . import queue_processor as _qp
        from . import inbound_processor as _ip
        
        def shutdown_queues():
            # Stop SMS queue
            q = _qp._global_queue
            if q is not None:
                try:
                    q.stop(timeout=10.0)
                except Exception:
                    pass
            
            # Stop inbound processor queue
            p = _ip._global_processor
            if p is not None:
                try:
                    p.stop(timeout=10.0)
                except Exception:
                    pass
        
        atexit.register(shutdown_queues)
