"""Public REST API for SMS send (no authentication)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import OutboundSms
from .serializers import OutboundSmsCreateSerializer, OutboundSmsSerializer
from .services import dispatch_outbound_mmcli

if TYPE_CHECKING:
    from rest_framework.request import Request


class SendSmsView(APIView):
    """Send SMS via ModemManager (mmcli). No authentication required."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        summary='Send SMS',
        tags=['SMS'],
        description=(
            'Creates an outbound record and submits it via ModemManager/mmcli. '
            'Returns HTTP 202; inspect ``state`` for send outcome (`sent` / `failed`).'
        ),
        request=OutboundSmsCreateSerializer,
        responses={status.HTTP_202_ACCEPTED: OutboundSmsSerializer},
        examples=[
            OpenApiExample(
                name='Portuguese_mobile',
                value={'modem_index': 0, 'to': '+351913000387', 'text': 'Hello from hiWaveTel'},
                request_only=True,
            ),
        ],
    )
    def post(self, request: Request) -> Response:
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
