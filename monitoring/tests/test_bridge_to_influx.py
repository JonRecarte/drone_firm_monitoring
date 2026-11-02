"""Pruebas del ingestor. No necesitan InfluxDB ni el bridge.

    python3 -m unittest discover -s tests -v      (desde drone-monitoring/)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bridge_to_influx as b2i  # noqa: E402

EXAMPLE = HERE / "datos" / "cf_bridge_smoke_jsonl_example.txt"


def cycle(t, sim, alt, motors=(0.55, 0.55, 0.55, 0.55), **extra):
    obj = {
        "t_wall": t, "sim": sim, "alt": alt, "vz": 0.0, "thrust": 30000.0, "u": 0.0,
        "motors": list(motors), "omega": [-55.0, 55.0, -55.0, 55.0],
        "gyro": [0.0, 0.0, 0.0], "accel": [0.0, 0.0, 1000.0], "pressure": 101318.0,
    }
    obj.update(extra)
    return obj


def start(t, alt_target=0.5):
    return {"t_wall": t, "kind": "session_start", "mode": "closed-loop",
            "alt_hold": True, "fw_zdist": False, "alt_target": alt_target}


def _split_fields(s):
    """Parte el bloque de campos por comas que no estén dentro de un string."""
    parts, cur, quoted, esc = [], "", False, False
    for ch in s:
        if esc:
            cur, esc = cur + ch, False
        elif ch == "\\":
            cur, esc = cur + ch, True
        elif ch == '"':
            cur, quoted = cur + ch, not quoted
        elif ch == "," and not quoted:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def parse_line(line):
    """Descompone una línea (sin espacios en tags ni en strings): (medida, tags, campos, ts)."""
    head, rest = line.split(" ", 1)
    fields, ts = rest.rsplit(" ", 1)
    meas, *tag_parts = head.split(",")
    tags = dict(p.split("=", 1) for p in tag_parts)
    out = {}
    for part in _split_fields(fields):
        k, v = part.split("=", 1)
        if v in ("true", "false"):
            out[k] = v == "true"
        elif v.startswith('"'):
            out[k] = v[1:-1]
        else:
            out[k] = float(v)
    return meas, tags, out, int(ts)


class LineProtocolTest(unittest.TestCase):
    def test_escapes_tags_and_fields(self):
        line = b2i.to_line("bridge cycle", {"host": "lab 1,a=b"},
                           {"msg": 'dice "hola"\nadiós', "x": 1.5, "ok": True}, 7)
        self.assertEqual(
            line,
            'bridge\\ cycle,host=lab\\ 1\\,a\\=b msg="dice \\"hola\\"\\nadiós",ok=true,x=1.5 7',
        )

    def test_numbers_are_floats_and_empty_tags_dropped(self):
        line = b2i.to_line("m", {"a": "", "b": None, "c": "x"}, {"n": float(3)}, 1)
        self.assertEqual(line, "m,c=x n=3.0 1")

    def test_no_fields_no_line(self):
        self.assertIsNone(b2i.to_line("m", {"a": "x"}, {}, 1))


class FlattenTest(unittest.TestCase):
    def test_known_and_unknown_vectors(self):
        stats = b2i.Stats()
        out = b2i.flatten({"t_wall": 1.0, "motors": [1, 2, 3, 4], "gyro": [0.1, 0.2, 0.3],
                           "rtt_ms": [5, 6], "cpu": {"sim_speed": 0.4, "util": 80},
                           "bad": float("nan"), "none": None, "flag": False}, stats)
        self.assertEqual(out["m1"], 1.0)
        self.assertEqual(out["m4"], 4.0)
        self.assertEqual(out["gyro_z"], 0.3)
        self.assertEqual(out["rtt_ms_2"], 6.0)
        self.assertEqual(out["cpu_sim_speed"], 0.4)
        self.assertIs(out["flag"], False)
        self.assertNotIn("bad", out)
        self.assertNotIn("none", out)
        self.assertNotIn("t_wall", out)
        self.assertEqual(stats.non_finite, 1)


class ConverterTest(unittest.TestCase):
    def test_session_tags_and_derived_fields(self):
        conv = b2i.Converter({"host": "lab"})
        conv.feed(start(100.0))
        conv.feed(cycle(100.5, 0.0, 0.10))
        _, tags, f, ts = parse_line(conv.feed(cycle(101.5, 0.25, 0.20, motors=(0.5, 0.6, 0.5, 0.6)))[0])
        self.assertEqual(tags, {"host": "lab", "mode": "closed-loop", "session": "1970-01-01T00:01:40Z"})
        self.assertEqual(ts, 101_500_000_000)
        self.assertEqual(f["cycle"], 2.0)
        self.assertAlmostEqual(f["alt_error"], -0.30)
        self.assertAlmostEqual(f["dt_wall"], 1.0)
        self.assertAlmostEqual(f["loop_hz"], 1.0)
        self.assertAlmostEqual(f["dt_sim"], 0.25)
        self.assertAlmostEqual(f["sim_speed"], 0.25)
        self.assertAlmostEqual(f["vz_sim"], 0.40)
        self.assertAlmostEqual(f["motor_spread"], 0.1)

    def test_fakeplant_without_sim_clock_has_no_sim_speed(self):
        conv = b2i.Converter()
        conv.feed(start(10.0))
        conv.feed(cycle(10.1, 0.0, 0.1))
        _, _, f, _ = parse_line(conv.feed(cycle(10.2, 0.0, 0.2))[0])
        for k in ("dt_sim", "sim_speed", "vz_sim"):
            self.assertNotIn(k, f)
        self.assertIn("loop_hz", f)

    def test_derived_never_overwrite_bridge_fields(self):
        conv = b2i.Converter()
        conv.feed(start(10.0))
        _, _, f, _ = parse_line(conv.feed(cycle(10.1, 0.1, 0.4, alt_target=0.7))[0])
        self.assertEqual(f["alt_target"], 0.7)

    def test_new_session_resets_previous_cycle(self):
        conv = b2i.Converter()
        conv.feed(start(10.0))
        conv.feed(cycle(10.5, 1.0, 0.5))
        conv.feed(start(20.0))
        _, tags, f, _ = parse_line(conv.feed(cycle(20.5, 0.1, 0.1))[0])
        self.assertEqual(tags["session"], "1970-01-01T00:00:20Z")
        self.assertEqual(f["cycle"], 1.0)
        self.assertNotIn("dt_wall", f)

    def test_events_keep_detail(self):
        conv = b2i.Converter()
        meas, tags, f, _ = parse_line(conv.feed(start(10.0))[0])
        self.assertEqual(meas, "bridge_event")
        self.assertEqual(tags["kind"], "session_start")
        self.assertIs(f["alt_hold"], True)
        self.assertEqual(f["mode"], "closed-loop")
        self.assertIn("alt_target", f["detail"])

    def test_cycle_before_session_start_is_unknown(self):
        conv = b2i.Converter()
        _, tags, f, _ = parse_line(conv.feed(cycle(5.0, 0.1, 0.1))[0])
        self.assertEqual(tags, {"session": "unknown"})
        self.assertNotIn("alt_error", f)

    def test_invalid_timestamp_is_counted(self):
        conv = b2i.Converter()
        self.assertEqual(conv.feed({"alt": 1.0}), [])
        self.assertEqual(conv.feed({"t_wall": "ayer", "alt": 1.0}), [])
        self.assertEqual(conv.stats.invalid, 2)


class ReadLinesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_line_waits_for_newline(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}\n{"b": ')
        gen = b2i.read_lines(self.path, follow=True, poll=0.0)
        self.assertEqual(next(gen), '{"a": 1}')
        self.assertIs(next(gen), b2i.IDLE)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('2}\n')
        self.assertEqual(next(gen), '{"b": 2}')
        gen.close()

    def test_truncation_yields_reset(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}\n{"a": 2}\n')
        gen = b2i.read_lines(self.path, follow=True, poll=0.0)
        self.assertEqual([next(gen), next(gen)], ['{"a": 1}', '{"a": 2}'])
        self.assertIs(next(gen), b2i.IDLE)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"z": 9}\n')
        self.assertIs(next(gen), b2i.RESET)
        self.assertEqual(next(gen), '{"z": 9}')
        gen.close()

    def test_replay_returns_unterminated_tail(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}\n{"a": 2}')
        self.assertEqual(list(b2i.read_lines(self.path, follow=False)), ['{"a": 1}', '{"a": 2}'])


@unittest.skipUnless(EXAMPLE.is_file(), f"no está {EXAMPLE}")
class ExampleLogTest(unittest.TestCase):
    """El log de ejemplo de Dani: 57 ciclos y dos eventos."""

    @classmethod
    def setUpClass(cls):
        cls.conv = b2i.Converter()
        cls.lines = []
        for raw in b2i.read_lines(str(EXAMPLE), follow=False):
            cls.lines.extend(cls.conv.feed(json.loads(raw)))
        cls.cycles = [parse_line(l) for l in cls.lines if l.startswith("bridge_cycle")]

    def test_counts(self):
        self.assertEqual(self.conv.stats.cycles, 57)
        self.assertEqual(self.conv.stats.events, 2)
        self.assertEqual(self.conv.stats.invalid, 0)
        self.assertEqual(self.conv.stats.non_finite, 0)

    def test_session(self):
        self.assertEqual({t["session"] for _, t, _, _ in self.cycles}, {"2026-10-23T07:37:39Z"})

    def test_fidelity_metrics(self):
        speeds = [f["sim_speed"] for _, _, f, _ in self.cycles if "sim_speed" in f]
        hz = [f["loop_hz"] for _, _, f, _ in self.cycles if "loop_hz" in f]
        self.assertEqual(len(speeds), 56)
        self.assertAlmostEqual(sum(speeds) / len(speeds), 0.316, places=2)
        self.assertAlmostEqual(sum(hz) / len(hz), 1.23, places=1)

    def test_final_altitude_error(self):
        self.assertAlmostEqual(self.cycles[-1][2]["alt_error"], 0.1006, places=3)


if __name__ == "__main__":
    unittest.main()
