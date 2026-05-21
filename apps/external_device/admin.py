"""Django admin for external device gateway models."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError as DRFValidationError

from .hidishelink_client import HiDishelinkApiClient, HiDishelinkApiError, mqtt_config_flat
from .models import (
    ExternalDevice,
    HiDishelinkDevice,
    InboxMessage,
    MqttGatewayCatalogEntry,
    SmsRecipientStatus,
    SmsRequest,
)
from .mqtt_client import ephemeral_connection_from_flat, publish_json_ephemeral, sanitize_device_id
from .services import (
    delete_external_device_and_dependencies,
    external_device_delete_preview,
    generate_token,
    hash_token,
    process_sms_request,
    sync_inbox_from_modem_store,
)


class ExternalDeviceSmsForm(forms.Form):
    recipients = forms.CharField(
        label=_('Recipient number(s)'),
        widget=forms.Textarea(attrs={'rows': 4, 'cols': 80}),
        help_text=_(
            'Contact phone number(s) in E.164 format (e.g. +351912345678); '
            'one number per line if sending to several recipients.'
        ),
    )
    message = forms.CharField(
        label=_('SMS body'),
        help_text=_(
            'Text sent to each recipient; cannot be empty or whitespace only '
            '(same validation as POST /api/v1/sms/send/).'
        ),
        widget=forms.Textarea(
            attrs={
                'rows': 10,
                'cols': 80,
                'placeholder': _('Type the SMS text here…'),
            }
        ),
    )
    priority = forms.ChoiceField(
        choices=[
            ('normal', _('Normal')),
            ('high', _('High')),
            ('urgent', _('Urgent')),
        ],
        label=_('Priority'),
        initial='normal',
    )


class InboxMessageAddForm(forms.ModelForm):
    """Add-only form with optional outbound SMS dispatch."""

    also_send_via_modem = forms.BooleanField(
        required=False,
        initial=False,
        label=_('Also send via modem'),
        help_text=_(
            'If checked, sends this message text as an outbound SMS to the Sender phone number '
            'using the selected gateway device (same pipeline as POST /api/v1/sms/send/). '
            'The device must be Active.'
        ),
    )

    class Meta:
        model = InboxMessage
        fields = ('device', 'sender', 'body', 'ack_sent')


class HiDishelinkMqttPublishForm(forms.Form):
    topic = forms.CharField(max_length=1024)
    payload_json = forms.CharField(
        label='Payload (JSON)',
        widget=forms.Textarea(attrs={'rows': 14, 'cols': 80}),
        initial='{}',
    )


def _hid_flat(payload: dict) -> dict[str, object]:
    return mqtt_config_flat(payload)


def _extract_api_key(payload: dict) -> str:
    flat = _hid_flat(payload)
    raw = flat.get('api_key') or payload.get('api_key')
    return str(raw).strip() if raw else ''


def _extract_session(payload: dict) -> tuple[str, object | None]:
    flat = _hid_flat(payload)
    sid = flat.get('session_id') or payload.get('session_id')
    sid_s = str(sid).strip() if sid else ''
    exp_raw = flat.get('expires_at') or payload.get('expires_at')
    exp_dt = parse_datetime(str(exp_raw)) if exp_raw else None
    return sid_s, exp_dt


@admin.register(ExternalDevice)
class ExternalDeviceAdmin(admin.ModelAdmin):
    change_form_template = 'admin/externaldevice_change_form.html'
    delete_confirmation_template = 'admin/externaldevice_delete_confirmation.html'

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
        'device_inbox_preview',
    ]
    fieldsets = (
        ('Device Info', {
            'fields': ('device_id', 'name', 'device_type', 'mqtt_client_id', 'metadata'),
            'description': format_html(
                'Optional JSON metadata. Use <code>"modem_index": N</code> to tie this device to '
                'one ModemManager modem. If several ExternalDevices share the same modem but only '
                'one should receive mirrored inbox SMS, set '
                '<code>"modem_inbox_mirror": false</code> on the others.'
            ),
        }),
        ('Authentication', {
            'fields': ('api_key_hash', 'registration_token_hash', 'registration_token_expires_at'),
        }),
        ('Status', {
            'fields': ('status', 'is_available', 'last_seen'),
        }),
        ('SMS inbox (MQTT / modem mirror)', {
            'fields': ('device_inbox_preview',),
            'description': 'Recent rows from InboxMessage for this device. Use “Sync inbox from modem” on the changelist to refresh from server modem store. '
            'Duplicate mmcli mirrors across devices are avoided by setting `"modem_inbox_mirror": false` in metadata on secondary devices.',
        }),
        ('Limits', {
            'fields': ('max_recipients_per_request', 'daily_sms_limit'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    actions = [
        'generate_registration_token',
        'sync_inbox_from_modem',
        'delete_device_and_dependencies',
        'delete_selected_devices_and_dependencies',
    ]

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

    def sync_inbox_from_modem(self, request, queryset):  # type: ignore[override]
        """Mirror modem InboundSms into InboxMessage for the selected device (same as API inbox list)."""
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one device.', level=messages.ERROR)
            return

        device = queryset.first()
        assert device is not None
        before = InboxMessage.objects.filter(device=device).count()
        sync_inbox_from_modem_store(device)
        after = InboxMessage.objects.filter(device=device).count()
        self.message_user(
            request,
            format_html(
                'Synced inbox from modem for <strong>{}</strong>: {} → {} message(s).',
                device.device_id,
                before,
                after,
            ),
            level=messages.SUCCESS,
        )

    sync_inbox_from_modem.short_description = 'Sync inbox from modem (select one device)'  # type: ignore[attr-defined]

    def delete_device_and_dependencies(self, request, queryset):  # type: ignore[override]
        """Redirect to admin delete confirmation for the selected device."""
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one device.', level=messages.ERROR)
            return None

        device = queryset.first()
        assert device is not None
        delete_url = reverse(
            'admin:%s_%s_delete' % (self.opts.app_label, self.opts.model_name),
            args=[device.pk],
            current_app=self.admin_site.name,
        )
        return HttpResponseRedirect(delete_url)

    delete_device_and_dependencies.short_description = 'Delete device and dependencies'  # type: ignore[attr-defined]

    def delete_selected_devices_and_dependencies(self, request, queryset):  # type: ignore[override]
        """Delete selected devices and all cascade dependencies (no Django PROTECT block)."""
        if not queryset.exists():
            self.message_user(request, 'Select at least one device.', level=messages.ERROR)
            return None
        self.delete_queryset(request, queryset)
        return None

    delete_selected_devices_and_dependencies.short_description = (  # type: ignore[attr-defined]
        'Delete selected devices and dependencies'
    )

    def device_inbox_preview(self, obj: ExternalDevice | None) -> str:
        """Recent InboxMessage rows for change form."""
        if obj is None or not obj.pk:
            return format_html('<p>Save the device first to see inbox preview.</p>')
        rows = list(InboxMessage.objects.filter(device=obj).order_by('-received_at')[:25])
        if not rows:
            return format_html('<p>No inbox rows yet for <code>{}</code>.</p>', obj.device_id)
        tbody_parts = []
        for m in rows:
            body_preview = (m.body or '')[:120]
            if len(m.body or '') > 120:
                body_preview += '…'
            tbody_parts.append(
                format_html(
                    '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>',
                    m.message_id,
                    m.sender,
                    m.received_at.isoformat(timespec='seconds'),
                    body_preview,
                )
            )
        tbody = mark_safe(''.join(str(x) for x in tbody_parts))
        return format_html(
            '<table class="listing"><thead><tr>'
            '<th>message_id</th><th>sender</th><th>received_at</th><th>body (trunc.)</th>'
            '</tr></thead><tbody>{}</tbody></table>',
            tbody,
        )

    device_inbox_preview.short_description = 'Recent inbox (MQTT / modem)'  # type: ignore[attr-defined]

    def change_view(self, request, object_id, form_url='', extra_context=None):  # type: ignore[override]
        extra_context = extra_context or {}
        info = self.opts.app_label, self.opts.model_name
        extra_context['external_send_sms_url'] = reverse(
            'admin:%s_%s_sendsms' % info,
            args=[object_id],
            current_app=self.admin_site.name,
        )
        extra_context['external_delete_url'] = reverse(
            'admin:%s_%s_delete' % info,
            args=[object_id],
            current_app=self.admin_site.name,
        )
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def delete_view(self, request, object_id, extra_context=None):  # type: ignore[override]
        extra_context = extra_context or {}
        obj = self.get_object(request, object_id)
        if obj is not None:
            extra_context['delete_preview'] = external_device_delete_preview(obj)
        return super().delete_view(request, object_id, extra_context=extra_context)

    def get_deleted_objects(self, objs, request):  # type: ignore[override]
        """Allow admin delete; PROTECT FKs are removed in :meth:`delete_model` cascade."""
        deleted_objects, model_count, _perms_needed, _protected = super().get_deleted_objects(objs, request)
        return deleted_objects, model_count, set(), set()

    def get_urls(self):  # type: ignore[override]
        info = self.opts.app_label, self.opts.model_name
        urls = [
            path(
                '<path:object_id>/send-sms/',
                self.admin_site.admin_view(self.send_sms_view),
                name='%s_%s_sendsms' % info,
            ),
        ]
        return urls + super().get_urls()

    def send_sms_view(self, request, object_id):  # type: ignore[no-untyped-def]
        try:
            obj = ExternalDevice.objects.get(pk=object_id)
        except ExternalDevice.DoesNotExist:
            messages.error(request, 'Device not found.')
            return HttpResponseRedirect(reverse('admin:external_device_externaldevice_changelist'))

        if not self.has_change_permission(request, obj):
            messages.error(request, 'Permission denied.')
            return HttpResponseRedirect(reverse('admin:index'))

        is_active = obj.status == ExternalDevice.Status.ACTIVE

        if request.method == 'POST':
            if not is_active:
                messages.error(request, 'Device must be Active to send SMS (same rule as API v1).')
                return HttpResponseRedirect(
                    reverse(
                        'admin:%s_%s_change' % (self.opts.app_label, self.opts.model_name),
                        args=[obj.pk],
                        current_app=self.admin_site.name,
                    )
                )
            form = ExternalDeviceSmsForm(request.POST)
            if form.is_valid():
                raw_lines = form.cleaned_data['recipients'].replace(',', '\n').splitlines()
                recipients = [ln.strip() for ln in raw_lines if ln.strip()]
                message = form.cleaned_data['message']
                priority = form.cleaned_data['priority']
                try:
                    sms_request = process_sms_request(
                        device=obj,
                        recipients=recipients,
                        message=message,
                        priority=priority,
                    )
                except DRFValidationError as exc:
                    detail = getattr(exc, 'detail', exc)
                    if isinstance(detail, dict):
                        msg = '; '.join(f'{k}: {detail[k]}' for k in sorted(detail))
                    else:
                        msg = str(detail)
                    messages.error(request, msg)
                else:
                    rid = sms_request.request_id
                    st = sms_request.status
                    sent_n = sms_request.sent_count
                    fail_n = sms_request.failed_count
                    if st == SmsRequest.Status.FAILED:
                        messages.error(
                            request,
                            format_html(
                                'SMS request <code>{}</code>: nothing sent ({} failed). '
                                'Inspect OutboundSms / mmcli logs; check MODEM_MMCLI_INDEX or '
                                'device metadata <code>modem_index</code>.',
                                rid,
                                fail_n,
                            ),
                        )
                    elif st == SmsRequest.Status.PARTIAL:
                        messages.warning(
                            request,
                            format_html(
                                'SMS request <code>{}</code>: partial — sent {}, failed {}.',
                                rid,
                                sent_n,
                                fail_n,
                            ),
                        )
                    else:
                        messages.success(
                            request,
                            format_html(
                                'SMS request <code>{}</code> completed — sent {} recipient(s).',
                                rid,
                                sent_n,
                            ),
                        )
                    return HttpResponseRedirect(
                        reverse(
                            'admin:%s_%s_change' % (self.opts.app_label, self.opts.model_name),
                            args=[obj.pk],
                            current_app=self.admin_site.name,
                        )
                    )
        else:
            form = ExternalDeviceSmsForm()

        ctx = {
            **self.admin_site.each_context(request),
            'title': 'Send SMS',
            'opts': self.opts,
            'original': obj,
            'form': form,
            'is_active': is_active,
        }
        return render(request, 'admin/externaldevice_send_sms.html', ctx)

    def delete_model(self, request, obj):  # type: ignore[override]
        stats = delete_external_device_and_dependencies(obj)
        self.message_user(
            request,
            format_html(
                'Deleted device <strong>{}</strong> and dependencies: <code>{}</code>',
                obj.device_id,
                json.dumps(stats, sort_keys=True),
            ),
            level=messages.SUCCESS,
        )

    def delete_queryset(self, request, queryset):  # type: ignore[override]
        combined: dict[str, int] = {}
        devices = list(queryset)
        for device in devices:
            part = delete_external_device_and_dependencies(device)
            for key, val in part.items():
                combined[key] = combined.get(key, 0) + val
        self.message_user(
            request,
            format_html(
                'Deleted <strong>{}</strong> device(s). Totals: <code>{}</code>',
                len(devices),
                json.dumps(combined, sort_keys=True),
            ),
            level=messages.SUCCESS,
        )


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


@admin.register(MqttGatewayCatalogEntry)
class MqttGatewayCatalogEntryAdmin(admin.ModelAdmin):
    list_display = ['kind', 'received_at']
    list_filter = ['kind']
    readonly_fields = ['kind', 'payload', 'received_at']


@admin.register(HiDishelinkDevice)
class HiDishelinkDeviceAdmin(admin.ModelAdmin):
    change_form_template = 'admin/hidishelink_change_form.html'

    list_display = [
        'device_id',
        'api_url',
        'status',
        'mqtt_config_fetched_at',
        'session_expires_at',
        'last_seen',
        'updated_at',
    ]
    list_filter = ['status']
    search_fields = ['device_id', 'api_url', 'notes']
    readonly_fields = [
        'mqtt_config_display',
        'inbox_preview',
        'mqtt_config_fetched_at',
        'session_expires_at',
        'last_seen',
        'created_at',
        'updated_at',
    ]
    fieldsets = (
        ('Remote hiDisheLink API', {
            'fields': ('api_url', 'device_id'),
            'description': 'Base URL example: <code>http://192.168.1.77:5201</code> (no <code>/api</code> suffix).',
        }),
        ('Credentials', {
            'fields': ('registration_token', 'api_key', 'session_id'),
            'description': 'Use admin actions to call remote REST endpoints; api_key is stored in plaintext for operators.',
        }),
        ('MQTT snapshot (from GET mqtt-config)', {
            'fields': ('mqtt_config_display', 'mqtt_config_fetched_at'),
        }),
        ('Inbox preview (matching ExternalDevice)', {
            'fields': ('inbox_preview',),
        }),
        ('Status', {
            'fields': ('status', 'sync_external_device', 'last_seen', 'last_api_error', 'notes'),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
        }),
    )
    actions = [
        'action_remote_register',
        'action_remote_get_pending_key',
        'action_remote_login',
        'action_remote_refresh_session',
        'action_remote_logout',
        'action_remote_fetch_mqtt_config',
        'action_remote_device_status',
        'action_open_send_mqtt_form',
        'action_start_mqtt_background',
        'action_clear_last_error',
    ]

    def save_model(self, request, obj, form, change):  # type: ignore[override]
        obj.api_url = obj.api_url.strip().rstrip('/')
        super().save_model(request, obj, form, change)

    def mqtt_config_display(self, obj: HiDishelinkDevice) -> str:
        if not obj.mqtt_config:
            return '(fetch mqtt-config via admin action)'
        try:
            pretty = json.dumps(obj.mqtt_config, indent=2, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            pretty = str(obj.mqtt_config)
        return format_html('<pre style="max-height:480px;overflow:auto;">{}</pre>', pretty)

    mqtt_config_display.short_description = 'MQTT config JSON'  # type: ignore[attr-defined]

    def inbox_preview(self, obj: HiDishelinkDevice) -> str:
        rows = (
            InboxMessage.objects.filter(device__device_id=obj.device_id)
            .select_related('device')
            .order_by('-received_at')[:25]
        )
        if not rows:
            return format_html('<p>No inbox rows for device_id=<code>{}</code>.</p>', obj.device_id)
        parts = ['<table class="listing"><thead><tr><th>message_id</th><th>sender</th><th>received_at</th></tr></thead><tbody>']
        for m in rows:
            parts.append(
                '<tr><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
                    m.message_id,
                    m.sender,
                    m.received_at.isoformat(timespec='seconds'),
                )
            )
        parts.append('</tbody></table>')
        return format_html(''.join(parts))

    inbox_preview.short_description = 'Recent inbox (MQTT)'  # type: ignore[attr-defined]

    def change_view(self, request, object_id, form_url='', extra_context=None):  # type: ignore[override]
        extra_context = extra_context or {}
        info = self.opts.app_label, self.opts.model_name
        extra_context['hid_send_mqtt_url'] = reverse(
            'admin:%s_%s_sendmqtt' % info,
            args=[object_id],
            current_app=self.admin_site.name,
        )
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def get_urls(self):  # type: ignore[override]
        info = self.opts.app_label, self.opts.model_name
        urls = [
            path(
                '<path:object_id>/send-mqtt/',
                self.admin_site.admin_view(self.send_mqtt_view),
                name='%s_%s_sendmqtt' % info,
            ),
        ]
        return urls + super().get_urls()

    def send_mqtt_view(self, request, object_id):  # type: ignore[no-untyped-def]
        obj = HiDishelinkDevice.objects.get(pk=object_id)
        if not self.has_change_permission(request, obj):
            messages.error(request, 'Permission denied.')
            return HttpResponseRedirect(reverse('admin:index'))

        cfg = obj.mqtt_config if isinstance(obj.mqtt_config, dict) else {}
        conn = ephemeral_connection_from_flat(cfg)

        initial_topic = ''
        tpl = cfg.get('TOPIC_SMS_SEND')
        if isinstance(tpl, str) and '{device_id}' in tpl:
            initial_topic = tpl.replace('{device_id}', sanitize_device_id(obj.device_id))

        if request.method == 'POST':
            form = HiDishelinkMqttPublishForm(request.POST)
            if form.is_valid():
                try:
                    body = json.loads(form.cleaned_data['payload_json'])
                    if not isinstance(body, dict):
                        raise ValueError('Payload must be a JSON object.')
                except (json.JSONDecodeError, ValueError) as exc:
                    messages.error(request, f'Invalid JSON: {exc}')
                else:
                    publish_json_ephemeral(form.cleaned_data['topic'], body, mqtt_connection=conn)
                    messages.success(request, 'MQTT publish submitted.')
                    return HttpResponseRedirect(
                        reverse(
                            'admin:%s_%s_change' % (self.opts.app_label, self.opts.model_name),
                            args=[obj.pk],
                            current_app=self.admin_site.name,
                        )
                    )
        else:
            form = HiDishelinkMqttPublishForm(initial={'topic': initial_topic})

        ctx = {
            **self.admin_site.each_context(request),
            'title': 'Publish MQTT JSON',
            'opts': self.opts,
            'original': obj,
            'form': form,
            'conn_preview_keys': ', '.join(sorted(conn.keys())) if conn else '(defaults from Django settings)',
        }
        return render(request, 'admin/hidishelink_send_mqtt.html', ctx)

    # --- Admin actions (remote REST) ---

    def _require_one(self, request, queryset):  # type: ignore[no-untyped-def]
        if queryset.count() != 1:
            self.message_user(request, 'Select exactly one HiDisheLink device.', level=messages.ERROR)
            return None
        return queryset.first()

    def _client_for(self, obj: HiDishelinkDevice) -> HiDishelinkApiClient:
        return HiDishelinkApiClient(base_url=obj.api_url)

    def _fail(self, obj: HiDishelinkDevice, exc: HiDishelinkApiError | Exception) -> None:
        msg = str(exc)
        if isinstance(exc, HiDishelinkApiError) and exc.body:
            msg = f'{msg} — {exc.body}'
        obj.last_api_error = msg[:8000]
        obj.status = HiDishelinkDevice.Status.ERROR
        obj.save(update_fields=['last_api_error', 'status', 'updated_at'])

    def action_remote_register(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.registration_token.strip():
            self.message_user(request, 'Set registration_token on the device row first.', level=messages.ERROR)
            return
        try:
            payload = self._client_for(obj).register(
                device_id=obj.device_id,
                registration_token=obj.registration_token,
            )
            key = _extract_api_key(payload)
            if key:
                obj.api_key = key
            obj.registration_token = ''
            obj.status = HiDishelinkDevice.Status.REGISTERED
            obj.last_api_error = ''
            obj.save(update_fields=['api_key', 'registration_token', 'status', 'last_api_error', 'updated_at'])
            self.message_user(request, 'Remote register succeeded; api_key saved.', level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'Remote register failed: {exc}', level=messages.ERROR)

    action_remote_register.short_description = 'Remote: POST register (uses registration_token)'  # type: ignore[attr-defined]

    def action_remote_get_pending_key(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        try:
            payload = self._client_for(obj).get_pending_key(device_id=obj.device_id)
            key = _extract_api_key(payload)
            if not key:
                raise HiDishelinkApiError('No api_key in response', status_code=None, body=payload)
            obj.api_key = key
            obj.status = HiDishelinkDevice.Status.REGISTERED
            obj.last_api_error = ''
            obj.save(update_fields=['api_key', 'status', 'last_api_error', 'updated_at'])
            self.message_user(request, 'get-pending-key succeeded; api_key saved.', level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'get-pending-key failed: {exc}', level=messages.ERROR)

    action_remote_get_pending_key.short_description = 'Remote: POST get-pending-key'  # type: ignore[attr-defined]

    def action_remote_login(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.api_key.strip():
            self.message_user(request, 'api_key is required.', level=messages.ERROR)
            return
        try:
            payload = self._client_for(obj).login(device_id=obj.device_id, api_key=obj.api_key)
            sid, exp = _extract_session(payload)
            obj.session_id = sid
            obj.session_expires_at = exp
            obj.status = HiDishelinkDevice.Status.ACTIVE
            obj.last_api_error = ''
            obj.save(
                update_fields=['session_id', 'session_expires_at', 'status', 'last_api_error', 'updated_at']
            )
            self.message_user(request, 'Remote login succeeded.', level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'Login failed: {exc}', level=messages.ERROR)

    action_remote_login.short_description = 'Remote: POST login (session)'  # type: ignore[attr-defined]

    def action_remote_refresh_session(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.session_id.strip():
            self.message_user(request, 'session_id is required.', level=messages.ERROR)
            return
        try:
            payload = self._client_for(obj).refresh_session(device_id=obj.device_id, session_id=obj.session_id)
            _, exp = _extract_session(payload)
            if exp:
                obj.session_expires_at = exp
            obj.last_api_error = ''
            obj.save(update_fields=['session_expires_at', 'last_api_error', 'updated_at'])
            self.message_user(request, 'Session refreshed.', level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'refresh failed: {exc}', level=messages.ERROR)

    action_remote_refresh_session.short_description = 'Remote: POST refresh session'  # type: ignore[attr-defined]

    def action_remote_logout(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.session_id.strip():
            self.message_user(request, 'Nothing to logout.', level=messages.INFO)
            return
        try:
            self._client_for(obj).logout(device_id=obj.device_id, session_id=obj.session_id)
            obj.session_id = ''
            obj.session_expires_at = None
            obj.last_api_error = ''
            obj.save(update_fields=['session_id', 'session_expires_at', 'last_api_error', 'updated_at'])
            self.message_user(request, 'Remote logout succeeded.', level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'Logout failed: {exc}', level=messages.ERROR)

    action_remote_logout.short_description = 'Remote: POST logout'  # type: ignore[attr-defined]

    def action_remote_fetch_mqtt_config(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.api_key.strip():
            self.message_user(request, 'api_key is required.', level=messages.ERROR)
            return
        try:
            raw = self._client_for(obj).get_mqtt_config(device_id=obj.device_id, api_key=obj.api_key)
            flat = _hid_flat(raw)
            obj.mqtt_config = flat
            obj.mqtt_config_fetched_at = timezone.now()
            obj.last_seen = timezone.now()
            obj.last_api_error = ''
            obj.save(
                update_fields=[
                    'mqtt_config',
                    'mqtt_config_fetched_at',
                    'last_seen',
                    'last_api_error',
                    'updated_at',
                ]
            )
            self.message_user(request, 'mqtt-config fetched and stored.', level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'mqtt-config failed: {exc}', level=messages.ERROR)

    action_remote_fetch_mqtt_config.short_description = 'Remote: GET mqtt-config'  # type: ignore[attr-defined]

    def action_remote_device_status(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.api_key.strip():
            self.message_user(request, 'api_key is required.', level=messages.ERROR)
            return
        try:
            raw = self._client_for(obj).get_device_status(device_id=obj.device_id, api_key=obj.api_key)
            obj.last_seen = timezone.now()
            obj.last_api_error = ''
            obj.save(update_fields=['last_seen', 'last_api_error', 'updated_at'])
            self.message_user(request, format_html('<pre>{}</pre>', json.dumps(raw, indent=2, ensure_ascii=False)), level=messages.SUCCESS)
        except HiDishelinkApiError as exc:
            self._fail(obj, exc)
            self.message_user(request, f'device/status failed: {exc}', level=messages.ERROR)

    action_remote_device_status.short_description = 'Remote: GET device status (shows JSON)'  # type: ignore[attr-defined]

    def action_open_send_mqtt_form(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not obj.mqtt_config:
            self.message_user(request, 'Fetch mqtt-config first.', level=messages.ERROR)
            return
        info = self.opts.app_label, self.opts.model_name
        url = reverse('admin:%s_%s_sendmqtt' % info, args=[obj.pk], current_app=self.admin_site.name)
        return HttpResponseRedirect(url)

    action_open_send_mqtt_form.short_description = 'Open MQTT publish form (uses mqtt-config broker)'  # type: ignore[attr-defined]

    def action_start_mqtt_background(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        if not isinstance(obj.mqtt_config, dict):
            self.message_user(request, 'Fetch mqtt-config first.', level=messages.ERROR)
            return

        from .mqtt_client import GatewayMqttClient

        cfg = dict(obj.mqtt_config)

        def runner() -> None:
            client = GatewayMqttClient(mqtt_config=cfg)
            client.connect()
            client.loop_forever()

        threading.Thread(target=runner, daemon=True, name=f'hidishelink-mqtt-{obj.pk}').start()
        self.message_user(
            request,
            'Started daemon MQTT thread (development only). Prefer management command run_mqtt_gateway for production.',
            level=messages.WARNING,
        )

    action_start_mqtt_background.short_description = 'Start MQTT client loop (daemon thread)'  # type: ignore[attr-defined]

    def action_clear_last_error(self, request, queryset):  # type: ignore[override]
        obj = self._require_one(request, queryset)
        if not obj:
            return
        obj.last_api_error = ''
        obj.save(update_fields=['last_api_error', 'updated_at'])
        self.message_user(request, 'Cleared last_api_error.', level=messages.SUCCESS)

    action_clear_last_error.short_description = 'Clear last_api_error field'  # type: ignore[attr-defined]


@admin.register(InboxMessage)
class InboxMessageAdmin(admin.ModelAdmin):
    list_display = ['message_id', 'device', 'sender', 'received_at', 'ack_sent', 'created_at']
    list_filter = ['device', 'ack_sent']
    search_fields = ['message_id', 'sender', 'body', 'device__device_id']
    readonly_fields = ['message_id', 'device', 'sender', 'body', 'received_at', 'created_at']
    fieldsets = (
        (_('Message'), {
            'fields': ('message_id', 'device', 'sender', 'body', 'received_at'),
        }),
        (_('Status'), {
            'fields': ('ack_sent',),
        }),
        (_('Timestamps'), {
            'fields': ('created_at',),
        }),
    )

    def get_fieldsets(self, request, obj=None):  # type: ignore[override]
        """Minimal add form: gateway device, sender phone, and body only."""
        if obj is None:
            return (
                (
                    _('Manual inbox entry'),
                    {
                        'description': _(
                            'Pick the gateway device this inbound SMS belongs to, the sender '
                            'contact number, and the message text. Message ID and received time '
                            'are assigned automatically. By default only the inbox row is stored; '
                            'use «Also send via modem» to dispatch the same text to the Sender number.'
                        ),
                        'fields': ('device', 'sender', 'body', 'ack_sent', 'also_send_via_modem'),
                    },
                ),
                (
                    _('Timestamps'),
                    {'fields': ('created_at',)},
                ),
            )
        return super().get_fieldsets(request, obj)

    def get_exclude(self, request, obj=None):  # type: ignore[override]
        if obj is None:
            return ('message_id', 'received_at')
        return super().get_exclude(request, obj) or ()

    def get_form(self, request, obj=None, change=False, **kwargs):  # type: ignore[override]
        if obj is None and not change:
            kwargs.setdefault('form', InboxMessageAddForm)
        return super().get_form(request, obj, change=change, **kwargs)

    def get_readonly_fields(self, request, obj=None):  # type: ignore[override]
        """Allow operators to fill canonical fields when adding; keep rows immutable after save."""
        if obj is None:
            return ['created_at']
        return list(self.readonly_fields)

    def save_model(self, request, obj, form, change):  # type: ignore[override]
        """Generate inbox_manual_* id and timestamp when omitted from the add form."""
        if not change:
            if not (obj.message_id or '').strip():
                obj.message_id = f'inbox_manual_{uuid.uuid4().hex}'
            if obj.received_at is None:
                obj.received_at = timezone.now()
        super().save_model(request, obj, form, change)

        if change or form is None or not hasattr(form, 'cleaned_data'):
            return
        if not form.cleaned_data.get('also_send_via_modem'):
            return

        device = obj.device
        if device.status != ExternalDevice.Status.ACTIVE:
            self.message_user(
                request,
                _(
                    'Inbox row was saved; SMS was not sent because the gateway device is not Active.'
                ),
                level=messages.WARNING,
            )
            return

        recipient = (obj.sender or '').strip()
        if not recipient:
            self.message_user(
                request,
                _('Inbox row was saved; SMS was not sent because Sender is empty.'),
                level=messages.WARNING,
            )
            return

        try:
            sms_request = process_sms_request(
                device=device,
                recipients=[recipient],
                message=obj.body,
                priority='normal',
            )
        except DRFValidationError as exc:
            detail = getattr(exc, 'detail', exc)
            if isinstance(detail, dict):
                msg = '; '.join(f'{k}: {detail[k]}' for k in sorted(detail))
            else:
                msg = str(detail)
            self.message_user(request, msg, level=messages.ERROR)
            return

        rid = sms_request.request_id
        st = sms_request.status
        sent_n = sms_request.sent_count
        fail_n = sms_request.failed_count
        if st == SmsRequest.Status.FAILED:
            fail_detail = ''
            rs = (
                SmsRecipientStatus.objects.filter(request=sms_request)
                .exclude(error_message='')
                .order_by('id')
                .first()
            )
            if rs and rs.error_message:
                fail_detail = rs.error_message
            if fail_detail:
                self.message_user(
                    request,
                    format_html(
                        'SMS dispatch for this row: nothing sent (<code>{}</code>, {} failed). '
                        'Modem/mmcli: <strong>{}</strong>. '
                        'If the modem was disabled, retry after it shows enabled in mmcli; '
                        'otherwise check MODEM_MMCLI_INDEX or device metadata '
                        '<code>modem_index</code>.',
                        rid,
                        fail_n,
                        fail_detail,
                    ),
                    level=messages.ERROR,
                )
            else:
                self.message_user(
                    request,
                    format_html(
                        'SMS dispatch for this row: nothing sent (<code>{}</code>, {} failed). '
                        'Check OutboundSms / mmcli; MODEM_MMCLI_INDEX or device metadata '
                        '<code>modem_index</code>.',
                        rid,
                        fail_n,
                    ),
                    level=messages.ERROR,
                )
        elif st == SmsRequest.Status.PARTIAL:
            self.message_user(
                request,
                format_html(
                    'SMS dispatch for this row: partial success <code>{}</code> — sent {}, failed {}.',
                    rid,
                    sent_n,
                    fail_n,
                ),
                level=messages.WARNING,
            )
        else:
            self.message_user(
                request,
                format_html(
                    'SMS also sent: request <code>{}</code> — {} recipient(s) ok.',
                    rid,
                    sent_n,
                ),
                level=messages.SUCCESS,
            )
