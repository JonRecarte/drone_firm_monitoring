"""Generates dashboards/crazyflie_bridge.json.

    python3 build_dashboard.py              # writes next to this script
    python3 build_dashboard.py other.json   # or to the given path

The provisioned JSON is the output of this script: to add a panel (e.g. when
the bridge telemetry grows) edit it here and regenerate. Changes made by hand
in the Grafana UI are lost when regenerating.
"""
import json
import sys
from pathlib import Path

DS = {"type": "influxdb", "uid": "influx-drones"}
# Validated categorical palette (dark theme), in fixed order: blue, orange, aqua, yellow.
C = ["#3987e5", "#d95926", "#199e70", "#c98500"]
MUTED = "#898781"

# dfm tags every point with `drone`. Older flights do not have it and show up as UNTAGGED.
UNTAGGED = "(untagged)"
DRONE = '(if exists r.drone then r.drone else "' + UNTAGGED + '")'
DRONE_FILTER = "  |> filter(fn: (r) => contains(value: " + DRONE + ", set: ${drone:json}))\n"
# `r with`: a record with only `_value` drops the untagged points.
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
    '  |> rename(columns: {_value: "detail", kind: "event"})'
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
    """series: list of (field, display name, color, style)."""
    refs = "ABCDEFGH"
    targets, overrides = [], []
    for i, (field, name, color, style) in enumerate(series):
        targets.append({"refId": refs[i], "datasource": DS, "query": series_q(field)})
        if len(series) == 1:
            # A single quantity: one color per drone and its name in the legend.
            props = [{"id": "displayName", "value": "${__field.labels.drone}"}]
        else:
            # Several quantities: the usual color per quantity; the legend says which drone it is.
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


# ---------------------------------------------------------------- Summary
row("Summary of the time range")
stat("Altitude (last value)", "Last plant altitude (Webots GPS) in the selected time range and drones.",
     "alt", "last", "suffix: m", 0, 5, 3)
stat("Altitude error (last)", "alt − target. Positive = above the session_start target.",
     "alt_error", "last", "suffix: m", 5, 5, 3)
stat("Mean loop rate", "Mean of 1/Δt_wall between consecutive bridge cycles. The bridge asks for --loop-hz (12 Hz by default in dfm).",
     "loop_hz", "mean", "suffix: Hz", 10, 5, 2)
stat("Mean simulation speed", "Mean of Δsim/Δwall: simulated Webots seconds per real second. 1 = real time. It does not include Renode's virtual time, which the bridge does not log yet.",
     "sim_speed", "mean", "none", 15, 5, 2, suffix="×")
stat("Cycles in the range", "Number of control cycles logged in the JSONL.",
     "cycle", "count", "none", 20, 4, 0)
y += 5

# ---------------------------------------------------------------- Flight
row("Flight")
timeseries("Altitude and target",
           "Plant altitude (Webots GPS) and the alt_target set in session_start.",
           [("alt", "Altitude", C[0], "solid"), ("alt_target", "Target", MUTED, "dash")],
           "suffix: m", 0, 12)
timeseries("Altitude error",
           "alt − target. A steady error points to a HOVER_THRUST bias or to the integrator of the bridge's PD+I being saturated.",
           [("alt_error", "Altitude error", C[0], "solid")], "suffix: m", 12, 12, threshold_line=0)
y += 8
timeseries("Vertical speed",
           "Bridge vz: the Δalt/Δt its PD+I uses, in simulated time with Webots (wall time with FakePlant). Physical vz: Δalt/Δsim, computed by the ingester from the logged samples. With Webots both should match; a gap means the controller and the log see different clocks.",
           [("vz", "Bridge vz", C[0], "solid"), ("vz_sim", "Physical vz (simulated time)", C[1], "solid")],
           "velocityms", 0, 8)
timeseries("Commanded thrust",
           "Thrust of the RPYT setpoint the bridge writes into the firmware (Crazyflie scale 0–65535).",
           [("thrust", "Thrust", C[0], "solid")], "none", 8, 8, decimals=0)
timeseries("Altitude controller output (u)",
           "Dimensionless output of the bridge's altitude PD+I: thrust = HOVER_THRUST + u·14000.",
           [("u", "u", C[0], "solid")], "none", 16, 8)
