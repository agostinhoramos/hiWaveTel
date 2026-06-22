"""Modem registry REST endpoints (public, no auth)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ModemDevice
from .modem_registry import (
    build_modem_summary,
    get_modem_detail,
    sync_detected_modems,
)
from .serializers import (
    ModemDeviceDetailSerializer,
    ModemDeviceSummarySerializer,
    ModemDeviceUpdateSerializer,
)


class ModemListView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='List detected modems',
        tags=['Modems'],
        responses={status.HTTP_200_OK: ModemDeviceSummarySerializer(many=True)},
    )
    def get(self, request) -> Response:  # noqa: ARG002
        devices = ModemDevice.objects.order_by('modem_index')
        payload = [build_modem_summary(device) for device in devices]
        serializer = ModemDeviceSummarySerializer(payload, many=True)
        return Response(serializer.data)


class ModemSyncView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Sync modems from ModemManager',
        tags=['Modems'],
        responses={status.HTTP_200_OK: ModemDeviceSummarySerializer(many=True)},
    )
    def post(self, request) -> Response:  # noqa: ARG002
        devices = sync_detected_modems()
        payload = [build_modem_summary(device) for device in devices]
        serializer = ModemDeviceSummarySerializer(payload, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ModemDetailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Modem detail',
        tags=['Modems'],
        responses={
            status.HTTP_200_OK: ModemDeviceDetailSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def get(self, request, modem_index: int) -> Response:  # noqa: ARG002
        detail = get_modem_detail(modem_index)
        if detail is None:
            return Response(
                {'detail': f'Modem {modem_index} has not been detected yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = ModemDeviceDetailSerializer(detail)
        return Response(serializer.data)

    @extend_schema(
        summary='Update modem settings',
        tags=['Modems'],
        request=ModemDeviceUpdateSerializer,
        responses={
            status.HTTP_200_OK: ModemDeviceDetailSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def put(self, request, modem_index: int) -> Response:
        try:
            device = ModemDevice.objects.get(modem_index=modem_index)
        except ModemDevice.DoesNotExist:
            return Response(
                {'detail': f'Modem {modem_index} has not been detected yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        update_ser = ModemDeviceUpdateSerializer(data=request.data)
        update_ser.is_valid(raise_exception=True)
        device.enabled = update_ser.validated_data['enabled']
        device.save(update_fields=['enabled'])

        detail = get_modem_detail(modem_index)
        serializer = ModemDeviceDetailSerializer(detail)
        return Response(serializer.data)
