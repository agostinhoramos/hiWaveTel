"""Pure unit tests for ``MMCLIClient`` and mmcli parsers."""

from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from apps.sms.mmcli_client import (
    MMCLIClient,
    MmcliError,
    _CREATED_PATH_RE,
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
    client = MMCLIClient()
    keyvalue_empty = CompletedProcess(['mmcli'], 0, stdout='', stderr='')
    json_ok = CompletedProcess(['mmcli'], 0, stdout='{\n "number":"+449", \n "text":"hi"\n}\n', stderr='')
    with patch.object(MMCLIClient, '_run', side_effect=[keyvalue_empty, json_ok]) as mocked:
        payload = client.show_sms('/org/freedesktop/ModemManager1/SMS/1')
    assert extract_text(payload) == 'hi'
    assert mocked.call_count == 2


def test_extract_from_number_helpers():
    assert extract_from_number({'number': '+44123'}) == '+44123'
    assert extract_from_number({}) == ''


def test_extract_text_helpers():
    assert extract_text({'text': 'body'}) == 'body'


def test_created_path_regex_matches_mmcli_stdout():
    out = 'Successfully created new SMS: /org/freedesktop/ModemManager1/SMS/2\n'
    m = _CREATED_PATH_RE.search(out)
    assert m is not None
    assert m.group(1) == '/org/freedesktop/ModemManager1/SMS/2'