y += 8

# ---------------------------------------------------------------- Actuators
row("Actuators · firmware → plant")
timeseries("Motor ratios",
           "motor_ratios of the firmware mixer, read from Renode (motor_stats), 0–1.",
           [("m1", "M1", C[0], "solid"), ("m2", "M2", C[1], "solid"),
            ("m3", "M3", C[2], "solid"), ("m4", "M4", C[3], "solid")], "percentunit", 0, 8, decimals=1,
           soft_min=0)
timeseries("Propeller speed ω",
           "ω applied in Webots: ratio × 100 rad/s with Bitcraze's spin signs (−, +, −, +).",
           [("omega1", "ω1", C[0], "solid"), ("omega2", "ω2", C[1], "solid"),
            ("omega3", "ω3", C[2], "solid"), ("omega4", "ω4", C[3], "solid")], "rotrads", 8, 8, decimals=1)
timeseries("Motor spread",
           "max − min of the four ratios. Zero = symmetric thrust (no attitude correction).",
           [("motor_spread", "max − min", C[0], "solid")], "percentunit", 16, 8, decimals=2,
           soft_min=0, soft_max=0.05)
y += 8

# ---------------------------------------------------------------- Sensors
row("Sensors · plant → firmware")
timeseries("Gyroscope",
           "Angular rate sent to the emulated BMI088, in degrees/s, after the bridge's low-pass filter.",
           [("gyro_x", "x", C[0], "solid"), ("gyro_y", "y", C[1], "solid"), ("gyro_z", "z", C[2], "solid")],
           "none", 0, 8)
timeseries("Accelerometer",
           "Proper acceleration sent to the BMI088, in mg (1000 = 1 g). The PROTO has no accelerometer: the bridge derives it from the GPS and sends exactly (0, 0, 1000) when the drone is almost still.",
           [("accel_x", "x", C[0], "solid"), ("accel_y", "y", C[1], "solid"), ("accel_z", "z", C[2], "solid")],
           "none", 8, 8, decimals=1)
timeseries("Barometric pressure",
           "Pressure sent to the emulated BMP388, derived from the plant altitude.",
           [("pressure", "Pressure", C[0], "solid")], "suffix: Pa", 16, 8, decimals=1)
y += 8

# ---------------------------------------------------------------- Fidelity
row("Co-simulation fidelity")
timeseries("Bridge loop rate",
           "1/Δt of wall time between consecutive cycles. Computed by the ingester. Every extra query to the Renode monitor makes the cycle longer (e.g. the bridge's status line every --status-every s).",
           [("loop_hz", "Rate", C[0], "solid")], "suffix: Hz", 0, 8, decimals=2)
timeseries("Simulation speed",
           "Δsim/Δwall between cycles. The grey line marks 1 (real time). Computed by the ingester; it does not include Renode's virtual time.",
           [("sim_speed", "Δsim/Δwall", C[0], "solid")], "none", 8, 8, threshold_line=1)
timeseries("Simulation advance per cycle",
           "Simulated Webots seconds between cycles. The bridge's catch-up limits the advance to ~0.25 s per cycle.",
           [("dt_sim", "Δsim per cycle", C[0], "solid")], "s", 16, 8)
y += 8

# ---------------------------------------------------------------- Events
row("Session events")
panels.append({
    "type": "table", "id": next(_ids), "title": "session_start / session_end",
    "description": "Events from the JSONL with their parameters, as the bridge writes them.",
    "datasource": DS, "gridPos": {"h": 7, "w": 24, "x": 0, "y": y},
    "targets": [{"refId": "A", "datasource": DS, "query": EVENTS_Q}],
    "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                    "overrides": [{"matcher": {"id": "byName", "options": "_time"},
                                   "properties": [{"id": "displayName", "value": "Time"}]}]},
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
})

dashboard = {
    "uid": "crazyflie-bridge",
    "title": "Crazyflie · Webots↔Renode bridge",
    "description": "JSONL telemetry of the drone_firm_monitoring bridge, ingested with bridge_to_influx.py",
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
        "name": "drone", "label": "Drone", "type": "query", "datasource": DS,
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
print(f"{len(panels)} panels -> {out}")
