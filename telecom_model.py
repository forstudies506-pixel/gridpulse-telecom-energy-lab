#!/usr/bin/env python3
"""Causal synthetic telecom demand and energy-control prototype.

This module is deliberately a small, inspectable prototype rather than a
claim about real network operations.  It uses only the Python standard
library.  If pandas is installed, public functions accept and return pandas
``DataFrame`` objects; otherwise they accept and return lists of dictionaries.

The intended flow is::

    frame = generate_dataset(seed=7)
    model = train_forecaster(frame)
    frame = add_predictions(frame, model)
    frame = detect_spikes(frame)
    frame = simulate_controller(frame)
    summary = summarize_results(frame)

Forecasts at timestamp ``t`` use only observations for the same tower with a
timestamp strictly earlier than ``t``.  Current timestamp cyclical features
and static metadata are allowed; current active users and current demand are
never model features.  The controller is a deterministic simulation with an
explicit coverage floor and installed-capacity limit.  Its coverage wording
is conceptual only and makes no real-world coverage guarantee.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # Optional convenience only; the numerical core has no pandas dependency.
    import pandas as _pd  # type: ignore
except Exception:  # pragma: no cover - exercised in minimal Python installs.
    _pd = None


DEFAULT_SEED = 42
DEFAULT_DAYS = 14
DEFAULT_TOWERS_PER_CITY = 3
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_MIN_HISTORY = 2
DEFAULT_RIDGE_ALPHA = 1.0e-8
DEFAULT_CALIBRATION_FLOOR_Mbps = 1.0

Row = Dict[str, Any]
Table = Any

# These are the only model features.  In particular, current ``active_users``
# and current ``demand_mbps`` are intentionally absent.  ``lag_*`` and
# ``rolling_*`` values are computed from strict prior observations per tower.
FEATURE_COLUMNS: Tuple[str, ...] = (
    "lag_demand_mbps_1",
    "lag_demand_mbps_2",
    "rolling_demand_mbps_3",
    "lag_active_users_1",
    "rolling_active_users_3",
    "history_count",
    "time_of_day_sin",
    "time_of_day_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    "is_weekend",
    # Event/weather observations are exogenous values available at t; the
    # anomaly and demo labels below are intentionally absent.
    "event_flag",
    "event_intensity",
    "temperature_c",
    "humidity_pct",
    "rainfall_mm",
    "wind_speed_kph",
    "weather_load_index",
    "capacity_mbps",
    "installed_capacity_mbps",
    "city_index",
    "tower_index",
)

_MODE_ORDER: Tuple[str, ...] = ("sleep", "eco", "normal", "boost")

_CITY_PROFILES: Tuple[Dict[str, Any], ...] = (
    {
        "city": "Metroville",
        "base_users": 760.0,
        "base_demand_mbps": 250.0,
        "temperature_offset": 4.0,
    },
    {
        "city": "Riverton",
        "base_users": 610.0,
        "base_demand_mbps": 205.0,
        "temperature_offset": 0.0,
    },
    {
        "city": "Lakeside",
        "base_users": 495.0,
        "base_demand_mbps": 170.0,
        "temperature_offset": -2.0,
    },
)


def _is_dataframe(value: Any) -> bool:
    return _pd is not None and isinstance(value, _pd.DataFrame)


def _rows(value: Table) -> List[Row]:
    """Return detached row dictionaries for either supported table form."""

    if _is_dataframe(value):
        return [dict(row) for row in value.to_dict(orient="records")]
    if isinstance(value, Mapping):
        return [dict(value)]
    return [dict(row) for row in value]


def _read_rows(value: Table) -> List[Row]:
    """Read rows without copying an internal list-of-dicts hot-path input."""

    if isinstance(value, list):
        return value
    return _rows(value)


def _table_from_rows(rows: Sequence[Row], template: Optional[Table] = None) -> Table:
    """Build the same table kind as ``template`` while preserving DataFrame indexes."""

    if _is_dataframe(template):
        # Construct in one operation rather than repeatedly inserting columns;
        # this avoids pandas fragmentation warnings on the model's many output
        # metadata columns while retaining the caller's index and column order.
        result = _pd.DataFrame(list(rows), index=template.index.copy())
        columns = list(template.columns)
        columns.extend(column for column in result.columns if column not in columns)
        return result.reindex(columns=columns)
    return [dict(row) for row in rows]


def _copy_with_rows(value: Table, rows: Sequence[Row]) -> Table:
    return _table_from_rows(rows, value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _finite_number(value: Any) -> Tuple[bool, float]:
    """Return whether a value is a finite float without silently accepting None."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return False, 0.0
    if not math.isfinite(result):
        return False, 0.0
    return True, result


def _nonnegative_telemetry(value: Any) -> Tuple[bool, float]:
    valid, number = _finite_number(value)
    return valid and number >= 0.0, max(0.0, number)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_timestamp(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is None else value.replace(tzinfo=None)
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        parsed = to_pydatetime()
        if isinstance(parsed, datetime):
            return parsed.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            pass
    return fallback


def _normalise_config(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, Mapping):
        return dict(config)
    raise TypeError("config must be a mapping or None")


def _city_specs(config: Mapping[str, Any]) -> List[Dict[str, Any]]:
    supplied = config.get(
        "city_profiles",
        config.get("cities", config.get("num_cities", config.get("n_cities"))),
    )
    if supplied is None:
        return [dict(profile) for profile in _CITY_PROFILES]
    if isinstance(supplied, int):
        count = max(1, supplied)
        specs: List[Dict[str, Any]] = []
        for index in range(count):
            profile = dict(_CITY_PROFILES[index % len(_CITY_PROFILES)])
            profile["city"] = "City-%02d" % (index + 1)
            specs.append(profile)
        return specs
    if isinstance(supplied, str):
        supplied = [supplied]
    specs = []
    for index, item in enumerate(supplied):
        if isinstance(item, Mapping):
            profile = dict(_CITY_PROFILES[index % len(_CITY_PROFILES)])
            profile.update(item)
        else:
            profile = dict(_CITY_PROFILES[index % len(_CITY_PROFILES)])
            profile["city"] = str(item)
        profile.setdefault("city", "City-%02d" % (index + 1))
        specs.append(profile)
    return specs or [dict(profile) for profile in _CITY_PROFILES]


def _normalise_start(value: Any) -> datetime:
    return _parse_timestamp(value, datetime(2025, 1, 6, 0, 0, 0))


def _safe_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, number)


def _weather_for_hour(
    timestamp: datetime,
    profile: Mapping[str, Any],
    city_index: int,
    rng: random.Random,
) -> Tuple[float, float, float, float, float]:
    """Create deterministic, non-negative weather telemetry for one city/hour."""

    day_of_year = timestamp.timetuple().tm_yday
    seasonal = math.sin((2.0 * math.pi * (day_of_year - 80)) / 365.0)
    daily = math.sin((2.0 * math.pi * (timestamp.hour - 7)) / 24.0)
    temperature = 19.0 + _as_float(profile.get("temperature_offset"), 0.0)
    temperature += 8.0 * seasonal + 3.5 * daily + (rng.random() - 0.5) * 2.4
    temperature = max(0.0, temperature)
    humidity = 63.0 - (temperature - 20.0) * 0.65 + (rng.random() - 0.5) * 12.0
    humidity = _clamp(humidity, 18.0, 98.0)
    rain_chance = 0.075 + 0.025 * (1.0 - seasonal) + 0.01 * (city_index % 3)
    rainfall = 0.0
    if rng.random() < rain_chance:
        rainfall = max(0.0, rng.random() * 8.0 + rng.random() * 3.0)
    wind = 7.0 + rng.random() * 17.0 + rainfall * 0.45
    weather_load = 1.0 + max(0.0, temperature - 24.0) * 0.018
    weather_load += rainfall * 0.006 + max(0.0, humidity - 70.0) * 0.002
    return temperature, humidity, rainfall, wind, weather_load


def _synthetic_site_position(tower_index: int, towers_per_city: int) -> Tuple[float, float]:
    """Return a deterministic city-local position for a tower visualization.

    These normalized coordinates are deliberately not latitude/longitude and
    must not be interpreted as an RF propagation or geographic-radius model.
    """

    count = max(1, int(towers_per_city))
    index = max(0, min(count - 1, int(tower_index)))
    if count == 1:
        return 0.5, 0.5
    columns = max(1, int(math.ceil(math.sqrt(count))))
    rows = max(1, int(math.ceil(count / columns)))
    column = index % columns
    row = index // columns
    return (
        (column + 0.5) / columns,
        (row + 0.5) / rows,
    )


