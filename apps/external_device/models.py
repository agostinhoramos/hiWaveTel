"""External device gateway models: ExternalDevice, SmsRequest, SmsRecipientStatus, InboxMessage."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class ExternalDevice(models.Model):
    """External device registered to send/receive SMS via this gateway."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'
        SUSPENDED = 'suspended', 'Suspended'

    device_id = models.CharField(max_length=64, primary_key=True, help_text='Unique device identifier (e.g. phone number)')
    name = models.CharField(max_length=255, help_text='Human-readable device name')
    device_type = models.CharField(max_length=64, default='modem', help_text='Device type (modem, gateway, etc.)')
    api_key_hash = models.CharField(max_length=64, blank=True, help_text='SHA-256 hash of API key')
    registration_token_hash = models.CharField(max_length=64, blank=True, help_text='SHA-256 hash of one-time registration token')
    registration_token_expires_at = models.DateTimeField(null=True, blank=True, help_text='Registration token expiry time')
    mqtt_client_id = models.CharField(max_length=255, blank=True, help_text='MQTT client ID for this device')
    metadata = models.JSONField(default=dict, blank=True, help_text='Additional device metadata')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    is_available = models.BooleanField(default=False, help_text='Device is currently online/reachable')
    last_seen = models.DateTimeField(null=True, blank=True, help_text='Last authentication time')
    max_recipients_per_request = models.PositiveSmallIntegerField(default=50, help_text='Max recipients per SMS send request')
    daily_sms_limit = models.PositiveIntegerField(default=500, help_text='Daily SMS sending limit')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.device_id} ({self.status})'

    def mark_seen(self) -> None:
        """Update last_seen and mark available."""
        self.last_seen = timezone.now()
        self.is_available = True
        self.save(update_fields=['last_seen', 'is_available'])

    @property
    def is_authenticated(self) -> bool:
        """Always return True for compatibility with DRF request.user."""
        return True


class SmsRequest(models.Model):
    """SMS send request from an external device."""

    class Priority(models.TextChoices):
        NORMAL = 'normal', 'Normal'
        HIGH = 'high', 'High'
        URGENT = 'urgent', 'Urgent'

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        PARTIAL = 'partial', 'Partial'
        FAILED = 'failed', 'Failed'

    request_id = models.CharField(max_length=32, unique=True, db_index=True, help_text='Unique request identifier')
    device = models.ForeignKey(ExternalDevice, on_delete=models.PROTECT, related_name='sms_requests')
    recipients = models.JSONField(help_text='List of recipient phone numbers')
    message = models.TextField(help_text='SMS message text')
    priority = models.CharField(max_length=16, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    sent_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['device', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self) -> str:
        return f'{self.request_id} ({self.status})'


class SmsRecipientStatus(models.Model):
    """Per-recipient status for an SMS request."""

    class Status(models.TextChoices):
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    request = models.ForeignKey(SmsRequest, on_delete=models.CASCADE, related_name='recipient_statuses')
    phone_number = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    message_id = models.CharField(max_length=255, blank=True, help_text='External device message ID')
    error_message = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['phone_number']
        indexes = [
            models.Index(fields=['request', 'phone_number']),
        ]

    def __str__(self) -> str:
        return f'{self.phone_number} ({self.status})'


class InboxMessage(models.Model):
    """Incoming SMS message from an external device."""

    message_id = models.CharField(max_length=255, unique=True, db_index=True, help_text='External device message ID')
    device = models.ForeignKey(ExternalDevice, on_delete=models.PROTECT, related_name='inbox_messages')
    sender = models.CharField(max_length=64, db_index=True, help_text='Message sender phone number')
    body = models.TextField(help_text='Message body')
    received_at = models.DateTimeField(help_text='When the device received the message')
    ack_sent = models.BooleanField(default=False, help_text='ACK has been sent to device')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['device', '-received_at']),
            models.Index(fields=['-received_at']),
        ]

    def __str__(self) -> str:
        return f'{self.sender} → {self.device_id} @{self.received_at.isoformat(timespec="seconds")}'
