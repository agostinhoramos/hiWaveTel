"""Pure unit tests for ``MMCLIClient`` and mmcli parsers."""

from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from apps.sms.mmcli_client import (
    MMCLIClient,
    MmcliError,
    _CREATED_PATH_RE,
    _flatten_dict,
    _merge_mmcli_sources,
    _normalize_mmcli_json,
    extract_from_number,
    extract_text,
)


def test_list_modem_indices_parses():
    stdout = '\n/org/freedesktop/ModemManager1/Modem/2 [foo]\n/org/freedesktop/ModemManager1/Modem/10 ...\n'
    client = MMCLIClient()
    cp = CompletedProcess([client.mmcli_path, '-L'], 0, stdout=stdout)
    with patch.object(MMCLIClient, '_run', return_value=cp):
        assert client.list_modem_indices() == [2, 10]


def test_create_sms_retrying_run_transient_then_ok():
    client = MMCLIClient(max_retries=10)
    bad = CompletedProcess(['mmcli'], 1, stdout='', stderr="couldn't acquire lock dbus")
    good = CompletedProcess(
        ['mmcli'],
        0,
        stdout='Successfully created new SMS: /org/freedesktop/ModemManager1/SMS/9\n',
        stderr='',
    )
    with patch.object(MMCLIClient, '_run', side_effect=[bad, bad, good]):
        sms_path = client.create_sms(0, '+351913', 'hi')
    assert sms_path == '/org/freedesktop/ModemManager1/SMS/9'


def test_ensure_modem_index_raises_when_missing():
    client = MMCLIClient()
    cp = CompletedProcess([client.mmcli_path, '-L'], 0, stdout='/Modem/none', stderr='')
    with patch.object(MMCLIClient, '_run', return_value=cp):
        with pytest.raises(MmcliError):
            client.ensure_modem_index(0)


def test_resolve_modem_mmcli_index_uses_configured_when_present():
    from apps.sms.mmcli_client import resolve_modem_mmcli_index

    client = MMCLIClient()
    with patch.object(MMCLIClient, 'list_modem_indices', return_value=[0, 1]):
        assert resolve_modem_mmcli_index(1, client=client) == 1


def test_resolve_modem_mmcli_index_falls_back_to_primary():
    from apps.sms.mmcli_client import resolve_modem_mmcli_index

    client = MMCLIClient()
    with patch.object(MMCLIClient, 'list_modem_indices', return_value=[1]):
        with patch.object(MMCLIClient, 'primary_modem_index', return_value=1):
            assert resolve_modem_mmcli_index(0, client=client) == 1


def test_list_sms_paths_parses():
    hay = '/org/freedesktop/ModemManager1/SMS/1 foo\n/org/freedesktop/ModemManager1/SMS/2\n'
    client = MMCLIClient()
    good = CompletedProcess(['mmcli'], 0, stdout=hay, stderr='')
    with patch.object(MMCLIClient, '_retrying_run', return_value=good):
        paths = client.list_sms_paths(0)
    assert paths == [
        '/org/freedesktop/ModemManager1/SMS/1',
        '/org/freedesktop/ModemManager1/SMS/2',
    ]


def test_show_sms_prefers_json():
    """Keyvalue alone may be metadata-only; JSON supplies text — both runs are merged."""
    client = MMCLIClient()
    keyvalue_meta_no_text = CompletedProcess(
        ['mmcli'],
        0,
        stdout=(
            'sms.properties.state:\treceived\n'
            'sms.content.number:\t+351913000387\n'
        ),
        stderr='',
    )
    long_body = 'A' * 400
    json_ok = CompletedProcess(
        ['mmcli'],
        0,
        stdout=f'{{"sms.content.number":"+351913000387","sms.content.text":"{long_body}"}}\n',
        stderr='',
    )
    with patch.object(MMCLIClient, '_run', side_effect=[keyvalue_meta_no_text, json_ok]) as mocked:
        payload = client.show_sms('/org/freedesktop/ModemManager1/SMS/1')
    assert extract_text(payload) == long_body
    assert extract_from_number(payload) == '+351913000387'
    assert mocked.call_count == 2


def test_flatten_dict_nested():
    nested = {
        'sms': {
            'content': {'text': 'hello', 'number': '+351913'},
            'properties': {'state': 'received'},
        }
    }
    flat = _flatten_dict(nested)
    assert flat == {
        'sms.content.text': 'hello',
        'sms.content.number': '+351913',
        'sms.properties.state': 'received',
    }


