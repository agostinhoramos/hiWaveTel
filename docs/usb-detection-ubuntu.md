# Detecção do modem USB no Ubuntu e actualização no hiWaveTel

Este guia descreve, passo a passo, como identificar o modem no **Ubuntu** no **anfitrião** e como reflectir correctamente caminhos e interfaces na configuração do projecto (**`.env`** à raiz e [`docker/docker-compose.yml`](../docker/docker-compose.yml)).

Para **SMS via AT** são necessários nós série (`/dev/ttyUSB*` ou `/dev/ttyACM*`). Para modems **QMI** (ex.: Quectel EC25 em modo QMI) também precisa do **`/dev/cdc-wdm*`** dentro do mesmo *namespace* de rede da aplicação; o Compose do projecto já usa **`network_mode: host`** por defeito.

---

## Antes de começar

1. Ligue o modem por USB / M.2‑USB / HAT e espere alguns segundos.
2. No anfitrião, quando usa o container com **ModemManager próprio**, pare serviços que possam reservar o modem (evitar dois gestores ao mesmo tempo):
   ```bash
   sudo systemctl stop ModemManager NetworkManager
   ```
   Reinicie quando terminar os testes, se precisar de rede pelo host: `sudo systemctl start ModemManager NetworkManager`.

---

## Passo 1 — Confirmar que o barramento USB vê o dispositivo

Instale ferramentas (se ainda não tiver):

```bash
sudo apt update
sudo apt install -y usbutils
```

Liste dispositivos USB:

```bash
lsusb
```

Procure pela linha do fabricante/modelo do módulo (ex.: Qualcomm / Quectel). Anote o **bus** e **device** só para referência; o mais importante são os **nós em `/dev`**.

Para mais detalhe sobre um device concreto (substitua `001` `002` pelos valores de `lsusb`):

```bash
lsusb -t
```

Opcionalmente, informação do *sysfs* para regras udev persistentes mais tarde:

```bash
sudo udevadm info --name=/dev/ttyUSB0 --attribute-walk | less
```
(Altere `/dev/ttyUSB0` para o nó que estiver em uso.)

---

## Passo 2 — Ver o que o kernel criou (`dmesg`)

Após ligar o cabo/USB, veja últimas mensagens relacionadas com `usb`, `tty`, `modem`, `qmi`, `cdc`:

```bash
sudo dmesg -T --color=never | tail -n 80
```

Ou siga ao vivo:

```bash
journalctl -k -f
```

Confirme que aparecem entradas de **driver** ligadas ao chipset (cdc_acm, option, qmi_wwan, etc.) sem erros graves de permissão/porta.

---

## Passo 3 — Listar portas série e QMI

Portas série (AT SMS costuma estar num `ttyUSB` ou `ttyACM`):

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

Interfaces QMI/MBIM (ModemManager em QMI costuma usar um destes):

```bash
ls -l /dev/cdc-wdm* 2>/dev/null
```

Interface de dados móveis (LTE) no anfitrião (quando existe):

```bash
ip link show
```

Procure típicamente **`wwan0`** (nome pode mudar).

Se existir apenas **uma** porta AT clara ou várias linhas **`ttyUSB`**, confirme qual é a porta de **modem/AT SMS** usando (com ModemManager ligado só num dos lados):

```bash
mmcli -L
mmcli -m 0 | head
```

(No projecto Docker, **`MODEM_MMCLI_INDEX`** pode ser **0** por defeito ou auto‑detectado; ver secção seguinte.)

Verificar que nenhum processo externo está a segurar permanentemente uma porta pode ajudar em falhas estranhas:

```bash
sudo fuser -v /dev/ttyUSB2 /dev/ttyUSB3 /dev/cdc-wdm0 2>/dev/null
```

---

## Passo 4 — Mapear para o hiWaveTel (ficheiros a editar)

O Compose lê **`../.env`** na raiz do repositório. As variáveis relevantes aparecem em [`docker/.env.example`](../docker/.env.example) e são passadas pelo [`docker/docker-compose.yml`](../docker/docker-compose.yml).

### Valores típicos a preencher

| Variável | O que significa | Exemplo |
|----------|-----------------|---------|
| `MODEM_TTY_PRIMARY` | Porta série principal AT (SMS) | `/dev/ttyUSB2` |
| `MODEM_TTY_SECONDARY` | Segunda porta AT (opcional/reserva) | `/dev/ttyUSB3` |
| `MODEM_QMI_DEVICE` | Dispositivo QMI | `/dev/cdc-wdm0` |
| `MODEM_LTE_INTERFACE` | Interface de rede LTE visível onde corre a app (`wwan0` no host quando `network_mode: host`) | `wwan0` |
| `MODEM_MMCLI_INDEX` | Índice do modem em `mmcli -L` (Modem **`N`**) | `0` |
| `AUTO_DETECT_MMCLI_INDEX` | Permite ao *entrypoint* alinhar o índice ao primeiro modem listado | `true` |

**No `.env`** (à raiz, não faça commit de segredos), ajuste por exemplo:

```env
MODEM_TTY_PRIMARY=/dev/ttyUSB2
MODEM_TTY_SECONDARY=/dev/ttyUSB3
MODEM_QMI_DEVICE=/dev/cdc-wdm0
MODEM_LTE_INTERFACE=wwan0
AUTO_DETECT_MMCLI_INDEX=true
MODEM_MMCLI_INDEX=0
```

As directivas **`devices:`** no Compose fazem bind dos caminhos do **anfitrião** para o **mesmo caminho dentro do container**; não é obrigatório mudar mais nada em `docker-compose.yml` desde que estas variáveis correspondam aos nós reais.

---

## Passo 5 — Voltar a subir o compose e validar

Na raiz do repositório:

```bash
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up --build
```

No log do *entrypoint* deve aparecer o *checklist* das TTY; erros **`missing in container`** indicam que caminhos no `.env` não existem ou `devices:` não faz *bind* aos nós certos no anfitrião.

Dentro do contentor (opcional; as variáveis vêm do ambiente Compose):

```bash
docker compose -f docker/docker-compose.yml exec hiwavetel bash -lc 'ls -l "${MODEM_TTY_PRIMARY:?}"'
docker compose -f docker/docker-compose.yml exec hiwavetel mmcli -L
```

No anfitrião, `GET /api/health/` (sem JWT) permite ver se o ModemManager dentro do stack responde; ver também [`GET_STARTED.md`](../GET_STARTED.md).

---

## Problemas frequentes — resumo

| Sintoma | O que verificar |
|---------|----------------|
| **`ttyUSB` mudou de número** após reboot | Rever `ls -l /dev/ttyUSB*` e actualizar `.env`; considere regra **udev** com `SYMLINK` estável (`/dev/quectel_at`, etc.). |
| **Dois ModemManager** (host + Docker) em conflito | Parar o do host antes de arrancar o contentor ou usar uma arquitectura com um único D‑Bus/MM. |
| **QMI “sem net port”** | Ligação `wwan`/QMI deve estar visível ao MM no mesmo espaço de rede; o Compose já usa **`network_mode: host`**. Confirmar **`MODEM_QMI_DEVICE`** e `cdc-wdm` mapeados. |
| **mmcli sem modem / porta ocupada** | Ver `fuser`/`lsof` e serviços de rede no host. |

---

## Referências no repositório

- [`docker/docker-compose.yml`](../docker/docker-compose.yml) — `devices:`, redes, QMI.
- [`docker/.env.example`](../docker/.env.example) — modelo de variáveis.
- [`docker/entrypoint.sh`](../docker/entrypoint.sh) — pré‑voo ModemManager antes do Gunicorn.
