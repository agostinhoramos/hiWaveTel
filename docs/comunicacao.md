# Interface de comunicação hiWaveTel

Este documento descreve todos os mecanismos públicos de integração: **HTTP (REST)** e **MQTT**. Serve como guia para outro serviço ou equipamento reportar SMS recebidos, pedir envios e acompanhar estado.

Referência normativa complementar: **OpenAPI** em `GET /api/schema/` e interface **Swagger** em `GET /api/docs/` (quando o servidor Django está a correr).

Especificação autocontida para um **gateway cliente MQTT** frente a servidor tipo hiDisheLink (tópicos, JSON, mosquitto, checklist): ficheiro `docs/gateway-mqtt-hidishelink-especificacao.md`.

---

## 1. Visão geral

Existem **dois eixos** independentes:

| Eixo | Base URL | Autenticação típica | Finalidade |
|------|----------|---------------------|------------|
| **Gateway dispositivos externos** | `https://{HOST}/api/v1/` | Chave API (`X-API-Key` ou `Authorization: ApiKey …`) | Integração de **dispositivos/aplicações externas** com o modem gerido pelo hiWaveTel (envio agregado, inbox por dispositivo). |
| **API SMS operador** | `https://{HOST}/api/sms/…` | JWT Bearer (utilizador Django) | Operação directa sobre SMS persistidos em base de dados e envio via **mmcli** no servidor. |

Em paralelo, um processo opcional **`run_mqtt_gateway`** (arrancado no contentor quando `RUN_MQTT_GATEWAY=true`) mantém um cliente MQTT persistente: subscreve tópicos de dispositivos, publica pedidos de envio e telemetria do modem, conforme descrito na secção MQTT. Quando `MQTT_HEALTH_SERVER_PING_INTERVAL_SEC` é maior que zero, esse mesmo cliente publica periodicamente pings **tipo B** (`source: django`) para cada `ExternalDevice` em estado activo — comportamento que **não** ocorre apenas com o servidor HTTP Gunicorn sem este processo.

---

## 2. Autenticação HTTP

### 2.1 API v1 — dispositivo externo

Implementação: `apps/external_device/authentication.py`.

- **`X-API-Key: <chave_em_claro>`** — recomendado para integrações simples.
- **`Authorization: ApiKey <chave_em_claro>`** — alternativa compatível.

A chave só é mostrada **uma vez** no registo (ver `POST …/register/`). O servidor guarda hash SHA-256.

### 2.2 API SMS — JWT

1. Obter tokens: `POST /api/auth/token/` com corpo JSON `{"username":"…","password":"…"}` (utilizador Django válido).
2. Chamadas subsequentes: cabeçalho **`Authorization: Bearer <access>`**.

Renovação: `POST /api/auth/token/refresh/` com `{"refresh":"…"}`.

### 2.3 Endpoints sem autenticação obrigatória

- `POST /api/v1/external-devices/register/` — público; exige `registration_token` válido criado no admin.
- `POST /api/sms/device/register/` — público (contrato app Android hiDisheLink); mesmo modelo `ExternalDevice`, envelope `{ success, data, error }`.
- `GET /api/health/` — sonda de disponibilidade modem/mmcli (sem segredos).

---

## 3. Referência REST — Gateway `/api/v1/`

Prefixo: `{BASE}/api/v1/` onde `{BASE}` é por exemplo `http://127.0.0.1:8000` ou o URL público do gateway.

Rotas definidas em `apps/external_device/urls.py`. Modelos de pedido/resposta: `apps/external_device/serializers.py`.

### Superfície paralela — App hiDisheLink (`/api/sms/device/`)

Contrato compatível com a **app Android hiDisheLink SMS**: **`{BASE}/api/sms/device/`** (`register`, `login`, `refresh`, `logout`, `status`, `mqtt-config`, `get-pending-key`), envelope **`success`** / **`data`** / **`error`**, sessões em base (`DeviceSession`). `GET status` e `GET mqtt-config` usam **`X-API-Key`** e **`device_id`**. O corpo `data` de `GET mqtt-config` é obtido por proxy do servidor hiDisheLink (`HIDISHELINK_API_URL` ou credenciais em `HiDishelinkDevice`), espelhando o JSON remoto (`MQTT_*`, `TOPIC_*` com **`{device_id}`**, etc.). Ficheiros: `apps/external_device/device_urls.py`, `device_api_views.py`, `mqtt_config_remote.py`.

