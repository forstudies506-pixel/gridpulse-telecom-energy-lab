#!/usr/bin/env python3
"""Small local dashboard server for the synthetic telecom energy prototype.

The dashboard deliberately uses the same Python model module as the CLI.  A
browser POSTs a few simulation controls to ``/api/simulation`` and receives a
JSON snapshot containing aggregated city/tower telemetry, model metrics,
spike alerts, and controller results.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
from collections import defaultdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import unquote, urlsplit

from telecom_model import (
    add_predictions,
    build_coverage_grid,
    detect_faults,
    detect_spikes,
    generate_dataset,
    estimate_coverage,
    simulate_controller,
    simulate_rescue_dispatch,
    summarize_results,
    summarize_fault_detection,
    train_forecaster,
)


ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 16 * 1024
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "lab.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "lab.css": "text/css; charset=utf-8",
    "main.js": "text/javascript; charset=utf-8",
    "lab.js": "text/javascript; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}

STATIC_PREFIXES = {
    "assets/": {
        ".webp": "image/webp",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    },
    "fonts/": {
        ".woff2": "font/woff2",
    },
}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is not allowed: {value}")


def _records(table: Any) -> List[Dict[str, Any]]:
    if hasattr(table, "to_dict"):
        records = [dict(row) for row in table.to_dict(orient="records")]
    else:
        records = [dict(row) for row in table]
    # Convert pandas Timestamp objects once at the boundary. The projections
    # below revisit timestamps many times; native Python datetimes avoid
    # repeated pandas adapter conversion on the dashboard hot path.
    for row in records:
        value = row.get("timestamp")
        to_pydatetime = getattr(value, "to_pydatetime", None)
        if callable(to_pydatetime):
            parsed = to_pydatetime()
            if isinstance(parsed, datetime):
                row["timestamp"] = parsed.replace(tzinfo=None)
    return records


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is None else value.replace(tzinfo=None)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        parsed = to_pydatetime()
        if isinstance(parsed, datetime):
            return parsed.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return datetime(2025, 1, 6)


def _json_value(value: Any) -> Any:
    """Convert model values into strict JSON-safe builtins."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _number(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _rounded(value: float, places: int = 2) -> float:
    return round(float(value), places)


def _group_rows(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, List[Mapping[str, Any]]]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key, "Unknown"))].append(row)
    return groups


def _aggregate_timeline(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_timestamp(row.get("timestamp")).isoformat()].append(row)

    timeline: List[Dict[str, Any]] = []
    for key in sorted(groups):
        bucket = groups[key]
        total_demand = sum(_number(row, "demand_mbps") for row in bucket)
        total_capacity = sum(_number(row, "capacity_mbps") for row in bucket)
        total_active = sum(_number(row, "active_capacity_mbps") for row in bucket)
        timeline.append(
            {
                "timestamp": key,
                "demand_mbps": _rounded(total_demand),
                "predicted_demand_mbps": _rounded(sum(_number(row, "predicted_demand_mbps") for row in bucket)),
                "active_users": _rounded(sum(_number(row, "active_users") for row in bucket)),
                "capacity_mbps": _rounded(total_capacity),
                "active_capacity_mbps": _rounded(total_active),
                "always_on_power_kw": _rounded(sum(_number(row, "always_on_power_kw") for row in bucket)),
                "adaptive_power_kw": _rounded(sum(_number(row, "adaptive_power_kw") for row in bucket)),
                "energy_savings_kwh": _rounded(sum(_number(row, "energy_savings_kwh") for row in bucket)),
                "spike_count": sum(bool(row.get("is_spike", False)) for row in bucket),
                "event_count": sum(bool(row.get("event_flag", False)) for row in bucket),
                "utilization_pct": _rounded(100.0 * total_active / total_capacity) if total_capacity else 0.0,
            }
        )
    return timeline