def generate_dataset(
    config: Optional[Mapping[str, Any]] = None,
    seed: int = DEFAULT_SEED,
    **overrides: Any,
) -> Table:
    """Generate deterministic hourly city/tower telemetry.

    Supported configuration includes ``days``/``num_days``, ``hours``,
    ``cities``/``city_profiles``, ``towers_per_city``, ``start``,
    ``interval_minutes``, ``event_probability``, ``anomaly_probability``,
    ``fault_probability`` and ``surge_demo``.  A numeric positional argument
    is accepted as a seed.

    ``surge_demo=True`` adds a deterministic, unannounced 3x users and traffic
    surge to the first city's first tower for at least two observations near
    85% of the generated timeline.  It does not alter event/schedule flags.
    Anomalies are labels, not model features, and demand is never capped at
    installed capacity; overload is therefore visible to the controller.
    """

    if isinstance(config, (int, float)):
        if seed == DEFAULT_SEED:
            seed = int(config)
        config = None
    cfg = _normalise_config(config)
    cfg.update(overrides)
    if "seed" in cfg and seed == DEFAULT_SEED:
        seed = int(cfg["seed"])

    rng = random.Random(int(seed))
    profiles = _city_specs(cfg)
    towers_per_city = _safe_positive_int(
        cfg.get(
            "towers_per_city",
            cfg.get("num_towers_per_city", cfg.get("num_towers", cfg.get("n_towers"))),
        ),
        DEFAULT_TOWERS_PER_CITY,
    )
    interval_minutes = _safe_positive_int(
        cfg.get("interval_minutes", cfg.get("frequency_minutes", DEFAULT_INTERVAL_MINUTES)),
        DEFAULT_INTERVAL_MINUTES,
    )
    if "hours" in cfg or "num_hours" in cfg:
        periods = _safe_positive_int(
            cfg.get("hours", cfg.get("num_hours")), DEFAULT_DAYS * 24
        )
    elif "periods" in cfg or "num_periods" in cfg:
        periods = _safe_positive_int(
            cfg.get("periods", cfg.get("num_periods")), DEFAULT_DAYS * 24
        )
    else:
        days = _safe_positive_int(
            cfg.get("days", cfg.get("num_days", cfg.get("n_days", DEFAULT_DAYS))),
            DEFAULT_DAYS,
        )
        periods = max(1, int(round(days * 24.0 * 60.0 / interval_minutes)))
    start = _normalise_start(cfg.get("start", cfg.get("start_timestamp")))
    event_probability = _clamp(_as_float(cfg.get("event_probability"), 0.055), 0.0, 1.0)
    anomaly_probability = _clamp(
        _as_float(cfg.get("anomaly_probability"), 0.012), 0.0, 1.0
    )
    fault_probability = _clamp(
        _as_float(cfg.get("fault_probability"), 0.0), 0.0, 1.0
    )
    surge_enabled = bool(cfg.get("surge_demo", False))
    surge_duration = _safe_positive_int(cfg.get("surge_duration_intervals"), 2)
    if surge_enabled:
        periods = max(periods, surge_duration)
    surge_start = int(round(periods * 0.85))
    surge_start = max(0, min(max(0, periods - surge_duration), surge_start))

    rows: List[Row] = []
    interval = timedelta(minutes=interval_minutes)
    for period in range(periods):
        timestamp = start + interval * period
        hour_float = timestamp.hour + timestamp.minute / 60.0
        tod_angle = 2.0 * math.pi * hour_float / 24.0
        dow_angle = 2.0 * math.pi * timestamp.weekday() / 7.0
        is_weekend = int(timestamp.weekday() >= 5)
        morning_peak = math.exp(-((hour_float - 9.0) / 3.3) ** 2)
        evening_peak = math.exp(-((hour_float - 20.0) / 3.6) ** 2)
        overnight_dip = math.exp(-((hour_float - 3.0) / 2.4) ** 2)
        time_factor = (
            0.47
            + 0.23 * morning_peak
            + 0.31 * evening_peak
            - 0.13 * overnight_dip
        )
        weekday_factor = 0.91 if is_weekend else 1.03

        for city_index, profile in enumerate(profiles):
            temperature, humidity, rainfall, wind, weather_load = _weather_for_hour(
                timestamp, profile, city_index, rng
            )
            city_name = str(profile.get("city", "City-%02d" % (city_index + 1)))
            base_users = max(1.0, _as_float(profile.get("base_users"), 500.0))
            base_demand = max(1.0, _as_float(profile.get("base_demand_mbps"), 180.0))
            event_flag = int(rng.random() < event_probability)
            event_type = "none"
            event_intensity = 0.0
            if event_flag:
                event_type = rng.choice(("concert", "sports", "festival", "emergency"))
                event_intensity = 0.45 + rng.random() * 0.55

            for tower_index in range(towers_per_city):
                tower_id = "%s-T%02d" % (
                    city_name.lower().replace(" ", "-"),
                    tower_index + 1,
                )
                tower_factor = 0.86 + 0.10 * (
                    (tower_index + 1) / max(1, towers_per_city)
                )
                site_x, site_y = _synthetic_site_position(
                    tower_index, towers_per_city
                )
                # Leave normal traffic headroom while making the documented
                # 3x surge capable of exposing an honest installed-capacity
                # overload instead of being silently clipped.
                installed_capacity = max(300.0, base_demand * 1.65 * tower_factor)
                tower_users = base_users * tower_factor
                demand_signal = tower_users * time_factor * weekday_factor * weather_load
                demand_signal *= 1.0 + event_intensity * (0.18 + 0.06 * (tower_index % 2))
                demand = 0.29 * demand_signal + 0.10 * base_demand
                demand += (rng.random() - 0.5) * (0.045 * max(demand, 1.0))
                demand = max(0.0, demand)

                users = tower_users * time_factor * weekday_factor
                users *= 1.0 + event_intensity * 0.25
                users += (rng.random() - 0.5) * max(8.0, users * 0.035)
                users = max(0.0, users)

                target_tower = city_index == 0 and tower_index == 0
                surge_demo = int(
                    surge_enabled
                    and target_tower
                    and surge_start <= period < surge_start + surge_duration
                )
                pre_anomaly_users = users
                pre_anomaly_demand = demand
                anomaly_triggered = int(rng.random() < anomaly_probability)
                random_anomaly_multiplier = 1.0
                if anomaly_triggered:
                    # Consume this draw even during surge_demo so toggling the
                    # demo does not perturb later deterministic telemetry.
                    random_anomaly_multiplier += 0.32 + rng.random() * 0.30
                telemetry_anomaly = anomaly_triggered
                anomaly_multiplier = random_anomaly_multiplier
                if telemetry_anomaly:
                    # Anomaly labels correspond to a physical-looking change
                    # in both observed users and observed traffic.
                    users = pre_anomaly_users * anomaly_multiplier
                    demand = pre_anomaly_demand * anomaly_multiplier
                if surge_demo:
                    # Deliberately unannounced: event fields remain unchanged.
                    # The 3x multiplier is applied after any independent
                    # anomaly, so the same-seed no-surge row is exactly 3x.
                    users *= 3.0
                    demand *= 3.0
                    telemetry_anomaly = 1
                    anomaly_multiplier *= 3.0

                synthetic_fault = int(rng.random() < fault_probability)
                synthetic_fault_type = "none"
                if synthetic_fault:
                    synthetic_fault_type = rng.choice(
                        (
                            "missing_demand",
                            "invalid_capacity",
                            "invalid_power",
                            "capacity_mismatch",
                        )
                    )

                baseline_power = 8.0 + installed_capacity * 0.018
                rows.append(
                    {
                        "timestamp": timestamp,
                        "city": city_name,
                        "tower_id": tower_id,
                        "city_index": city_index,
                        "tower_index": tower_index,
                        "site_x": site_x,
                        "site_y": site_y,
                        "hour": timestamp.hour,
                        "day_of_week": timestamp.weekday(),
                        "is_weekend": is_weekend,
                        "time_of_day_sin": math.sin(tod_angle),
                        "time_of_day_cos": math.cos(tod_angle),
                        "day_of_week_sin": math.sin(dow_angle),
                        "day_of_week_cos": math.cos(dow_angle),
                        "event_flag": event_flag,
                        "event_type": event_type,
                        "event_intensity": event_intensity,
                        "temperature_c": temperature,
                        "humidity_pct": humidity,
                        "rainfall_mm": rainfall,
                        "wind_speed_kph": wind,
                        "weather_load_index": weather_load,
                        "active_users": users,
                        "users": users,
                        "demand_mbps": demand,
                        "traffic_mbps": demand,
                        # ``capacity_mbps`` is retained for the old dashboard;
                        # installed_capacity_mbps is the immutable controller
                        # reference copied from the same generated metadata.
                        "capacity_mbps": installed_capacity,
                        "installed_capacity_mbps": installed_capacity,
                        "installed_capacity": installed_capacity,
                        "baseline_power_kw": baseline_power,
                        "interval_minutes": interval_minutes,
                        "sampling_interval_minutes": interval_minutes,
                        "interval_hours": interval_minutes / 60.0,
                        "telemetry_anomaly": telemetry_anomaly,
                        "surge_demo": surge_demo,
                        # ``anomaly_multiplier`` already includes the optional
                        # 3x demo surge above.  Do not multiply it a second
                        # time in the exported telemetry label.
                        "anomaly_multiplier": anomaly_multiplier,
                        "synthetic_fault": synthetic_fault,
                        "synthetic_fault_type": synthetic_fault_type,
                    }
                )

                # Fault mutations happen after a complete healthy row is
                # created. The immutable installed capacity remains available
                # for the controller and coverage proxy to inspect.
                if synthetic_fault:
                    row = rows[-1]
                    if synthetic_fault_type == "missing_demand":
                        row["demand_mbps"] = None
                        row["traffic_mbps"] = None
                    elif synthetic_fault_type == "invalid_capacity":
                        row["capacity_mbps"] = None
                    elif synthetic_fault_type == "invalid_power":
                        row["baseline_power_kw"] = None
                    elif synthetic_fault_type == "capacity_mismatch":
                        row["capacity_mbps"] = installed_capacity * 0.55

    if _pd is not None:
        result = _pd.DataFrame(rows)
        if not result.empty:
            result["timestamp"] = _pd.to_datetime(result["timestamp"])
        return result
    return rows


def detect_faults(
    value: Table,
    capacity_tolerance: float = 0.05,
) -> Table:
    """Validate tower telemetry and flag synthetic equipment-data faults.

    This detector intentionally does not treat demand spikes, overloads,
    scheduled events, or ``telemetry_anomaly`` labels as equipment faults.
    Those are traffic conditions. Faults are missing/invalid core readings or
    an inconsistent reported-versus-installed capacity pair.
    """

    rows = _rows(value)
    tolerance = max(0.0, _as_float(capacity_tolerance, 0.05))
    for row in rows:
        codes: List[str] = []
        demand_valid, demand = _nonnegative_telemetry(row.get("demand_mbps"))
        capacity_valid, capacity = _nonnegative_telemetry(row.get("capacity_mbps"))
        installed_valid, installed = _nonnegative_telemetry(
            row.get("installed_capacity_mbps", row.get("capacity_mbps"))
        )
        power_valid, power = _finite_number(row.get("baseline_power_kw"))

        if not demand_valid:
            codes.append("missing_or_invalid_demand")
        if not capacity_valid or capacity <= 0.0:
            codes.append("missing_or_invalid_reported_capacity")
        if not installed_valid or installed <= 0.0:
            codes.append("missing_or_invalid_installed_capacity")
        if not power_valid or power < 0.0:
            codes.append("missing_or_invalid_power")
        if (
            capacity_valid
            and installed_valid
            and installed > 0.0
            and abs(capacity - installed) / installed > tolerance
        ):
            codes.append("reported_capacity_mismatch")

        if not codes:
            severity = "none"
            reason = "none"
        elif any(
            code in codes
            for code in (
                "missing_or_invalid_demand",
                "missing_or_invalid_installed_capacity",
            )
        ):
            severity = "critical"
            reason = "; ".join(codes)
        else:
            severity = "high"
            reason = "; ".join(codes)

        row["fault_detected"] = bool(codes)
        row["fault_flag"] = int(bool(codes))
        row["fault_codes"] = codes
        row["fault_reason"] = reason
        row["fault_severity"] = severity
        row["fault_validation_source"] = "telemetry_rules"
    return _copy_with_rows(value, rows)


