# savia-cloud

Backend puente entre **The Things Network (TTN)** y la estación **Savia**. Es la
pieza de fase-2: recibe los uplinks del nodo, capta la calidad de señal de subida
(RSSI/SNR de los metadatos del gateway), guarda las lecturas, corre el LSTM en la
nube para las estaciones en modo *forward* y programa los downlinks con **hora +
pronóstico de temperatura del aire (Open-Meteo)** que la estación necesita.

## Arquitectura (hexagonal / puertos y adaptadores)

```
app/
  domain/          Núcleo: entidades (models.py) + puertos/interfaces (ports.py). Sin framework.
  application/     Casos de uso (services.py) + errores mapeables (errors.py).
  adapters/        Adaptadores dirigidos (implementan puertos):
    ttn/           codec.py (payload wire v2, = firmware) + client.py (downlink AS API)
    openmeteo/     client.py (pronóstico TA horario)
    inference/     lstm.py (mismo modelo int8 que embebe el firmware)
    repository/    orm.py + db.py + sqlite.py (SQLAlchemy) ; memory.py (tests sin BD)
  interfaces/http/ Adaptador conductor: rutas Flask (routes.py)
  factory.py       create_app(): composition root, cablea adaptadores en servicios
config.py          Settings desde entorno
run.py             Entrypoint de desarrollo
tests/             codec (golden), auth, webhook, ventana LSTM, inferencia, cron
```

Regla: `domain` y `application` **no importan** Flask ni `requests`; dependen solo
de puertos. El hashing de contraseñas (werkzeug) y el reloj se inyectan como
callables. El único sitio que elige implementaciones concretas es `create_app()`.

## Puesta en marcha

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # rellena TTN_API_KEY, WEBHOOK_SECRET, CRON_SECRET
python run.py                 # http://localhost:8000/health
pytest -q                     # toda la batería
```

La inferencia en la nube requiere un intérprete TFLite (`ai-edge-litert`,
`tflite-runtime` o `tensorflow`); si no está instalado, el backend funciona igual y
el test de inferencia se salta limpio (`skipif`).

## Endpoints

| Método | Ruta | Auth | Qué hace |
|---|---|---|---|
| GET  | `/health` | — | Sonda de vida. |
| POST | `/auth/register` | — | Crea usuario `{email, password}`. 409 si ya existe. |
| POST | `/auth/login` | — | Devuelve `{token}` (bearer, 30 días). |
| POST | `/stations/claim` | Bearer | Vincula una estación al usuario. 409 si ya es de otro. |
| GET  | `/stations/<dev_eui>` | Bearer (owner) | Datos de la estación. |
| PUT  | `/stations/<dev_eui>` | Bearer (owner) | Edita `lat/lon/utc_offset_min/name/mode`. |
| GET  | `/stations/<dev_eui>/signal` | — | Última señal de subida (RSSI/SNR). |
| POST | `/stations/<dev_eui>/downlink` | Bearer (owner) | Programa el downlink hora + TA. |
| POST | `/stations/<dev_eui>/config` | Bearer (owner) | Programa un parche de config (TLV). |
| POST | `/ttn/uplink` | `X-Webhook-Token` | Webhook de TTN: decodifica + persiste. |
| POST | `/cron/daily` | `X-Cron-Token` | Corre la inferencia diaria (ver abajo). |

## Modelo de autenticación (auth sobre LoRa)

La estación no tiene internet y su único enlace propio es LoRaWAN, que **ya cifra y
autentica extremo a extremo con la `AppKey`** (OTAA). Sobre ese hecho:

- **La contraseña de usuario NUNCA viaja por LoRa.** Se valida por BLE en la app
  (challenge-response HMAC contra la clave de la estación) y sirve para operar el
  nodo localmente.
- **La propiedad se establece una sola vez** vía `POST /stations/claim`, tras que la
  app haya validado el password BLE. A partir de ahí la estación queda ligada a una
  cuenta.
- **Los downlinks de config/hora se autorizan por *ownership* de la sesión**: solo el
  dueño puede programarlos (`Authorization: Bearer <token>`); nada sensible se manda
  en claro por el aire porque LoRaWAN ya lo protege.
- **El webhook confía en TTN mediante un secreto compartido** (`X-Webhook-Token`), no
  por identidad de la estación: el nodo ya está autenticado frente a TTN por la red.

## Inferencia en la nube (modo FORWARD)

Las estaciones en modo `forward` no corren el LSTM a bordo: suben sus lecturas de
suelo y el backend hace la inferencia con **el mismo modelo `.tflite` int8 que embebe
el firmware**, reusando las mismas constantes del `StandardScaler` (copiadas de
`savia_c/src/system/scaler.c`). El servicio arma la ventana de 48 h (huecos de TA
rellenos con el histórico de Open-Meteo; huecos de suelo por *last-observation-
carried-forward*; falla si hay >6 h contiguas sin suelo), guarda el pronóstico de
24 h y programa de vuelta el downlink `TIME_TA` con la hora y la ventana de TA.

### Cron externo

`POST /cron/daily` no se auto-agenda: se dispara desde un **scheduler externo**
(cron del sistema, GitHub Actions, Cloud Scheduler…) **una vez por hora**. En cada
llamada, cada estación *forward* corre su inferencia cuando su **hora local**
(derivada de `utc_offset_min`) coincide con `CRON_DAILY_HOUR`. Ejemplo de crontab:

```cron
0 * * * *  curl -s -X POST https://<host>/cron/daily -H "X-Cron-Token: $CRON_SECRET"
```

## Integración con TTN

- **Uplinks:** configura un *Webhook* en TTN (Application → Integrations → Webhooks)
  que apunte a `POST /ttn/uplink` con la cabecera `X-Webhook-Token`.
- **Downlinks:** `client.py` usa la Application Server API
  (`/api/v3/as/applications/{app}/devices/{dev}/down/push`) con `TTN_API_KEY`.
- Payload: ver `app/adapters/ttn/codec.py` (wire v2, byte-compatible con
  `savia_c/include/savia/lora_codec.h`; fijado por los golden vectors compartidos en
  `tests/test_codec.py` ↔ `savia_c/test/test_lora_codec.c`).

## Persistencia

SQLAlchemy 2 + SQLite (`DATABASE_URL`, por defecto `sqlite:///savia.db`; los tests
usan `:memory:`). Las tablas se crean al arrancar (`create_all`) — suficiente para el
alcance del TFM; una migración formal (Alembic) queda fuera de alcance. Tablas:
`users`, `sessions`, `stations`, `soil_readings` (PK compuesta, *upsert*),
`forecasts`, `downlink_log`.
