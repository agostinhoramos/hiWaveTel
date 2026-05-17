# hiWaveTel — exemplos de comunicação (HTTP API e MQTT)

Este ficheiro é um **exemplo de README** com chamadas **reais** ao contrato implementado no código.  
Substitua `BASE`, portas, `device_id`, tokens e chaves pelos seus valores.

- **API base (Django):** por defeito `http://127.0.0.1:8000` (variável `HIWAVE_PORT` no Compose).
- **Prefixo v1 (gateway de dispositivos externos):** ` /api/v1/ `
- **Documentação OpenAPI:** `GET /api/schema/` · **Swagger UI:** `GET /api/docs/`  
  No Swagger, use **Authorize** → **apiKeyAuth** (`X-API-Key`) para rotas `/api/v1/…` e **jwtAuth** (Bearer) para `/api/sms/…`. As credenciais podem ficar guardadas após atualizar a página (`persistAuthorization`).

## ⚙️ Detecção automática de SMS recebidas

Quando uma SMS é recebida pelo modem interno:
1. O D-Bus watcher detecta e persiste em `InboundSms`
2. **Automaticamente** (via sinal `post_save`), é espelhada para `InboxMessage` de todos os `ExternalDevice` activos
3. Dispositivos com `metadata.modem_index` definido só recebem SMS do índice correspondente
4. Dispositivos sem `metadata.modem_index` recebem **todas** as SMS (qualquer índice)

Logs disponíveis em:
- Ficheiro: `./logs/hiwavetel-api.log` (rotação diária)
- Docker: `docker logs -f hiwavetel` (stdout/stderr)

---

## 1. API REST

### 1.1. Health (sem autenticação)

Prova de vida para balanceadores / containers:

```bash
curl -sf "http://127.0.0.1:8000/api/health/"
```

### 1.2. JWT — API SMS com modem (utilizador Django)

Obter token (corpo com utilizador e palavra-passe Django válidos):

```bash
curl -s -X POST "http://127.0.0.1:8000/api/auth/token/" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"sua_password"}'
```

Resposta típica (Simple JWT): `access` e `refresh`. Chamada autenticada (exemplo listagem SMS recebidos — router registado em `/api/`):

```bash
TOKEN="<colar_access_aqui>"
curl -sf -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8000/api/sms/inbound/"
```

### 1.3. Gateway de dispositivo externo — API Key

A API do gateway aceita **uma** destas formas:

- Cabeçalho `Authorization: ApiKey <chave_crua>`
- Cabeçalho `X-API-Key: <chave_crua>`

Os exemplos abaixo usam `Authorization: ApiKey …`.

#### Registo do dispositivo (sem API key; primeira chamada pode auto-criar o device)

`POST /api/v1/external-devices/register/`

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/external-devices/register/" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "+351912329317",
    "registration_token": "token_admin_uma_vez",
    "name": "Modem site A",
    "device_type": "modem",
    "mqtt_client_id": "meu_cliente_mqtt_opcional",
    "metadata": {"site": "Lisboa"}
  }'
```

Resposta de sucesso (200): inclui `api_key`, `device_id` e `status` — **guarde a `api_key`**, não volta a ser mostrada.

#### Enviar SMS

`POST /api/v1/sms/send/` — resposta **202 Accepted**.

```bash
API_KEY="<api_key_obtida_no_registo>"
curl -s -X POST "http://127.0.0.1:8000/api/v1/sms/send/" \
  -H "Content-Type: application/json" \
  -H "Authorization: ApiKey ${API_KEY}" \
  -d '{
    "recipients": ["+351912345678"],
    "message": "Mensagem de teste",
    "priority": "normal"
  }'
```

`priority` admite: `normal`, `high`, `urgent`.

#### Estado de um pedido de envio

`GET /api/v1/sms/status/?request_id=<id>`

```bash
curl -sf "http://127.0.0.1:8000/api/v1/sms/status/?request_id=sms_abc123XY" \
  -H "Authorization: ApiKey ${API_KEY}"
```

Corpo de resposta inclui `request_id`, `status`, `sent_count`, `failed_count` e lista `recipients` com `phone_number`, `status`, `message_id`, `error_message`.

#### Listar inbox (SMS recebidos associados ao dispositivo)

`GET /api/v1/sms/inbox/`

```bash
curl -sf "http://127.0.0.1:8000/api/v1/sms/inbox/" \
  -H "Authorization: ApiKey ${API_KEY}"