def _tower_key(row: Mapping[str, Any], fallback_index: int = 0) -> str:
    tower_id = row.get("tower_id")
    if tower_id not in (None, ""):
        return str(tower_id)
    return "%s::%s::%s" % (
        row.get("city", "city-0"),
        row.get("city_index", 0),
        row.get("tower_index", fallback_index),
    )


def _row_timestamp(row: Mapping[str, Any], fallback_index: int) -> datetime:
    return _parse_timestamp(row.get("timestamp"), datetime(2025, 1, 6) + timedelta(hours=fallback_index))


def _interval_minutes(row: Mapping[str, Any], default: int = DEFAULT_INTERVAL_MINUTES) -> int:
    value = row.get("interval_minutes", row.get("sampling_interval_minutes"))
    valid, number = _finite_number(value)
    if valid and number > 0.0:
        return max(1, int(round(number)))
    hours_valid, hours = _finite_number(row.get("interval_hours"))
    if hours_valid and hours > 0.0:
        return max(1, int(round(hours * 60.0)))
    return default


def _prepare_causal_rows(
    value: Table,
    *,
    return_table: bool = True,
) -> Tuple[Optional[Table], List[Row]]:
    """Add strict-prior per-tower features without changing caller row order."""

    rows = _rows(value)
    ordered = sorted(
        enumerate(rows),
        key=lambda pair: (_tower_key(pair[1], pair[0]), _row_timestamp(pair[1], pair[0]), pair[0]),
    )
    histories: Dict[str, Dict[str, Any]] = {}
    for original_index, row in ordered:
        tower = _tower_key(row, original_index)
        timestamp = _row_timestamp(row, original_index)
        state = histories.setdefault(
            tower,
            {"demands": [], "users": [], "timestamps": [], "last_timestamp": None},
        )
        # If malformed/caller-supplied data contains duplicate observations at
        # one tower/timestamp, freeze the prior snapshot for that timestamp;
        # no row at t may become a lag for another row at the same t.
        if state.get("feature_timestamp") != timestamp:
            state["feature_timestamp"] = timestamp
            state["feature_prior_timestamp"] = state["last_timestamp"]
            state["feature_demands"] = list(state["demands"])
            state["feature_users"] = list(state["users"])
        prior_timestamp = state.get("feature_prior_timestamp")
        interval_minutes = _interval_minutes(row)
        if prior_timestamp is not None:
            elapsed = (timestamp - prior_timestamp).total_seconds() / 60.0
            if elapsed > 0.0:
                interval_minutes = max(1, int(round(elapsed)))
        demand_history = state["feature_demands"]
        users_history = state["feature_users"]
        row["lag_demand_mbps_1"] = demand_history[-1] if demand_history else 0.0
        row["lag_demand_mbps_2"] = demand_history[-2] if len(demand_history) >= 2 else 0.0
        row["rolling_demand_mbps_3"] = (
            sum(demand_history[-3:]) / min(3, len(demand_history))
            if demand_history
            else 0.0
        )
        row["lag_active_users_1"] = users_history[-1] if users_history else 0.0
        row["rolling_active_users_3"] = (
            sum(users_history[-3:]) / min(3, len(users_history))
            if users_history
            else 0.0
        )
        row["history_count"] = len(demand_history)

        row["timestamp"] = timestamp
        row["hour"] = int(_as_float(row.get("hour"), timestamp.hour))
        row["day_of_week"] = int(
            _as_float(row.get("day_of_week"), timestamp.weekday())
        )
        row["is_weekend"] = int(row["day_of_week"] >= 5)
        tod_angle = 2.0 * math.pi * (row["hour"] / 24.0)
        dow_angle = 2.0 * math.pi * (row["day_of_week"] / 7.0)
        row["time_of_day_sin"] = math.sin(tod_angle)
        row["time_of_day_cos"] = math.cos(tod_angle)
        row["day_of_week_sin"] = math.sin(dow_angle)
        row["day_of_week_cos"] = math.cos(dow_angle)
        row.setdefault("event_flag", 0)
        row.setdefault("event_intensity", 0.0)
        row.setdefault("temperature_c", 20.0)
        row.setdefault("humidity_pct", 60.0)
        row.setdefault("rainfall_mm", 0.0)
        row.setdefault("wind_speed_kph", 10.0)
        row.setdefault("weather_load_index", 1.0)
        installed = row.get("installed_capacity_mbps", row.get("capacity_mbps"))
        installed_valid, installed_number = _nonnegative_telemetry(installed)
        if not installed_valid or installed_number <= 0.0:
            installed_number = max(1.0, _as_float(row.get("demand_mbps"), 1.0) * 1.5)
        row["installed_capacity_mbps"] = installed_number
        row.setdefault("capacity_mbps", installed_number)
        row.setdefault("installed_capacity", installed_number)
        row.setdefault("traffic_mbps", row.get("demand_mbps"))
        row.setdefault("users", row.get("active_users"))
        row.setdefault("city_index", 0)
        row.setdefault("tower_index", 0)
        for feature in FEATURE_COLUMNS:
            row[feature] = _as_float(row.get(feature), 0.0)

        row["interval_minutes"] = interval_minutes
        row["sampling_interval_minutes"] = interval_minutes
        row["interval_hours"] = interval_minutes / 60.0
        row["forecast_origin"] = (
            prior_timestamp
            if prior_timestamp is not None
            else timestamp - timedelta(minutes=interval_minutes)
        )
        row["forecast_horizon_minutes"] = interval_minutes

        demand_valid, observed_demand = _nonnegative_telemetry(row.get("demand_mbps"))
        users_valid, observed_users = _nonnegative_telemetry(row.get("active_users"))
        if demand_valid:
            state["demands"].append(observed_demand)
        if users_valid:
            state["users"].append(observed_users)
        state["timestamps"].append(timestamp)
        state["last_timestamp"] = timestamp

    return (_copy_with_rows(value, rows) if return_table else None), rows


# Kept as a private compatibility name for code that imported the old helper.
def _ensure_features(value: Table) -> Tuple[Table, List[Row]]:
    return _prepare_causal_rows(value)


def _solve_linear_system(matrix: List[List[float]], vector: List[float]) -> List[float]:
    """Solve a small dense system with deterministic partial-pivot elimination."""

    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1.0e-12:
            augmented[column][column] += DEFAULT_RIDGE_ALPHA
            pivot = column
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        if abs(divisor) < 1.0e-20:
            divisor = 1.0e-20
            augmented[column][column] = divisor
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) < 1.0e-15:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][size] for row in range(size)]


def _ols_fit(
    rows: Sequence[Row], target_column: str
) -> Tuple[List[float], List[float], List[float]]:
    means: List[float] = []
    scales: List[float] = []
    for feature in FEATURE_COLUMNS:
        values = [_as_float(row.get(feature), 0.0) for row in rows]
        mean = sum(values) / max(1, len(values))
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values))
        scales.append(math.sqrt(variance) if variance > 1.0e-12 else 1.0)
        means.append(mean)

    design: List[List[float]] = []
    targets: List[float] = []
    for row in rows:
        design.append(
            [1.0]
            + [
                (_as_float(row.get(feature), 0.0) - means[index]) / scales[index]
                for index, feature in enumerate(FEATURE_COLUMNS)
            ]
        )
        targets.append(max(0.0, _as_float(row.get(target_column), 0.0)))

    width = len(FEATURE_COLUMNS) + 1
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    rhs = [0.0 for _ in range(width)]
    for design_row, target in zip(design, targets):
        for left in range(width):
            rhs[left] += design_row[left] * target
            for right in range(width):
                normal[left][right] += design_row[left] * design_row[right]
    # This is ridge-guarded OLS: alpha is only a numerical regularisation
    # term for a potentially singular normal equation, not a learned model
    # hyperparameter.  It is stored in the model metadata for honesty.
    for index in range(1, width):
        normal[index][index] += DEFAULT_RIDGE_ALPHA
    return _solve_linear_system(normal, rhs), means, scales


def _predict_row(
    row: Mapping[str, Any],
    coefficients: Sequence[float],
    means: Sequence[float],
    scales: Sequence[float],
) -> float:
    result = coefficients[0] if coefficients else 0.0
    for index, feature in enumerate(FEATURE_COLUMNS):
        value = (_as_float(row.get(feature), 0.0) - means[index]) / scales[index]
        if index + 1 < len(coefficients):
            result += coefficients[index + 1] * value
    return max(0.0, result)


def _metric_values(actual: Sequence[float], predicted: Sequence[float]) -> Dict[str, float]:
    if not actual:
        return {"mae": 0.0, "rmse": 0.0, "count": 0}
    errors = [prediction - observed for observed, prediction in zip(actual, predicted)]
    return {
        "mae": sum(abs(error) for error in errors) / len(errors),
        "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "count": len(errors),
    }


def _robust_center_scale(values: Sequence[float]) -> Tuple[float, float]:
    if not values:
        return 0.0, DEFAULT_CALIBRATION_FLOOR_Mbps
    center = statistics.median(values)
    mad = statistics.median([abs(value - center) for value in values])
    robust_scale = 1.4826 * mad
    standard_scale = statistics.pstdev(values) if len(values) > 1 else 0.0
    scale = max(DEFAULT_CALIBRATION_FLOOR_Mbps, robust_scale, standard_scale)
    return center, scale


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


