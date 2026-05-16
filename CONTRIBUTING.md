## Contributing

### Environment

Python **3.10+** matching the project `Dockerfile` / host `venv`. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Settings

`DJANGO_SETTINGS_MODULE` defaults to **`config.settings`** (package). Control runtime via:

| Variable | Purpose |
|----------|---------|
| `DJANGO_ENV` | `development` (default) or `production` |
| `DJANGO_SECRET_KEY` | **Required** in production |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list; **`production` forbids `*`** |
| `DJANGO_DEBUG` | Overrides production `DEBUG` only when explicitly set truthy |

### Tests & coverage

```bash
python -m pytest
coverage run -m pytest
coverage report
```

Older `manage.py test` no longer discovers any tests (`apps/sms/tests.py` was removed after the pytest migration). Use **pytest-django**, configured in **`pyproject.toml`**.

**`pyproject.toml`** holds pytest/pytest-django settings. Coverage thresholds remain in **[`.coveragerc`](.coveragerc)** (`fail_under = 80`).

### API authentication

Authenticate REST calls with JWT:

```bash
TOKEN="$(curl -s -X POST http://127.0.0.1:8000/api/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"YOU","password":"PASS"}' | jq -r .access)"

curl -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8000/api/sms/inbound/
```

`GET /api/health/` does **not** require a token — keep probes cheap.

### Code style guidelines

1. Prefer **explicit** service helpers (`persist_inbound_sms`, `dispatch_outbound_mmcli`) over fat views.
2. Use **British English** in user-visible strings/comments (project convention).
3. Keep **_modem / mmcli_** integrations inside `apps.sms` unless a second consumer appears.
4. Run `python manage.py check` **and** `python -m pytest` before opening a PR.

### Docker notes

Compose defaults **`DJANGO_ENV=development`** for frictionless ModemManagerBring-up. Flip to **`production`** once `DJANGO_SECRET_KEY` exists in `.env`. **Stop host ModemManager** before giving the container the modem.
