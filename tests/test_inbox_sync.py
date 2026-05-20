"""Tests for InboundSms → InboxMessage sync and signal-based mirroring."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.external_device.models import ExternalDevice, InboxMessage
from apps.external_device.services import sync_inbox_from_modem_store, sync_single_inbound_to_all_devices
from apps.sms.models import InboundSms

pytestmark = pytest.mark.django_db


@pytest.fixture
def device_with_modem_index():
    """ExternalDevice with explicit modem_index in metadata."""
    device = ExternalDevice.objects.create(
        device_id='+351913000100',
        name='Device with modem_index',
        device_type='modem',
        metadata={'modem_index': 1},
        status=ExternalDevice.Status.ACTIVE,
    )
    return device


@pytest.fixture
def device_without_modem_index():
    """ExternalDevice without modem_index in metadata."""
    device = ExternalDevice.objects.create(
        device_id='+351913000200',
        name='Device without modem_index',
        device_type='modem',
        metadata={},
        status=ExternalDevice.Status.ACTIVE,
    )
    return device


class TestSyncInboxFromModemStore:
    """Test sync_inbox_from_modem_store behavior with and without modem_index."""

    def test_sync_filters_by_modem_index_when_set(self, device_with_modem_index):
        """When metadata.modem_index is set, only InboundSms from that index are mirrored."""
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/100',
            modem_index=1,
            from_number='+351911111111',
            text='Index 1 message',
        )
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/101',
            modem_index=0,
            from_number='+351922222222',
            text='Index 0 message (should not appear)',
        )

        sync_inbox_from_modem_store(device_with_modem_index)

        inbox_messages = InboxMessage.objects.filter(device=device_with_modem_index)
        assert inbox_messages.count() == 1
        assert inbox_messages.first().sender == '+351911111111'

    def test_sync_includes_all_when_no_modem_index(self, device_without_modem_index):
        """When metadata.modem_index is absent, all InboundSms are mirrored."""
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/200',
            modem_index=0,
            from_number='+351911111111',
            text='Index 0 message',
        )
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/201',
            modem_index=1,
            from_number='+351922222222',
            text='Index 1 message',
        )

        sync_inbox_from_modem_store(device_without_modem_index)

        inbox_messages = InboxMessage.objects.filter(device=device_without_modem_index)
        assert inbox_messages.count() == 2
        senders = {msg.sender for msg in inbox_messages}
        assert senders == {'+351911111111', '+351922222222'}

    def test_sync_handles_empty_inbound_store(self, device_without_modem_index):
        """Sync completes gracefully when InboundSms is empty."""
        sync_inbox_from_modem_store(device_without_modem_index)
        inbox_messages = InboxMessage.objects.filter(device=device_without_modem_index)
        assert inbox_messages.count() == 0

    def test_sync_creates_stable_message_id(self, device_without_modem_index):
        """InboxMessage.message_id is stable (mmcli_<pk>_dev_<device.pk>) to prevent duplicates."""
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/300',
            modem_index=0,
            from_number='+351911111111',
            text='Test',
        )

        sync_inbox_from_modem_store(device_without_modem_index)
        inbox = InboxMessage.objects.get(device=device_without_modem_index)
        assert inbox.message_id == f'mmcli_{inbound.pk}_dev_{device_without_modem_index.pk}'

        # Call sync again — should not create duplicate
        sync_inbox_from_modem_store(device_without_modem_index)
        assert InboxMessage.objects.filter(device=device_without_modem_index).count() == 1


class TestSyncSingleInboundToAllDevices:
    """Test sync_single_inbound_to_all_devices mirrors to all active devices."""

    def test_mirrors_to_all_active_devices(self):
        """Single InboundSms is mirrored to all active ExternalDevices."""
        device1 = ExternalDevice.objects.create(
            device_id='+351913000001',
            name='Device 1',
            status=ExternalDevice.Status.ACTIVE,
        )
        device2 = ExternalDevice.objects.create(
            device_id='+351913000002',
            name='Device 2',
            status=ExternalDevice.Status.ACTIVE,
        )
        ExternalDevice.objects.create(
            device_id='+351913000003',
            name='Inactive Device',
            status=ExternalDevice.Status.INACTIVE,
        )

        # Creating InboundSms triggers post_save signal → sync_single_inbound_to_all_devices
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/400',
            modem_index=0,
            from_number='+351911111111',
            text='Broadcast message',
        )

        # Signal should have mirrored to both active devices
        assert InboxMessage.objects.filter(device=device1, sender='+351911111111').exists()
        assert InboxMessage.objects.filter(device=device2, sender='+351911111111').exists()
        assert InboxMessage.objects.filter(device__device_id='+351913000003').count() == 0

    def test_does_not_duplicate_existing_message(self):
        """Creating InboundSms twice with same pk does not duplicate (signal + manual call)."""
        device = ExternalDevice.objects.create(
            device_id='+351913000001',
            name='Device 1',
            status=ExternalDevice.Status.ACTIVE,
        )
        # Creating InboundSms triggers signal automatically
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/500',
            modem_index=0,
            from_number='+351911111111',
            text='Test',
        )

        # Call manually again (simulates GET /inbox/ sync) — should not duplicate
        sync_single_inbound_to_all_devices(inbound)

        assert InboxMessage.objects.filter(device=device).count() == 1

    def test_skips_device_when_modem_inbox_mirror_false(self):
        """Secondary registrations can opt out of modem inbox mirroring."""
        ExternalDevice.objects.create(
            device_id='+351913000011',
            name='Mirror on',
            status=ExternalDevice.Status.ACTIVE,
            metadata={},
        )
        ExternalDevice.objects.create(
            device_id='+351913000012',
            name='Mirror off',
            status=ExternalDevice.Status.ACTIVE,
            metadata={'modem_inbox_mirror': False},
        )
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/mirror_off',
            modem_index=0,
            from_number='+351977',
            text='only one device',
        )
        assert InboxMessage.objects.count() == 1
        assert InboxMessage.objects.filter(device__device_id='+351913000011').exists()
        assert not InboxMessage.objects.filter(device__device_id='+351913000012').exists()

    def test_skips_mmcli_when_recent_manual_matches_inbound(self):
        """Avoid inbox_manual plus mmcli duplicate on the same device."""
        device = ExternalDevice.objects.create(
            device_id='+351913000013',
            name='manual first',
            status=ExternalDevice.Status.ACTIVE,
        )
        InboxMessage.objects.create(
            message_id='inbox_manual_pref',
            device=device,
            sender='+351966',
            body='duplicate narrative',
            received_at=timezone.now(),
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/manual_pref',
            modem_index=0,
            from_number='+351966',
            text='duplicate narrative',
        )
        assert InboxMessage.objects.filter(device=device).count() == 1
        assert not InboxMessage.objects.filter(message_id=f'mmcli_{inbound.pk}_dev_{device.pk}').exists()

    def test_skips_mmcli_on_all_devices_when_manual_on_another_device(self):
        """Manual inbox on device A prevents mmcli mirror on device B (same sender/body)."""
        device_a = ExternalDevice.objects.create(
            device_id='+351913000011',
            name='A',
            status=ExternalDevice.Status.ACTIVE,
        )
        device_b = ExternalDevice.objects.create(
            device_id='+351913000012',
            name='B',
            status=ExternalDevice.Status.ACTIVE,
        )
        InboxMessage.objects.create(
            message_id='inbox_manual_other_dev',
            device=device_a,
            sender='+351966',
            body='shared story',
            received_at=timezone.now(),
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/other_dev',
            modem_index=0,
            from_number='+351966',
            text='shared story',
        )
        assert InboxMessage.objects.filter(device=device_a).count() == 1
        assert InboxMessage.objects.filter(device=device_b).count() == 0
        assert not InboxMessage.objects.filter(message_id=f'mmcli_{inbound.pk}_dev_{device_b.pk}').exists()

    def test_skips_mmcli_when_inbound_echoes_recent_outbound(self):
        """Outbound sent via modem must not create extra inbox rows on mirror."""
        from apps.sms.models import OutboundSms

        device = ExternalDevice.objects.create(
            device_id='+351913000014',
            name='echo',
            status=ExternalDevice.Status.ACTIVE,
        )
        OutboundSms.objects.create(
            modem_index=0,
            to_number='+351977',
            text='echo body',
            state=OutboundSms.State.SENT,
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/echo',
            modem_index=0,
            from_number='+351977',
            text='echo body',
        )
        assert InboxMessage.objects.filter(device=device).count() == 0


class TestPostSaveSignal:
    """Test that post_save signal on InboundSms triggers inbox mirroring."""

    def test_signal_mirrors_on_inbound_creation(self):
        """Creating InboundSms automatically triggers mirroring to all active devices."""
        device = ExternalDevice.objects.create(
            device_id='+351913000001',
            name='Device 1',
            status=ExternalDevice.Status.ACTIVE,
        )

        # Create InboundSms — signal should trigger
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/600',
            modem_index=0,
            from_number='+351911111111',
            text='Signal test',
        )

        # Check that InboxMessage was created
        inbox = InboxMessage.objects.filter(device=device, sender='+351911111111')
        assert inbox.exists()
        assert inbox.first().message_id == f'mmcli_{inbound.pk}_dev_{device.pk}'

    def test_signal_does_not_trigger_on_update(self):
        """Updating InboundSms does not trigger additional mirroring."""
        device = ExternalDevice.objects.create(
            device_id='+351913000001',
            name='Device 1',
            status=ExternalDevice.Status.ACTIVE,
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/700',
            modem_index=0,
            from_number='+351911111111',
            text='Original',
        )

        assert InboxMessage.objects.filter(device=device).count() == 1

        # Update text — should not create duplicate
        inbound.text = 'Updated'
        inbound.save()

        assert InboxMessage.objects.filter(device=device).count() == 1

    def test_signal_skips_mirror_until_sender_or_body_present(self):
        """Empty mmcli snapshot must not create inbox rows; mirror runs once content arrives."""
        device = ExternalDevice.objects.create(
            device_id='+351913000099',
            name='late content',
            status=ExternalDevice.Status.ACTIVE,
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/late',
            modem_index=0,
            from_number='',
            text='',
        )
        assert InboxMessage.objects.filter(device=device).count() == 0

        inbound.from_number = '+351988'
        inbound.text = 'filled later'
        inbound.save()

        assert InboxMessage.objects.filter(device=device).count() == 1


class TestInboxEndpointIntegration:
    """Test GET /api/v1/sms/inbox/ returns mirrored InboundSms."""

    def test_inbox_endpoint_returns_mirrored_inbound_sms(self, api_client):
        """GET /api/v1/sms/inbox/ returns InboundSms mirrored to device inbox."""
        from apps.external_device.authentication import hash_api_key
        from apps.external_device.services import generate_token

        raw_api_key = generate_token(48)
        api_key_hash = hash_api_key(raw_api_key)

        device = ExternalDevice.objects.create(
            device_id='+351913000001',
            name='Test Device',
            api_key_hash=api_key_hash,
            status=ExternalDevice.Status.ACTIVE,
        )

        # Create InboundSms — signal mirrors automatically
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/800',
            modem_index=0,
            from_number='+351911111111',
            text='Test message',
        )

        api_client.credentials(HTTP_X_API_KEY=raw_api_key)
        response = api_client.get('/api/v1/sms/inbox/')

        assert response.status_code == 200
        assert response.data['count'] == 1
        assert response.data['results'][0]['sender'] == '+351911111111'
        assert response.data['results'][0]['body'] == 'Test message'

    def test_inbox_endpoint_returns_empty_when_no_inbound_sms(self, api_client):
        """GET /api/v1/sms/inbox/ returns empty when no InboundSms exists."""
        from apps.external_device.authentication import hash_api_key
        from apps.external_device.services import generate_token

        raw_api_key = generate_token(48)
        api_key_hash = hash_api_key(raw_api_key)

        device = ExternalDevice.objects.create(
            device_id='+351913000001',
            name='Test Device',
            api_key_hash=api_key_hash,
            status=ExternalDevice.Status.ACTIVE,
        )

        api_client.credentials(HTTP_X_API_KEY=raw_api_key)
        response = api_client.get('/api/v1/sms/inbox/')

        assert response.status_code == 200
        assert response.data['count'] == 0


class TestRegistrationToInboxFlow:
    """Test complete flow from device registration to inbox retrieval."""

    def test_receive_sms_after_device_registration(self, api_client):
        """
        Integration test matching user's reproduction case:
        1. Register device via POST /api/v1/external-devices/register/
        2. InboundSms arrives (simulated D-Bus signal → persist_inbound_sms)
        3. GET /api/v1/sms/inbox/ returns the message
        
        This verifies the post_save signal handler is wired correctly.
        """
        # Step 1: Register device
        registration_data = {
            'device_id': '+351912329317',
            'registration_token': 'token_admin_uma_vez',
            'name': 'Modem site B',
            'device_type': 'modem',
            'mqtt_client_id': 'meu_cliente_mqtt',
            'metadata': {'site': 'Lisboa'},
        }
        
        response = api_client.post(
            '/api/v1/external-devices/register/',
            registration_data,
            format='json',
        )
        
        assert response.status_code == 200
        assert 'api_key' in response.data
        assert response.data['device_id'] == '+351912329317'
        assert response.data['status'] == 'active'
        
        raw_api_key = response.data['api_key']
        
        # Step 2: Simulate SMS arrival (D-Bus signal → persist_inbound_sms → post_save signal)
        # Creating InboundSms triggers the post_save signal which mirrors to InboxMessage
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/test_flow_001',
            modem_index=0,
            from_number='+351911222333',
            text='Test message after registration',
        )
        
        # Step 3: GET /api/v1/sms/inbox/ should return the message
        api_client.credentials(HTTP_X_API_KEY=raw_api_key)
        response = api_client.get('/api/v1/sms/inbox/')
        
        assert response.status_code == 200
        assert response.data['count'] == 1
        assert response.data['results'][0]['sender'] == '+351911222333'
        assert response.data['results'][0]['body'] == 'Test message after registration'
        
        # Verify InboxMessage was created with correct message_id format
        device = ExternalDevice.objects.get(device_id='+351912329317')
        inbox_msg = InboxMessage.objects.get(device=device)
        assert inbox_msg.message_id == f'mmcli_{inbound.pk}_dev_{device.pk}'
    
    def test_receive_multiple_sms_after_registration(self, api_client):
        """Test that multiple received SMS all appear in inbox."""
        # Register device
        registration_data = {
            'device_id': '+351913000999',
            'registration_token': 'test_token',
            'name': 'Multi SMS Device',
            'device_type': 'modem',
        }
        
        response = api_client.post(
            '/api/v1/external-devices/register/',
            registration_data,
            format='json',
        )
        
        assert response.status_code == 200
        raw_api_key = response.data['api_key']
        
        # Simulate multiple SMS arrivals
        for i in range(3):
            InboundSms.objects.create(
                mm_path=f'/org/freedesktop/ModemManager1/SMS/multi_{i}',
                modem_index=0,
                from_number=f'+35191100000{i}',
                text=f'Message {i}',
            )
        
        # Verify all appear in inbox
        api_client.credentials(HTTP_X_API_KEY=raw_api_key)
        response = api_client.get('/api/v1/sms/inbox/')
        
        assert response.status_code == 200
        assert response.data['count'] == 3
        
        senders = {msg['sender'] for msg in response.data['results']}
        assert senders == {'+351911000000', '+351911000001', '+351911000002'}
