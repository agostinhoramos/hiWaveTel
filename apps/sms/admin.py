from django.conf import settings
from django.contrib import admin, messages
from django.utils.html import format_html

from .models import InboundSms, OutboundSms
from .services import persist_inbound_sms


@admin.register(InboundSms)
class InboundSmsAdmin(admin.ModelAdmin):
    list_display = ('id', 'from_number', 'modem_index', 'mm_state', 'created_at')
    search_fields = ('from_number', 'text', 'mm_path')
    list_filter = ('modem_index', 'mm_state', 'created_at')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'mm_path')
    actions = ['refresh_from_modem']

    @admin.action(description='Refresh selected from modem (mmcli re-fetch)')
    def refresh_from_modem(self, request, queryset):  # type: ignore[override]
        updated = 0
        for row in queryset:
            before_text = (row.text or '').strip()
            before_state = (row.mm_state or '').strip()
            try:
                refreshed = persist_inbound_sms(row.mm_path, row.modem_index, None)
            except Exception as exc:
                self.message_user(
                    request,
                    format_html('Failed pk={}: {}', row.pk, exc),
                    level=messages.ERROR,
                )
                continue
            after_text = (refreshed.text or '').strip()
            after_state = (refreshed.mm_state or '').strip()
            if after_text != before_text or after_state != before_state:
                updated += 1
                self.message_user(
                    request,
                    format_html(
                        'pk={}: state {} → {}, text {} chars',
                        row.pk,
                        before_state or '(empty)',
                        after_state or '(empty)',
                        len(after_text),
                    ),
                    level=messages.SUCCESS,
                )
        if updated == 0:
            self.message_user(request, 'No changes after modem refresh.', level=messages.WARNING)

    def changelist_view(self, request, extra_context=None):  # type: ignore[override]
        extra_context = extra_context or {}
        modem_limit = getattr(settings, 'SMS_MAX_MESSAGES_PER_MODEM', 2000)
        per_type = max(1, modem_limit // 2)
        total = InboundSms.objects.count()
        multipart_hint = InboundSms.objects.filter(mm_state__icontains='multipart').count()
        extra_context['sms_modem_storage_note'] = (
            f'Total InboundSms: {total}. Rotation limit per modem_index: {per_type} '
            f'(SMS_MAX_MESSAGES_PER_MODEM={modem_limit}). '
            f'Rows with multipart in mm_state: {multipart_hint}. '
            'Multipart segments are stored separately; the app does not reassemble them.'
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
