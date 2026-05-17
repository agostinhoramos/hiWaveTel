# Getting started

Maintained reference docs: **[CONTRIBUTING.md](CONTRIBUTING.md)** · **[docs/architecture.md](docs/architecture.md)** · **[docs/comunicacao.md](docs/comunicacao.md)** (HTTP + MQTT: API v1, JWT, tópicos e exemplos para integração externa).

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

- Inventário canónico de variáveis (Django, SMS/MQTT, `docker/entrypoint.sh`): copie **[`.env.example`](.env.example)** para **`.env`** na raiz; o Compose usa [`env_file: ../.env`](docker/docker-compose.yml). O mesmo modelo está em [`docker/.env.example`](docker/.env.example) para navegação na pasta `docker/`.
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

### SQLite concurrency (default deploy)

SQLite does not tolerate many simultaneous writers well. Compose passes **`SQLITE_BUSY_TIMEOUT_SEC`**, WAL-related pragmas, and backoff retries during inbound modem SMS persistence; inbound rows are serialized across **SMS worker** threads inside the gateway process. If `database is locked` still appears under heavy concurrent API + modem traffic, increase those env tuning knobs or migrate to Postgres.

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
