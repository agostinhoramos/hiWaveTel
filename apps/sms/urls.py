from django.urls import path

from .views import SendSmsView
from .views_modems import ModemDetailView, ModemListView, ModemSyncView
from .views_system import ContainerRestartView, ModemAvailabilityView
from .views_webhooks import ModemWebhookCreateView, WebhookListView

urlpatterns = [
    path('sms/send/', SendSmsView.as_view(), name='sms-send'),
    path('sms/modems/', ModemListView.as_view(), name='sms-modem-list'),
    path('sms/modems/sync/', ModemSyncView.as_view(), name='sms-modem-sync'),
    path('sms/modems/<int:modem_index>/', ModemDetailView.as_view(), name='sms-modem-detail'),
    path(
        'sms/modems/<int:modem_index>/webhooks/',
        ModemWebhookCreateView.as_view(),
        name='sms-modem-webhook-create',
    ),
    path('sms/webhooks/', WebhookListView.as_view(), name='sms-webhook-list'),
    path(
        'sms/system/modem/<int:modem_index>/availability/',
        ModemAvailabilityView.as_view(),
        name='sms-modem-availability',
    ),
    path(
        'sms/system/container/restart/',
        ContainerRestartView.as_view(),
        name='sms-container-restart',
    ),
]