def test_normalize_mmcli_json_with_nested_structure():
    raw = (
        '{"sms":{"content":{"text":"Long body here","number":"+351913000387"},'
        '"properties":{"state":"received"}}}'
    )
    result = _normalize_mmcli_json(raw)
    assert result['smscontenttext'] == 'Long body here'
    assert result['smscontentnumber'] == '+351913000387'
    assert result['smspropertiesstate'] == 'received'


def test_show_sms_nested_mmcli_json_merge_with_keyvalue():
    """Real mmcli shape: nested JSON under ``sms`` merges with keyvalue fields."""
    client = MMCLIClient()
    kv = CompletedProcess(
        ['mmcli'],
        0,
        stdout='sms.properties.smsc:\t+351911616163\nsms.properties.state:\treceived\n',
        stderr='',
    )
    nested_json = (
        '{"sms":{"content":{"number":"+351913000387","text":"line1\\nline2\\nline3"},'
        '"properties":{"state":"received"}}}'
    )
    js = CompletedProcess(['mmcli'], 0, stdout=nested_json + '\n', stderr='')
    with patch.object(MMCLIClient, '_run', side_effect=[kv, js]):
        payload = client.show_sms('/org/freedesktop/ModemManager1/SMS/99')
    assert extract_text(payload) == 'line1\nline2\nline3'
    assert extract_from_number(payload) == '+351913000387'


def test_merge_mmcli_sources_longest_body_wins_same_key():
    kv = {'smscontenttext': 'short', 'smspropertiesstate': 'received'}
    js = {'smscontenttext': 'much longer sms body here'}
    m = _merge_mmcli_sources(kv, js)
    assert m['smscontenttext'] == 'much longer sms body here'
    assert m['smspropertiesstate'] == 'received'


def test_show_sms_falls_back_to_plain_when_kv_and_json_empty():
    """If keyvalue+json merge is empty (unlikely), plaintext mmcli -s fills in."""
    client = MMCLIClient()
    kv_empty = CompletedProcess(['mmcli'], 0, stdout='', stderr='')
    json_empty = CompletedProcess(['mmcli'], 0, stdout='', stderr='')
    plain_lines = CompletedProcess(['mmcli'], 0, stdout='sms.content.text:\tplain-only\n', stderr='')
    with patch.object(MMCLIClient, '_run', side_effect=[kv_empty, json_empty, plain_lines]):
        payload = client.show_sms('/org/freedesktop/ModemManager1/SMS/42')
    assert extract_text(payload) == 'plain-only'


def test_extract_from_number_helpers():
    # bare "number" key (JSON format)
    assert extract_from_number({'number': '+44123'}) == '+44123'
    # --output-keyvalue: sms.content.number → smscontentnumber
    assert extract_from_number({'smscontentnumber': '+351913000387'}) == '+351913000387'
    # "--" placeholder is treated as empty
    assert extract_from_number({'smscontentnumber': '--'}) == ''
    assert extract_from_number({}) == ''


def test_extract_text_helpers():
    assert extract_text({'text': 'body'}) == 'body'
    # --output-keyvalue: sms.content.text → smscontenttext
    assert extract_text({'smscontenttext': 'Annie are you ok'}) == 'Annie are you ok'
    assert extract_text({'smscontenttext': '--'}) == ''
    assert extract_text({'message': 'via message key'}) == 'via message key'
    assert extract_text({'body': 'via body'}) == 'via body'


def test_extract_text_prefers_early_keys_over_later_aliases():
    assert extract_text({'smscontenttext': 'first', 'message': 'second'}) == 'first'


def test_created_path_regex_matches_mmcli_stdout():
    out = 'Successfully created new SMS: /org/freedesktop/ModemManager1/SMS/2\n'
    m = _CREATED_PATH_RE.search(out)
    assert m is not None
    assert m.group(1) == '/org/freedesktop/ModemManager1/SMS/2'


def test_show_modem_merges_keyvalue_and_json():
    """``show_modem`` uses the same dual-run approach as ``show_sms``."""
    client = MMCLIClient()
    kv = CompletedProcess(
        ['mmcli'],
        0,
        stdout='modem.generic.state:\tenabled\n',
        stderr='',
    )
    json_ok = CompletedProcess(
        ['mmcli'],
        0,
        stdout='{"modem":{"generic":{"manufacturer":"ACME"}}}',
        stderr='',
    )
    with patch.object(MMCLIClient, '_run', side_effect=[kv, json_ok]):
        merged = client.show_modem(0)
    assert 'modem.generic.state' in merged or 'modemgenericstate' in merged