```

#### Saúde do dispositivo

`GET /api/v1/external-devices/<device_id>/health/`

```bash
curl -sf "http://127.0.0.1:8000/api/v1/external-devices/%2B351913000001/health/" \
  -H "Authorization: ApiKey ${API_KEY}"
```

(No URL, o `+` do E.164 deve ir codificado como `%2B`.)

---

## 2. MQTT — broker e tópicos

### 2.1. Configuração (ambiente)

Valores por defeito em `config/settings/base.py` (sobrescreveveis por variáveis de ambiente):

| Variável | Exemplo / defeito |
|----------|-------------------|
| `MQTT_BROKER_URL` | `localhost` |
| `MQTT_PORT` | `1883` (se `8883`, o cliente ativa TLS) |
| `MQTT_USER` / `MQTT_PASS` | vazio ou credenciais do broker (aspas no `.env` são removidas) |
| `MQTT_CLIENT_ID` | `hiwavetel_gateway` |
| `MQTT_EXTERNAL_TOPIC_PREFIX` | `hidishelink_external` |
| `MQTT_PUBLISH_SEND_REQUEST` | `true` por defeito: após cada `POST …/sms/send/` o gateway publica também o JSON em `…/sms/send` no broker |
| `MQTT_PUBLISH_MODEM_INBOX` | No **Compose** oficial o valor por defeito passado ao contentor é `true` (`docker-compose.yml`) — cada SMS modem espelhada na inbox gera pelo menos uma notificação MQTT (ver modo abaixo). Em `manage.py`/pytest sem essa env, falta declarar explicitamente (`false` só no código Django se a variável estiver omitida). |
| `MQTT_MODEM_INBOX_DELIVERY_MODE` | `broadcast` (**defeito**): uma publicação canónica em `{prefix}/modems/{modem_index}/sms/inbox_delivery` com `message_id` tipo `mmcli_<pk>`, listas opcionais `mirrored_device_ids` e mapa `device_message_ids`. `per_device`: comportamento legado — `{prefix}/devices/{id_sanitizado}/sms/inbox_delivery`, um publish por `ExternalDevice` que espelhou a mesma entrada. |
| `MQTT_MODEM_STATUS_SUBSCRIBE` | `true` por defeito: o gateway subscreve `{prefix}/modems/+/status/request` e responde em `…/status/response` com JSON de estado do modem (`mmcli`). Defina `false` para não subscrever (útil quando só se usa o modo efémero de inbox/send). |
| `MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC` | Timeout em segundos para cada comando `mmcli` usado a construir esse snapshot (`45` por defeito). |
| `MQTT_MODEM_STATUS_AUTO_PUBLISH` | `true` por defeito: ligado ao broker, o gateway enumera os modems com `mmcli -L`, publica o snapshot em `{prefix}/modems/N/status/telemetry` no arranque (e após cada reconexão bem sucedida) e opcionalmente em mudanças de estado (polling). |
| `MQTT_MODEM_STATUS_POLL_INTERVAL_SEC` | Intervalo entre verificações de mudança de estado (`30` por defeito). **`0`** desactiva o polling mas mantém a publicação de arranque/reconexão. |
| `MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC` | `15`: timeout (s) sobre `publish` QoS antes de libertar ligação curta desde o worker Gunicorn |
| Docker `RUN_MQTT_GATEWAY` | `true` por defeito no `docker-compose.yml`: arranca `manage.py run_mqtt_gateway` em background (subscribe `status` / `inbox`) |

Prefixo real dos tópicos: **`{MQTT_EXTERNAL_TOPIC_PREFIX}`** (ex.: `hidishelink_external`).

### 2.2. `device_id` nos tópicos

Caracteres `+` e `#` são **removidos** do identificador ao formar o segmento do tópico.  
Exemplo: dispositivo `+351913000001` → segmento `351913000001`.

### 2.3. Tópicos implementados

**O gateway subscreve:**

- `{prefix}/devices/+/sms/status`
- `{prefix}/devices/+/sms/inbox`
- `{prefix}/modems/+/status/request` quando `MQTT_MODEM_STATUS_SUBSCRIBE` é verdadeiro (**defeito**)

**O gateway publica:**

- `{prefix}/devices/{device_id_sanitizado}/sms/send` (notificação de pedidos de envio originados pela API HTTP; mesmo contrato §3.3)
- `{prefix}/devices/{device_id_sanitizado}/sms/inbox/ack`
- Com `MQTT_PUBLISH_MODEM_INBOX=true`:
  - **Modo recomendado** (`MQTT_MODEM_INBOX_DELIVERY_MODE=broadcast`, **defeito**): `{prefix}/modems/{modem_index}/sms/inbox_delivery` — uma cópia canónica da SMS quando o espelho da inbox ficou útil aos subscritores (ver §3.4).
  - **Modo legado** (`per_device`): `{prefix}/devices/{device_id_sanitizado}/sms/inbox_delivery` — até N publicações (uma por dispositivo activo sem filtro de modem compatível).

