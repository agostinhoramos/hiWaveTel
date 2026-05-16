from django.core.validators import MinLengthValidator
from django.db import models

from .validators import sms_destination_validator


class InboundSms(models.Model):
    """SMS received via ModemManager (stored when D-Bus signals new SMS objects)."""

    mm_path = models.CharField(max_length=512, unique=True)
    modem_index = models.PositiveSmallIntegerField(default=0, db_index=True)
    from_number = models.CharField(max_length=64, blank=True, db_index=True)
    text = models.TextField(blank=True)
    mm_state = models.CharField(max_length=64, blank=True)
    smsc = models.CharField(max_length=64, blank=True)
    modem_timestamp_raw = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at', 'from_number']),
        ]

    def __str__(self) -> str:
        return f'{self.from_number or "?"} @{self.created_at.isoformat(timespec="seconds")}'


class OutboundSms(models.Model):
    """Outbound SMS lifecycle (create via mmcli, then send)."""

    class State(models.TextChoices):
        CREATED = 'created', 'Created'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    mm_path = models.CharField(max_length=512, blank=True)
    modem_index = models.PositiveSmallIntegerField(default=0, db_index=True)
    to_number = models.CharField(max_length=64, db_index=True, validators=[sms_destination_validator])
    text = models.TextField(validators=[MinLengthValidator(1)])
    state = models.CharField(max_length=16, choices=State.choices, default=State.CREATED, db_index=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['state', '-created_at']),
            models.Index(fields=['modem_index', '-created_at']),
            models.Index(fields=['-created_at']),
        ]

    def __str__(self) -> str:
        return f'→ {self.to_number} ({self.state})'