@dataclass
class ForecasterModel:
    """Causal ridge-guarded OLS model and train-only calibration metadata."""

    target_column: str
    feature_columns: Tuple[str, ...]
    coefficients: Tuple[float, ...]
    feature_means: Tuple[float, ...]
    feature_scales: Tuple[float, ...]
    split_timestamp: Optional[datetime]
    forecast_horizon_minutes: int
    min_history: int
    train_rows: int
    test_rows: int
    warmup_rows: int
    train_period_rows: int
    train_metrics: Dict[str, float]
    test_metrics: Dict[str, float]
    warmup_metrics: Dict[str, float]
    calibration_center_mbps: float
    calibration_scale_mbps: float
    calibration_count: int
    regularization_alpha: float = DEFAULT_RIDGE_ALPHA

    def predict_row(self, row: Mapping[str, Any]) -> float:
        return _predict_row(row, self.coefficients, self.feature_means, self.feature_scales)

    def predict(self, value: Table) -> List[float]:
        _, rows = _prepare_causal_rows(value, return_table=False)
        return [self.predict_row(row) for row in rows]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_column": self.target_column,
            "feature_columns": list(self.feature_columns),
            "coefficients": list(self.coefficients),
            "feature_means": list(self.feature_means),
            "feature_scales": list(self.feature_scales),
            "split_timestamp": _iso_or_none(self.split_timestamp),
            "forecast_horizon_minutes": self.forecast_horizon_minutes,
            "min_history": self.min_history,
            "train_rows": self.train_rows,
            "test_rows": self.test_rows,
            "split_index": self.split_index,
            "warmup_rows": self.warmup_rows,
            "train_period_rows": self.train_period_rows,
            "train_metrics": dict(self.train_metrics),
            "test_metrics": dict(self.test_metrics),
            "warmup_metrics": dict(self.warmup_metrics),
            "calibration_center_mbps": self.calibration_center_mbps,
            "calibration_scale_mbps": self.calibration_scale_mbps,
            "calibration_count": self.calibration_count,
            "calibration": self.calibration,
            "regularization_alpha": self.regularization_alpha,
        }

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "features": self.feature_columns,
            "coef": self.coefficients,
            "coefficients": self.coefficients,
            "train_mae": self.train_metrics.get("mae", 0.0),
            "train_rmse": self.train_metrics.get("rmse", 0.0),
            "test_mae": self.test_metrics.get("mae", 0.0),
            "test_rmse": self.test_metrics.get("rmse", 0.0),
            "calibration_center": self.calibration_center_mbps,
            "calibration_scale": self.calibration_scale_mbps,
        }
        if key in aliases:
            return aliases[key]
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (AttributeError, KeyError):
            return default

    @property
    def calibration(self) -> Dict[str, Any]:
        """Dict-shaped view for dashboard adapters and serialized notebooks."""

        return {
            "center": self.calibration_center_mbps,
            "scale": self.calibration_scale_mbps,
            "center_mbps": self.calibration_center_mbps,
            "scale_mbps": self.calibration_scale_mbps,
            "count": self.calibration_count,
            "source": "train_history",
        }

    @property
    def split_index(self) -> int:
        """Legacy row-count alias for callers that used the earlier model."""

        return self.train_period_rows


def _model_value(model: Any, name: str, default: Any = None) -> Any:
    if isinstance(model, Mapping):
        return model.get(name, default)
    return getattr(model, name, default)


def _model_split_timestamp(model: Any) -> Optional[datetime]:
    value = _model_value(model, "split_timestamp")
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if value is not None:
        return _parse_timestamp(value, datetime(2025, 1, 6))
    return None


def _model_predict(model: Any, row: Mapping[str, Any]) -> float:
    if hasattr(model, "predict_row"):
        return max(0.0, _as_float(model.predict_row(row), 0.0))
    if isinstance(model, Mapping):
        coefficients = model.get("coefficients", model.get("coef", ()))
        means = model.get(
            "feature_means", model.get("means", (0.0,) * len(FEATURE_COLUMNS))
        )
        scales = model.get(
            "feature_scales", model.get("scales", (1.0,) * len(FEATURE_COLUMNS))
        )
        return _predict_row(row, coefficients, means, scales)
    raise TypeError("model must be a ForecasterModel or compatible mapping")


def _evaluation_label(row: Mapping[str, Any], model: Any) -> str:
    if _as_float(row.get("history_count"), 0.0) < _as_float(
        _model_value(model, "min_history", DEFAULT_MIN_HISTORY), DEFAULT_MIN_HISTORY
    ):
        return "warmup"
    split_timestamp = _model_split_timestamp(model)
    if split_timestamp is not None and _row_timestamp(row, 0) >= split_timestamp:
        return "test"
    return "train"


def train_forecaster(
    value: Table,
    train_ratio: float = 0.8,
    target_column: str = "demand_mbps",
    split_ratio: Optional[float] = None,
) -> ForecasterModel:
    """Fit causal ordinary least squares on a chronological train period.

    The current row is not used as a feature.  The timestamp boundary is
    selected before model fitting, and calibration residuals are calculated
    only from non-warmup rows in that train period.  Test metrics therefore
    remain a real held-out, future-period estimate.
    """

    _, rows = _prepare_causal_rows(value, return_table=False)
    if not rows:
        raise ValueError("cannot train a forecaster on an empty dataset")
    for row in rows:
        if target_column not in row:
            raise ValueError("dataset is missing target column %r" % target_column)

    ordered = sorted(
        rows,
        key=lambda row: (_row_timestamp(row, 0), _tower_key(row)),
    )
    unique_times = sorted({_row_timestamp(row, 0) for row in ordered})
    if split_ratio is not None:
        train_ratio = split_ratio
    ratio = _clamp(_as_float(train_ratio, 0.8), 0.5, 0.98)
    split_timestamp: Optional[datetime] = None
    if len(unique_times) > 1:
        split_index = max(1, min(len(unique_times) - 1, int(len(unique_times) * ratio)))
        split_timestamp = unique_times[split_index]

    for row in rows:
        timestamp = _row_timestamp(row, 0)
        row["evaluation_split"] = (
            "test"
            if split_timestamp is not None and timestamp >= split_timestamp
            else "train"
        )
        if _as_float(row.get("history_count"), 0.0) < DEFAULT_MIN_HISTORY:
            row["evaluation_split"] = "warmup"

    train_period = [row for row in rows if row["evaluation_split"] == "train"]
    test_period = [row for row in rows if row["evaluation_split"] == "test"]
    warmup_period = [row for row in rows if row["evaluation_split"] == "warmup"]
    fit_rows = [
        row for row in train_period if _as_float(row.get("history_count"), 0.0) >= DEFAULT_MIN_HISTORY
    ]
    if not fit_rows:
        # Tiny one-period inputs have no usable warm history.  This fallback is
        # explicit and lets a dashboard still get a deterministic baseline.
        fit_rows = train_period or warmup_period
    if not fit_rows:
        raise ValueError("dataset has no rows available for model fitting")

    coefficients, means, scales = _ols_fit(fit_rows, target_column)

    def actuals_and_predictions(subset: Sequence[Row]) -> Tuple[List[float], List[float]]:
        actuals = [max(0.0, _as_float(row.get(target_column), 0.0)) for row in subset]
        predictions = [_predict_row(row, coefficients, means, scales) for row in subset]
        return actuals, predictions

    train_actual, train_predicted = actuals_and_predictions(fit_rows)
    test_actual, test_predicted = actuals_and_predictions(test_period)
    warmup_actual, warmup_predicted = actuals_and_predictions(warmup_period)
    calibration_residuals = [
        actual - prediction for actual, prediction in zip(train_actual, train_predicted)
    ]
    calibration_center, calibration_scale = _robust_center_scale(calibration_residuals)
    horizon_values = [
        _interval_minutes(row) for row in fit_rows if _interval_minutes(row) > 0
    ]
    horizon = int(round(statistics.median(horizon_values))) if horizon_values else DEFAULT_INTERVAL_MINUTES

    return ForecasterModel(
        target_column=target_column,
        feature_columns=FEATURE_COLUMNS,
        coefficients=tuple(coefficients),
        feature_means=tuple(means),
        feature_scales=tuple(scales),
        split_timestamp=split_timestamp,
        forecast_horizon_minutes=horizon,
        min_history=DEFAULT_MIN_HISTORY,
        train_rows=len(fit_rows),
        test_rows=len(test_period),
        warmup_rows=len(warmup_period),
        train_period_rows=len(train_period),
        train_metrics=_metric_values(train_actual, train_predicted),
        test_metrics=_metric_values(test_actual, test_predicted),
        warmup_metrics=_metric_values(warmup_actual, warmup_predicted),
        calibration_center_mbps=calibration_center,
        calibration_scale_mbps=calibration_scale,
        calibration_count=len(calibration_residuals),
    )


def add_predictions(value: Table, model: Any) -> Table:
    """Attach causal forecasts, split labels, origins, and train calibration."""

    _, rows = _prepare_causal_rows(value, return_table=False)
    center = _as_float(
        _model_value(model, "calibration_center_mbps", _model_value(model, "calibration_center", 0.0)),
        0.0,
    )
    scale = max(
        DEFAULT_CALIBRATION_FLOOR_Mbps,
        _as_float(
            _model_value(model, "calibration_scale_mbps", _model_value(model, "calibration_scale", 1.0)),
            1.0,
        ),
    )
    for row in rows:
        prediction = _model_predict(model, row)
        actual_valid, actual = _nonnegative_telemetry(row.get("demand_mbps"))
        row["predicted_demand_mbps"] = prediction
        row["forecast_demand_mbps"] = prediction
        row["forecast_valid"] = actual_valid
        row["residual_mbps"] = actual - prediction if actual_valid else 0.0
        row["forecast_residual_mbps"] = row["residual_mbps"]
        row["evaluation_split"] = _evaluation_label(row, model)
        row["calibration_center_mbps"] = center
        row["calibration_scale_mbps"] = scale
        row["calibration_center"] = center
        row["calibration_scale"] = scale
        row["residual_center_mbps"] = center
        row["residual_scale_mbps"] = scale
        row["forecast_residual_center_mbps"] = center
        row["forecast_residual_scale_mbps"] = scale
        row["calibration_source"] = "train_history"
        row["calibration_split"] = "train"
        row.setdefault(
            "forecast_horizon_minutes",
            _model_value(model, "forecast_horizon_minutes", DEFAULT_INTERVAL_MINUTES),
        )
    return _copy_with_rows(value, rows)