- Telemetria (não solicitada): `{prefix}/modems/{modem_index}/status/telemetry` com `event=bootstrap` após cada ligação ao broker bem sucedida, e `event=state_change` quando `mmcli` reporta dados alterados (ver `MQTT_MODEM_STATUS_POLL_INTERVAL_SEC`).
- Pedido sob demanda: `{prefix}/modems/{modem_index}/status/response` como resposta aos pedidos §3.5.

**Dispositivo externo:** deve publicar em `status` e `inbox`, e subscrever `send`, `inbox/ack`. Para receber notificações de SMS espelhadas a partir do modem, subscreve `…/sms/inbox_delivery` no modo `per_device` ou `…/modems/{N}/sms/inbox_delivery` no modo `broadcast`.

---

## 3. MQTT — payloads JSON reais

### 3.1. Estado do envio (`…/sms/status`)

O payload deve incluir **`request_id`** (igual ao devolvido pela API ou ao usado no fluxo).  
Campos usados pelo servidor: `status`, `sent`, `failed`, `details`.

Valores de `status` mapeados: `received`, `success`, `partial`, `error` (outros caem em processamento).

Exemplo alinhado com os testes:

```json
{
  "request_id": "sms_test123",
  "status": "success",
  "sent": 2,
  "failed": 0,
  "details": [
    {
      "recipient": "+351912345678",
      "status": "sent",
      "message_id": "msg_001"
    },
    {
      "recipient": "+351987654321",
      "status": "sent",
      "message_id": "msg_002"
    }
  ]
}
```

Publicar com `mosquitto_pub` (ajuste host, utilizador e palavra-passe):

```bash
mosquitto_pub -h localhost -p 1883 \
  -t "hidishelink_external/devices/351913000001/sms/status" \
  -m '{"request_id":"sms_test123","status":"success","sent":2,"failed":0,"details":[{"recipient":"+351912345678","status":"sent","message_id":"msg_001"}]}'
```

### 3.2. Inbox — SMS recebido (`…/sms/inbox`)

Campos obrigatórios: **`message_id`**, **`sender`**. Recomendado: **`body`**, **`timestamp`** (ISO 8601).

Exemplo alinhado com os testes:

```json
{
  "message_id": "inbox_mqtt_001",
  "sender": "+351911111111",
  "body": "MQTT inbox test",
  "timestamp": "2026-05-17T12:00:00+00:00"
}
```

```bash
mosquitto_pub -h localhost -p 1883 \
  -t "hidishelink_external/devices/351913000001/sms/inbox" \
  -m '{"message_id":"inbox_mqtt_001","sender":"+351911111111","body":"MQTT inbox test","timestamp":"2026-05-17T12:00:00+00:00"}'
```

O gateway responde com **ACK** no tópico `…/sms/inbox/ack`:

```json
{"message_id": "inbox_mqtt_001"}
```

### 3.3. Pedido de envio publicado pelo gateway (`…/sms/send`)

O cliente MQTT do gateway serializa um objeto JSON qualquer; o código regista `request_id` nos logs.  
Para integração com o dispositivo, use um payload coerente com o envio REST, por exemplo:

```json
{
  "request_id": "sms_abc123XY",
  "recipients": ["+351912345678"],
  "message": "Texto a enviar",
  "priority": "normal"
}
```

Subscrição no dispositivo (exemplo):

```bash
mosquitto_sub -h localhost -p 1883 \
  -t "hidishelink_external/devices/351913000001/sms/send" -v
```

Os pedidos são publicados quando `MQTT_PUBLISH_SEND_REQUEST=true` (defeito) após o envio físico pelo modem ser processado pela API (`POST /api/v1/sms/send/`).

### 3.4 Push de inbox vinda do modem (`…/sms/inbox_delivery`)

Com `MQTT_PUBLISH_MODEM_INBOX=true` (no **Docker Compose** oficial isto vai por defeito para o contentor via `environment`), o gateway publica sempre que uma linha modem é espelhada na inbox (criação ou preenchimento de corpo até então em branco). Procure nos logs `MQTT modem inbox_delivery (broadcast)` ou `(per_device)`.

