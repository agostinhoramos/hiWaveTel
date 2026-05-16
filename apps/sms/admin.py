from django.contrib import admin

from .models import InboundSms, OutboundSms


@admin.register(InboundSms)
class InboundSmsAdmin(admin.ModelAdmin):
    list_display = ('id', 'from_number', 'modem_index', 'mm_state', 'created_at')
    search_fields = ('from_number', 'text', 'mm_path')
    list_filter = ('modem_index', 'mm_state')
    readonly_fields = ('created_at', 'mm_path')


@admin.register(OutboundSms)
class OutboundSmsAdmin(admin.ModelAdmin):
    list_display = ('id', 'to_number', 'modem_index', 'state', 'created_at')
    search_fields = ('to_number', 'text', 'mm_path', 'error_message')
    list_filter = ('modem_index', 'state')
    readonly_fields = ('created_at', 'updated_at')