### 3.1 Registo de dispositivo

**`POST /api/v1/external-devices/register/`**

Corpo JSON esperado:

| Campo | Obrigatório | Descrição |
|-------|-------------|-----------|
| `device_id` | sim | Identificador estável do dispositivo (ex.: MSISDN ou UUID interno), até 64 caracteres. |
| `registration_token` | sim | Token único gerado no Django Admin para este dispositivo pendente. |
| `name` | sim | Nome legível. |
| `device_type` | não | Default `modem`. |
| `mqtt_client_id` | não | Opcional; pode ser usado em integrações MQTT. |
| `metadata` | não | Objeto JSON livre (ex.: `{ "modem_index": 0 }` para filtrar inbox espelhada por modem). |

Resposta `200` inclui `api_key`, `device_id`, `status`. **Guarde a `api_key`** — não volta a ser exibida.

### 3.2 Enviar SMS (agregado)

**`POST /api/v1/sms/send/`** — requer API key.

Corpo:

```json
{
  "recipients": ["+351912345678"],
  "message": "Texto UTF-8 do SMS",
  "priority": "normal"
}
```

`priority`: `normal` \| `high` \| `urgent`.

Resposta típica **`202 Accepted`**:

```json
{
  "request_id": "sms_<token>",
  "status": "processing"
}
```

O servidor cria `SmsRequest`, envia via mmcli para cada destinatário e atualiza estado. Se `MQTT_PUBLISH_SEND_REQUEST` estiver activo, também publica no MQTT (ver secção 6).

### 3.3 Estado do pedido de envio

**`GET /api/v1/sms/status/?request_id=<valor>`** — requer API key.

Devolve `request_id`, `status`, `sent_count`, `failed_count`, lista `recipients` com `phone_number`, `status`, `message_id`, `error_message`.

### 3.4 Inbox do dispositivo

**`GET /api/v1/sms/inbox/`** — requer API key.

- Resposta **paginada** (pagination DRF, típico `page` na query string; `page_size` definido em settings REST).
- Campos de cada mensagem: **`message_id`**, **`sender`**, **`body`**, **`received_at`** (ISO 8601).

Comportamento importante: antes de listar, o servidor chama **`sync_inbox_from_modem_store`**, que espelha linhas `InboundSms` (modem interno / mmcli) para `InboxMessage` deste dispositivo. Assim, SMS recebidos pelo watcher D-Bus do hiWaveTel aparecem na inbox API mesmo sem MQTT.

- Se `device.metadata["modem_index"]` for um inteiro, só entram SMS desse índice de modem; caso contrário entram todas as `InboundSms` visíveis (últimas 500 na sincronização).

### 3.5 Saúde do dispositivo

**`GET /api/v1/external-devices/{device_id}/health/`** — requer API key.

Devolve campos do modelo `ExternalDevice` relevantes para presença: `device_id`, `status`, `is_available`, `last_seen`.

### 3.6 Exemplos `curl` (placeholders)

Substitua `HOST`, `PORT`, `API_KEY`, `REQUEST_ID`.

```bash
# Registo (sem API key)
curl -s -X POST "http://HOST:PORT/api/v1/external-devices/register/" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"meu-modem-1","registration_token":"TOKEN_DO_ADMIN","name":"Gateway Loja"}'

# Enviar SMS
curl -s -X POST "http://HOST:PORT/api/v1/sms/send/" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: API_KEY" \
  -d '{"recipients":["+351912345678"],"message":"Olá","priority":"normal"}'

# Estado
curl -s "http://HOST:PORT/api/v1/sms/status/?request_id=REQUEST_ID" \
  -H "X-API-Key: API_KEY"

# Inbox
curl -s "http://HOST:PORT/api/v1/sms/inbox/" \
  -H "X-API-Key: API_KEY"

# Health do dispositivo
curl -s "http://HOST:PORT/api/v1/external-devices/meu-modem-1/health/" \
  -H "X-API-Key: API_KEY"
```

