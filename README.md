# savia-cloud

Backend puente entre **The Things Network (TTN)** y la estación **Savia**. Es la
pieza de fase-2: recibe los uplinks del nodo, capta la calidad de señal de subida
(RSSI/SNR de los metadatos del gateway) y programa los downlinks con **hora +
pronóstico de temperatura del aire (Open-Meteo)** que la estación necesita para el
LSTM.

> Solo esqueleto por ahora: el codec y el cableado están listos y testeados; las
> llamadas HTTP salientes (TTN downlink API, Open-Meteo) están marcadas con `TODO`.

## Arquitectura (hexagonal / puertos y adaptadores)

```
app/
  domain/          Núcleo: entidades (models.py) + puertos/interfaces (ports.py). Sin framework.
  application/     Casos de uso (services.py): orquestan dominio + puertos.
  adapters/        Adaptadores dirigidos (implementan puertos):
    ttn/           codec.py (payload, = firmware) + client.py (downlink AS API)
    openmeteo/     client.py (pronóstico TA)
    repository/    memory.py (estado por estación)
  interfaces/http/ Adaptador conductor: rutas Flask (routes.py)
  factory.py       create_app(): composition root, cablea adaptadores en servicios
  __init__.py      vacío a propósito (mantiene el paquete import-light: tests sin Flask)
config.py          Settings desde entorno
run.py             Entrypoint de desarrollo
tests/             test_codec.py
```

Regla: `domain` y `application` **no importan** Flask ni `requests`; dependen solo
de puertos. El único sitio que elige implementaciones concretas es `create_app()`.

## Puesta en marcha

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # rellena TTN_API_KEY
python run.py                 # http://localhost:8000/health
pytest                        # tests del codec
```

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| GET  | `/health` | Sonda de vida. |
| POST | `/ttn/uplink` | Webhook de TTN: decodifica el uplink + guarda RSSI/SNR de subida. |
| GET  | `/stations/<dev_id>/signal` | Última señal conocida (la de subida que la estación no puede medir). |
| POST | `/stations/<dev_id>/downlink` | Construye y programa el downlink hora + TA. |

## Integración con TTN

- **Uplinks:** configura un *Webhook* en TTN (Application → Integrations → Webhooks)
  que apunte a `POST /ttn/uplink`.
- **Downlinks:** `client.py` usa la Application Server API
  (`/api/v3/as/applications/{app}/devices/{dev}/down/push`) con `TTN_API_KEY`.
- Payload: ver `app/adapters/ttn/codec.py` (byte-compatible con
  `savia_c/include/savia/lora_codec.h`).
