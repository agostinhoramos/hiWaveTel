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
        def deliver_inbound_via_webhook(sender, instance, created, **kwargs):
            """Enqueue HTTP webhook delivery when inbound SMS is ready."""
            import logging

            from django.db import transaction
            from apps.sms.inbound_filters import (
                inbound_ready_for_webhook,
                inbound_should_skip_webhook,
            )
            from apps.sms.inbound_processor import get_inbound_processor
            from apps.sms.webhook_delivery import deliver_inbound_webhooks

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

            from apps.sms.outbound_processor import queues_enabled_in_process

            if not queues_enabled_in_process():
                try:
                    deliver_inbound_webhooks(instance)
                except Exception:
                    logger.exception('Sync webhook delivery failed pk=%s', instance.pk)
                return

            def enqueue_processing():
                processor = get_inbound_processor()
                if processor is not None:
                    success = processor.enqueue(instance.pk)
                    if not success:
                        logger.error(
                            'Inbound processor queue full pk=%s, falling back to sync webhook',
                            instance.pk,
                        )
                        deliver_inbound_webhooks(instance)
                else:
                    deliver_inbound_webhooks(instance)

            transaction.on_commit(enqueue_processing)

        import os

        queues_enabled = os.environ.get('HIWAVETEL_QUEUE_ENABLED', '').lower() == 'true'
        if queues_enabled and int(os.environ.get('INBOUND_PROCESSOR_WORKERS', '2')) > 0:
            try:
                from .inbound_processor import get_inbound_processor

                get_inbound_processor()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    'Failed to start inbound processor queue at app ready',
                )

        if queues_enabled:
            try:
                from .outbound_processor import get_outbound_processor

                get_outbound_processor()
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    'Failed to start outbound processor queue at app ready',
                )

        import atexit
        from . import inbound_processor as _ip
        from . import outbound_processor as _op
        from . import queue_processor as _qp

        def shutdown_queues():
            q = _qp._global_queue
            if q is not None:
                try:
                    q.stop(timeout=10.0)
                except Exception:
                    pass
            p = _ip._global_processor
            if p is not None:
                try:
                    p.stop(timeout=10.0)
                except Exception:
                    pass
            o = _op._global_outbound
            if o is not None:
                try:
                    o.stop(timeout=10.0)
                except Exception:
                    pass

        atexit.register(shutdown_queues)