---

## 4. Referência REST — API SMS (JWT) `/api/`

Prefixo: `{BASE}/api/sms/…` (sem o `v1`). Requer **`Authorization: Bearer <access>`** em todas as operações listadas.

### 4.1 SMS recebidos (modem → base de dados)

- **`GET /api/sms/inbound/`** — lista `InboundSms` (read-only).
- **`GET /api/sms/inbound/{id}/`** — detalhe.

Query opcional:

- `from` — filtro parcial ao número de origem.
- `since` — ISO 8601; apenas mensagens com `created_at >= since`.

Campos principais do modelo: `mm_path`, `modem_index`, `from_number`, `text`, `mm_state`, `created_at`, etc.

### 4.2 SMS enviados (servidor → mmcli)

- **`POST /api/sms/outbound/`** — cria e envia.

Corpo JSON:

```json
{
  "modem_index": 0,
  "to": "+351912345678",
  "text": "Corpo UTF-8; o limite efectivo é o da rede GSM/PDU, não este campo."
}
```

`modem_index` é opcional; default do ambiente (`MODEM_MMCLI_INDEX`).

Resposta **`202`** com estado (`sent`, `failed`, …) e eventual `error_message`.

### 4.3 Exemplo `curl` com JWT

```bash
TOKEN="$(curl -s -X POST "http://HOST:PORT/api/auth/token/" \
  -H "Content-Type: application/json" \
  -d '{"username":"USER","password":"PASS"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["access"])')"

curl -s "http://HOST:PORT/api/sms/inbound/" \
  -H "Authorization: Bearer ${TOKEN}"
```

---

## 5. Health do sistema (modem)

**`GET /api/health/`** — sem autenticação.

Resposta JSON (campos principais):

- `ok` — boolean, `true` se o modem configurado responde ao ping mmcli.
- `modem_mmcli_indices` — índices listados por `mmcli -L`.
- `settings_modem_mmcli_index` — índice esperado pela configuração.
- `modem_mmcli_ping_ok` — resultado do ping ao índice configurado.
- `mmcli_notes` — texto curto de diagnóstico em caso de falha.

Código HTTP **503** quando não há modems ou ping falha; **200** quando `ok` é verdadeiro.

---

## 6. MQTT — configuração

Todas as variáveis listadas em `.env.example` sob a secção MQTT; as mais relevantes:

| Variável | Função |
|----------|--------|
| `MQTT_EXTERNAL_TOPIC_PREFIX` | Prefixo base para **catálogo/modems** (`…/modems/…`) quando `MQTT_BASE_TOPIC_PREFIX` não está definido (default de código: `hidishelink_dev`). |
| `MQTT_BASE_TOPIC_PREFIX` | Prefixo explícito para modems/snapshot (hiDisheLink). Por defeito igual a `MQTT_EXTERNAL_TOPIC_PREFIX`. |
| `MQTT_DEVICE_TOPIC_PREFIX` | Prefixo completo **antes** de `/{id}/sms/…` e `/{id}/health/…` (hiDisheLink). Por defeito `{MQTT_BASE_TOPIC_PREFIX}/devices`. Valores como `${MQTT_BASE_TOPIC_PREFIX}/devices` são expandidos em `config/settings/base.py` (o Django não expande variáveis de shell genericamente). |
| `MQTT_BROKER_URL`, `MQTT_PORT` | Broker e porta. Se a porta for **8883**, o cliente activa TLS. |
| `MQTT_USER`, `MQTT_PASS` | Credenciais opcionais. |
| `MQTT_CLIENT_ID` | ID do cliente do **gateway** em modo loop persistente. |
| `MQTT_QOS`, `MQTT_CLEAN_SESSION` | Comportamento de sessão Paho. |
| `MQTT_PUBLISH_SEND_REQUEST` | Se verdadeiro, após `POST …/sms/send/` o gateway também publica o pedido em `{device_prefix}/{id}/sms/send` (publicação **efémera** por pedido HTTP). |
| `MQTT_PUBLISH_MODEM_INBOX` | Se verdadeiro, ao espelhar SMS do modem para dispositivos, o gateway pode publicar em `inbox_delivery`. |
| `MQTT_MODEM_INBOX_DELIVERY_MODE` | `broadcast` (um tópico por modem) ou `per_device` (um tópico por `device_id`). |

