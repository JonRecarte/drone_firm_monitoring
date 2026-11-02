"""Genera dashboards/crazyflie_bridge.json.

    python3 build_dashboard.py            # escribe junto a este script
    python3 build_dashboard.py otra.json  # o en la ruta indicada

El JSON aprovisionado es el resultado de este script: para añadir un panel
(p. ej. al ampliar la telemetría del bridge) se edita aquí y se regenera.
Cambios hechos a mano en la interfaz de Grafana se pierden al regenerar.
"""
import json
import sys
from pathlib import Path

DS = {"type": "influxdb", "uid": "influx-drones"}
# Paleta categórica validada (tema oscuro), en orden fijo: azul, naranja, aqua, amarillo.
C = ["#3987e5", "#d95926", "#199e70", "#c98500"]
MUTED = "#898781"

# dfm etiqueta cada punto con `drone`. Los vuelos anteriores no la llevan y aparecen como SIN.
SIN = "(sin etiqueta)"
DRONE = '(if exists r.drone then r.drone else "' + SIN + '")'
DRONE_FILTER = "  |> filter(fn: (r) => contains(value: " + DRONE + ", set: ${drone:json}))\n"
# `r with`: con un registro de solo `_value` se pierden los puntos sin etiqueta.
WITH_DRONE = "  |> map(fn: (r) => ({r with drone: " + DRONE + "}))\n"


def series_q(field, measurement="bridge_cycle"):
    return (
        "from(bucket: v.defaultBucket)\n"
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}")\n'
        + DRONE_FILTER +
        "  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n"
        + WITH_DRONE +
        '  |> group(columns: ["_field", "drone"])\n'
        '  |> sort(columns: ["_time"])\n'
        '  |> keep(columns: ["_time", "_value", "_field", "drone"])'
    )


def stat_q(field, reducer):
    tail = {
        "last": '  |> group(columns: ["drone"])\n  |> sort(columns: ["_time"])\n  |> last()',
        "mean": '  |> group(columns: ["drone"])\n  |> mean()',
        "count": '  |> group(columns: ["drone"])\n  |> count()',
    }[reducer]
    return (
        "from(bucket: v.defaultBucket)\n"
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "bridge_cycle" and r._field == "{field}")\n'
        + DRONE_FILTER + WITH_DRONE + tail + '\n  |> keep(columns: ["_value", "drone"])'
    )


EVENTS_Q = (
    "from(bucket: v.defaultBucket)\n"
    "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
    '  |> filter(fn: (r) => r._measurement == "bridge_event" and r._field == "detail")\n'
    + DRONE_FILTER + WITH_DRONE +
    "  |> group()\n"
    '  |> sort(columns: ["_time"])\n'
    '  |> keep(columns: ["_time", "drone", "session", "kind", "_value"])\n'
    '  |> rename(columns: {_value: "detalle", kind: "evento", session: "sesión", drone: "dron"})'
)


DRONE_VAR_Q = (
    "from(bucket: v.defaultBucket)\n"
    "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
    '  |> filter(fn: (r) => r._measurement == "bridge_cycle" and r._field == "cycle")\n'
    "  |> map(fn: (r) => ({r with _value: " + DRONE + "}))\n"
    "  |> group()\n"
    '  |> distinct(column: "_value")'
)

_ids = iter(range(1, 1000))
panels = []
y = 0


def row(title):
    global y
    panels.append({"type": "row", "id": next(_ids), "title": title, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []})
    y += 1


def timeseries(title, desc, series, unit, x, w, h=8, decimals=None, threshold_line=None,
               soft_min=None, soft_max=None):
    """series: lista de (campo, nombre visible, color, estilo)."""
    refs = "ABCDEFGH"
    targets, overrides = [], []
    for i, (field, name, color, style) in enumerate(series):
        targets.append({"refId": refs[i], "datasource": DS, "query": series_q(field)})
        if len(series) == 1:
            # Un solo dato: un color por dron y su nombre en la leyenda.
            props = [{"id": "displayName", "value": "${__field.labels.drone}"}]
        else:
            # Varios datos: los colores de siempre por dato; la leyenda dice de qué dron es.
            props = [
                {"id": "displayName", "value": name + " · ${__field.labels.drone}"},
                {"id": "color", "value": {"mode": "fixed", "fixedColor": color}},
            ]
        if style == "dash":
            props.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}})
            props.append({"id": "custom.lineWidth", "value": 1})
        overrides.append({"matcher": {"id": "byFrameRefID", "options": refs[i]}, "properties": props})
    custom = {
        "drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
        "lineInterpolation": "linear", "spanNulls": False, "insertNulls": 10000,
        "axisPlacement": "auto", "axisBorderShow": False, "gradientMode": "none",
        "thresholdsStyle": {"mode": "line" if threshold_line is not None else "off"},
    }
    if soft_min is not None:
        custom["axisSoftMin"] = soft_min
    if soft_max is not None:
        custom["axisSoftMax"] = soft_max
    steps = [{"color": "transparent", "value": None}]
    if threshold_line is not None:
        steps.append({"color": MUTED, "value": threshold_line})
    color = {"mode": "palette-classic-by-name"} if len(series) == 1 else {"mode": "fixed", "fixedColor": C[0]}
    defaults = {"unit": unit, "custom": custom, "color": color,
                "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    panels.append({
        "type": "timeseries", "id": next(_ids), "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides},
        "options": {
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom", "calcs": []},
            "tooltip": {"mode": "multi", "sort": "none"},
        },
    })


