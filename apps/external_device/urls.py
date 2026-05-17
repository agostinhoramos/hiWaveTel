"""URL routing for external device gateway API."""

from django.urls import path

from .views import (
    DeviceHealthPingView,
    DeviceHealthView,
    RegisterDeviceView,
    SmsInboxView,
    SmsSendView,
    SmsStatusView,
)

urlpatterns = [
    path('external-devices/register/', RegisterDeviceView.as_view(), name='external-device-register'),
    path('sms/send/', SmsSendView.as_view(), name='external-device-sms-send'),
    path('sms/status/', SmsStatusView.as_view(), name='external-device-sms-status'),
    path('sms/inbox/', SmsInboxView.as_view(), name='external-device-sms-inbox'),
    path('external-devices/<str:device_id>/health/ping/', DeviceHealthPingView.as_view(), name='external-device-health-ping'),
    path('external-devices/<str:device_id>/health/', DeviceHealthView.as_view(), name='external-device-health'),
]