def _causal_sorted_entries(rows: Sequence[Row]) -> List[Tuple[int, Row, datetime, str]]:
    return [
        (index, row, _row_timestamp(row, index), _tower_key(row, index))
        for index, row in sorted(
            enumerate(rows),
            key=lambda pair: (
                _tower_key(pair[1], pair[0]),
                _row_timestamp(pair[1], pair[0]),
                pair[0],
            ),
        )
    ]


def detect_spikes(
    value: Table,
    threshold: float = 3.5,
    minimum_residual_mbps: float = 8.0,
) -> Table:
    """Detect causal upward residual/rate spikes.

    A row uses its attached train-only calibration.  If no calibration is
    present, center/scale are estimated from residuals observed earlier for the
    same tower only.  No median, standard deviation, or threshold is computed
    from future rows in the input batch.  Downward residuals never become
    upward spikes.
    """

    rows = _rows(value)
    cutoff = max(1.0, _as_float(threshold, 3.5))
    minimum = max(0.0, _as_float(minimum_residual_mbps, 8.0))
    prior_by_tower: Dict[str, Dict[str, Any]] = {}
    for _, row, _, tower in _causal_sorted_entries(rows):
        history = prior_by_tower.setdefault(
            tower, {"residuals": [], "last_demand": None}
        )
        timestamp = _row_timestamp(row, 0)
        if history.get("feature_timestamp") != timestamp:
            history["feature_timestamp"] = timestamp
            history["feature_last_demand"] = history["last_demand"]
            history["feature_residuals"] = list(history["residuals"])
        prior_residuals = history["feature_residuals"]
        predicted_value = row.get(
            "predicted_demand_mbps", row.get("forecast_demand_mbps")
        )
        predicted_valid, predicted = _nonnegative_telemetry(predicted_value)
        demand_valid, demand = _nonnegative_telemetry(row.get("demand_mbps"))
        if demand_valid and predicted_valid:
            residual = demand - predicted
        elif "residual_mbps" in row:
            residual = _as_float(row.get("residual_mbps"), 0.0)
        else:
            residual = 0.0

        calibration_valid, calibration_center = _finite_number(
            row.get(
                "calibration_center_mbps",
                row.get("residual_center_mbps", row.get("forecast_residual_center_mbps")),
            )
        )
        scale_valid, calibration_scale = _finite_number(
            row.get(
                "calibration_scale_mbps",
                row.get("residual_scale_mbps", row.get("forecast_residual_scale_mbps")),
            )
        )
        if not calibration_valid:
            calibration_center = (
                statistics.median(prior_residuals)
                if prior_residuals
                else 0.0
            )
        if not scale_valid or calibration_scale <= 0.0:
            _, calibration_scale = _robust_center_scale(prior_residuals)
        calibration_scale = max(DEFAULT_CALIBRATION_FLOOR_Mbps, calibration_scale)

        prior_demand = history["feature_last_demand"]
        if prior_demand is None or prior_demand <= 0.0:
            rate_growth = 0.0
        else:
            rate_growth = (demand - prior_demand) / prior_demand if demand_valid else 0.0
        # Keep the semantic direction explicit: a negative residual is not a
        # rising-demand residual merely because the calibrated center is more
        # negative.  Calibration affects the score/threshold, while the
        # residual route still requires an actually positive residual.
        positive_residual = max(0.0, residual)
        calibrated_excess = max(0.0, residual - calibration_center) if residual > 0.0 else 0.0
        residual_component = calibrated_excess / calibration_scale
        rate_component = max(0.0, rate_growth)
        score = max(residual_component, rate_component)
        # A positive calibrated residual is sufficient evidence on its own;
        # positive causal rate growth is an additional route to a spike.  A
        # downward residual can satisfy neither route.
        residual_trigger = (
            residual > 0.0
            and positive_residual >= minimum
            and residual_component >= cutoff
        )
        rate_trigger = prior_demand is not None and rate_component >= max(0.75, cutoff * 0.25)
        upward = residual_trigger or rate_component > 0.0
        is_spike = bool(
            (demand_valid and predicted_valid) or "residual_mbps" in row
        ) and bool(
            upward and (residual_trigger or rate_trigger)
        )

        row["residual_mbps"] = residual
        row["forecast_residual_mbps"] = residual
        row["calibration_center_mbps"] = calibration_center
        row["calibration_scale_mbps"] = calibration_scale
        row["residual_zscore"] = score
        row["spike_score"] = score
        row["positive_residual_mbps"] = positive_residual
        row["rate_growth"] = rate_growth
        row["rate_growth_pct"] = rate_growth * 100.0
        row["upward_spike"] = is_spike
        row["is_spike"] = is_spike
        row["spike_flag"] = int(is_spike)
        row["spike_detected"] = is_spike

        if demand_valid:
            history["last_demand"] = demand
        if demand_valid and predicted_valid:
            history["residuals"].append(residual)
            # Bound the causal fallback memory; attached model calibration is
            # still used whenever available.
            if len(history["residuals"]) > 24:
                history["residuals"] = history["residuals"][-24:]
    return _copy_with_rows(value, rows)


def _mode_fractions(coverage_floor_fraction: float) -> Dict[str, float]:
    floor = _clamp(_as_float(coverage_floor_fraction, 0.12), 0.01, 0.40)
    return {
        "sleep": floor,
        "eco": max(floor + 0.05, 0.45),
        "normal": max(floor + 0.10, 0.72),
        "boost": 1.0,
    }


def _scheduled_mode(hour: int) -> str:
    """Documented fixed benchmark: sleep 00-05, eco 06-07 and 23, normal day."""

    if 0 <= hour <= 5:
        return "sleep"
    if hour in (6, 7, 23):
        return "eco"
    return "normal"


def _required_mode(required: float, installed: float, fractions: Mapping[str, float]) -> str:
    if installed <= 0.0:
        return "boost"
    for mode in _MODE_ORDER:
        if required <= installed * fractions[mode] + 1.0e-9:
            return mode
    return "boost"


