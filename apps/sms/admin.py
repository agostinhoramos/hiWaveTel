from django.conf import settings
from django.contrib import admin

from .models import InboundSms, OutboundSms


@admin.register(InboundSms)
class InboundSmsAdmin(admin.ModelAdmin):
    list_display = ('id', 'from_number', 'modem_index', 'mm_state', 'created_at')
    search_fields = ('from_number', 'text', 'mm_path')
    list_filter = ('modem_index', 'mm_state', 'created_at')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'mm_path')

    def changelist_view(self, request, extra_context=None):  # type: ignore[override]
        extra_context = extra_context or {}
        modem_limit = getattr(settings, 'SMS_MAX_MESSAGES_PER_MODEM', 2000)
        per_type = max(1, modem_limit // 2)
        total = InboundSms.objects.count()
        extra_context['sms_modem_storage_note'] = (
            f'Total InboundSms: {total}. Rotation limit per modem_index: {per_type} '
            f'(SMS_MAX_MESSAGES_PER_MODEM={modem_limit}).'
        )
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(OutboundSms)
class OutboundSmsAdmin(admin.ModelAdmin):
    list_display = ('id', 'to_number', 'modem_index', 'state', 'created_at')
    search_fields = ('to_number', 'text', 'mm_path', 'error_message')
    list_filter = ('modem_index', 'state', 'created_at')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at')

    def changelist_view(self, request, extra_context=None):  # type: ignore[override]
        extra_context = extra_context or {}
        modem_limit = getattr(settings, 'SMS_MAX_MESSAGES_PER_MODEM', 2000)
        per_type = max(1, modem_limit // 2)
        total = OutboundSms.objects.count()
        extra_context['sms_modem_storage_note'] = (
            f'Total OutboundSms: {total}. Rotation limit per modem_index: {per_type} '
            f'(SMS_MAX_MESSAGES_PER_MODEM={modem_limit}).'
        )
        return super().changelist_view(request, extra_context=extra_context)
