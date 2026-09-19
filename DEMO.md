# Enlace por HTTP (grabación sin cobertura LoRa)

Modo de arranque del backend para grabar el sistema completo cuando la estación no
tiene cobertura LoRa. El salto de radio se sustituye por un túnel a través del móvil:

    estación (savia_c)  <--BLE-->  TerraLink (puente)  <--HTTP-->  savia-cloud

Por el túnel viajan **exactamente los mismos bytes** que irían por LoRaWAN (tramas
wire v2, `app/adapters/ttn/codec.py`); solo cambia el transporte. Se conserva la
semántica de clase A: un downlink solo sale como respuesta a un uplink, como mucho
uno por uplink y el más antiguo primero.

Dos interruptores de entorno lo activan; con ambos apagados el backend se comporta
como en producción:

| Variable | Valor | Efecto |
|---|---|---|
| `LINK_MODE` | `http` (por defecto `ttn`) | Habilita `POST /link/uplink`; los downlinks esperan en la tabla `link_outbox` en vez de ir a TTN. |
| `FORECAST_SOURCE` | `dataset` (por defecto `openmeteo`) | La temperatura del aire sale del conjunto de entrenamiento del LSTM (`app/adapters/dataset/replay_2018.json`), no de Open-Meteo. |
| `REPLAY_UTC_OFFSET_MIN` | `120` | Offset UTC fijo que ancla las horas del conjunto de datos al reloj. Debe coincidir con `SAVIA_DEMO_UTC_OFFSET_MIN` del firmware. |
| `LINK_SECRET` | vacío | Si se define, `POST /link/uplink` exige la cabecera `X-Link-Token`. |

## Arranque

```sh
make demo          # crea .env.demo desde .env.demo.example si no existe y arranca
make demo-reset    # borra demo.db para empezar de cero
make test          # batería completa
```

`make demo` imprime la URL que hay que escribir en TerraLink (`http://<IP-del-Mac>:8000`);
el móvil y el Mac deben estar en la misma red. Para otro puerto: `make demo PORT=8011`.

Panel: <http://localhost:8000/home/> · usuario `admin` · contraseña `savia-panel-2026`
(definida en `.env.demo`, así no aparece el cambio de contraseña obligatorio).

## Por qué la temperatura sale del conjunto de datos

La estación reproduce 48 h de humedad de suelo del nodo 4 del conjunto de
entrenamiento (2018). Para que la inferencia a bordo coincida con lo que se midió de
verdad, la temperatura del aire tiene que ser la de esas mismas horas. Firmware y
backend eligen la fila con la misma regla, cada uno por su cuenta:

    h       = hora del día con el offset fijo REPLAY_UTC_OFFSET_MIN (+120, hora de verano peninsular)
    fila    = 58 + ((h - 10) mod 24)          # 58..81
    pasado  = filas [fila-47 .. fila]          # 48 valores
    futuro  = filas [fila+1 .. fila+24]        # 24 valores

El offset es fijo a propósito: el de la estación puede cambiar a mitad de sesión
(TerraLink lo envía al abrir la pantalla de configuración) y movería el ancla. El que
manda la app en `utc_offset_min` se guarda igualmente en la estación, para que el panel
muestre su hora local.

**La regla da la vuelta a las 10:00 (hora peninsular de verano): no grabes una toma que
cruce las 10:00**, porque la ventana saltaría de la fila 81 a la 58.

## Flujo de la grabación (lado backend)

1. Abre el panel y entra en la estación (aparece sola con su primer uplink). En la
   pestaña **Resumen**, la tarjeta **Comunicación en vivo** se actualiza cada 1,5 s.
2. Espera los uplinks. El primero es `boot` y recibe en la misma respuesta un
   downlink `time_ta` de 8 B con la hora. Después llega un `forecast` cada ~15 s
   («petición de ventana RX» mientras la estación no tiene pronóstico).
3. Pulsa **Sincronizar hora + TA**. Aparece el aviso **«Enviando paquete por LoRa…»**
   (`time_ta · 80 B`) y la trama queda *en cola* en la línea de tiempo, con las 72
   temperaturas dibujadas (48 h anteriores + 24 h de previsión).
4. En el siguiente uplink (≤ 15 s) la estación recoge el paquete: el aviso pasa a
   **«Paquete entregado a la estación»** y la trama a *entregado a la estación*.
5. La estación infiere a bordo y su siguiente `forecast` trae el resultado: el aviso
   muestra **«La estación ha ejecutado el modelo · HS30 mínimo previsto 0,742»** y el
   valor queda fijo en la tarjeta **Última inferencia**.

Una estación que se da de alta sola figura en el backend como `forward`. Por este
enlace el panel sigue el modo que el instalador eligió en TerraLink: pasa a `local` con
el primer `forecast` que trae un valor (solo una estación LOCAL infiere) y a `forward`
con el primer `soil`. También se puede cambiar desde el panel con «Modo inferencia» en
**Configuración → Configurar por LoRa**, y así se ve el ciclo completo de una
configuración (*en cola* → *entregado a la estación* → *aplicada* al llegar el `CFG_ACK`).

## Ensayo sin placa ni móvil

`tools/rehearse_station.py` hace de estación **y** de pasarela: envía a `/link/uplink`
las mismas tramas que el firmware (`BOOT`, `FORECAST` periódicos, `SOIL` con `--forward`),
imprime cada downlink y reacciona como la placa (con el `TIME_TA` completo «infiere» y
anuncia el HS30 mínimo; a un `CONFIG` contesta con `CFG_ACK`). Sirve para ensayar el
panel y comprobar el backend antes de grabar:

```sh
make demo                                  # en una terminal
.venv/bin/python tools/rehearse_station.py # en otra (solo biblioteca estándar)
```

## Modo FORWARD (inferencia en la nube)

Con la estación en `forward` los uplinks son `soil` (hasta 4 registros horarios por
trama, uno cada ~3 s mientras haya atraso). Se ven llegar en **Comunicación en vivo** y
van llenando la pestaña **Lecturas**. Con las 48 h recibidas, **Inferir ahora** ejecuta
el LSTM en el backend, dibuja las 24 h en **Última inferencia** y encola el `time_ta`
de vuelta, que sigue el mismo camino *en cola* → *entregado*.

## Contrato del enlace

```
POST /link/uplink        Content-Type: application/json   [X-Link-Token: <LINK_SECRET>]
{"device_id": "savia-estacion-01", "f_port": 8, "frm_payload": "<base64>",
 "seq": 12, "utc_offset_min": 120}

200 {"ok": true, "type": "forecast", "downlink": null}
200 {"ok": true, "type": "boot",
     "downlink": {"f_port": 8, "frm_payload": "<base64>", "kind": "time_ta"}}
```

`404` si `LINK_MODE` no es `http` · `401` token incorrecto · `400` cuerpo o base64 mal
formados. Una trama que no se puede decodificar se registra igualmente como `unknown`.
Si la app repite el mismo `seq` con la misma trama en menos de 10 s (respuesta perdida),
recibe la misma respuesta y el uplink no se registra dos veces. `GET /health` sirve de
prueba de conexión.

Estado en vivo del panel (con sesión): `GET /home/stations/<id>/live.json`.
