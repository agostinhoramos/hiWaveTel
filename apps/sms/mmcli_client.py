"""Wrappers around `mmcli` for ModemManager SMS operations."""

from __future__ import annotations

import json
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
        for flag in ("--output-keyvalue", "--output-json"):
            argv = [self.mmcli_path, "-s", sms_path, flag]
            cp = self._run(argv)
            if cp.returncode != 0:
                raise MmcliError(
                    f"Failed to inspect SMS ({flag})",
                    stdout=cp.stdout or "",
                    stderr=cp.stderr or "",
                    exit_code=cp.returncode,
                )
            if flag.endswith("json"):
                try:
                    return _normalize_mmcli_json(cp.stdout or "")
                except (json.JSONDecodeError, ValueError):
                    continue
            data = _parse_keyvalue(cp.stdout or "")
            if data:
                return data
        cp = self._run([self.mmcli_path, "-s", sms_path])
        self._ensure_ok(cp, "mmcli -s show")
        return _parse_keyvalue(cp.stdout or "")


def _normalize_key(raw: str) -> str:
    return "".join(ch for ch in str(raw).lower() if ch.isalnum())


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


def _normalize_mmcli_json(raw: str) -> dict[str, str]:
    blob = json.loads(raw)
    if not isinstance(blob, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, val in blob.items():
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
    for nk in ("number", "sender", "smssrc", "smsnumber", "mobilenumber"):
        v = details.get(nk)
        if v:
            return v.strip()
    return ""


def extract_text(details: dict[str, str]) -> str:
    for nk in ("text", "smstext"):
        v = details.get(nk)
        if v:
            return v.strip()
    return ""


def extract_state(details: dict[str, str]) -> str:
    for nk in ("state", "smsstate"):
        v = details.get(nk)
        if v:
            return v.strip()
    return ""


def extract_smsc(details: dict[str, str]) -> str:
    for nk in ("smsc", "smsservicecenter", "servicecenter"):
        v = details.get(nk)
        if v:
            return v.strip()
    return ""


def extract_timestamp(details: dict[str, str]) -> str:
    for nk in ("timestamp", "datetime", "stamp", "date"):
        v = details.get(nk)
        if v:
            return v.strip()
    return ""
