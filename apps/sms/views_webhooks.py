"""Inbound webhook REST endpoints (public, no auth)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import InboundWebhook
from .modem_registry import ModemNotEnumeratedError, assert_modem_enumerated
from .serializers import InboundWebhookCreateSerializer, InboundWebhookSerializer


class WebhookListView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='List inbound webhooks',
        tags=['Webhooks'],
        responses={status.HTTP_200_OK: InboundWebhookSerializer(many=True)},
    )
    def get(self, request) -> Response:  # noqa: ARG002
        queryset = InboundWebhook.objects.order_by('modem_index', 'id')
        serializer = InboundWebhookSerializer(queryset, many=True)
        return Response(serializer.data)


class ModemWebhookCreateView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Create inbound webhook for a modem',
        tags=['Webhooks'],
        request=InboundWebhookCreateSerializer,
        responses={
            status.HTTP_201_CREATED: InboundWebhookSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def post(self, request, modem_index: int) -> Response:
        try:
            assert_modem_enumerated(modem_index)
        except ModemNotEnumeratedError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_404_NOT_FOUND)

        serializer = InboundWebhookCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        webhook = InboundWebhook.objects.create(
            modem_index=modem_index,
            **data,
        )
        return Response(
            InboundWebhookSerializer(webhook).data,
            status=status.HTTP_201_CREATED,
        )