**Modo `broadcast` (defeito):** `{prefix}/modems/{modem_index}/sms/inbox_delivery` — payload JSON típico: `message_id` (`mmcli_<pk>` da `InboundSms`), `sender`, `body`, `received_at`, `modem_index`, e opcionalmente `mirrored_device_ids` / `device_message_ids` (`mmcli_<pk>_dev_<ExternalDevice.pk>` por dispositivo).

**Modo `per_device`:** `{prefix}/devices/{id_sanitizado}/sms/inbox_delivery`, com `message_id`=`mmcli_<pk>_dev_<ExternalDevice.pk>`.

Para desativar neste servidor: no `.env` ou Compose defina `MQTT_PUBLISH_MODEM_INBOX=false`.

```bash
mosquitto_sub -h mqtt.hidishe.com -p 43827 \
  -u utilizador_broker \
  -P palavra_passe \
  -t "hidishelink_external/modems/0/sms/inbox_delivery" -v
```

Legado (`per_device`):

```bash
mosquitto_sub -h mqtt.hidishe.com -p 43827 \
  -u utilizador_broker \
  -P palavra_passe \
  -t "hidishelink_external/devices/351913000001/sms/inbox_delivery" -v
```

### 3.5 Estado completo do modem (pedido / resposta)

Com `MQTT_MODEM_STATUS_AUTO_PUBLISH=true` (**defeito**), assim que o gateway se liga ao broker publica também (por modem enumerado pelo `mmcli -L`) em:

```text
{prefix}/modems/{modem_index}/status/telemetry
```

Corpo igual ao formato abaixo, com campo extra `event`: `bootstrap` na ligação/reconexão. Se `MQTT_MODEM_STATUS_POLL_INTERVAL_SEC` é maior que zero, esse tópico é voltado a publicar quando um hash SHA-256 do `mmcli_flat` deixa de coincidir com o último ciclo (`event`: `state_change`).

---

Com `MQTT_MODEM_STATUS_SUBSCRIBE=true` (**defeito**), pode pedir um snapshot `mmcli` publicando um corpo JSON vazio `{}` ou outro objeto no tópico:

```text
{prefix}/modems/{modem_index}/status/request
```

O gateway corre `mmcli` fora da thread MQTT e publica uma resposta em:

```text
{prefix}/modems/{modem_index}/status/response
```

Exemplo rápido (modificar host/credenciais):

```bash
mosquitto_pub -h localhost -p 1883 \
  -t "hidishelink_external/modems/0/status/request" -m '{}' -q 1

mosquitto_sub -h localhost -p 1883 \
  -t "hidishelink_external/modems/0/status/response" -C 1 -v
```

---

## 4. Comando de gestão Django — cliente MQTT do gateway

Corre o processo que liga ao broker e trata `status` / `inbox` dos dispositivos externos, pedidos de snapshot modem (`modems/+/status/request` → `…/status/response`) e telemetria automática (`…/status/telemetry` quando `MQTT_MODEM_STATUS_AUTO_PUBLISH` está activo):

```bash
python manage.py run_mqtt_gateway
```

No **Docker Compose** oficial, esse comando é iniciado automaticamente quando `RUN_MQTT_GATEWAY=true` na entrypoint (`docker-compose.yml` exporta esta variável ao contentor).

As publicações geradas pela **API HTTP** (tópico `…/sms/send`) e opcionalmente por **SMS recebida no modem** (nos tópicos `…/modems/{N}/sms/inbox_delivery` em modo broadcast ou `…/devices/{id}/sms/inbox_delivery` em modo `per_device`) usam ligações **efémeras** por pedido nos workers Gunicorn; continuam disponíveis mesmo que o cliente em background falhe iniciar até o broker estar alcançável — ver timeouts em `MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC` nos logs.

---

## 5. Referência rápida de rotas HTTP

| Método | Caminho | Auth |
|--------|---------|------|
| GET | `/api/health/` | — |
| POST | `/api/auth/token/` | corpo user/password |
| POST | `/api/auth/token/refresh/` | refresh token |
| GET | `/api/schema/`, `/api/docs/` | público (schema/UI) |
| POST | `/api/v1/external-devices/register/` | — (token de registo) |
| POST | `/api/v1/sms/send/` | ApiKey |
| GET | `/api/v1/sms/status/?request_id=` | ApiKey |
| GET | `/api/v1/sms/inbox/` | ApiKey |
| GET | `/api/v1/external-devices/<device_id>/health/` | ApiKey |
| * | `/api/sms/inbound/`, `/api/sms/outbound/` | JWT (modem interno) |

Para detalhes de cada campo, use **Swagger** em `/api/docs/` ou o schema OpenAPI em `/api/schema/`.
