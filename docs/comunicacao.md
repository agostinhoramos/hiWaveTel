# Interface de comunicação hiWaveTel

API mínima para envio de SMS via modem e entrega de SMS recebidos por **webhooks HTTP**. **Sem autenticação** — proteja o acesso em rede (firewall/VPN).

Referência OpenAPI: `GET /api/schema/` · Swagger UI: `GET /api/docs/`

---

## 1. Visão geral

| Mecanismo | Direcção | Descrição |
|-----------|----------|-----------|
| `POST /api/sms/send/` | Cliente → hiWaveTel | Enviar SMS via mmcli |
| Webhooks HTTP | hiWaveTel → Cliente | SMS recebidos no modem |
| `GET /api/health/` | Cliente → hiWaveTel | Sonda modem/mmcli |
| `GET /api/sms/modems/` | Cliente → hiWaveTel | Modems detectados |
| `GET/PUT /api/sms/modems/{id}/` | Cliente → hiWaveTel | Detalhe / editar `enabled` |
| `POST /api/sms/modems/sync/` | Cliente → hiWaveTel | Re-detectar modems (mmcli -L) |
| `GET /api/sms/webhooks/` | Cliente → hiWaveTel | Listar webhooks |
| `POST /api/sms/modems/{id}/webhooks/` | Cliente → hiWaveTel | Registar webhook por modem |
| `GET/PUT/PATCH/DELETE /api/sms/modems/{id}/webhooks/{webhook_id}/` | Cliente → hiWaveTel | Consultar / editar / apagar webhook |
| `POST /api/sms/system/container/restart/` | Cliente → hiWaveTel | Reiniciar container |

Processos em segundo plano (Docker `RUN_SMS_WATCHER=true`):

- **`run_sms_watcher`** — escuta D-Bus `Messaging.Added`, enfileira persistência (`SmsProcessingQueue`) e grava `InboundSms` na BD.
- **`run_webhook_worker`** — processo separado; lê jobs `WebhookDeliveryJob` da BD e envia HTTP POST aos webhooks (fila durável antes da entrega).
- **`sync_modems`** — no arranque Docker, regista modems detectados em `ModemDevice`.

---

## 2. Autenticação

Nenhuma. Todos os endpoints REST públicos aceitam pedidos anónimos.

**Risco aceite:** qualquer host na rede pode enviar SMS, reiniciar o container (se activo) e alterar configuração. Restrinja por firewall.

---

## 3. Enviar SMS

**`POST /api/sms/send/`**

Corpo JSON:

```json
{
  "to": "+351912345678",
  "text": "Mensagem de teste",
  "modem_index": 0
}
```

| Campo | Obrigatório | Descrição |
|-------|-------------|-----------|
| `to` | sim | Destinatário E.164 ou nacional |
| `text` | sim | Corpo da mensagem |
| `modem_index` | não | Índice mmcli (default: `MODEM_MMCLI_INDEX`) |

Resposta **202 Accepted**:

```json
{
  "id": 42,
  "state": "sent",
  "to": "+351912345678",
  "error_message": ""
}
```

Estados: `created`, `sent`, `failed`. Em falha mmcli, `error_message` contém detalhe.

Exemplo:

```bash
curl -s -X POST "http://HOST:5202/api/sms/send/" \
  -H "Content-Type: application/json" \
  -d '{"to":"+351912345678","text":"teste"}'
```

---

## 4. Modems

Modems são detectados automaticamente via ModemManager (`mmcli -L`) e persistidos em `ModemDevice` no arranque e em sync periódico do watcher.

### `GET /api/sms/modems/`

Lista modems registados com estado live (telefone, fabricante, `state`, `available`).

### `POST /api/sms/modems/sync/`

Força nova detecção via `mmcli -L` e devolve a lista actualizada.

### `GET /api/sms/modems/{modem_index}/`

Detalhe completo: identidade mmcli, disponibilidade, última actividade SMS, timestamps de detecção.

**404** se o modem nunca foi detectado (sem registo em BD).

### `PUT /api/sms/modems/{modem_index}/`

Actualiza configuração persistida. Campo editável:

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `enabled` | bool | Se `false`, o modem permanece registado mas pode ser ignorado por integrações futuras |

```bash
curl -s -X PUT "http://HOST:5202/api/sms/modems/0/" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

### `GET /api/sms/system/modem/{modem_index}/availability/`

Sonda de disponibilidade (estado ModemManager, ping, índices enumerados). **200** se disponível, **503** se não.

---

## 5. SMS recebidos — webhooks

Quando o modem recebe SMS, hiWaveTel persiste `InboundSms` e faz **POST JSON** a cada URL activa **desse modem**.

### Configurar destinos

1. **API** — `POST /api/sms/modems/{modem_index}/webhooks/` (modem tem de existir em `mmcli -L`).
2. **Django Admin** → **Inbound webhooks** — CRUD por `modem_index`.

### `GET /api/sms/webhooks/`

Lista todos os webhooks (`id`, `modem_index`, `name`, `url`, `enabled`, `created_at`).

### `POST /api/sms/modems/{modem_index}/webhooks/`

```json
{
  "name": "app-principal",
  "url": "https://app.example/hooks/sms",
  "enabled": true
}
```

**404** se o índice não estiver enumerado pelo ModemManager.

Use o **URL de endpoint** do destino (ex.: `https://webhook.site/{uuid}`), não a página de edição do browser (`#!/edit/...`). O gateway normaliza automaticamente URLs webhook.site mal copiadas.

