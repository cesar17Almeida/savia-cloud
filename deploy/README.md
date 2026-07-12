# Despliegue en EC2 (Ubuntu 24.04)

Guía paso a paso para dejar `savia-cloud` corriendo en una instancia EC2 con
HTTPS (obligatorio: TTN no acepta webhooks sin TLS) y el cron horario de
inferencia.

## 0. Instancia y red

- **Tipo:** `t3.micro` (x86_64) sobra; `t4g.micro` (ARM) también vale, hay wheels
  aarch64 de `ai-edge-litert`.
- **AMI:** Ubuntu Server 24.04 LTS.
- **Security group:** entrantes 22 (SSH, restringido a tu IP), 80 y 443 (Caddy).
  El puerto 8000 NO se abre: gunicorn escucha solo en localhost.
- **DNS:** apunta un nombre a la IP pública (dominio propio o DuckDNS). Sin
  nombre DNS no hay certificado TLS y TTN rechaza el webhook.

## 1. Sistema base

```sh
sudo apt update && sudo apt install -y python3-venv git caddy curl
sudo useradd --system --home /opt/savia-cloud --shell /usr/sbin/nologin savia
```

## 2. Código y entorno Python

```sh
sudo git clone https://github.com/cesar17Almeida/savia-cloud.git /opt/savia-cloud
cd /opt/savia-cloud
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt gunicorn ai-edge-litert
```

## 3. Modelo LSTM

El mismo `.tflite` int8 que embebe el firmware:

```sh
sudo mkdir -p /opt/savia-cloud/models /var/lib/savia-cloud
# desde tu Mac:
scp docs/sensor_documentation/model/modelo_lstm/lstm_hs30_int8_pt.tflite \
    ubuntu@<host>:/tmp/ && ssh ubuntu@<host> \
    'sudo mv /tmp/lstm_hs30_int8_pt.tflite /opt/savia-cloud/models/'
```

## 4. Configuración

```sh
sudo mkdir -p /etc/savia-cloud
sudo cp deploy/env.example /etc/savia-cloud/env
sudo chmod 600 /etc/savia-cloud/env && sudo chown savia:savia /etc/savia-cloud/env
sudo nano /etc/savia-cloud/env   # rellenar TTN_API_KEY, WEBHOOK_SECRET, CRON_SECRET
sudo chown -R savia:savia /opt/savia-cloud /var/lib/savia-cloud
```

Genera los secretos con `openssl rand -hex 24`.

## 5. Servicio + proxy TLS + cron

```sh
sudo cp deploy/savia-cloud.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now savia-cloud

sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # editar el hostname antes
sudo systemctl reload caddy

sudo install -m 755 deploy/cron-daily.sh /opt/savia-cloud/deploy/cron-daily.sh
echo '7 * * * * savia /opt/savia-cloud/deploy/cron-daily.sh >> /var/log/savia-cloud-cron.log 2>&1' \
  | sudo tee /etc/cron.d/savia-cloud
```

El cron dispara **cada hora**; el backend decide por estación (según su
`utc_offset_min`) si le toca la inferencia diaria a la `CRON_DAILY_HOUR` local.

## 6. Webhook en la consola TTN

Consola eu1 → aplicación `savia` → *Integrations → Webhooks → Add webhook →
Custom*:

- **Base URL:** `https://<tu-hostname>`
- **Uplink message path:** `/ttn/uplink`
- **Additional headers:** `X-Webhook-Token: <WEBHOOK_SECRET>`

## 7. Comprobación

```sh
curl https://<tu-hostname>/health                        # {"status": "ok"}
sudo journalctl -u savia-cloud -f                        # logs de la app
# tras el primer uplink real del nodo:
curl https://<tu-hostname>/stations/<DevEUI>/signal      # RSSI/SNR
```