def stat(title, desc, field, reducer, unit, x, w, decimals, suffix=None):
    defaults = {"unit": unit, "decimals": decimals, "color": {"mode": "fixed", "fixedColor": C[0]},
                "displayName": "${__field.labels.drone}",
                "thresholds": {"mode": "absolute", "steps": [{"color": C[0], "value": None}]}}
    if suffix:
        defaults["unit"] = f"suffix:{suffix}"
    panels.append({
        "type": "stat", "id": next(_ids), "title": title, "description": desc, "datasource": DS,
        "gridPos": {"h": 5, "w": w, "x": x, "y": y},
        "targets": [{"refId": "A", "datasource": DS, "query": stat_q(field, reducer)}],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "none", "graphMode": "none", "textMode": "value_and_name",
                    "justifyMode": "auto", "orientation": "horizontal", "wideLayout": True,
                    "text": {"titleSize": 13, "valueSize": 20},
                    "showPercentChange": False},
    })


# ---------------------------------------------------------------- Resumen
row("Resumen del intervalo")
stat("Altitud (último valor)", "Última altitud de la planta (GPS de Webots) en el intervalo y sesiones seleccionados.",
     "alt", "last", "suffix: m", 0, 5, 3)
stat("Error de altura (último)", "alt − consigna. Positivo = por encima de la consigna de session_start.",
     "alt_error", "last", "suffix: m", 5, 5, 3)
stat("Frecuencia media del bucle", "Media de 1/Δt_pared entre ciclos consecutivos del bridge. El bridge pide --loop-hz (12 Hz en run_hover_demo.sh).",
     "loop_hz", "mean", "suffix: Hz", 10, 5, 2)
stat("Velocidad de simulación media", "Media de Δsim/Δpared: segundos simulados de Webots por segundo real. 1 = tiempo real. No incluye el tiempo virtual de Renode, que el bridge aún no registra.",
     "sim_speed", "mean", "none", 15, 5, 2, suffix="×")
stat("Ciclos en el intervalo", "Número de ciclos de control registrados en el JSONL.",
     "cycle", "count", "none", 20, 4, 0)
y += 5

# ---------------------------------------------------------------- Vuelo
row("Vuelo")
timeseries("Altitud y consigna",
           "Altitud de la planta (GPS de Webots) y consigna alt_target de session_start.",
           [("alt", "Altitud", C[0], "solid"), ("alt_target", "Consigna", MUTED, "dash")],
           "suffix: m", 0, 12, decimals=3)
timeseries("Error de altura",
           "alt − consigna. Un error estacionario indica sesgo de HOVER_THRUST o saturación del integrador del PD+I del bridge.",
           [("alt_error", "Error de altura", C[0], "solid")], "suffix: m", 12, 12, decimals=3, threshold_line=0)
y += 8
timeseries("Velocidad vertical",
           "vz del bridge: Δalt/Δt de PARED (lo que usa su PD+I). vz física: Δalt/Δsim, calculada por el ingestor. Divergen cuando la simulación no va en tiempo real.",
           [("vz", "vz del bridge (reloj de pared)", C[0], "solid"), ("vz_sim", "vz física (tiempo simulado)", C[1], "solid")],
           "velocityms", 0, 8, decimals=3)
timeseries("Empuje comandado",
           "Thrust de la consigna RPYT que el bridge escribe en el firmware (escala Crazyflie 0–65535).",
           [("thrust", "Empuje", C[0], "solid")], "none", 8, 8, decimals=0)
timeseries("Salida del controlador de altura (u)",
           "Salida adimensional del PD+I de altura del bridge: thrust = HOVER_THRUST + u·14000.",
           [("u", "u", C[0], "solid")], "none", 16, 8, decimals=3)
y += 8

# ---------------------------------------------------------------- Actuadores
row("Actuadores · firmware → planta")
timeseries("Ratios de motor",
           "motor_ratios del mixer del firmware leídos de Renode (motor_stats), 0–1.",
           [("m1", "M1", C[0], "solid"), ("m2", "M2", C[1], "solid"),
            ("m3", "M3", C[2], "solid"), ("m4", "M4", C[3], "solid")], "percentunit", 0, 8, decimals=1,
           soft_min=0)
