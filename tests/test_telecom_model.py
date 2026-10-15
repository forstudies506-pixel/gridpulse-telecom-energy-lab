"""Behavioural tests for the causal telecom-energy prototype."""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telecom_model import (  # noqa: E402
    add_predictions,
    build_coverage_grid,
    detect_faults,
    detect_spikes,
    estimate_coverage,
    generate_dataset,
    simulate_controller,
    simulate_rescue_dispatch,
    summarize_results,
    train_forecaster,
)


def _columns(table):
    return list(table.columns) if hasattr(table, "columns") else list(table[0].keys())


def _records(table):
    if hasattr(table, "to_dict"):
        return table.to_dict(orient="records")
    return [dict(row) for row in table]


def _key(row):
    return (str(row.get("tower_id")), str(row.get("timestamp")))


def _manual_rows(demands, forecasts=None, spikes=None, capacity=100.0):
    forecasts = list(demands if forecasts is None else forecasts)
    spikes = list([False] * len(demands) if spikes is None else spikes)
    rows = []
    for index, demand in enumerate(demands):
        rows.append(
            {
                "timestamp": datetime(2025, 1, 1) + timedelta(hours=index),
                "tower_id": "manual-T01",
                "city": "Manual",
                "city_index": 0,
                "tower_index": 0,
                "demand_mbps": demand,
                "predicted_demand_mbps": forecasts[index],
                "capacity_mbps": capacity,
                "installed_capacity_mbps": capacity,
                "baseline_power_kw": 10.0,
                "interval_minutes": 60,
                "interval_hours": 1.0,
                "is_spike": spikes[index],
            }
        )
    return rows


