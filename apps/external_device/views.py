"""DRF views for external device gateway API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ApiKeyAuthentication, IsActiveExternalDevice
from .models import ExternalDevice, InboxMessage, SmsRequest
from .mqtt_client import publish_health_ping_ephemeral
from .serializers import (
    DeviceHealthPingResponseSerializer,
    DeviceHealthSerializer,
    InboxMessageSerializer,
    RegisterDeviceSerializer,
    RegisterDeviceResponseSerializer,
    SmsSendRequestSerializer,
    SmsSendResponseSerializer,
    SmsStatusResponseSerializer,
)
from .services import persist_inbox_from_mqtt, process_sms_request, register_device, sync_inbox_from_modem_store

if TYPE_CHECKING:
    from rest_framework.request import Request

_LOGGER = logging.getLogger(__name__)


class RegisterDeviceView(APIView):
    """Register an external device and receive API key."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        request=RegisterDeviceSerializer,
        responses={200: RegisterDeviceResponseSerializer},
        summary='Register external device',
        description='Register a device using a one-time registration token (created by admin). Returns API key (shown once only).',
        tags=['External Device'],
    )
    def post(self, request: Request) -> Response:
        """Register device and return API key."""
        serializer = RegisterDeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device, raw_api_key = register_device(serializer.validated_data)

        response_data = {
            'api_key': raw_api_key,
            'device_id': device.device_id,
            'status': device.status,
        }
        response_serializer = RegisterDeviceResponseSerializer(response_data)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


class SmsSendView(APIView):
    """Send SMS via gateway."""

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsActiveExternalDevice]

    @extend_schema(
        request=SmsSendRequestSerializer,
        responses={202: SmsSendResponseSerializer},
        summary='Send SMS',
        description='Send SMS to one or more recipients. Requires API key authentication.',
        tags=['SMS'],
    )
    def post(self, request: Request) -> Response:
        """Send SMS request."""
        serializer = SmsSendRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device = request.user
        if not isinstance(device, ExternalDevice):
            return Response({'error': 'Invalid authentication.'}, status=status.HTTP_401_UNAUTHORIZED)

        sms_request = process_sms_request(
            device=device,
            recipients=serializer.validated_data['recipients'],
            message=serializer.validated_data['message'],
            priority=serializer.validated_data['priority'],
        )

        response_data = {
            'request_id': sms_request.request_id,
            'status': sms_request.status,
        }
        response_serializer = SmsSendResponseSerializer(response_data)
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)


class SmsStatusView(GenericAPIView):
    """Get SMS request status."""

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsActiveExternalDevice]
    serializer_class = SmsStatusResponseSerializer

    @extend_schema(
        summary='Get SMS status',
        description='Retrieve status of an SMS request by request_id. Requires API key authentication.',
        parameters=[
            OpenApiParameter(
                name='request_id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=True,
                description='SMS request ID returned by POST /api/v1/sms/send/.',
            ),
        ],
        tags=['SMS'],
    )
    def get(self, request: Request) -> Response:
        """Get SMS request status by request_id query param."""
        request_id = request.query_params.get('request_id', '').strip()
        if not request_id:
            return Response({'error': 'request_id query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)

        device = request.user
        if not isinstance(device, ExternalDevice):
            return Response({'error': 'Invalid authentication.'}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            sms_request = SmsRequest.objects.prefetch_related('recipient_statuses').get(
                request_id=request_id,
                device=device,
            )
        except SmsRequest.DoesNotExist:
            return Response({'error': 'SMS request not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = self.get_serializer(sms_request)
        return Response(serializer.data, status=status.HTTP_200_OK)


class SmsInboxView(ListAPIView):
    """List inbox messages."""

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsActiveExternalDevice]
    serializer_class = InboxMessageSerializer

    @extend_schema(
        summary='List inbox messages',
        description='List SMS messages received by this device. Requires API key authentication.',
        tags=['SMS'],
    )
    def get_queryset(self):  # type: ignore[override]
        """Filter inbox by authenticated device."""
        device = self.request.user
        if isinstance(device, ExternalDevice):
            before_count = InboxMessage.objects.filter(device=device).count()
            _LOGGER.info(
                'Inbox list request device=%s status=%s metadata=%s before_count=%s',
                device.device_id,
                device.status,
                device.metadata,
                before_count,
            )
            sync_inbox_from_modem_store(device)
            queryset = InboxMessage.objects.filter(device=device)
            after_count = queryset.count()
            _LOGGER.info(
                'Inbox list response-prep device=%s after_count=%s',
                device.device_id,
                after_count,
            )
            if after_count == 0:
                _LOGGER.warning(
                    'Inbox list device=%s returned empty results after modem sync',
                    device.device_id,
                )
            return queryset
        return InboxMessage.objects.none()


class DeviceHealthView(RetrieveAPIView):
    """Get device health status."""

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsActiveExternalDevice]
    serializer_class = DeviceHealthSerializer
    lookup_field = 'device_id'
    queryset = ExternalDevice.objects.all()

    @extend_schema(
        summary='Get device health',
        description='Retrieve health status of a device. Requires API key authentication.',
        tags=['External Device'],
    )
    def get(self, request: Request, *args, **kwargs) -> Response:
        """Get device health status."""
        return super().get(request, *args, **kwargs)


class DeviceHealthPingView(APIView):
    """Publish active MQTT health ping (hiDisheLink) so the gateway/app can answer with ``health/pong``."""

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsActiveExternalDevice]

    @extend_schema(
        request=None,
        responses={200: DeviceHealthPingResponseSerializer},
        summary='Publish MQTT health ping',
        description=(
            'Publishes a JSON ping with ``source: django`` on ``TOPIC_HEALTH_PING`` from mqtt-config '
            '(or legacy `{prefix}/devices/{sanitized}/health/ping`). Broker credentials come from '
            'stored HiDisheLink mqtt-config when present, otherwise Django MQTT env settings.'
        ),
        tags=['External Device'],
    )
    def post(self, request: Request, device_id: str) -> Response:
        auth_device = request.user
        if not isinstance(auth_device, ExternalDevice):
            return Response({'error': 'Invalid authentication.'}, status=status.HTTP_401_UNAUTHORIZED)
        if auth_device.device_id != device_id:
            return Response({'error': 'Device mismatch.'}, status=status.HTTP_403_FORBIDDEN)

        body, ok, topic = publish_health_ping_ephemeral(auth_device.device_id)
        payload = {
            'ping_id': body['ping_id'],
            'timestamp': body['timestamp'],
            'source': body['source'],
            'published': ok,
            'mqtt_topic': topic,
        }
        ser = DeviceHealthPingResponseSerializer(payload)
        return Response(ser.data, status=status.HTTP_200_OK)