timeseries("Velocidad de hélices ω",
           "ω aplicada en Webots: ratio × 100 rad/s con los signos de giro de Bitcraze (−, +, −, +).",
           [("omega1", "ω1", C[0], "solid"), ("omega2", "ω2", C[1], "solid"),
            ("omega3", "ω3", C[2], "solid"), ("omega4", "ω4", C[3], "solid")], "rotrads", 8, 8, decimals=1)
timeseries("Dispersión entre motores",
           "max − min de los cuatro ratios. Cero = empuje simétrico (sin corrección de actitud).",
           [("motor_spread", "max − min", C[0], "solid")], "percentunit", 16, 8, decimals=2,
           soft_min=0, soft_max=0.05)
y += 8

# ---------------------------------------------------------------- Sensores
row("Sensores · planta → firmware")
timeseries("Giróscopo",
           "Velocidad angular enviada al BMI088 emulado, en grados/s, tras el filtro paso bajo del bridge.",
           [("gyro_x", "x", C[0], "solid"), ("gyro_y", "y", C[1], "solid"), ("gyro_z", "z", C[2], "solid")],
           "none", 0, 8, decimals=3)
timeseries("Acelerómetro",
           "Aceleración propia enviada al BMI088, en mg (1000 = 1 g). El PROTO no tiene acelerómetro: el bridge la deriva del GPS y envía exactamente (0, 0, 1000) cuando el dron está casi quieto.",
           [("accel_x", "x", C[0], "solid"), ("accel_y", "y", C[1], "solid"), ("accel_z", "z", C[2], "solid")],
           "none", 8, 8, decimals=1)
timeseries("Presión barométrica",
           "Presión enviada al BMP388 emulado, derivada de la altitud de la planta.",
           [("pressure", "Presión", C[0], "solid")], "suffix: Pa", 16, 8, decimals=1)
y += 8

# ---------------------------------------------------------------- Fidelidad
row("Fidelidad de la co-simulación")
timeseries("Frecuencia del bucle del bridge",
           "1/Δt de pared entre ciclos consecutivos. Calculada por el ingestor. Cada consulta extra al monitor de Renode alarga el ciclo (p. ej. la línea de estado del bridge cada --status-every s).",
           [("loop_hz", "Frecuencia", C[0], "solid")], "suffix: Hz", 0, 8, decimals=2)
timeseries("Velocidad de simulación",
           "Δsim/Δpared entre ciclos. La línea gris marca 1 (tiempo real). Calculada por el ingestor; no incluye el tiempo virtual de Renode.",
           [("sim_speed", "Δsim/Δpared", C[0], "solid")], "none", 8, 8, decimals=3, threshold_line=1)
timeseries("Avance de simulación por ciclo",
           "Segundos simulados de Webots entre ciclos. El catch-up del bridge limita el avance a ~0,25 s por ciclo.",
           [("dt_sim", "Δsim por ciclo", C[0], "solid")], "s", 16, 8, decimals=3)
y += 8

# ---------------------------------------------------------------- Eventos
row("Eventos de sesión")
panels.append({
    "type": "table", "id": next(_ids), "title": "session_start / session_end",
    "description": "Eventos del JSONL con sus parámetros tal como los escribe el bridge.",
    "datasource": DS, "gridPos": {"h": 7, "w": 24, "x": 0, "y": y},
    "targets": [{"refId": "A", "datasource": DS, "query": EVENTS_Q}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                    "overrides": [{"matcher": {"id": "byName", "options": "_time"},
                                   "properties": [{"id": "displayName", "value": "Hora"}]}]},
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
})

dashboard = {
    "uid": "crazyflie-bridge",
    "title": "Crazyflie · bridge Webots↔Renode",
    "description": "Telemetría JSONL del bridge de CF_renode_emulator ingerida con bridge_to_influx.py",
    "tags": ["drones", "crazyflie", "bridge"],
    "timezone": "browser",
    "editable": True,
    "graphTooltip": 1,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5s",
    "time": {"from": "now-15m", "to": "now"},
    "timepicker": {"refresh_intervals": ["1s", "5s", "10s", "30s", "1m"]},
    "fiscalYearStartMonth": 0,
    "liveNow": False,
    "links": [],
    "annotations": {"list": [{
        "builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
        "name": "Annotations & Alerts", "type": "dashboard"}]},
    "templating": {"list": [{
        "name": "drone", "label": "Dron", "type": "query", "datasource": DS,
        "query": DRONE_VAR_Q, "definition": DRONE_VAR_Q,
        "refresh": 2, "sort": 1, "multi": True, "includeAll": True,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "options": [], "hide": 0, "regex": "", "skipUrlSync": False,
    }]},
    "panels": panels,
}

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("dashboards") / "crazyflie_bridge.json"
with open(out, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(dashboard, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(f"{len(panels)} paneles -> {out}")