class TelecomModelTests(unittest.TestCase):
    def test_generation_is_deterministic_and_nonnegative(self):
        config = {"days": 2, "cities": 2, "towers_per_city": 2}
        first = generate_dataset(config, seed=17)
        second = generate_dataset(config, seed=17)
        if hasattr(first, "equals"):
            self.assertTrue(first.equals(second))
        else:
            self.assertEqual(first, second)
        different = generate_dataset(config, seed=18)
        self.assertNotEqual(_records(first), _records(different))

        numeric_telemetry = {
            "city_index",
            "tower_index",
            "hour",
            "day_of_week",
            "is_weekend",
            "event_flag",
            "event_intensity",
            "temperature_c",
            "humidity_pct",
            "rainfall_mm",
            "wind_speed_kph",
            "weather_load_index",
            "active_users",
            "demand_mbps",
            "capacity_mbps",
            "installed_capacity_mbps",
            "baseline_power_kw",
            "interval_minutes",
            "interval_hours",
        }
        for row in _records(first):
            for column in numeric_telemetry.intersection(_columns(first)):
                self.assertGreaterEqual(float(row[column]), 0.0, (column, row[column]))

    def test_causal_features_have_no_same_time_user_leak_and_holdout_is_chronological(self):
        frame = generate_dataset(
            {"days": 7, "cities": 1, "towers_per_city": 1}, seed=11
        )
        model = train_forecaster(frame)
        self.assertNotIn("active_users", model.feature_columns)
        self.assertNotIn("demand_mbps", model.feature_columns)
        self.assertNotIn("telemetry_anomaly", model.feature_columns)
        self.assertNotIn("surge_demo", model.feature_columns)
        self.assertGreater(model.train_rows, 0)
        self.assertGreater(model.test_rows, 0)
        self.assertGreater(model.warmup_rows, 0)
        self.assertEqual(
            model.train_rows + model.test_rows + model.warmup_rows,
            len(frame),
        )

        predicted = add_predictions(frame, model)
        rows = _records(predicted)
        self.assertIn("evaluation_split", _columns(predicted))
        self.assertIn("forecast_origin", _columns(predicted))
        self.assertIn("forecast_horizon_minutes", _columns(predicted))
        self.assertIn("calibration_center_mbps", _columns(predicted))
        self.assertIn("calibration_scale_mbps", _columns(predicted))
        self.assertEqual({row["forecast_horizon_minutes"] for row in rows}, {60})
        self.assertTrue(all(row["calibration_source"] == "train_history" for row in rows))
        self.assertTrue(all(row["evaluation_split"] in {"train", "test", "warmup"} for row in rows))

        train_times = [
            row["timestamp"] for row in rows if row["evaluation_split"] == "train"
        ]
        test_times = [
            row["timestamp"] for row in rows if row["evaluation_split"] == "test"
        ]
        self.assertLess(max(train_times), min(test_times))

        # Changing current and all future observed users cannot change the
        # current forecast: current active_users is never a model input.
        ordered_rows = sorted(rows, key=lambda row: row["timestamp"])
        target = ordered_rows[len(ordered_rows) // 2]
        changed = [dict(row) for row in rows]
        for row in changed:
            if row["tower_id"] == target["tower_id"] and row["timestamp"] >= target["timestamp"]:
                row["active_users"] = float(row["active_users"]) + 1_000_000.0
                row["demand_mbps"] = float(row["demand_mbps"]) + 1_000_000.0
        changed_predictions = add_predictions(changed, model)
        base_by_key = {_key(row): row for row in rows}
        changed_by_key = {_key(row): row for row in _records(changed_predictions)}
        self.assertEqual(
            base_by_key[_key(target)]["predicted_demand_mbps"],
            changed_by_key[_key(target)]["predicted_demand_mbps"],
        )

        shuffled_predictions = add_predictions(list(reversed(rows)), model)
        shuffled_by_key = {
            _key(row): row for row in _records(shuffled_predictions)
        }
        self.assertEqual(
            {key: row["evaluation_split"] for key, row in base_by_key.items()},
            {key: row["evaluation_split"] for key, row in shuffled_by_key.items()},
        )

    def test_forecasting_output_and_proper_heldout_metrics(self):
        frame = generate_dataset({"days": 3, "cities": 2, "towers_per_city": 2}, seed=12)
        model = train_forecaster(frame)
        predicted = add_predictions(frame, model)
        self.assertGreater(model.test_metrics["count"], 0)
        self.assertEqual(model.test_metrics["count"], model.test_rows)
        self.assertEqual(model.train_metrics["count"], model.train_rows)
        self.assertGreaterEqual(model.calibration_scale_mbps, 1.0)
        self.assertEqual(len(model.coefficients), len(model.feature_columns) + 1)
        for row in _records(predicted):
            self.assertGreaterEqual(float(row["predicted_demand_mbps"]), 0.0)
            self.assertIsNotNone(row["forecast_origin"])
            self.assertEqual(row["calibration_center_mbps"], model.calibration_center_mbps)

    def test_surges_are_deterministic_three_x_and_not_clamped(self):
        config = {
            "days": 3,
            "cities": 1,
            "towers_per_city": 1,
            "event_probability": 0.0,
            "anomaly_probability": 0.0,
        }
        normal = _records(generate_dataset(config, seed=4))
        surge_config = dict(config)
        surge_config["surge_demo"] = True
        surge = _records(generate_dataset(surge_config, seed=4))
        surge_rows = [row for row in surge if row["surge_demo"]]
        self.assertGreaterEqual(len(surge_rows), 2)
        normal_by_key = {_key(row): row for row in normal}
        for row in surge_rows:
            baseline = normal_by_key[_key(row)]
            self.assertAlmostEqual(row["active_users"] / baseline["active_users"], 3.0)
            self.assertAlmostEqual(row["demand_mbps"] / baseline["demand_mbps"], 3.0)
            self.assertEqual(row["telemetry_anomaly"], 1)
            self.assertEqual(row["event_flag"], baseline["event_flag"])
        self.assertTrue(all(row["anomaly_multiplier"] >= 3.0 for row in surge_rows))

        anomaly_config = dict(config)
        anomaly_config["anomaly_probability"] = 1.0
        anomalous = _records(generate_dataset(anomaly_config, seed=4))
        self.assertTrue(all(row["telemetry_anomaly"] == 1 for row in anomalous))
        self.assertTrue(all(row["anomaly_multiplier"] > 1.0 for row in anomalous))

    def test_detector_is_causal_and_positive_only(self):
        frame = generate_dataset({"days": 4, "cities": 1, "towers_per_city": 1}, seed=21)
        model = train_forecaster(frame)
        predicted = _records(add_predictions(frame, model))
        ordered = sorted(predicted, key=lambda row: row["timestamp"])
        target = ordered[len(ordered) // 2]

        upward = [dict(row) for row in predicted]
        downward = [dict(row) for row in predicted]
        for row in upward:
            if _key(row) == _key(target):
                row["demand_mbps"] = float(row["predicted_demand_mbps"]) + 500.0
        for row in downward:
            if _key(row) == _key(target):
                row["demand_mbps"] = max(0.0, float(row["predicted_demand_mbps"]) - 500.0)
        upward_detected = {_key(row): row for row in _records(detect_spikes(upward))}
        downward_detected = {_key(row): row for row in _records(detect_spikes(downward))}
        self.assertTrue(upward_detected[_key(target)]["is_spike"])
        self.assertFalse(downward_detected[_key(target)]["is_spike"])
        self.assertGreater(upward_detected[_key(target)]["spike_score"], 0.0)
        self.assertEqual(downward_detected[_key(target)]["spike_score"], 0.0)

        # Changing only future demand cannot alter the earlier score.  Detector
        # calibration is attached to each row and fallback history is causal.
        future_changed = [dict(row) for row in predicted]
        for row in future_changed:
            if row["timestamp"] > target["timestamp"]:
                row["demand_mbps"] = float(row["demand_mbps"]) + 100_000.0
        base_detected = {_key(row): row for row in _records(detect_spikes(predicted))}
        future_detected = {_key(row): row for row in _records(detect_spikes(future_changed))}
        self.assertEqual(
            base_detected[_key(target)]["spike_score"],
            future_detected[_key(target)]["spike_score"],
        )

    def test_stateful_controller_boost_dwell_floor_overload_and_fail_safe(self):
        frame = _manual_rows(
            [0.0, 80.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            forecasts=[0.0, 80.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            spikes=[False, True] + [False] * 8,
        )
        controlled = _records(simulate_controller(frame))
        self.assertEqual(controlled[1]["controller_mode"], "boost")
        self.assertEqual(controlled[1]["active_capacity_mbps"], 100.0)
        self.assertEqual(controlled[2]["controller_mode"], "boost")
        self.assertEqual(controlled[3]["controller_mode"], "boost")
        self.assertLess(controlled[4]["active_capacity_mbps"], 100.0)
        self.assertGreater(controlled[0]["coverage_floor_mbps"], 0.0)
        self.assertEqual(controlled[0]["controller_mode"], "sleep")
        self.assertLessEqual(
            max(row["adaptive_power_kw"] for row in controlled),
            max(row["always_on_power_kw"] for row in controlled),
        )

        low_only = _records(simulate_controller(_manual_rows([0.0] * 5)))
        self.assertTrue(all(row["active_capacity_mbps"] > 0.0 for row in low_only))
        self.assertTrue(all(row["controller_mode"] == "sleep" for row in low_only))

        overload = _records(simulate_controller(_manual_rows([140.0], forecasts=[140.0])))
        self.assertEqual(overload[0]["controller_mode"], "boost")
        self.assertEqual(overload[0]["active_capacity_mbps"], 100.0)
        self.assertGreater(overload[0]["overload_mbps"], 0.0)
        self.assertGreater(overload[0]["unmet_demand_mbps"], 0.0)
        self.assertTrue(overload[0]["safety_violation"])

        invalid = _manual_rows([None], forecasts=[None])
        invalid[0]["capacity_mbps"] = None
        invalid[0]["installed_capacity_mbps"] = 100.0
        failsafe = _records(simulate_controller(invalid))[0]
        self.assertEqual(failsafe["controller_mode"], "boost")
        self.assertTrue(failsafe["fail_safe_boost"])
        self.assertLessEqual(failsafe["adaptive_power_kw"], failsafe["always_on_power_kw"])

        invalid_capacity = _manual_rows([20.0], forecasts=[20.0])
        invalid_capacity[0]["capacity_mbps"] = None
        invalid_capacity_result = _records(simulate_controller(invalid_capacity))[0]
        self.assertTrue(invalid_capacity_result["fail_safe_boost"])
        self.assertEqual(invalid_capacity_result["controller_mode"], "boost")

    def test_summary_uses_heldout_rows_and_reports_benchmarks(self):
        frame = generate_dataset({"days": 4, "cities": 2, "towers_per_city": 2}, seed=9)
        model = train_forecaster(frame)
        frame = add_predictions(frame, model)
        frame = detect_spikes(frame)
        frame = simulate_controller(frame)
        summary = summarize_results(frame)
        self.assertEqual(summary["evaluation_split_used"], "test")
        self.assertEqual(summary["evaluation_rows"], model.test_rows)
        self.assertGreater(summary["always_on_energy_kwh"], 0.0)
        self.assertGreater(summary["adaptive_energy_kwh"], 0.0)
        self.assertGreater(summary["scheduled_energy_kwh"], 0.0)
        self.assertIn("mode_switches", summary)
        self.assertIn("spike_count", summary)
        self.assertIn("scheduled_unmet_demand_mbps", summary)
        self.assertLessEqual(summary["adaptive_energy_kwh"], summary["always_on_energy_kwh"] + 1e-9)
        self.assertLessEqual(summary["service_level_pct"], 100.0)

    def test_coverage_topology_is_deterministic_and_not_a_model_feature(self):
        config = {"days": 1, "cities": 2, "towers_per_city": 3}
        first = _records(generate_dataset(config, seed=31))
        second = _records(generate_dataset(config, seed=31))
        positions = [(row["tower_id"], row["site_x"], row["site_y"]) for row in first]
        repeat_positions = [(row["tower_id"], row["site_x"], row["site_y"]) for row in second]
        self.assertEqual(positions, repeat_positions)
        self.assertTrue(all(0.0 <= row["site_x"] <= 1.0 and 0.0 <= row["site_y"] <= 1.0 for row in first))

        grid = build_coverage_grid(first, grid_rows=3, grid_cols=4)
        for city in grid["cities"].values():
            self.assertEqual(len(city["cells"]), 12)
            self.assertEqual(len({cell["cell_id"] for cell in city["cells"]}), 12)
            self.assertTrue(all(cell["primary_tower_id"] for cell in city["cells"]))
        model = train_forecaster(first)
        for excluded in ("site_x", "site_y", "synthetic_fault", "synthetic_fault_type", "fault_detected"):
            self.assertNotIn(excluded, model.feature_columns)

    def test_fault_validation_distinguishes_faults_from_traffic_conditions(self):
        valid_overload = _manual_rows([140.0], forecasts=[140.0], capacity=100.0)[0]
        valid_overload["telemetry_anomaly"] = True
        valid_overload["is_spike"] = True
        checked = _records(detect_faults([valid_overload]))[0]
        self.assertFalse(checked["fault_detected"])

        invalid = _manual_rows([20.0], forecasts=[20.0], capacity=100.0)[0]
        invalid["capacity_mbps"] = None
        invalid["baseline_power_kw"] = None
        checked_invalid = _records(detect_faults([invalid]))[0]
        self.assertTrue(checked_invalid["fault_detected"])
        self.assertIn("missing_or_invalid_reported_capacity", checked_invalid["fault_codes"])
        self.assertIn("missing_or_invalid_power", checked_invalid["fault_codes"])

        controlled = _records(simulate_controller([checked_invalid]))[0]
        self.assertEqual(controlled["controller_mode"], "boost")
        self.assertTrue(controlled["fail_safe_boost"])
        self.assertEqual(controlled["controller_reason"], "fail_safe_detected_telemetry_fault")

    def test_coverage_backup_and_dispatch_are_deterministic(self):
        rows = []
        for tower_id, x, fault in (("Manual-T01", 0.25, True), ("Manual-T02", 0.75, False)):
            row = _manual_rows([0.0], forecasts=[0.0], capacity=100.0)[0]
            row.update({
                "tower_id": tower_id,
                "site_x": x,
                "site_y": 0.5,
                "fault_detected": fault,
                "fault_reason": "missing_or_invalid_power" if fault else "none",
                "fault_severity": "high" if fault else "none",
                "telemetry_valid": not fault,
                "active_capacity_mbps": 100.0,
                "coverage_floor_mbps": 12.0,
                "controller_mode": "boost" if fault else "normal",
            })
            rows.append(row)
        grid = build_coverage_grid(rows, grid_rows=3, grid_cols=3)
        coverage = estimate_coverage(rows, grid)
        latest = coverage["cities"]["Manual"]["latest"]
        self.assertGreater(latest["at_risk_cells"], 0)
        self.assertEqual(latest["uncovered_cells"], 0)
        self.assertTrue(all(cell["status"] in {"covered", "at_risk_backup", "uncovered"} for cell in latest["cells"]))

        operations = simulate_rescue_dispatch(rows, coverage)
        repeat_operations = simulate_rescue_dispatch(rows, estimate_coverage(rows, grid))
        self.assertEqual(operations, repeat_operations)
        self.assertTrue(operations["simulation_only"])
        self.assertGreater(operations["summary"]["incident_count"], 0)
        self.assertTrue(all(item["simulation_only"] for item in operations["dispatches"]))

        single_fault = [dict(rows[0])]
        single_fault[0]["backup_tower_id"] = None
        single_grid = build_coverage_grid(single_fault, grid_rows=2, grid_cols=2)
        single_latest = estimate_coverage(single_fault, single_grid)["cities"]["Manual"]["latest"]
        self.assertEqual(single_latest["uncovered_cells"], 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
