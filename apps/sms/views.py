"""Django REST views for ModemManager-backed SMS endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import InboundSms, OutboundSms
from .serializers import (
    InboundSmsSerializer,
    OutboundSmsCreateSerializer,
    OutboundSmsSerializer,
)
from .services import dispatch_outbound_mmcli

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from rest_framework.request import Request


@extend_schema_view(
    list=extend_schema(
        summary='List received SMS',
        tags=['SMS - inbound'],
        parameters=[
            OpenApiParameter(
                name='from',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description='Filter by originating number (contains). Requires JWT (`Authorization: Bearer <access>`).',
            ),
            OpenApiParameter(
                name='since',
                type=OpenApiTypes.DATETIME,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    'Messages with created_at greater than or equal to this ISO-8601 timestamp. '
                    'Requires JWT (see `/api/auth/token/`).'
                ),
            ),
        ],
    ),
    retrieve=extend_schema(
        summary='Retrieve received SMS',
        tags=['SMS - inbound'],
        description='Requires JWT obtained from `/api/auth/token/`.',
    ),
)
class InboundSmsViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = InboundSms.objects.all()
    serializer_class = InboundSmsSerializer

    def get_queryset(self) -> QuerySet:
        qs = super().get_queryset()

        frm_raw = self.request.query_params.get('from')
        if frm_raw is not None and len(frm_raw) > 256:
            raise ValidationError({'from': ['Query parameter is too long (max 256 characters).']})
        if frm_raw:
            qs = qs.filter(from_number__icontains=frm_raw)

        since = self.request.query_params.get('since')
        if since:
            dt = parse_datetime(since)
            if dt is None:
                raise ValidationError({'since': ['Invalid ISO-8601 format.']})
            qs = qs.filter(created_at__gte=dt)
        return qs


@extend_schema_view(
    list=extend_schema(
        summary='List sent SMS',
        tags=['SMS - outbound'],
        description='Requires JWT (`Authorization: Bearer <access>`).',
    ),
    retrieve=extend_schema(
        summary='Retrieve sent SMS',
        tags=['SMS - outbound'],
        description='Retrieve a stored outbound record. Requires JWT.',
    ),
)
class OutboundSmsViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = OutboundSms.objects.all()

    def get_serializer_class(self):  # type: ignore[override]
        if self.action == 'create':
            return OutboundSmsCreateSerializer
        return OutboundSmsSerializer

    @extend_schema(
        summary='Send SMS (mmcli: create object + send)',
        tags=['SMS - outbound'],
        description=(
            'Creates an outbound record and submits it via ModemManager (JWT required). '
            'Check the ``state`` field: ``sent`` means delivered to the modem; ``failed`` means mmcli aborted. '
            'HTTP 202 indicates the record has been accepted for processing — inspect ``state`` for the mmcli outcome.'
        ),
        responses={status.HTTP_202_ACCEPTED: OutboundSmsSerializer},
        examples=[
            OpenApiExample(
                name='Portuguese_mobile',
                value={'modem_index': 0, 'to': '+351913000387', 'text': 'Hello from hiWaveTel'},
                request_only=True,
            ),
        ],
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        ser = OutboundSmsCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        modem_index = int(ser.validated_data.get('modem_index', settings.MODEM_MMCLI_INDEX))
        to_number = ser.validated_data['to'].strip()
        text_body = ser.validated_data['text']

        outbound = OutboundSms.objects.create(
            modem_index=modem_index,
            to_number=to_number,
            text=text_body,
            state=OutboundSms.State.CREATED,
        )

        from apps.sms.outbound_processor import enqueue_outbound_job, outbound_async_enabled

        if outbound_async_enabled():
            enqueue_outbound_job('outbound', str(outbound.pk), priority='normal')
        else:
            dispatch_outbound_mmcli(outbound)

        out = OutboundSmsSerializer(outbound, context={'request': request})
        return Response(out.data, status=status.HTTP_202_ACCEPTED)
