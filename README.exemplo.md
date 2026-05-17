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
| `MQTT_USER` / `MQTT_PASS` | vazio ou credenciais do broker |
| `MQTT_CLIENT_ID` | `hiwavetel_gateway` |
| `MQTT_EXTERNAL_TOPIC_PREFIX` | `hidishelink_external` |

Prefixo real dos tópicos: **`{MQTT_EXTERNAL_TOPIC_PREFIX}`** (ex.: `hidishelink_external`).

### 2.2. `device_id` nos tópicos

Caracteres `+` e `#` são **removidos** do identificador ao formar o segmento do tópico.  
Exemplo: dispositivo `+351913000001` → segmento `351913000001`.

### 2.3. Tópicos implementados

**O gateway subscreve:**

- `{prefix}/devices/+/sms/status`
- `{prefix}/devices/+/sms/inbox`

**O gateway publica:**

- `{prefix}/devices/{device_id_sanitizado}/sms/send`
- `{prefix}/devices/{device_id_sanitizado}/sms/inbox/ack`

**Dispositivo externo:** deve publicar em `status` e `inbox`, e subscrever `send` e `inbox/ack` (para receber pedidos de envio e confirmações de leitura da inbox).

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

---

## 4. Comando de gestão Django — cliente MQTT do gateway

Corre o processo que liga ao broker e trata `status` / `inbox`:

```bash
python manage.py run_mqtt_gateway
```

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
