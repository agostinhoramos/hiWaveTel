"""Wrappers around `mmcli` for ModemManager SMS operations."""

from __future__ import annotations

import json
from typing import Any
import logging
import os
import re
import subprocess
import time
_LOGGER = logging.getLogger(__name__)

_CREATED_PATH_RE = re.compile(
    r"Successfully created new SMS:\s*(/org/freedesktop/ModemManager1/SMS/\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_SMS_PATH_RE = re.compile(r"/org/freedesktop/ModemManager1/SMS/\d+")
_modem_path_re = re.compile(r"/org/freedesktop/ModemManager1/Modem/(\d+)")


class MmcliError(Exception):
    def __init__(self, message: str, stdout: str = "", stderr: str = "", exit_code: int | None = None):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class MMCLIClient:
    def __init__(
        self,
        mmcli_path: str | None = None,
        timeout_sec: float | None = None,
        max_retries: int | None = None,
        retry_backoff_base_sec: float | None = None,
    ) -> None:
        self.mmcli_path = mmcli_path or os.environ.get("MMCLI_PATH", "mmcli")
        self.timeout_sec = timeout_sec if timeout_sec is not None else float(os.environ.get("MMCLI_TIMEOUT", "45"))
        self.max_retries = max(1, int(max_retries) if max_retries is not None else int(os.environ.get("MMCLI_RETRY_MAX", "3")))
        self.retry_backoff_base_sec = (
            float(retry_backoff_base_sec)
            if retry_backoff_base_sec is not None
            else float(os.environ.get("MMCLI_RETRY_BACKOFF_SEC", "0.5"))
        )

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        start = time.perf_counter()
        _LOGGER.debug("mmcli invoke argv=%s", argv)
        try:
            proc = subprocess.run(
                argv,
                check=False,
                text=True,
                capture_output=True,
                timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - start
            _LOGGER.error("mmcli timeout after %.2fs argv=%s", elapsed, argv)
            raise MmcliError(f"mmcli timed out ({self.timeout_sec}s)", exit_code=-1) from exc

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        _LOGGER.debug(
            "mmcli done rc=%s elapsed_ms=%s stderr_first=%s",
            proc.returncode,
            elapsed_ms,
            ((proc.stderr or "").strip().replace("\n", " ")[:200]),
        )
        return proc

    @staticmethod
    def _default_should_retry(exc: MmcliError) -> bool:
        if exc.exit_code == -1:
            return True
        text = ((exc.stderr or "") + "\n" + (exc.stdout or "")).lower()
        retries = (
            "couldn't acquire",
            "timed out",
            "dbus",
            "not provided",
            "in progress",
            "not enabled yet",
            "not enabled",
            "enabling",
            "initializing",
            "unknown state",
        )
        return any(s in text for s in retries)

    @staticmethod
    def should_retry(exc: Exception) -> bool:
        """Public hook for callers needing custom transient detection."""
        if isinstance(exc, MmcliError):
            return MMCLIClient._default_should_retry(exc)
        return False

    def _retrying_run(self, argv: list[str], what: str) -> subprocess.CompletedProcess[str]:
        """Run subprocess with retries on `_ensure_ok` failures when transient."""
        max_retries = self.max_retries
        base_sleep = self.retry_backoff_base_sec
        last_err: MmcliError | None = None
        attempt = 0

        while attempt < max_retries:
            attempt += 1
            cp = self._run(argv)
            try:
                self._ensure_ok(cp, what)
                return cp
            except MmcliError as exc:
                last_err = exc
                if attempt >= max_retries or not self._default_should_retry(exc):
                    raise
                delay = base_sleep * (2 ** (attempt - 1))
                _LOGGER.warning(
                    "mmcli %s failed rc=%s attempt %s/%s sleep %.2fs",
                    what,
                    exc.exit_code,
                    attempt,
                    max_retries,
                    delay,
                )
                time.sleep(delay)

        assert last_err is not None
        raise last_err

    @staticmethod
    def _ensure_ok(cp: subprocess.CompletedProcess[str], what: str) -> None:
        if cp.returncode != 0:
            raise MmcliError(
                f"{what} failed (exit {cp.returncode})",
                stdout=cp.stdout or "",
                stderr=cp.stderr or "",
                exit_code=cp.returncode,
            )

    @staticmethod
    def _quote_field(value: str) -> str:
        return value.replace("'", "\\'")

    def list_modem_indices(self) -> list[int]:
        cp = self._run([self.mmcli_path, "-L"])
        self._ensure_ok(cp, "mmcli -L")
        haystack = (cp.stdout or "") + "\n" + (cp.stderr or "")
        return sorted({int(x) for x in _modem_path_re.findall(haystack)})

    def ensure_modem_index(self, modem_index: int) -> None:
        present = self.list_modem_indices()
        if modem_index not in present:
            raise MmcliError(
                f"Modem index {modem_index} not enumerated by ModemManager "
                f"(present indices={present}); adjust MODEM_MMCLI_INDEX",
                stderr="modem index missing",
                exit_code=-2,
            )

    def modem_ping(self, modem_index: int) -> tuple[bool, str]:
        argv = [self.mmcli_path, "-m", str(modem_index)]
        cp = self._run(argv)
        err = ((cp.stderr or "") + "\n" + (cp.stdout or "")).strip()
        return (cp.returncode == 0), err[:2000]

    def show_modem(self, modem_index: int) -> dict[str, str]:
        """Fetch modem overview from mmcli (key-value + JSON merge, analogous to ``show_sms``)."""
        mi = str(modem_index)
        cp_kv = self._run([self.mmcli_path, '-m', mi, '--output-keyvalue'])
        if cp_kv.returncode != 0:
            raise MmcliError(
                'Failed to inspect modem (--output-keyvalue)',
                stdout=cp_kv.stdout or '',
                stderr=cp_kv.stderr or '',
                exit_code=cp_kv.returncode,
            )
        kv_data = _parse_keyvalue(cp_kv.stdout or '')

        js_data: dict[str, str] = {}
        cp_js = self._run([self.mmcli_path, '-m', mi, '--output-json'])
        if cp_js.returncode == 0 and (cp_js.stdout or '').strip():
            try:
                js_data = _normalize_mmcli_json(cp_js.stdout or '')
            except (json.JSONDecodeError, ValueError):
                js_data = {}
        merged_main = _merge_mmcli_sources(kv_data, js_data)
        if merged_main:
            return merged_main

        cp_plain = self._run([self.mmcli_path, '-m', mi])
        self._ensure_ok(cp_plain, 'mmcli -m')
        fallback = _parse_keyvalue(cp_plain.stdout or '')
        return _merge_mmcli_sources(kv_data, js_data, fallback)

    def create_sms(self, modem_index: int, number: str, text: str) -> str:
        text_esc = self._quote_field(text)
        num_esc = self._quote_field(number)
        arg = f"text='{text_esc}',number='{num_esc}'"
        argv = [self.mmcli_path, "-m", str(modem_index), "--messaging-create-sms", arg]
        cp = self._retrying_run(argv, "--messaging-create-sms")
        m = _CREATED_PATH_RE.search((cp.stdout or "") + "\n" + (cp.stderr or ""))
        if not m:
            raise MmcliError(
                "Could not parse SMS object path from mmcli output",
                stdout=cp.stdout or "",
                stderr=cp.stderr or "",
                exit_code=cp.returncode,
            )
        return m.group(1)

    def send_sms(self, sms_path: str) -> None:
        argv = [self.mmcli_path, "-s", sms_path, "--send"]
        cp = self._retrying_run(argv, "--send")
        self._ensure_ok(cp, "--send")

    def list_sms_paths(self, modem_index: int) -> list[str]:
        argv = [self.mmcli_path, "-m", str(modem_index), "--messaging-list-sms"]
        cp = self._retrying_run(argv, "--messaging-list-sms")
        haystack = (cp.stdout or "") + "\n" + (cp.stderr or "")
        return sorted(set(_SMS_PATH_RE.findall(haystack)))

    def show_sms(self, sms_path: str) -> dict[str, str]:
        """Fetch SMS details from mmcli.

        ModemManager exposes overlapping data via ``--output-keyvalue`` and
        ``--output-json``. Keyvalue alone can omit ``sms.content.text`` while
        metadata is present — we merge both snapshots and prefer the longest
        non-empty string per field so concatenated/long bodies are not dropped.
        """
        cp_kv = self._run([self.mmcli_path, "-s", sms_path, "--output-keyvalue"])
        if cp_kv.returncode != 0:
            raise MmcliError(
                'Failed to inspect SMS (--output-keyvalue)',
                stdout=cp_kv.stdout or '',
                stderr=cp_kv.stderr or '',
                exit_code=cp_kv.returncode,
            )
        kv_data = _parse_keyvalue(cp_kv.stdout or '')

        js_data: dict[str, str] = {}
        cp_js = self._run([self.mmcli_path, "-s", sms_path, '--output-json'])
        if cp_js.returncode == 0 and (cp_js.stdout or '').strip():
            try:
                js_data = _normalize_mmcli_json(cp_js.stdout or '')
            except (json.JSONDecodeError, ValueError):
                js_data = {}
        merged_main = _merge_mmcli_sources(kv_data, js_data)
        if merged_main:
            return merged_main

        cp_plain = self._run([self.mmcli_path, '-s', sms_path])
        self._ensure_ok(cp_plain, 'mmcli -s show')
        fallback = _parse_keyvalue(cp_plain.stdout or '')
        merged_fb = _merge_mmcli_sources(kv_data, js_data, fallback)
        return merged_fb


def _normalize_key(raw: str) -> str:
    return "".join(ch for ch in str(raw).lower() if ch.isalnum())


def _is_blank_mmcli_value(val: str) -> bool:
    s = (val or '').strip()
    return not s or s == '--'


def _merge_mmcli_sources(*parts: dict[str, str]) -> dict[str, str]:
    """Combine dicts produced from mmcli snapshots; longest non-blank wins per key."""
    merged: dict[str, str] = {}
    keys: set[str] = set()
    for p in parts:
        keys |= set(p)
    for k in keys:
        best = ''
        for p in parts:
            v = (p.get(k) or '').strip()
            if _is_blank_mmcli_value(v):
                continue
            if not best or len(v) > len(best):
                best = v
        if best:
            merged[k] = best
    return merged


def _parse_keyvalue(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        nk = _normalize_key(key)
        val = val.strip()
        if nk and val:
            out.setdefault(nk, val)
    return out


def _flatten_dict(nested: dict[str, Any], parent_key: str = '', sep: str = '.') -> dict[str, Any]:
    """Recursively flatten nested dicts.

    ``mmcli --output-json`` returns nested objects like
    ``{"sms": {"content": {"text": "...", "number": "..."}}}``; we need flat
    keys ``sms.content.text`` so ``_normalize_key`` produces ``smscontenttext``
    like the keyvalue output path.
    """
    items: list[tuple[str, Any]] = []
    for k, v in nested.items():
        new_key = f'{parent_key}{sep}{k}' if parent_key else k
        if isinstance(v, dict):
            items.extend(_flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def _normalize_mmcli_json(raw: str) -> dict[str, str]:
    blob = json.loads(raw)
    if not isinstance(blob, dict):
        return {}
    flattened = _flatten_dict(blob)
    normalized: dict[str, str] = {}
    for key, val in flattened.items():
        nk = _normalize_key(key)
        if not nk:
            continue
        if isinstance(val, (dict, list)):
            normalized[nk] = json.dumps(val, ensure_ascii=False)
        elif val is None:
            normalized[nk] = ""
        else:
            normalized[nk] = str(val)
    return normalized


def extract_from_number(details: dict[str, str]) -> str:
    # --output-keyvalue: sms.content.number → smscontentnumber
    # --output-json: varies; also try bare "number", "sender"
    for nk in ("smscontentnumber", "number", "sender", "smssrc", "smsnumber", "mobilenumber"):
        v = details.get(nk)
        if v and v.strip() not in ("--", ""):
            return v.strip()
    return ""


def extract_text(details: dict[str, str]) -> str:
    # sms.content.text → smscontenttext; variants from JSON / ModemManager dumps
    for nk in (
        'smscontenttext',
        'text',
        'smstext',
        'smscontent',
        'smsmessage',
        'message',
        'body',
        'content',
        'smsbody',
    ):
        v = details.get(nk)
        if v and v.strip() not in ('--', ''):
            return v.strip()
    return ''


def extract_state(details: dict[str, str]) -> str:
    # --output-keyvalue: sms.properties.state → smspropertiesstate
    for nk in ("smspropertiesstate", "state", "smsstate"):
        v = details.get(nk)
        if v and v.strip() not in ("--", ""):
            return v.strip()
    return ""


def extract_smsc(details: dict[str, str]) -> str:
    # --output-keyvalue: sms.properties.smsc → smspropertiessmsc
    for nk in ("smspropertiessmsc", "smsc", "smsservicecenter", "servicecenter"):
        v = details.get(nk)
        if v and v.strip() not in ("--", ""):
            return v.strip()
    return ""


def extract_timestamp(details: dict[str, str]) -> str:
    # --output-keyvalue: sms.properties.timestamp → smspropertiestimestamp
    for nk in ("smspropertiestimestamp", "timestamp", "datetime", "stamp", "date"):
        v = details.get(nk)
        if v and v.strip() not in ("--", ""):
            return v.strip()
    return ""