def simulate_controller(
    value: Table,
    safety_margin: float = 0.15,
    minimum_active_fraction: float = 0.20,
    downgrade_dwell_intervals: int = 3,
    hysteresis: float = 0.10,
    coverage_floor_fraction: Optional[float] = None,
) -> Table:
    """Run a stateful per-tower controller and fixed schedule benchmark.

    ``sleep`` means optional carriers are asleep while the conceptual coverage
    layer remains at a nonzero floor.  ``eco``, ``normal`` and ``boost`` have
    progressively larger active capacity fractions; ``boost`` is exactly the
    immutable installed capacity.  Upgrades/spike responses are immediate.
    Downgrades move at most one mode after a three-observation dwell and use a
    10% hysteresis band by default.  Invalid telemetry is fail-safe ``boost``.
    """

    rows = _rows(value)
    margin = max(0.0, _as_float(safety_margin, 0.15))
    requested_floor = (
        minimum_active_fraction
        if coverage_floor_fraction is None
        else coverage_floor_fraction
    )
    fractions = _mode_fractions(requested_floor)
    dwell = max(1, int(_as_float(downgrade_dwell_intervals, 3)))
    hysteresis_fraction = _clamp(_as_float(hysteresis, 0.10), 0.0, 0.50)
    states: Dict[str, Dict[str, Any]] = {}

    for _, row, _, tower in _causal_sorted_entries(rows):
        state = states.setdefault(
            tower,
            {
                "mode": "sleep",
                "mode_age": 0,
                "observations": 0,
                "installed_capacity": None,
                "previous_timestamp": None,
            },
        )
        current_mode = str(state["mode"])
        if state["observations"] > 0:
            state["mode_age"] += 1

        has_capacity_observation = "capacity_mbps" in row
        raw_capacity = row.get("capacity_mbps", row.get("installed_capacity_mbps"))
        raw_capacity_valid, capacity_value = _nonnegative_telemetry(raw_capacity)
        # Installed capacity is immutable metadata; a missing capacity
        # observation is still a telemetry fault and therefore fail-safe.
        capacity_valid = has_capacity_observation and raw_capacity_valid
        metadata_valid, metadata_capacity = _nonnegative_telemetry(
            row.get("installed_capacity_mbps", raw_capacity)
        )
        if (
            state["installed_capacity"] is None
            and metadata_valid
            and metadata_capacity > 0.0
        ):
            state["installed_capacity"] = metadata_capacity
        installed = _as_float(state["installed_capacity"], 0.0)
        demand_valid, demand = _nonnegative_telemetry(row.get("demand_mbps"))
        forecast_value = row.get(
            "predicted_demand_mbps", row.get("forecast_demand_mbps", row.get("demand_mbps"))
        )
        forecast_valid, forecast = _nonnegative_telemetry(forecast_value)
        baseline_valid, baseline_power = _finite_number(row.get("baseline_power_kw"))
        telemetry_valid = bool(
            capacity_valid
            and capacity_value > 0.0
            and metadata_valid
            and metadata_capacity > 0.0
            and demand_valid
            and forecast_valid
            and baseline_valid
            and baseline_power >= 0.0
        )
        detected_fault = bool(row.get("fault_detected", False))
        fail_safe = not telemetry_valid or detected_fault
        if not capacity_valid and installed > 0.0:
            # Keep immutable installed metadata from a previous observation,
            # but still fail-safe for this invalid observation.
            installed = _as_float(state["installed_capacity"], 0.0)
        elif not capacity_valid:
            installed = 0.0
        if not baseline_valid or baseline_power < 0.0:
            baseline_power = 8.0 + installed * 0.018
        if not demand_valid:
            demand = 0.0
        if not forecast_valid:
            forecast = demand

        upward_spike = bool(
            row.get("upward_spike", row.get("is_spike", row.get("spike_flag", False)))
        )
        known_event = bool(_as_float(row.get("event_flag"), 0.0) > 0.0)
        required = max(demand, forecast) * (1.0 + margin)
        desired_mode = (
            "boost"
            if fail_safe or upward_spike or known_event
            else _required_mode(required, installed, fractions)
        )
        current_rank = _MODE_ORDER.index(current_mode)
        desired_rank = _MODE_ORDER.index(desired_mode)
        selected_mode = current_mode
        mode_switch = False
        if desired_rank > current_rank:
            selected_mode = desired_mode
            mode_switch = selected_mode != current_mode
            state["mode_age"] = 0
        elif desired_rank < current_rank:
            lower_mode = _MODE_ORDER[current_rank - 1]
            lower_capacity = installed * fractions[lower_mode]
            can_downgrade = (
                state["mode_age"] >= dwell
                and required <= lower_capacity * (1.0 - hysteresis_fraction) + 1.0e-9
            )
            if can_downgrade:
                selected_mode = lower_mode
                mode_switch = selected_mode != current_mode
                state["mode_age"] = 0

        if selected_mode != current_mode:
            mode_switch = True
        if current_mode == "sleep" and state["mode_age"] == 0 and not mode_switch:
            # The initial state is not a switch and must not be penalised in
            # mode-switch metrics.
            state["mode_age"] = 1
        state["mode"] = selected_mode
        state["observations"] += 1
        active_fraction = fractions[selected_mode]
        active_capacity = installed * active_fraction
        if selected_mode == "boost":
            active_capacity = installed
        active_capacity = _clamp(active_capacity, 0.0, installed)
        served = min(demand, active_capacity)
        unmet = max(0.0, demand - served)
        overload = max(0.0, demand - installed)
        safety_violation = bool(unmet > 1.0e-9 or active_capacity > installed + 1.0e-9)

        # Separate fixed idle and mode-dependent capacity power.  The boost
        # ledger equals always-on; lower modes can never exceed it.
        idle_power = baseline_power * 0.34
        capacity_power = max(0.0, baseline_power - idle_power)
        adaptive_power = min(
            baseline_power,
            idle_power + capacity_power * active_fraction,
        )
        interval_hours = max(0.0, _as_float(row.get("interval_hours"), 1.0))
        always_on_energy = max(0.0, baseline_power * interval_hours)
        adaptive_energy = max(0.0, adaptive_power * interval_hours)

        timestamp = _row_timestamp(row, 0)
        scheduled = _scheduled_mode(timestamp.hour)
        scheduled_fraction = fractions[scheduled]
        scheduled_capacity = installed * scheduled_fraction
        scheduled_served = min(demand, scheduled_capacity)
        scheduled_unmet = max(0.0, demand - scheduled_served)
        scheduled_power = min(
            baseline_power,
            idle_power + capacity_power * scheduled_fraction,
        )
        scheduled_energy = max(0.0, scheduled_power * interval_hours)

        if detected_fault:
            controller_reason = "fail_safe_detected_telemetry_fault"
        elif fail_safe:
            controller_reason = "fail_safe_invalid_or_missing_telemetry"
        elif upward_spike:
            controller_reason = "upward_demand_spike"
        elif known_event:
            controller_reason = "scheduled_event_headroom"
        elif selected_mode != current_mode and _MODE_ORDER.index(selected_mode) > current_rank:
            controller_reason = "demand_rise_capacity_restore"
        elif selected_mode != current_mode:
            controller_reason = "low_demand_after_dwell"
        else:
            controller_reason = "hold_mode_with_hysteresis"

        row["installed_capacity_mbps"] = installed
        row["always_on_capacity_mbps"] = installed
        row["coverage_floor_mbps"] = installed * fractions["sleep"]
        row["active_capacity_mbps"] = active_capacity
        row["adaptive_capacity_mbps"] = active_capacity
        row["mode_capacity_fraction"] = active_fraction
        row["served_demand_mbps"] = served
        row["unmet_demand_mbps"] = unmet
        row["unserved_demand_mbps"] = unmet
        row["overload_mbps"] = overload
        row["capacity_headroom_mbps"] = max(0.0, installed - active_capacity)
        row["controller_mode"] = selected_mode
        row["mode"] = selected_mode
        row["controller_reason"] = controller_reason
        row["coverage_layer_protected"] = bool(installed > 0.0 and active_capacity >= row["coverage_floor_mbps"] - 1.0e-9)
        row["mode_switch"] = mode_switch
        row["mode_switch_flag"] = int(mode_switch)
        row["mode_age_intervals"] = state["mode_age"]
        row["telemetry_valid"] = telemetry_valid
        row["telemetry_invalid"] = not telemetry_valid
        row["fault_detected"] = detected_fault
        row["fail_safe_boost"] = fail_safe
        row["safety_violation"] = safety_violation
        row["safety_violation_flag"] = int(safety_violation)
        row["capacity_violation"] = safety_violation
        row["always_on_power_kw"] = baseline_power
        row["idle_power_kw"] = idle_power
        row["capacity_power_kw"] = capacity_power
        row["adaptive_power_kw"] = adaptive_power
        row["always_on_energy_kwh"] = always_on_energy
        row["adaptive_energy_kwh"] = adaptive_energy
        row["baseline_energy_kwh"] = always_on_energy
        row["energy_savings_kwh"] = max(0.0, always_on_energy - adaptive_energy)
        row["scheduled_mode"] = scheduled
        row["scheduled_capacity_mbps"] = scheduled_capacity
        row["scheduled_served_demand_mbps"] = scheduled_served
        row["scheduled_unmet_demand_mbps"] = scheduled_unmet
        row["scheduled_power_kw"] = scheduled_power
        row["scheduled_energy_kwh"] = scheduled_energy
        row["scheduled_violation"] = bool(scheduled_unmet > 1.0e-9)
        row["scheduled_violation_flag"] = int(scheduled_unmet > 1.0e-9)

    return _copy_with_rows(value, rows)


def _coverage_site_position(
    row: Mapping[str, Any],
    fallback_index: int,
    tower_count: int,
) -> Tuple[float, float]:
    valid_x, x = _finite_number(row.get("site_x"))
    valid_y, y = _finite_number(row.get("site_y"))
    if valid_x and valid_y and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
        return x, y
    tower_index = int(_as_float(row.get("tower_index"), fallback_index))
    return _synthetic_site_position(tower_index, tower_count)


def build_coverage_grid(
    value: Table,
    grid_rows: int = 5,
    grid_cols: int = 5,
) -> Dict[str, Any]:
    """Build a deterministic city-local cell topology.

    The grid is a visualization and capacity proxy. Coordinates are normalized
    city-local positions, not latitude/longitude and not a radio-propagation
    calculation.
    """

    rows = _read_rows(value)
    rows_count = max(1, min(12, int(_as_float(grid_rows, 5))))
    cols_count = max(1, min(12, int(_as_float(grid_cols, 5))))
    towers_by_city: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        city = str(row.get("city", "Unknown"))
        tower_id = str(row.get("tower_id", f"{city}-T01"))
        towers_by_city.setdefault(city, {}).setdefault(tower_id, row)

    cities: Dict[str, Dict[str, Any]] = {}
    for city, tower_map in sorted(towers_by_city.items()):
        tower_rows = sorted(
            tower_map.values(),
            key=lambda row: (
                int(_as_float(row.get("tower_index"), 0)),
                str(row.get("tower_id", "")),
            ),
        )
        sites: List[Dict[str, Any]] = []
        for index, row in enumerate(tower_rows):
            x, y = _coverage_site_position(row, index, len(tower_rows))
            sites.append(
                {
                    "tower_id": str(row.get("tower_id", f"{city}-T{index + 1:02d}")),
                    "x": float(x),
                    "y": float(y),
                }
            )

        cells: List[Dict[str, Any]] = []
        for row_index in range(rows_count):
            for col_index in range(cols_count):
                x = (col_index + 0.5) / cols_count
                y = (row_index + 0.5) / rows_count
                ranked_sites = sorted(
                    sites,
                    key=lambda site: (
                        (site["x"] - x) ** 2 + (site["y"] - y) ** 2,
                        site["tower_id"],
                    ),
                )
                primary = ranked_sites[0]["tower_id"] if ranked_sites else None
                backup = ranked_sites[1]["tower_id"] if len(ranked_sites) > 1 else None
                cells.append(
                    {
                        "cell_id": f"{city}-C{col_index + 1:02d}-R{row_index + 1:02d}",
                        "row": row_index,
                        "col": col_index,
                        "x": float(x),
                        "y": float(y),
                        "primary_tower_id": primary,
                        "backup_tower_id": backup,
                    }
                )
        cities[city] = {"sites": sites, "cells": cells}

    return {
        "mode": "synthetic_grid_capacity_proxy",
        "grid": {
            "rows": rows_count,
            "cols": cols_count,
            "coordinate_space": "city_local_normalized",
        },
        "cities": cities,
        "limitation": (
            "Synthetic cell availability only; this does not estimate RF propagation, "
            "signal strength, geographic radius, subscriber reach, or an SLA."
        ),
    }


def _coverage_row_is_faulted(row: Optional[Mapping[str, Any]]) -> bool:
    if row is None:
        return True
    return bool(
        row.get("fault_detected", False)
        or row.get("telemetry_invalid", False)
        or not row.get("telemetry_valid", True)
    )


def _coverage_capacity(row: Optional[Mapping[str, Any]]) -> float:
    if row is None or _coverage_row_is_faulted(row):
        return 0.0
    return max(0.0, _as_float(row.get("active_capacity_mbps"), 0.0))


def _coverage_demand(row: Optional[Mapping[str, Any]]) -> float:
    return max(0.0, _as_float(row.get("demand_mbps"), 0.0)) if row else 0.0