**Sanitização de `device_id` nos tópicos:** caracteres `+` e `#` são removidos do identificador quando inserido no path MQTT (evitar conflito com wildcards MQTT).

---

## 7. MQTT — tópicos e payloads

Nos exemplos abaixo, `{device_prefix}` = `MQTT_DEVICE_TOPIC_PREFIX` efectivo (por defeito `{MQTT_BASE_TOPIC_PREFIX}/devices`, tipicamente `{MQTT_EXTERNAL_TOPIC_PREFIX}/devices`), `{modem_prefix}` = `MQTT_BASE_TOPIC_PREFIX` efectivo, `{id}` = `device_id` sanitizado, `{N}` = índice numérico do modem (mmcli).

Payloads são **JSON UTF-8** salvo indicação em contrário.

| Tópico (padrão) | Publica | Subscreve | Payload / notas |
|-----------------|---------|-----------|------------------|
| `{device_prefix}/{id}/sms/send` | Gateway | Dispositivo externo (opcional) | Quando activo: `request_id`, `recipients`, `message`, `priority` (igual ao processamento HTTP). |
| `{device_prefix}/+/sms/status` | Dispositivo | Gateway | Atualização de estado do pedido. Ver tabela abaixo. |
| `{device_prefix}/+/sms/inbox` | Dispositivo | Gateway | SMS recebido reportado pelo dispositivo. Ver tabela abaixo. |
| `{device_prefix}/{id}/sms/inbox/ack` | Gateway | Dispositivo | `{"message_id":"…"}` após persistir inbox. |
| `{device_prefix}/{id}/sms/inbox_delivery` | Gateway | Qualquer cliente | Espelho modem→dispositivo, modo **`per_device`**. Campos: `message_id`, `sender`, `body`, `received_at`. |
| `{modem_prefix}/modems/{N}/sms/inbox_delivery` | Gateway | Qualquer cliente | Modo **`broadcast`**: inclui também `modem_index`, `mirrored_device_ids`, `device_message_ids` e `message_id` agregado. |
| `{modem_prefix}/modems/+/status/request` | Cliente | Gateway | Corpo ignorado para lógica; dispara snapshot mmcli. |
| `{modem_prefix}/modems/{N}/status/response` | Gateway | Cliente | Resposta única com `modem_index`, `gathered_at`, `mmcli_flat`, `success`, `error`. |
| `{modem_prefix}/modems/{N}/status/telemetry` | Gateway | Cliente | Telemetria periódica ou `event` (`bootstrap`, `state_change`): mesma forma que `response` mais campo `event`. |

### 7.1 Payload `…/sms/status` (dispositivo → gateway)

O gateway actualiza `SmsRequest` via `update_request_from_mqtt_status`:

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `request_id` | string | **Obrigatório.** Deve coincidir com o devolvido pelo `POST …/sms/send/`. |
| `status` | string | `received` → processing; `success` → completed; `partial` → partial; `error` → failed; outros → processing. |
| `sent` | número | Contagem enviados com sucesso. |
| `failed` | número | Contagem falhas. |
| `details` | lista | Cada elemento: `recipient`, `status` (`sent` ou outro), `message_id`, `error_message`. |

### 7.2 Payload `…/sms/inbox` (dispositivo → gateway)

`persist_inbox_from_mqtt` exige:

| Campo | Obrigatório | Descrição |
|-------|-------------|-----------|
| `message_id` | sim | ID único da mensagem no sistema do dispositivo. |
| `sender` | sim | Número ou identificador do remetente. |
| `body` | não | Texto; pode ser string vazia. |
| `timestamp` | não | ISO 8601; se inválido ou ausente, usa relógio do servidor. |

---

## 8. Fluxos para integração de “outro projeto”

### 8.1 Apenas HTTP

1. Admin cria dispositivo pendente + `registration_token`.
2. Cliente chama `POST …/register/` e guarda `api_key`.
3. `POST …/sms/send/` com lista de destinatários.
4. Polling `GET …/sms/status/` até estado terminal.
5. `GET …/sms/inbox/` para SMS recebidos (incluindo os espelhados do modem interno).

