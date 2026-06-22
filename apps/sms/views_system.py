"""System-level SMS endpoints (public, no auth)."""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .container_restart import container_restart_allowed, schedule_container_restart
from .modem_readiness import check_modem_availability
from .serializers import ContainerRestartSerializer, ModemAvailabilitySerializer


class ModemAvailabilityView(APIView):
    """Lightweight probe for a single ModemManager index."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Modem availability',
        tags=['System'],
        description=(
            'Checks whether the given mmcli modem index exists and responds. '
            'No authentication required.'
        ),
        responses={
            status.HTTP_200_OK: ModemAvailabilitySerializer,
            status.HTTP_503_SERVICE_UNAVAILABLE: ModemAvailabilitySerializer,
        },
    )
    def get(self, request, modem_index: int) -> Response:  # noqa: ARG002
        result = check_modem_availability(modem_index)
        serializer = ModemAvailabilitySerializer(result.to_dict())
        http_status = (
            status.HTTP_200_OK if result.available else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(serializer.data, status=http_status)


class ContainerRestartView(APIView):
    """Schedule container recycle via SIGTERM to PID 1 (Docker restart policy)."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Restart container',
        tags=['System'],
        description=(
            'Schedules SIGTERM to PID 1 after a short delay so the HTTP response can flush. '
            'Requires Docker restart policy (e.g. unless-stopped). No authentication required.'
        ),
        responses={
            status.HTTP_202_ACCEPTED: ContainerRestartSerializer,
            status.HTTP_403_FORBIDDEN: None,
        },
    )
    def post(self, request) -> Response:
        if not container_restart_allowed():
            return Response(
                {'detail': 'Container restart via API is disabled.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        requested_by = (request.META.get('REMOTE_ADDR') or 'unknown').strip()
        delay_sec = schedule_container_restart(requested_by=requested_by)
        scheduled_at = timezone.now().isoformat()
        payload = {
            'accepted': True,
            'message': 'Container restart scheduled.',
            'scheduled_at': scheduled_at,
            'delay_sec': delay_sec,
            'requested_by': requested_by,
        }
        serializer = ContainerRestartSerializer(payload)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)
