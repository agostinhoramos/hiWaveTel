from rest_framework.routers import DefaultRouter

from .views import InboundSmsViewSet, OutboundSmsViewSet

router = DefaultRouter()
router.register('sms/inbound', InboundSmsViewSet, basename='sms-inbound')
router.register('sms/outbound', OutboundSmsViewSet, basename='sms-outbound')

urlpatterns = router.urls