### 8.2 HTTP + MQTT

Mesmo fluxo HTTP; em paralelo:

- **Subscrever** `{prefix}/modems/{N}/sms/inbox_delivery` (broadcast) ou `{prefix}/devices/{id}/sms/inbox_delivery` (per_device) para receber cópias push de SMS recebidos pelo modem do hiWaveTel.
- **Subscrever** `{prefix}/devices/{id}/sms/send` se quiser replicação dos pedidos de envio quando `MQTT_PUBLISH_SEND_REQUEST` está activo.
- **Publicar** em `{prefix}/devices/{id}/sms/status` para actualizar estado sem HTTP.
- **Subscrever** `{prefix}/modems/{N}/status/telemetry` para telemetria mmcli.

### 8.3 Dispositivo que recebe SMS localmente (sem usar inbox do servidor)

Publicar para **o mesmo `{prefix}` e `device_id` registado**:

- Tópico: `{prefix}/devices/<id_sanitizado>/sms/inbox`
- Payload JSON válido com `message_id`, `sender` e opcionalmente `body`, `timestamp`.

O gateway persiste na inbox API e pode publicar **ACK** em `{prefix}/devices/{id}/sms/inbox/ack`.

### 8.4 Exemplo mínimo com `mosquitto`

```bash
# Subscrever telemetria do modem 0 (substituir PREFIX e HOST do broker)
mosquitto_sub -h MQTT_HOST -p 1883 \
  -t 'PREFIX/modems/0/status/telemetry' -v

# Pedir snapshot puntual ao gateway (broker precisa aceitar publishes do cliente)
mosquitto_pub -h MQTT_HOST -p 1883 \
  -t 'PREFIX/modems/0/status/request' -m '{}'
```

### 8.5 Esboço Python (`paho-mqtt`)

```python
import json
import paho.mqtt.client as mqtt

PREFIX = "hidishelink_dev"
DEVICE_ID = "meu-modem-1".replace("+", "").replace("#", "")

def on_connect(client, userdata, flags, rc):
    client.subscribe(f"{PREFIX}/devices/{DEVICE_ID}/sms/send", qos=1)
    client.subscribe(f"{PREFIX}/devices/{DEVICE_ID}/sms/inbox/ack", qos=1)

def on_message(client, userdata, msg):
    data = json.loads(msg.payload.decode("utf-8"))
    if msg.topic.endswith("/sms/send"):
        # Reagir a request_id, recipients, message, priority
        print("send job", data)
    elif msg.topic.endswith("/inbox/ack"):
        print("acked", data.get("message_id"))

client = mqtt.Client(client_id="ext_proj_01", protocol=mqtt.MQTTv311)
client.on_connect = on_connect
client.on_message = on_message
client.connect("MQTT_HOST", 1883, 60)
client.loop_forever()
```

Para **reportar** um SMS recebido:

```python
topic = f"{PREFIX}/devices/{DEVICE_ID}/sms/inbox"
payload = {
    "message_id": "ext-001",
    "sender": "+351912345678",
    "body": "Olá",
    "timestamp": "2026-05-17T12:00:00Z",
}
client.publish(topic, json.dumps(payload), qos=1)
```

---

## 9. OpenAPI e Swagger

- **Schema:** `GET /api/schema/` (OpenAPI 3, JSON ou YAML conforme configuração drf-spectacular).
- **UI:** `GET /api/docs/` — exploração interactiva; útil para validar campos exactos e códigos de resposta por endpoint.

---

## 10. Resumo rápido

| Necessidade | Caminho típico |
|-------------|----------------|
| Integração app/dispositivo com chave fixa | `/api/v1/…` + MQTT opcional nos tópicos `{prefix}/devices/…` |
| Consola técnica / backoffice Django | `/api/sms/…` com JWT |
| Sonda uptime modem | `/api/health/` |
| Contratos e exemplos formais | `/api/schema/` + `/api/docs/` |

Para variáveis de ambiente que afectam broker, prefixo MQTT e quotas, consulte **[`.env.example`](../.env.example)** na raiz do repositório.
