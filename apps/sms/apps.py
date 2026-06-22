from django.apps import AppConfig


class SmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.sms'

    def ready(self) -> None:
        """Register signal handlers when app is ready."""
        import os

        from django.db.models.signals import post_save
        from django.dispatch import receiver

        from .models import InboundSms

        @receiver(post_save, sender=InboundSms)
        def schedule_inbound_webhook_on_save(sender, instance, created, **kwargs):
            """Persist webhook work to DB queue when inbound SMS is ready."""
            import logging

            from django.db import transaction
            from apps.sms.inbound_filters import (
                inbound_ready_for_webhook,
                inbound_should_skip_webhook,
            )
            from apps.sms.webhook_outbox import schedule_inbound_webhook

            logger = logging.getLogger(__name__)

            if not inbound_ready_for_webhook(instance):
                return
            if inbound_should_skip_webhook(instance):
                return

            if not created:
                update_fields = kwargs.get('update_fields')
                if update_fields is not None:
                    patched = set(update_fields)
                    if not (patched & {'text', 'from_number', 'mm_state'}):
                        return

            def enqueue_to_outbox():
                try:
                    schedule_inbound_webhook(instance)
                except Exception:
                    logger.exception('Failed to queue webhook job inbound pk=%s', instance.pk)

            transaction.on_commit(enqueue_to_outbox)

        role = os.environ.get('HIWAVETEL_ROLE', '').strip().lower()
        queues_enabled = os.environ.get('HIWAVETEL_QUEUE_ENABLED', '').lower() == 'true'

        # Detection worker: start persist queue eagerly; no in-memory webhook processor.
        if role in {'watcher', 'detector'} and queues_enabled:
            try:
                from .queue_processor import get_sms_queue

                get_sms_queue()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    'Failed to start SMS persist queue at app ready',
                )

        if queues_enabled and role not in {'watcher', 'detector', 'webhook'}:
            try:
                from .outbound_processor import get_outbound_processor

                get_outbound_processor()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    'Failed to start outbound processor queue at app ready',
                )

        import atexit
        from . import outbound_processor as _op
        from . import queue_processor as _qp

        def shutdown_queues():
            q = _qp._global_queue
            if q is not None:
                try:
                    q.stop(timeout=10.0)
                except Exception:
                    pass
            o = _op._global_outbound
            if o is not None:
                try:
                    o.stop(timeout=10.0)
                except Exception:
                    pass

        atexit.register(shutdown_queues)
