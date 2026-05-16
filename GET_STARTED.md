# Getting started

Maintained reference docs: **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[docs/architecture.md](docs/architecture.md)**.

## Virtualenv (host)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser  # needed for JWT-backed API access
python manage.py runserver
```

Environment:

- `DJANGO_SETTINGS_MODULE` defaults to **`config.settings`** (loads `development` or `production` from `DJANGO_ENV`).
- Compose defaults to **`DJANGO_ENV=development`** with a dev-only `SECRET_KEY` fallback.

## Authenticated API smoke test

```bash
TOKEN="$(curl -s -X POST http://127.0.0.1:8000/api/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"YOU","password":"PASS"}' | python -c 'import sys,json; print(json.load(sys.stdin)["access"])')"
curl -sf -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8000/api/sms/inbound/
```

`GET /api/health/` does **not** require a token (container probes).

## Modem / Docker quick path

```bash
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml build --no-cache
docker compose -f docker/docker-compose.yml up
```

Stop host **ModemManager** / **NetworkManager** before attaching USB modems to the container (see comments in `docker/docker-compose.yml`).

## Quick checks (mmcli / Django quality)

At repository root:

```bash
./tests/test_mmcli_host.sh
coverage run -m pytest && coverage report
```

Inside the running container:

```bash
docker compose -f docker/docker-compose.yml exec hiwavetel bash /app/scripts/test_container_env.sh
```