def _aggregate_city_timelines(rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_city: Dict[str, List[Mapping[str, Any]]] = _group_rows(rows, "city")
    return {city: _aggregate_timeline(city_rows) for city, city_rows in sorted(by_city.items())}


def _hour_profile(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[int(_number(row, "hour"))].append(row)
    profile: List[Dict[str, Any]] = []
    for hour in range(24):
        bucket = groups.get(hour, [])
        if not bucket:
            continue
        count = len(bucket)
        profile.append(
            {
                "hour": hour,
                "demand_mbps": _rounded(sum(_number(row, "demand_mbps") for row in bucket) / count),
                "predicted_demand_mbps": _rounded(sum(_number(row, "predicted_demand_mbps") for row in bucket) / count),
                "adaptive_power_kw": _rounded(sum(_number(row, "adaptive_power_kw") for row in bucket) / count),
                "always_on_power_kw": _rounded(sum(_number(row, "always_on_power_kw") for row in bucket) / count),
            }
        )
    return profile


def _city_summary(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for city, bucket in sorted(_group_rows(rows, "city").items()):
        demand = [_number(row, "demand_mbps") for row in bucket]
        savings = sum(_number(row, "energy_savings_kwh") for row in bucket)
        baseline = sum(_number(row, "always_on_energy_kwh") for row in bucket)
        output.append(
            {
                "city": city,
                "average_demand_mbps": _rounded(sum(demand) / max(1, len(demand))),
                "peak_demand_mbps": _rounded(max(demand, default=0.0)),
                "energy_savings_kwh": _rounded(savings),
                "energy_savings_pct": _rounded(100.0 * savings / baseline) if baseline else 0.0,
                "spike_count": sum(bool(row.get("is_spike", False)) for row in bucket),
                "tower_count": len({str(row.get("tower_id")) for row in bucket}),
            }
        )
    return output


def _tower_state(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Project one model row into the safe, browser-facing tower contract."""

    capacity = _number(
        row,
        "installed_capacity_mbps",
        _number(row, "capacity_mbps"),
    )
    active_capacity = _number(row, "active_capacity_mbps")
    return {
        "tower_id": str(row.get("tower_id", "unknown")),
        "city": str(row.get("city", "Unknown")),
        "timestamp": _timestamp(row.get("timestamp")).isoformat(),
        "active_users": _rounded(_number(row, "active_users")),
        "demand_mbps": _rounded(_number(row, "demand_mbps")),
        "forecast_mbps": _rounded(
            _number(row, "predicted_demand_mbps", _number(row, "forecast_demand_mbps"))
        ),
        "predicted_demand_mbps": _rounded(
            _number(row, "predicted_demand_mbps", _number(row, "forecast_demand_mbps"))
        ),
        "capacity_mbps": _rounded(capacity),
        "installed_capacity_mbps": _rounded(capacity),
        "active_capacity_mbps": _rounded(active_capacity),
        "utilization_pct": _rounded(100.0 * active_capacity / max(1.0, capacity)),
        "adaptive_power_kw": _rounded(_number(row, "adaptive_power_kw")),
        "always_on_power_kw": _rounded(_number(row, "always_on_power_kw")),
        "mode": str(row.get("controller_mode", row.get("mode", "normal"))),
        "controller_mode": str(row.get("controller_mode", row.get("mode", "normal"))),
        "controller_reason": str(row.get("controller_reason", "hold_mode_with_hysteresis")),
        "spike": bool(row.get("is_spike", row.get("upward_spike", False))),
        "event": bool(_number(row, "event_flag") > 0.0),
        "fault": bool(row.get("fault_detected", False)),
        "fault_detected": bool(row.get("fault_detected", False)),
        "fault_reason": str(row.get("fault_reason", "none")),
        "fault_severity": str(row.get("fault_severity", "none")),
        "unmet_demand_mbps": _rounded(_number(row, "unmet_demand_mbps")),
        "overload_mbps": _rounded(_number(row, "overload_mbps")),
        "safety_violation": bool(row.get("safety_violation", False)),
        "overload": bool(_number(row, "overload_mbps") > 0.0),
        "coverage_floor_mbps": _rounded(_number(row, "coverage_floor_mbps")),
        "coverage_layer_protected": bool(row.get("coverage_layer_protected", False)),
        "telemetry_valid": bool(row.get("telemetry_valid", True)),
        "fail_safe_boost": bool(row.get("fail_safe_boost", False)),
        "mode_switch": bool(row.get("mode_switch", False)),
    }


def _tower_snapshot(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Mapping[str, Any]] = {}
    for row in rows:
        tower_id = str(row.get("tower_id", "unknown"))
        if tower_id not in latest or _timestamp(row.get("timestamp")) > _timestamp(latest[tower_id].get("timestamp")):
            latest[tower_id] = row
    return [_tower_state(row) for _, row in sorted(latest.items())]


def _replay_frames(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Build chronological, per-tower interval state for the UI replay."""

    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_timestamp(row.get("timestamp")).isoformat()].append(row)
    return [
        {
            "timestamp": timestamp,
            "towers": [
                _tower_state(row)
                for row in sorted(
                    bucket,
                    key=lambda item: (
                        str(item.get("city", "")),
                        str(item.get("tower_id", "")),
                    ),
                )
            ],
        }
        for timestamp, bucket in sorted(grouped.items())
    ]


def _evaluation_metadata(rows: Sequence[Mapping[str, Any]], model: Any) -> Dict[str, Any]:
    ordered = sorted((_timestamp(row.get("timestamp")) for row in rows))
    train = sorted(
        (_timestamp(row.get("timestamp")) for row in rows if row.get("evaluation_split") == "train")
    )
    test = sorted(
        (_timestamp(row.get("timestamp")) for row in rows if row.get("evaluation_split") == "test")
    )
    return {
        "interval_minutes": int(getattr(model, "forecast_horizon_minutes", 60)),
        "train_end": train[-1].isoformat() if train else None,
        "test_start": test[0].isoformat() if test else None,
        "test_end": test[-1].isoformat() if test else (ordered[-1].isoformat() if ordered else None),
        "timezone": "Asia/Kolkata",
        "chronological_holdout": True,
        "feature_policy": "strict-prior tower lags plus current time/event/weather metadata",
    }


def _detector_metrics(rows: Sequence[Mapping[str, Any]], model: Any) -> Dict[str, Any]:
    evaluation = [row for row in rows if row.get("evaluation_split") == "test"]
    if not evaluation:
        evaluation = [row for row in rows if row.get("evaluation_split") != "warmup"] or list(rows)
    true_alerts = [bool(row.get("telemetry_anomaly", False)) for row in evaluation]
    predicted_alerts = [bool(row.get("is_spike", False)) for row in evaluation]
    true_positive = sum(actual and predicted for actual, predicted in zip(true_alerts, predicted_alerts))
    false_positive = sum((not actual) and predicted for actual, predicted in zip(true_alerts, predicted_alerts))
    false_negative = sum(actual and (not predicted) for actual, predicted in zip(true_alerts, predicted_alerts))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    negatives = sum(not value for value in true_alerts)
    positives = sum(true_alerts)
    return {
        "evaluation_rows": len(evaluation),
        "alert_count": sum(predicted_alerts),
        "true_alert_count": positives,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "precision_pct": precision * 100.0,
        "recall_pct": recall * 100.0,
        "f1_pct": f1 * 100.0,
        "false_positive_rate": false_positive / negatives if negatives else 0.0,
        "false_negative_rate": false_negative / positives if positives else 0.0,
        "threshold": 3.5,
        "minimum_residual_mbps": 8.0,
        "calibration_source": "train_history",
        "train_calibration_count": int(getattr(model, "calibration_count", 0)),
    }


def _scenario_metadata(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    surge_rows = sorted(
        (row for row in rows if bool(row.get("surge_demo", False))),
        key=lambda row: _timestamp(row.get("timestamp")),
    )
    if not surge_rows:
        return {"enabled": False}
    first = surge_rows[0]
    return {
        "enabled": True,
        "tower_id": str(first.get("tower_id", "")),
        "city": str(first.get("city", "")),
        "timestamp": _timestamp(first.get("timestamp")).isoformat(),
        "duration_intervals": len(surge_rows),
        "description": "Unannounced 3x active-user and traffic surge on one synthetic tower.",
    }


def _alert_rows(rows: Sequence[Mapping[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    interesting = [
        row
        for row in rows
        if bool(row.get("fault_detected", False))
        or bool(row.get("is_spike", False))
        or bool(row.get("event_flag", False))
    ]
    interesting.sort(key=lambda row: _timestamp(row.get("timestamp")), reverse=True)
    alerts: List[Dict[str, Any]] = []
    for row in interesting[:limit]:
        is_fault = bool(row.get("fault_detected", False))
        is_spike = bool(row.get("is_spike", False))
        kind = "Telemetry fault" if is_fault else "Demand spike" if is_spike else "Scheduled event"
        alerts.append(
            {
                "timestamp": _timestamp(row.get("timestamp")).isoformat(),
                "city": str(row.get("city", "Unknown")),
                "tower_id": str(row.get("tower_id", "Unknown")),
                "kind": kind,
                "detail": (
                    str(row.get("fault_reason", "Telemetry validation failed"))
                    if is_fault
                    else f"Actual {_number(row, 'demand_mbps'):.0f} Mbps vs forecast "
                    f"{_number(row, 'predicted_demand_mbps'):.0f} Mbps"
                    if is_spike
                    else f"{str(row.get('event_type', 'event')).title()} uplift"
                ),
                "score": _rounded(_number(row, "spike_score")),
                "mode": str(row.get("controller_mode", "normal")),
                "severity": str(row.get("fault_severity", "none")),
            }
        )
    return alerts


def run_simulation(options: Mapping[str, Any]) -> Dict[str, Any]:
    """Run the end-to-end pipeline and return a browser-friendly snapshot."""

    def integer(name: str, default: int, low: int, high: int) -> int:
        try:
            numeric = float(options.get(name, default))
            if not math.isfinite(numeric):
                raise ValueError
            value = int(numeric)
        except (TypeError, ValueError, OverflowError):
            value = default
        return max(low, min(high, value))

    def probability(name: str, default: float) -> float:
        try:
            value = float(options.get(name, default))
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            value = default
        return max(0.0, min(1.0, value))

    def boolean(name: str, default: bool = False) -> bool:
        raw = options.get(name, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return default

    config = {
        "days": integer("days", 14, 1, 30),
        "cities": integer("cities", 3, 1, 6),
        "towers_per_city": integer("towers_per_city", 3, 1, 8),
        "event_probability": probability("event_probability", 0.055),
        "anomaly_probability": probability("anomaly_probability", 0.012),
        "fault_probability": probability("fault_probability", 0.005),
        "surge_demo": boolean("surge_demo"),
    }
    seed = integer("seed", 42, 0, 999999)
    raw = detect_faults(generate_dataset(config, seed=seed))
    model = train_forecaster(raw)
    frame = add_predictions(raw, model)
    frame = detect_spikes(frame)
    frame = simulate_controller(frame)
    rows = _records(frame)
    # Reuse the one DataFrame-to-record conversion above. This avoids a second
    # full conversion inside summarize_results on the dashboard hot path.
    summary = summarize_results(rows)
    fault_metrics = summarize_fault_detection(rows)
    coverage_full = estimate_coverage(rows, build_coverage_grid(rows))
    operations = simulate_rescue_dispatch(rows, coverage_full, fault_metrics)
    coverage = {
        key: value for key, value in coverage_full.items() if not key.startswith("_")
    }
    cities = sorted({str(row.get("city", "Unknown")) for row in rows})
    timeline = _aggregate_timeline(rows)
    return {
        "config": {**config, "seed": seed},
        "summary": {key: _json_value(value) for key, value in summary.items()},
        "model": {
            "train_rows": model.train_rows,
            "test_rows": model.test_rows,
            "warmup_rows": model.warmup_rows,
            "train_mae_mbps": _rounded(model.train_metrics.get("mae", 0.0)),
            "test_mae_mbps": _rounded(model.test_metrics.get("mae", 0.0)),
            "test_rmse_mbps": _rounded(model.test_metrics.get("rmse", 0.0)),
            "feature_count": len(model.feature_columns),
            "forecast_horizon_minutes": model.forecast_horizon_minutes,
            "calibration_center_mbps": _rounded(model.calibration_center_mbps),
            "calibration_scale_mbps": _rounded(model.calibration_scale_mbps),
            "calibration_count": model.calibration_count,
        },
        "cities": cities,
        "timeline": timeline,
        "city_timelines": _aggregate_city_timelines(rows),
        "hour_profile": _hour_profile(rows),
        "city_summary": _city_summary(rows),
        "tower_snapshot": _tower_snapshot(rows),
        "replay": _replay_frames(rows),
        "alerts": _alert_rows(rows),
        "scenario": _scenario_metadata(rows),
        "evaluation": _evaluation_metadata(rows, model),
        "detector": _detector_metrics(rows, model),
        "coverage": coverage,
        "operations": operations,
        "calibration": {
            "center_mbps": _rounded(model.calibration_center_mbps),
            "scale_mbps": _rounded(model.calibration_scale_mbps),
            "count": model.calibration_count,
            "source": "train_history",
            "threshold": 3.5,
        },
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TelecomEnergyPrototype/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the terminal readable while still showing useful request lines.
        print(f"{self.command} {self.path}")

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        send_body: bool = True,
        allow: str | None = None,
    ) -> None:
        if status == 204:
            body = b""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        send_body: bool = True,
        allow: str | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            send_body=send_body,
            allow=allow,
        )

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        if path == "/api/health":
            self._send_json(200, {"ok": True, "service": "telecom-energy-prototype"})
            return
        if path == "/" or path == "/index.html":
            self._serve_file("index.html")
            return
        name = path.lstrip("/")
        if name in STATIC_FILES or self._is_asset_path(name):
            self._serve_file(name)
            return
        self._send_json(404, {"error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        if path == "/api/health":
            self._send_json(200, {"ok": True, "service": "telecom-energy-prototype"}, send_body=False)
            return
        if path == "/" or path == "/index.html":
            self._serve_file("index.html", send_body=False)
            return
        name = path.lstrip("/")
        if name in STATIC_FILES or self._is_asset_path(name):
            self._serve_file(name, send_body=False)
            return
        self._send_json(404, {"error": "not found"}, send_body=False)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        if path == "/api/simulation":
            self._send_json(204, {}, allow="POST, OPTIONS")
            return
        if path == "/api/health":
            self._send_json(204, {}, allow="GET, HEAD, OPTIONS")
            return
        if path == "/" or path == "/index.html" or path.lstrip("/") in STATIC_FILES or self._is_asset_path(path.lstrip("/")):
            self._send_json(204, {}, allow="GET, HEAD, OPTIONS")
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = unquote(urlsplit(self.path).path)
        if path != "/api/simulation":
            self._send_json(404, {"error": "not found"})
            return
        try:
            options = self._read_json()
            payload = run_simulation(options)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": str(error)})
            return
        except Exception:
            self._send_json(500, {"error": "simulation failed"})
            return
        self._send_json(200, payload)

    def _serve_file(self, name: str, *, send_body: bool = True) -> None:
        if name not in STATIC_FILES and not self._is_asset_path(name):
            self._send_json(404, {"error": "not found"}, send_body=send_body)
            return
        path = (ROOT / name).resolve()
        try:
            inside_root = os.path.commonpath((str(ROOT), str(path))) == str(ROOT)
        except ValueError:
            inside_root = False
        if not inside_root or not path.is_file():
            self._send_json(404, {"error": "not found"}, send_body=send_body)
            return
        content_type = STATIC_FILES.get(name) or self._asset_content_type(name)
        self._send_bytes(200, path.read_bytes(), content_type, send_body=send_body)

    @staticmethod
    def _is_asset_path(name: str) -> bool:
        if not any(name.startswith(prefix) for prefix in STATIC_PREFIXES):
            return False
        parts = name.split("/")
        return all(part and part not in {".", ".."} and not part.startswith(".") for part in parts)

    @staticmethod
    def _asset_content_type(name: str) -> str:
        for prefix, extensions in STATIC_PREFIXES.items():
            if name.startswith(prefix):
                for extension, content_type in extensions.items():
                    if name.endswith(extension):
                        return content_type
        return "application/octet-stream"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    actual_host, actual_port = server.server_address[:2]
    print(f"Telecom energy dashboard: http://{actual_host}:{actual_port}")
    print("Synthetic data only — no live network equipment is connected.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
