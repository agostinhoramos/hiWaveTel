# hiWaveTel — exemplos de comunicação (HTTP API)

API simplificada: **envio SMS** + **webhooks inbound**. Sem autenticação.

Documentação completa: [`docs/comunicacao.md`](docs/comunicacao.md) · OpenAPI: `GET /api/schema/` · Swagger: `GET /api/docs/`

Substitua `BASE` pela URL do servidor (ex.: `http://192.168.1.65:5202`).

## Enviar SMS

```bash
curl -s -X POST "${BASE}/api/sms/send/" \
  -H "Content-Type: application/json" \
  -d '{"to":"+351912345678","text":"Olá do hiWaveTel","modem_index":0}'
```

Resposta **202**:

```json
{
  "id": 1,
  "state": "sent",
  "to": "+351912345678",
  "error_message": ""
}
```

## Health

```bash
curl -s "${BASE}/api/health/"
```

## Disponibilidade do modem

```bash
curl -s "${BASE}/api/sms/system/modem/0/availability/"
```

## Modems

```bash
curl -s "${BASE}/api/sms/modems/"
curl -s "${BASE}/api/sms/modems/0/"
curl -s -X PUT "${BASE}/api/sms/modems/0/" -H "Content-Type: application/json" -d '{"enabled":true}'
curl -s -X POST "${BASE}/api/sms/modems/sync/"
```

## Webhooks inbound

Registe webhooks por modem via API ou Django Admin → **Inbound webhooks**:

```bash
curl -s -X POST "${BASE}/api/sms/modems/0/webhooks/" \
  -H "Content-Type: application/json" \
  -d '{"name":"app","url":"https://app.example/hooks/sms"}'
```

Listar todos:

```bash
curl -s "${BASE}/api/sms/webhooks/"
```

Quando o modem recebe SMS, hiWaveTel faz **POST JSON** a cada URL activa:

```json
{
  "id": 123,
  "sender": "+351912345678",
  "body": "texto",
  "modem_index": 0,
  "received_at": "2026-06-22T12:00:00+00:00",
  "mm_state": "received"
}
```

O endpoint destino deve responder **2xx**.

## Admin Django

`/admin/` — consultar SMS persistidos e gerir webhooks.

Superuser no Docker: variáveis `DJANGO_SUPERUSER_*` no `.env` (ver `.env.example`).
