"""Django admin for external device gateway models."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.contrib import messages
from django.utils import timezone
from django.utils.html import format_html

from .models import ExternalDevice, InboxMessage, SmsRecipientStatus, SmsRequest
from .services import generate_token, hash_token


@admin.register(ExternalDevice)
class ExternalDeviceAdmin(admin.ModelAdmin):
    list_display = ['device_id', 'name', 'device_type', 'status', 'is_available', 'last_seen', 'created_at']
    list_filter = ['status', 'device_type', 'is_available']
    search_fields = ['device_id', 'name', 'mqtt_client_id']
    readonly_fields = [
        'device_id',
        'api_key_hash',
        'registration_token_hash',
        'registration_token_expires_at',
        'last_seen',
        'created_at',
        'updated_at',
    ]
    fieldsets = (
        ('Device Info', {
            'fields': ('device_id', 'name', 'device_type', 'mqtt_client_id', 'metadata'),
        }),
        ('Authentication', {
            'fields': ('api_key_hash', 'registration_token_hash', 'registration_token_expires_at'),
        }),
        ('Status', {
            'fields': ('status', 'is_available', 'last_seen'),
        }),
        ('Limits', {
            'fields': ('max_recipients_per_request', 'daily_sms_limit'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    actions = ['generate_registration_token']

    def generate_registration_token(self, request, queryset):  # type: ignore[override]
        """Generate a registration token for selected devices."""
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one device.', level=messages.ERROR)
            return

        device = queryset.first()

        raw_token = generate_token(32)
        token_hash = hash_token(raw_token)
        expires_at = timezone.now() + timedelta(hours=24)

        device.registration_token_hash = token_hash
        device.registration_token_expires_at = expires_at
        device.status = ExternalDevice.Status.PENDING
        device.save()

        self.message_user(
            request,
            format_html(
                'Registration token generated for <strong>{}</strong>: <code>{}</code> (expires at {}). '
                'Copy this token now - it will not be shown again.',
                device.device_id,
                raw_token,
                expires_at.strftime('%Y-%m-%d %H:%M:%S %Z'),
            ),
            level=messages.SUCCESS,
        )

    generate_registration_token.short_description = 'Generate registration token'  # type: ignore[attr-defined]


@admin.register(SmsRequest)
class SmsRequestAdmin(admin.ModelAdmin):
    list_display = ['request_id', 'device', 'status', 'priority', 'sent_count', 'failed_count', 'created_at']
    list_filter = ['status', 'priority', 'device']
    search_fields = ['request_id', 'device__device_id', 'message']
    readonly_fields = ['request_id', 'device', 'recipients', 'message', 'created_at', 'updated_at']
    fieldsets = (
        ('Request Info', {
            'fields': ('request_id', 'device', 'recipients', 'message', 'priority'),
        }),
        ('Status', {
            'fields': ('status', 'sent_count', 'failed_count'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )


@admin.register(SmsRecipientStatus)
class SmsRecipientStatusAdmin(admin.ModelAdmin):
    list_display = ['phone_number', 'request', 'status', 'message_id', 'updated_at']
    list_filter = ['status', 'request__device']
    search_fields = ['phone_number', 'request__request_id', 'message_id']
    readonly_fields = ['request', 'phone_number', 'status', 'message_id', 'error_message', 'updated_at']


@admin.register(InboxMessage)
class InboxMessageAdmin(admin.ModelAdmin):
    list_display = ['message_id', 'device', 'sender', 'received_at', 'ack_sent', 'created_at']
    list_filter = ['device', 'ack_sent']
    search_fields = ['message_id', 'sender', 'body', 'device__device_id']
    readonly_fields = ['message_id', 'device', 'sender', 'body', 'received_at', 'created_at']
    fieldsets = (
        ('Message Info', {
            'fields': ('message_id', 'device', 'sender', 'body', 'received_at'),
        }),
        ('Status', {
            'fields': ('ack_sent',),
        }),
        ('Timestamps', {
            'fields': ('created_at',),
        }),
    )
