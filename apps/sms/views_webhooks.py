"""Inbound webhook REST endpoints (public, no auth)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import InboundWebhook
from .modem_registry import ModemNotEnumeratedError, assert_modem_enumerated
from .serializers import (
    InboundWebhookCreateSerializer,
    InboundWebhookSerializer,
    InboundWebhookUpdateSerializer,
)


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


def _get_modem_webhook(modem_index: int, webhook_id: int) -> InboundWebhook | None:
    try:
        webhook = InboundWebhook.objects.get(pk=webhook_id)
    except InboundWebhook.DoesNotExist:
        return None
    if webhook.modem_index != modem_index:
        return None
    return webhook


class ModemWebhookDetailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Get inbound webhook for a modem',
        tags=['Webhooks'],
        responses={
            status.HTTP_200_OK: InboundWebhookSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def get(self, request, modem_index: int, webhook_id: int) -> Response:  # noqa: ARG002
        webhook = _get_modem_webhook(modem_index, webhook_id)
        if webhook is None:
            return Response(
                {'detail': f'Webhook {webhook_id} not found for modem {modem_index}.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(InboundWebhookSerializer(webhook).data)

    @extend_schema(
        summary='Update inbound webhook for a modem',
        tags=['Webhooks'],
        request=InboundWebhookUpdateSerializer,
        responses={
            status.HTTP_200_OK: InboundWebhookSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def put(self, request, modem_index: int, webhook_id: int) -> Response:
        webhook = _get_modem_webhook(modem_index, webhook_id)
        if webhook is None:
            return Response(
                {'detail': f'Webhook {webhook_id} not found for modem {modem_index}.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = InboundWebhookUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(
                {'detail': 'At least one field (name, url, enabled) is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for field, value in serializer.validated_data.items():
            setattr(webhook, field, value)
        webhook.save()

        return Response(InboundWebhookSerializer(webhook).data)

    @extend_schema(
        summary='Partially update inbound webhook for a modem',
        tags=['Webhooks'],
        request=InboundWebhookUpdateSerializer,
        responses={
            status.HTTP_200_OK: InboundWebhookSerializer,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def patch(self, request, modem_index: int, webhook_id: int) -> Response:
        return self.put(request, modem_index, webhook_id)

    @extend_schema(
        summary='Delete inbound webhook for a modem',
        tags=['Webhooks'],
        responses={
            status.HTTP_204_NO_CONTENT: None,
            status.HTTP_404_NOT_FOUND: None,
        },
    )
    def delete(self, request, modem_index: int, webhook_id: int) -> Response:  # noqa: ARG002
        webhook = _get_modem_webhook(modem_index, webhook_id)
        if webhook is None:
            return Response(
                {'detail': f'Webhook {webhook_id} not found for modem {modem_index}.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        webhook.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
