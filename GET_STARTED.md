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

Stop host **ModemManager** / **NetworkManager** before starting the container — if host MM stays active, the modem often stays **`state: locked`** / **`lock: sim-pin`** inside Docker and SMS/Messaging never comes up:

```bash
sudo systemctl stop ModemManager NetworkManager
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d --build
```

Ensure **`.env`** defines per-modem SIM PIN and optional phone fallback (mmcli index `N` from `mmcli -L`):

```bash
MODEM_0_DEVICE_PIN_CODE='1369'
MODEM_0_DEVICE_PHONE_NUMBER=351961343706
# MODEM_1_DEVICE_PIN_CODE='3333'
```

On startup you should see `All modems SMS/Messaging ready` and `SMS watchers (--all-modems) started` in the container logs.

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


docker compose -f docker/docker-compose.yml exec hiwavetel bash -c 'cd /app/host && python -m pytest tests/ -q'

docker compose -f docker/docker-compose.yml up -d --build
docker exec -it hiwavetel bash -lc 'mmcli -L; echo MODEM_MMCLI_INDEX=$MODEM_MMCLI_INDEX; mmcli -m $MODEM_MMCLI_INDEX | head'
