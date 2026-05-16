from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.sms import dbus_watch
from apps.sms.mmcli_client import MMCLIClient, MmcliError, _CREATED_PATH_RE, extract_from_number, extract_text
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.serializers import OutboundSmsCreateSerializer
from apps.sms.services import dispatch_outbound_mmcli, format_public_mmcli_error, persist_inbound_sms
from apps.sms.validators import sms_destination_validator

User = get_user_model()


class AuthAPITestCase(APITestCase):
    """Base class that applies JWT/session auth headers for SMS + schema endpoints."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='api_user', password='test-password-secure')

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.user)


class OutboundSmsApiTests(AuthAPITestCase):
    @patch.object(MMCLIClient, 'send_sms', autospec=True)
    @patch.object(MMCLIClient, 'create_sms', autospec=True)
    @patch.object(MMCLIClient, 'ensure_modem_index', autospec=True)
    def test_create_send_success(self, mock_ensure, mock_create, mock_send):
        mock_create.return_value = '/org/freedesktop/ModemManager1/SMS/0'
        payload = {'to': '+351913000387', 'text': 'Test EC25', 'modem_index': 0}
        url = reverse('sms-outbound-list')
        resp = self.client.post(url, payload, format='json')

        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.data['state'], OutboundSms.State.SENT)
        self.assertEqual(resp.data['mm_path'], '/org/freedesktop/ModemManager1/SMS/0')
        self.assertEqual(OutboundSms.objects.count(), 1)
        mock_create.assert_called_once()
        mock_send.assert_called_once()
        mock_ensure.assert_called_once()

    @patch.object(MMCLIClient, 'send_sms', autospec=True)
    @patch.object(MMCLIClient, 'create_sms', autospec=True)
    @patch.object(MMCLIClient, 'ensure_modem_index', autospec=True)
    def test_create_fails_records_failed_state(self, mock_ensure, mock_create, mock_send):
        mock_create.side_effect = MmcliError('modem busy', stderr='EBUSY', exit_code=1)
        resp = self.client.post(
            reverse('sms-outbound-list'),
            {'to': '+4412345678910', 'text': 'x'},
            format='json',
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data['state'], OutboundSms.State.FAILED)
        mock_send.assert_not_called()

    def test_create_rejects_short_destination_number(self):
        resp = self.client.post(
            reverse('sms-outbound-list'),
            {'to': '+351913', 'text': 'hello'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)


class ModemHealthTests(APITestCase):
    """Health endpoint is Django-native and intentionally unauthenticated for probes."""

    def test_health_modem_ok(self):
        with patch.object(MMCLIClient, 'modem_ping') as mp, patch.object(MMCLIClient, 'list_modem_indices') as ml:
            ml.return_value = [0]
            mp.return_value = (True, '')
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get('ok'), True)

    def test_health_modem_index_mismatch(self):
        with override_settings(MODEM_MMCLI_INDEX=99), patch.object(MMCLIClient, 'modem_ping'), patch.object(
            MMCLIClient,
            'list_modem_indices',
            return_value=[0],
        ):
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 503)

    def test_health_no_modems(self):
        with patch.object(MMCLIClient, 'list_modem_indices', return_value=[]):
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertFalse(body.get('ok'))
        self.assertIn('zero modems', (body.get('mmcli_notes') or '').lower())

    def test_health_ping_failure(self):
        with patch.object(MMCLIClient, 'list_modem_indices', return_value=[0]), patch.object(
            MMCLIClient,
            'modem_ping',
            return_value=(False, 'modem offline'),
        ), override_settings(MODEM_MMCLI_INDEX=0):
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 503)

    def test_health_mmcli_error(self):
        with patch.object(MMCLIClient, 'list_modem_indices', side_effect=MmcliError('mmcli exploded', stderr='boom')):
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 503)

    def test_health_os_error(self):
        with patch.object(MMCLIClient, 'list_modem_indices', side_effect=OSError('ENOENT')):
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 503)

    def test_health_unexpected_error_sanitized(self):
        with patch(
            'apps.sms.views_health.MMCLIClient',
            side_effect=RuntimeError('do not expose this exact string'),
        ):
            resp = self.client.get(reverse('api-health-mm'))
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn('do not expose this exact string', resp.json().get('mmcli_notes', ''))


class InboundSmsApiTests(AuthAPITestCase):
    def setUp(self):
        super().setUp()
        self.a = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/0',
            modem_index=0,
            from_number='+351913000387',
            text='hello',
        )
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/1',
            modem_index=0,
            from_number='+123456781234567',
            text='later',
        )

    def test_list_filter_from(self):
        url = reverse('sms-inbound-list')
        resp = self.client.get(url, {'from': '913'})
        self.assertEqual(resp.status_code, 200)
        ids = {row['id'] for row in resp.data['results']}
        self.assertEqual(ids, {self.a.id})

    def test_since_invalid(self):
        url = reverse('sms-inbound-list')
        resp = self.client.get(url, {'since': 'not-valid'})
        self.assertEqual(resp.status_code, 400)

    def test_since_valid(self):
        url = reverse('sms-inbound-list')
        iso = timezone.now().isoformat()
        resp = self.client.get(url, {'since': iso})
        self.assertEqual(resp.status_code, 200)
        ids = {row['id'] for row in resp.data['results']}
        self.assertEqual(ids, set())

    def test_from_param_length_guard(self):
        resp = self.client.get(reverse('sms-inbound-list'), {'from': 'x' * 300})
        self.assertEqual(resp.status_code, 400)

    def test_pagination_returns_next_link_when_over_page_size(self):
        bulk = [
            InboundSms(
                mm_path=f'/org/freedesktop/ModemManager1/SMS/bulk/{i}',
                modem_index=0,
                from_number='+4412345678910',
                text='x',
            )
            for i in range(9000, 9051)
        ]
        InboundSms.objects.bulk_create(bulk)
        resp = self.client.get(reverse('sms-inbound-list'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.data.get('next'))
        self.assertEqual(len(resp.data['results']), 50)

    def test_retrieve_inbound_detail(self):
        url = reverse('sms-inbound-detail', args=[self.a.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['from_number'], '+351913000387')


class MMCLIClientBehaviorTests(TestCase):
    def test_list_modem_indices_parses(self):
        stdout = '\n/org/freedesktop/ModemManager1/Modem/2 [foo]\n/org/freedesktop/ModemManager1/Modem/10 ...\n'

        client = MMCLIClient()
        cp = CompletedProcess([client.mmcli_path, '-L'], 0, stdout=stdout)

        with patch.object(MMCLIClient, '_run', return_value=cp):
            self.assertEqual(client.list_modem_indices(), [2, 10])

    def test_retrying_run_transient_then_ok(self):
        client = MMCLIClient(max_retries=10)
        bad = CompletedProcess(['mmcli'], 1, stdout='', stderr="couldn't acquire lock dbus")
        good = CompletedProcess(
            ['mmcli'],
            0,
            stdout=f"Successfully created new SMS: /org/freedesktop/ModemManager1/SMS/9\n",
            stderr='',
        )

        with patch.object(MMCLIClient, '_run', side_effect=[bad, bad, good]):
            sms_path = client.create_sms(0, '+351913', 'hi')

        self.assertEqual(sms_path, '/org/freedesktop/ModemManager1/SMS/9')

    def test_ensure_modem_index_raises_when_missing(self):
        client = MMCLIClient()

        cp = CompletedProcess([client.mmcli_path, '-L'], 0, stdout='/Modem/none', stderr='')

        with patch.object(MMCLIClient, '_run', return_value=cp):
            with self.assertRaises(MmcliError):
                client.ensure_modem_index(0)

    def test_list_sms_paths_parses(self):
        hay = '/org/freedesktop/ModemManager1/SMS/1 foo\n/org/freedesktop/ModemManager1/SMS/2\n'
        client = MMCLIClient()
        good = CompletedProcess(['mmcli'], 0, stdout=hay, stderr='')
        with patch.object(MMCLIClient, '_retrying_run', return_value=good):
            paths = client.list_sms_paths(0)
        self.assertEqual(paths, ['/org/freedesktop/ModemManager1/SMS/1', '/org/freedesktop/ModemManager1/SMS/2'])

    def test_show_sms_prefers_json(self):
        client = MMCLIClient()
        keyvalue_empty = CompletedProcess(['mmcli'], 0, stdout='', stderr='')
        json_ok = CompletedProcess(['mmcli'], 0, stdout='{\n "number":"+449", \n "text":"hi"\n}\n', stderr='')

        with patch.object(MMCLIClient, '_run', side_effect=[keyvalue_empty, json_ok]) as mocked:
            payload = client.show_sms('/org/freedesktop/ModemManager1/SMS/1')

        self.assertEqual(extract_text(payload), 'hi')
        self.assertEqual(mocked.call_count, 2)


class ExtractHelperTests(TestCase):
    def test_extract_from_number(self):
        self.assertEqual(extract_from_number({'number': '+44123'}), '+44123')
        self.assertEqual(extract_from_number({}), '')

    def test_extract_text(self):
        self.assertEqual(extract_text({'text': 'body'}), 'body')


class PersistInboundSmsServiceTests(TestCase):
    def test_persist_creates_from_show(self):
        path = '/org/freedesktop/ModemManager1/SMS/z1'
        client = MMCLIClient()
        client.show_sms = MagicMock(
            return_value={
                'number': '+4498765432111',
                'text': 'hello',
                'state': 'received',
                'smsc': '+440000',
                'timestamp': '2024-05-05T01:02:03Z',
            },
        )

        obj = persist_inbound_sms(path, 3, client)
        self.assertEqual(obj.mm_path, path)
        self.assertEqual(obj.from_number, '+4498765432111')
        self.assertEqual(InboundSms.objects.count(), 1)

    def test_persist_handles_show_failure_gracefully(self):
        bad = MMCLIClient()
        bad.show_sms = MagicMock(side_effect=MmcliError('nope'))

        obj = persist_inbound_sms('/org/freedesktop/ModemManager1/SMS/x9', 0, bad)

        self.assertEqual(obj.mm_path, '/org/freedesktop/ModemManager1/SMS/x9')
        self.assertEqual(obj.modem_index, 0)


class DispatchOutboundSmsServiceTests(TestCase):
    def test_dispatch_success_updates_state(self):
        outbound = OutboundSms.objects.create(
            modem_index=0,
            to_number='+4412345678910',
            text='svc',
            state=OutboundSms.State.CREATED,
        )
        dummy = MMCLIClient()
        dummy.ensure_modem_index = MagicMock(return_value=None)
        dummy.create_sms = MagicMock(return_value='/org/freedesktop/ModemManager1/SMS/3')
        dummy.send_sms = MagicMock(return_value=None)

        updated = dispatch_outbound_mmcli(outbound, client=dummy)
        self.assertEqual(updated.state, OutboundSms.State.SENT)
        self.assertEqual(updated.mm_path, '/org/freedesktop/ModemManager1/SMS/3')


class FormatterTests(TestCase):
    def test_format_public_mmcli_error_truncates_stderr(self):
        err = MmcliError('root', stderr='first line stderr\nsecret line')
        formatted = format_public_mmcli_error(err)
        self.assertIn('root', formatted)
        self.assertLessEqual(len(formatted), 205)


class DbusWatchSyncTests(TestCase):
    def test_startup_snapshot_retries_then_persists(self):
        paths_first = ['/org/freedesktop/ModemManager1/SMS/1']
        boom = MMCLIClient()
        boom.list_sms_paths = MagicMock(side_effect=[MmcliError('boom', exit_code=1), paths_first])

        with patch.object(dbus_watch, 'persist_inbound_sms') as mock_persist:
            with patch('apps.sms.dbus_watch.time.sleep', return_value=None):
                n = dbus_watch.sync_modem_sms_snapshot(0, boom)

        self.assertEqual(n, 1)
        self.assertGreaterEqual(mock_persist.call_count, 1)


class OutboundSmsEnsureFailsTests(AuthAPITestCase):
    @patch.object(MMCLIClient, 'send_sms', autospec=True)
    @patch.object(MMCLIClient, 'create_sms', autospec=True)
    @patch.object(MMCLIClient, 'ensure_modem_index', autospec=True)
    def test_ensure_failure_marks_failed(self, mock_ensure, mock_create, mock_send):
        mock_ensure.side_effect = MmcliError(
            'Modem missing',
            stderr='modem index missing',
            exit_code=-2,
        )
        resp = self.client.post(
            reverse('sms-outbound-list'),
            {'to': '+4412345678910', 'text': 'x'},
            format='json',
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.data['state'], OutboundSms.State.FAILED)
        mock_create.assert_not_called()
        mock_send.assert_not_called()


class SerializerValidationTests(TestCase):
    def test_outbound_serialiser_validates_digit_length(self):
        ser = OutboundSmsCreateSerializer(data={'to': '+449111', 'text': 'hello'})
        self.assertFalse(ser.is_valid())

    def test_outbound_serialiser_requires_text(self):
        ser = OutboundSmsCreateSerializer(data={'to': '+4412345678910'})
        self.assertFalse(ser.is_valid())


class ValidatorUtilityTests(TestCase):
    def test_sms_destination_validator_accepts_international_numbers(self):
        sms_destination_validator(' +44 7911 112233 ')
        sms_destination_validator('00447911112233')

    def test_destination_validator_requires_min_digits(self):
        with self.assertRaises(DjangoValidationError):
            sms_destination_validator('+123456')


class MMCLIClientParseTests(TestCase):
    def test_created_path_regex(self):
        out = "Successfully created new SMS: /org/freedesktop/ModemManager1/SMS/2\n"
        m = _CREATED_PATH_RE.search(out)
        assert m is not None
        self.assertEqual(m.group(1), '/org/freedesktop/ModemManager1/SMS/2')


class SpectacularProtectedTests(APITestCase):
    """Schema + docs endpoints require JWT per ``IsAuthenticated``."""

    def test_schema_requires_auth(self):
        resp = self.client.get(reverse('schema'))
        self.assertIn(resp.status_code, (401, 403))


class AuthenticatedSpectacularTests(AuthAPITestCase):
    def test_schema_ok_when_authenticated(self):
        resp = self.client.get(reverse('schema'))
        self.assertEqual(resp.status_code, 200)