### `GET /api/sms/modems/{modem_index}/webhooks/{webhook_id}/`

Detalhe de um webhook do modem.

### `PUT /api/sms/modems/{modem_index}/webhooks/{webhook_id}/`

Actualiza `name`, `url` e/ou `enabled` (envie pelo menos um campo):

```json
{
  "name": "app-principal",
  "url": "https://webhook.site/e6138d12-ca64-4caa-ae32-fd304cdc063d",
  "enabled": true
}
```

**404** se o webhook não existir ou não pertencer ao `modem_index`.

### `PATCH /api/sms/modems/{modem_index}/webhooks/{webhook_id}/`

Actualização parcial (ex.: só `enabled` ou só `url`).

### `DELETE /api/sms/modems/{modem_index}/webhooks/{webhook_id}/`

Remove o webhook. Resposta **204 No Content** em sucesso.

**404** se o webhook não existir ou não pertencer ao `modem_index`.

### Payload entregue ao destino

**SMS recebido** (modem → cliente):

```json
{
  "id": 123,
  "sender": "+351912345678",
  "body": "texto recebido",
  "modem_index": 0,
  "received_at": "2026-06-22T12:00:00+00:00",
  "mm_state": "received"
}
```

**SMS enviado** via `POST /api/sms/send/` (cliente → modem):

```json
{
  "id": 140,
  "sender": "me",
  "body": "test message",
  "modem_index": 0,
  "received_at": "2026-06-22T14:01:50.729408+00:00",
  "mm_state": "sended"
}
```

O eco do modem na caixa de mensagens (D-Bus) **não** dispara um segundo webhook — só o envio bem-sucedido pela API notifica com o payload acima.

### Retry

| Variável | Default | Descrição |
|----------|---------|-----------|
| `SMS_WEBHOOK_TIMEOUT_SEC` | 15 | Timeout HTTP por tentativa |
| `SMS_WEBHOOK_RETRY_MAX` | 5 | Tentativas por URL |
| `SMS_WEBHOOK_RETRY_BASE_SEC` | 1.0 | Backoff exponencial (máx. 60s) |
| `SMS_WEBHOOK_SSL_VERIFY` | false | Verificar certificado HTTPS do destino (`false` = aceitar self-signed) |

O servidor destino deve responder **2xx**. Corpo da resposta é ignorado.

---

## 6. Reiniciar container

**`POST /api/sms/system/container/restart/`**

Agenda `SIGTERM` ao PID 1 após curto delay (para a resposta HTTP ser enviada). O Docker com `restart: unless-stopped` recria o container.

Resposta **202 Accepted**:

```json
{
  "accepted": true,
  "message": "Container restart scheduled.",
  "scheduled_at": "2026-06-22T12:00:00+00:00",
  "delay_sec": 1.0,
  "requested_by": "192.168.1.10"
}
```

Desactivar com `HIWAVE_ALLOW_CONTAINER_RESTART_API=false` → **403**.

---

## 7. Health

### `GET /api/health/`

Sonda rápida: Django activo, mmcli disponível, modem enumerado. **200** se OK, **503** se modem indisponível.

---

## 8. Admin Django

`/admin/` — consultar `InboundSms`, `OutboundSms`, **Modem devices**, **Inbound webhooks**.

Superuser: variáveis `DJANGO_SUPERUSER_*` no `.env` (bootstrap no arranque Docker) ou `createsuperuser` manual.

---

## 9. Variáveis de ambiente relevantes

Ver `.env.example`. Principais:

| Variável | Descrição |
|----------|-----------|
| `HIWAVE_PORT` | Porta HTTP (ex.: 5202) |
| `MODEM_N_DEVICE_PIN_CODE` | PIN SIM do modem índice N (ex.: `MODEM_0_DEVICE_PIN_CODE`) |
| `MODEM_N_DEVICE_PHONE_NUMBER` | MSISDN fallback quando mmcli não reporta número |
| `RUN_SMS_WATCHER` | Supervisor D-Bus + fila de persistência por modem |
| `RUN_WEBHOOK_WORKER` | Worker separado: fila BD → HTTP webhooks |
| `SMS_QUEUE_WORKERS` | Threads que persistem SMS (mmcli → BD) |
| `WEBHOOK_WORKER_THREADS` | Threads do worker de webhooks |
| `MODEM_SNAPSHOT_RECOVERY_INTERVAL_SEC` | Re-sync mmcli para recuperar SMS (default 60s) |
| `HIWAVE_ALLOW_CONTAINER_RESTART_API` | Permitir restart via API (default: true) |
| `HIWAVE_CONTAINER_RESTART_DELAY_SEC` | Delay antes do SIGTERM (default: 1.0) |
| `OUTBOUND_ASYNC_ENABLED` | Envio outbound assíncrono |

Hardware (ttyUSB, `cdc-wdm`, `wwan0`) e timeouts avançados: `docker/docker-compose.yml` e secção comentada em `.env.example`.

---

## 10. Checklist integração

1. Confirmar `GET /api/health/` → 200.
2. `POST /api/sms/modems/sync/` ou aguardar arranque — verificar `GET /api/sms/modems/`.
3. Registar webhook: `POST /api/sms/modems/0/webhooks/`.
4. Enviar SMS via `POST /api/sms/send/` e verificar `state`.
5. Restringir acesso de rede ao host/porta do serviço.
