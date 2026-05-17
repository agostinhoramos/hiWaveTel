"""URLs for `/api/sms/device/` (Android hiDisheLink contract)."""

from __future__ import annotations

from django.urls import path

from .device_api_views import (
    DeviceGetPendingKeyAndroidView,
    DeviceLoginAndroidView,
    DeviceLogoutAndroidView,
    DeviceMqttConfigAndroidView,
    DeviceRefreshAndroidView,
    DeviceRegisterAndroidView,
    DeviceStatusAndroidView,
)

urlpatterns = [
    path('register/', DeviceRegisterAndroidView.as_view(), name='sms-device-register'),
    path('login/', DeviceLoginAndroidView.as_view(), name='sms-device-login'),
    path('refresh/', DeviceRefreshAndroidView.as_view(), name='sms-device-refresh'),
    path('logout/', DeviceLogoutAndroidView.as_view(), name='sms-device-logout'),
    path('status/', DeviceStatusAndroidView.as_view(), name='sms-device-status'),
    path('mqtt-config/', DeviceMqttConfigAndroidView.as_view(), name='sms-device-mqtt-config'),
    path('get-pending-key/', DeviceGetPendingKeyAndroidView.as_view(), name='sms-device-get-pending-key'),
]