def estimate_coverage(
    value: Table,
    grid: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Estimate latest and interval cell availability from controller rows.

    A cell is ``covered`` when its primary tower is healthy and its allocated
    active capacity meets allocated demand. A healthy backup changes the status
    to ``at_risk_backup``. An unavailable primary or insufficient backup yields
    ``uncovered``. This is intentionally a synthetic capacity proxy.
    """

    rows = _read_rows(value)
    topology = dict(grid) if grid is not None else build_coverage_grid(rows)
    city_topology = topology.get("cities", {})
    grouped: Dict[Tuple[str, str], Dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        city = str(row.get("city", "Unknown"))
        timestamp = _row_timestamp(row, 0).isoformat()
        tower_id = str(row.get("tower_id", f"{city}-T01"))
        grouped.setdefault((city, timestamp), {})[tower_id] = row

    timeline: List[Dict[str, Any]] = []
    latest_by_city: Dict[str, Dict[str, Any]] = {}
    incident_inputs: List[Dict[str, Any]] = []

    for city in sorted(city_topology):
        static = city_topology[city]
        cells = list(static.get("cells", []))
        cell_counts: Dict[str, int] = defaultdict(int)
        for cell in cells:
            primary = cell.get("primary_tower_id")
            if primary:
                cell_counts[str(primary)] += 1
        city_intervals = sorted(
            (timestamp, tower_rows)
            for (group_city, timestamp), tower_rows in grouped.items()
            if group_city == city
        )
        for timestamp, tower_rows in city_intervals:
            evaluated: List[Dict[str, Any]] = []
            counts = {"covered": 0, "at_risk_backup": 0, "uncovered": 0}
            dispatch_cells: List[Dict[str, Any]] = []
            faulted_towers = sorted(
                tower_id
                for tower_id, row in tower_rows.items()
                if _coverage_row_is_faulted(row)
            )
            for cell in cells:
                primary_id = cell.get("primary_tower_id")
                backup_id = cell.get("backup_tower_id")
                primary = tower_rows.get(str(primary_id)) if primary_id else None
                backup = tower_rows.get(str(backup_id)) if backup_id else None
                primary_fault = _coverage_row_is_faulted(primary)
                backup_fault = _coverage_row_is_faulted(backup) if backup else True
                primary_count = max(1, cell_counts.get(str(primary_id), 1))
                backup_count = max(1, cell_counts.get(str(backup_id), 1))
                required = _coverage_demand(primary) / primary_count
                primary_available = _coverage_capacity(primary) / primary_count
                backup_required = _coverage_demand(backup) / backup_count
                backup_available = _coverage_capacity(backup) / backup_count
                primary_ok = not primary_fault and primary_available + 1.0e-9 >= required
                backup_ok = not backup_fault and backup_available + 1.0e-9 >= backup_required
                if primary_ok:
                    status = "covered"
                    serving = primary_id
                elif backup_ok:
                    status = "at_risk_backup"
                    serving = backup_id
                else:
                    status = "uncovered"
                    serving = None
                counts[status] += 1
                dispatch_needed = primary_fault or status == "uncovered"
                evaluated_cell = {
                    **cell,
                    "serving_tower_id": serving,
                    "status": status,
                    "required_capacity_mbps": float(required),
                    "available_capacity_mbps": float(primary_available),
                    "primary_fault": primary_fault,
                    "backup_fault": backup_fault,
                    "dispatch_needed": dispatch_needed,
                }
                evaluated.append(evaluated_cell)
                if dispatch_needed:
                    dispatch_cells.append(evaluated_cell)

            total = len(evaluated)
            serviceable = counts["covered"] + counts["at_risk_backup"]
            coverage_pct = 100.0 * serviceable / total if total else 100.0
            timeline_row = {
                "timestamp": timestamp,
                "city": city,
                "covered_cells": counts["covered"],
                "at_risk_cells": counts["at_risk_backup"],
                "uncovered_cells": counts["uncovered"],
                "coverage_pct": float(coverage_pct),
                "dispatch_needed_cells": len(dispatch_cells),
                "faulted_tower_ids": faulted_towers,
                # Keep the live browser grid compact: the static topology
                # already carries cell IDs/positions, so playback only needs
                # one status token per cell (c=covered, b=backup, u=uncovered).
                "cell_statuses": "".join(
                    {"covered": "c", "at_risk_backup": "b", "uncovered": "u"}[cell["status"]]
                    for cell in evaluated
                ),
            }
            timeline.append(timeline_row)
            if dispatch_cells:
                cell_ids = sorted({str(cell["cell_id"]) for cell in dispatch_cells})
                tower_ids = sorted(
                    {
                        str(tower_id)
                        for cell in dispatch_cells
                        for tower_id in (
                            cell.get("primary_tower_id"),
                            cell.get("backup_tower_id"),
                        )
                        if tower_id
                    }
                )
                has_fault = any(bool(cell.get("primary_fault")) for cell in dispatch_cells)
                trigger = "telemetry_fault" if has_fault else "capacity_gap"
                priority = "critical" if any(cell["status"] == "uncovered" for cell in dispatch_cells) else "high"
                fault_reasons = sorted(
                    {
                        str(tower_rows[tower_id].get("fault_reason", "telemetry validation failed"))
                        for tower_id in faulted_towers
                        if tower_id in tower_rows
                    }
                )
                incident_inputs.append(
                    {
                        "timestamp": timestamp,
                        "city": city,
                        "cell_ids": cell_ids,
                        "tower_ids": tower_ids,
                        "trigger": trigger,
                        "priority": priority,
                        "fault_reason": "; ".join(fault_reasons) if has_fault else "capacity gap",
                        "cell_positions": [
                            (int(cell["row"]), int(cell["col"])) for cell in dispatch_cells
                        ],
                    }
                )
            latest_by_city[city] = {
                "timestamp": timestamp,
                "covered_cells": counts["covered"],
                "at_risk_cells": counts["at_risk_backup"],
                "uncovered_cells": counts["uncovered"],
                "coverage_pct": float(coverage_pct),
                "faulted_tower_ids": faulted_towers,
                "cells": evaluated,
            }

    timeline.sort(key=lambda item: (item["timestamp"], item["city"]))
    latest_timestamp = max(
        (str(item.get("timestamp")) for item in latest_by_city.values()),
        default=None,
    )
    latest_values = [
        _as_float(item.get("coverage_pct"), 0.0) for item in latest_by_city.values()
    ]
    return {
        "mode": topology.get("mode", "synthetic_grid_capacity_proxy"),
        "grid": dict(topology.get("grid", {})),
        "cities": {
            city: {
                "sites": list(city_topology[city].get("sites", [])),
                "latest": latest_by_city.get(city, {
                    "timestamp": None,
                    "covered_cells": 0,
                    "at_risk_cells": 0,
                    "uncovered_cells": 0,
                    "coverage_pct": 100.0,
                    "faulted_tower_ids": [],
                    "cells": [],
                }),
            }
            for city in sorted(city_topology)
        },
        "timeline": timeline,
        "summary": {
            "latest_timestamp": latest_timestamp,
            "latest_coverage_pct": sum(latest_values) / len(latest_values) if latest_values else 100.0,
            "gap_intervals": sum(1 for item in timeline if item["uncovered_cells"] > 0),
            "uncovered_cell_intervals": sum(int(item["uncovered_cells"]) for item in timeline),
            "dispatch_needed_cell_intervals": sum(int(item["dispatch_needed_cells"]) for item in timeline),
        },
        "limitation": topology.get("limitation", "Synthetic coverage proxy only."),
        "_incident_inputs": incident_inputs,
    }


def summarize_fault_detection(value: Table) -> Dict[str, Any]:
    """Report synthetic fault-label metrics separately from traffic alerts."""

    rows = _read_rows(value)
    if rows and "fault_detected" not in rows[0]:
        rows = _rows(detect_faults(value))
    evaluation = [row for row in rows if row.get("evaluation_split") == "test"]
    if not evaluation:
        evaluation = [row for row in rows if row.get("evaluation_split") != "warmup"] or rows
    truth = [bool(row.get("synthetic_fault", False)) for row in evaluation]
    detected = [bool(row.get("fault_detected", False)) for row in evaluation]
    true_positive = sum(actual and predicted for actual, predicted in zip(truth, detected))
    false_positive = sum((not actual) and predicted for actual, predicted in zip(truth, detected))
    false_negative = sum(actual and (not predicted) for actual, predicted in zip(truth, detected))
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "evaluation_rows": len(evaluation),
        "evaluation_split_used": "test" if any(row.get("evaluation_split") == "test" for row in rows) else "available",
        "synthetic_fault_count": sum(truth),
        "detected_fault_count": sum(detected),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "precision_pct": precision * 100.0,
        "recall_pct": recall * 100.0,
        "f1_pct": f1 * 100.0,
        "label_source": "synthetic_fault",
    }


def simulate_rescue_dispatch(
    value: Table,
    coverage: Mapping[str, Any],
    fault_metrics: Optional[Mapping[str, Any]] = None,
    max_records: int = 12,
) -> Dict[str, Any]:
    """Simulate a deterministic technical-response queue.

    No real people, vehicles, emergency services, equipment, or external
    systems are contacted. This is only an operations-dashboard simulation.
    """

    rows = _read_rows(value)
    incidents = list(coverage.get("_incident_inputs", []))
    city_names = sorted(str(city) for city in coverage.get("cities", {}))
    teams = {
        city: [f"{city.lower().replace(' ', '-')}-team-{index:02d}" for index in range(1, 3)]
        for city in city_names
    }
    available_at: Dict[str, float] = {team: 0.0 for city in teams.values() for team in city}
    dispatches: List[Dict[str, Any]] = []
    for incident_index, incident in enumerate(
        sorted(incidents, key=lambda item: (str(item.get("timestamp")), str(item.get("city", ""))))
    ):
        city = str(incident.get("city", "Unknown"))
        city_teams = teams.get(city, [])
        if not city_teams:
            continue
        incident_time = _row_timestamp(incident, incident_index).timestamp() / 60.0
        team_id = min(city_teams, key=lambda team: (available_at[team], team))
        row_col_positions = incident.get("cell_positions", [])
        distance = max(
            (abs(row - 2) + abs(col - 2) for row, col in row_col_positions),
            default=0,
        )
        travel_minutes = 10 + 5 * int(distance)
        status = "simulated_dispatched" if available_at[team_id] <= incident_time else "simulated_queued"
        available_at[team_id] = max(available_at[team_id], incident_time) + travel_minutes + 30
        dispatches.append(
            {
                "incident_id": f"INC-{incident_index + 1:04d}",
                "timestamp": str(incident.get("timestamp")),
                "city": city,
                "cell_ids": list(incident.get("cell_ids", [])),
                "tower_ids": list(incident.get("tower_ids", [])),
                "trigger": str(incident.get("trigger", "capacity_gap")),
                "fault_reason": str(incident.get("fault_reason", "")),
                "priority": str(incident.get("priority", "high")),
                "team_id": team_id,
                "status": status,
                "travel_minutes": travel_minutes,
                "eta_minutes": travel_minutes,
                "simulation_only": True,
            }
        )

    faulted_towers = {
        str(row.get("tower_id"))
        for row in rows
        if bool(row.get("fault_detected", False))
    }
    record_limit = max(0, int(max_records))
    return {
        "simulation_only": True,
        "fault_detector": dict(fault_metrics or summarize_fault_detection(rows)),
        "summary": {
            "incident_count": len(incidents),
            "returned_dispatch_count": min(len(dispatches), record_limit),
            "affected_city_count": len({str(item.get("city")) for item in incidents}),
            "dispatch_needed_cells": sum(len(item.get("cell_ids", [])) for item in incidents),
            "faulted_tower_count": len(faulted_towers),
            "fault_event_count": sum(1 for row in rows if bool(row.get("fault_detected", False))),
            "team_count": sum(len(team_list) for team_list in teams.values()),
            "simulation_only": True,
        },
        "dispatches": dispatches[-record_limit:] if record_limit else [],
        "limitation": (
            "Deterministic technical-response queue only; it does not contact, locate, "
            "route, or instruct real people, vehicles, emergency services, network "
            "equipment, or external systems."
        ),
    }


def _sum_column(rows: Sequence[Row], key: str) -> float:
    return sum(_as_float(row.get(key), 0.0) for row in rows)


def _service_pct(rows: Sequence[Row], served_key: str, demand_key: str = "demand_mbps") -> float:
    demand_total = _sum_column(rows, demand_key)
    served_total = _sum_column(rows, served_key)
    if demand_total <= 0.0:
        return 100.0
    return _clamp(100.0 * served_total / demand_total, 0.0, 100.0)


def summarize_results(value: Table) -> Dict[str, Any]:
    """Summarize held-out adaptive/scheduled operation and legacy aliases."""

    rows = _read_rows(value)
    if rows and "adaptive_energy_kwh" not in rows[0]:
        rows = _rows(simulate_controller(value))
    if not rows:
        rows = []

    split_values = {row.get("evaluation_split") for row in rows}
    heldout = [row for row in rows if row.get("evaluation_split") == "test"]
    if heldout:
        evaluation_rows = heldout
        evaluation_split_used = "test"
    else:
        evaluation_rows = [
            row for row in rows if row.get("evaluation_split") not in ("warmup", None)
        ]
        if not evaluation_rows:
            evaluation_rows = rows
        evaluation_split_used = "available_nonwarmup" if split_values else "all"

    always_on = _sum_column(evaluation_rows, "always_on_energy_kwh")
    adaptive = _sum_column(evaluation_rows, "adaptive_energy_kwh")
    scheduled = _sum_column(evaluation_rows, "scheduled_energy_kwh")
    savings = always_on - adaptive
    savings_pct = 100.0 * savings / always_on if always_on > 0.0 else 0.0
    scheduled_savings = always_on - scheduled
    scheduled_savings_pct = (
        100.0 * scheduled_savings / always_on if always_on > 0.0 else 0.0
    )
    adaptive_service = _service_pct(evaluation_rows, "served_demand_mbps")
    scheduled_service = _service_pct(
        evaluation_rows, "scheduled_served_demand_mbps"
    )
    always_on_unmet = sum(
        max(
            0.0,
            _as_float(row.get("demand_mbps"), 0.0)
            - _as_float(row.get("installed_capacity_mbps", row.get("capacity_mbps")), 0.0),
        )
        for row in evaluation_rows
    )
    demand_total = _sum_column(evaluation_rows, "demand_mbps")
    residuals = [
        _as_float(row.get("residual_mbps"), 0.0)
        for row in evaluation_rows
        if "residual_mbps" in row
    ]
    forecast_mae = sum(abs(item) for item in residuals) / len(residuals) if residuals else 0.0
    forecast_rmse = (
        math.sqrt(sum(item * item for item in residuals) / len(residuals))
        if residuals
        else 0.0
    )
    violations = sum(1 for row in evaluation_rows if bool(row.get("safety_violation")))
    scheduled_violations = sum(
        1 for row in evaluation_rows if bool(row.get("scheduled_violation"))
    )
    telemetry_invalid = sum(1 for row in evaluation_rows if bool(row.get("telemetry_invalid")))
    overload_count = sum(1 for row in evaluation_rows if _as_float(row.get("overload_mbps"), 0.0) > 0.0)
    spikes = sum(1 for row in evaluation_rows if bool(row.get("is_spike", row.get("spike_flag", False))))
    fault_count = sum(1 for row in evaluation_rows if bool(row.get("fault_detected", False)))
    mode_switches = sum(1 for row in evaluation_rows if bool(row.get("mode_switch", False)))
    capacity_total = _sum_column(evaluation_rows, "installed_capacity_mbps")
    active_total = _sum_column(evaluation_rows, "active_capacity_mbps")
    utilization_pct = 100.0 * active_total / capacity_total if capacity_total > 0.0 else 0.0

    return {
        "rows": len(rows),
        "evaluation_rows": len(evaluation_rows),
        "evaluation_split_used": evaluation_split_used,
        "train_rows": sum(1 for row in rows if row.get("evaluation_split") == "train"),
        "test_rows": sum(1 for row in rows if row.get("evaluation_split") == "test"),
        "warmup_rows": sum(1 for row in rows if row.get("evaluation_split") == "warmup"),
        "always_on_energy_kwh": float(always_on),
        "adaptive_energy_kwh": float(adaptive),
        "scheduled_energy_kwh": float(scheduled),
        "baseline_energy_kwh": float(always_on),
        "energy_savings_kwh": float(savings),
        "energy_savings_pct": float(savings_pct),
        "energy_savings_percent": float(savings_pct),
        "scheduled_savings_kwh": float(scheduled_savings),
        "scheduled_savings_pct": float(scheduled_savings_pct),
        "savings_kwh": float(savings),
        "savings_percent": float(savings_pct),
        "always_on_energy": float(always_on),
        "adaptive_energy": float(adaptive),
        "always_on_kwh": float(always_on),
        "adaptive_kwh": float(adaptive),
        "scheduled_kwh": float(scheduled),
        "service_level_pct": float(adaptive_service),
        "demand_service_pct": float(adaptive_service),
        "adaptive_demand_service_pct": float(adaptive_service),
        "adaptive_service_level_pct": float(adaptive_service),
        "scheduled_demand_service_pct": float(scheduled_service),
        "scheduled_service_level_pct": float(scheduled_service),
        "always_on_service_level_pct": float(
            _clamp(
                100.0 * max(0.0, demand_total - always_on_unmet) / demand_total,
                0.0,
                100.0,
            )
            if demand_total > 0.0
            else 100.0
        ),
        "unmet_demand_mbps": float(_sum_column(evaluation_rows, "unmet_demand_mbps")),
        "scheduled_unmet_demand_mbps": float(
            _sum_column(evaluation_rows, "scheduled_unmet_demand_mbps")
        ),
        "always_on_unmet_demand_mbps": float(always_on_unmet),
        "safety_violation_count": int(violations),
        "safety_violations": int(violations),
        "violations": int(violations),
        "scheduled_violation_count": int(scheduled_violations),
        "telemetry_invalid_count": int(telemetry_invalid),
        "overload_count": int(overload_count),
        "fault_count": int(fault_count),
        "fault_detected_count": int(fault_count),
        "mode_switches": int(mode_switches),
        "mode_switch_count": int(mode_switches),
        "spike_count": int(spikes),
        "spikes": int(spikes),
        "forecast_mae_mbps": float(forecast_mae),
        "forecast_rmse_mbps": float(forecast_rmse),
        "average_active_capacity_pct": float(utilization_pct),
        "peak_capacity_mbps": max(
            (_as_float(row.get("installed_capacity_mbps", row.get("capacity_mbps")), 0.0) for row in evaluation_rows),
            default=0.0,
        ),
        "peak_active_capacity_mbps": max(
            (_as_float(row.get("active_capacity_mbps"), 0.0) for row in evaluation_rows),
            default=0.0,
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run an end-to-end terminal demo."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cities", type=int, default=3)
    parser.add_argument("--towers", type=int, default=DEFAULT_TOWERS_PER_CITY)
    parser.add_argument("--surge-demo", action="store_true")
    parser.add_argument("--json", action="store_true", help="print only summary JSON")
    args = parser.parse_args(argv)
    frame = generate_dataset(
        {
            "days": max(1, args.days),
            "cities": max(1, args.cities),
            "towers_per_city": max(1, args.towers),
            "surge_demo": args.surge_demo,
        },
        seed=args.seed,
    )
    model = train_forecaster(frame)
    frame = add_predictions(frame, model)
    frame = detect_spikes(frame)
    frame = simulate_controller(frame)
    summary = summarize_results(frame)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print("Telecom energy prototype")
        print(
            "rows: %d | train/test/warmup: %d/%d/%d"
            % (summary["rows"], model.train_rows, model.test_rows, model.warmup_rows)
        )
        print(
            "forecast test MAE: %.2f Mbps | residual spikes: %d"
            % (model.test_metrics["mae"], summary["spike_count"])
        )
        print(
            "always-on: %.1f kWh | adaptive: %.1f kWh | scheduled: %.1f kWh | savings: %.1f%%"
            % (
                summary["always_on_energy_kwh"],
                summary["adaptive_energy_kwh"],
                summary["scheduled_energy_kwh"],
                summary["energy_savings_pct"],
            )
        )
        print(
            "heldout service: %.2f%% | safety violations: %d | mode switches: %d"
            % (
                summary["service_level_pct"],
                summary["safety_violation_count"],
                summary["mode_switches"],
            )
        )
    return 0


__all__ = [
    "FEATURE_COLUMNS",
    "ForecasterModel",
    "add_predictions",
    "build_coverage_grid",
    "detect_faults",
    "detect_spikes",
    "estimate_coverage",
    "generate_dataset",
    "main",
    "simulate_controller",
    "simulate_rescue_dispatch",
    "summarize_results",
    "summarize_fault_detection",
    "train_forecaster",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
