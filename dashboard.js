/* GridPulse synthetic telecom energy dashboard.
 *
 * This file deliberately has no framework or external dependency.  The API
 * returns aggregate telemetry and (on newer servers) an interval replay.  All
 * response values are inserted with textContent or DOM properties so a
 * malformed/hostile response cannot become executable markup.
 */

(() => {
  "use strict";

  const TIME_ZONE = "Asia/Kolkata";
  const SAMPLE_MINUTES_FALLBACK = 60;
  const REQUEST_TIMEOUT_MS = 30000;
  // Resolve against the page instead of hard-coding the web server root. This
  // keeps the dashboard usable when a local preview serves this folder below
  // a path, while dashboard.py still resolves this to /api/simulation at root.
  const SIMULATION_URL = new URL("api/simulation", document.baseURI).toString();
  const FORM_LIMITS = {
    seed: [0, 999999],
    days: [1, 30],
    cities: [1, 6],
    towers_per_city: [1, 8],
    event_probability: [0, 0.25],
    anomaly_probability: [0, 0.08],
    fault_probability: [0, 0.05],
  };

  const state = {
    data: null,
    config: null,
    selectedCity: "__all__",
    replayFrames: [],
    replayIndex: 0,
    replayTimer: null,
    replayFallback: false,
    running: false,
    chartFrame: 0,
  };

  const $ = (id) => document.getElementById(id);
  const elements = {};

  function cacheElements() {
    [
      "simulation-form",
      "run-status",
      "form-error",
      "surge-demo-button",
      "event-probability-label",
      "anomaly-probability-label",
      "fault-probability-label",
      "metric-savings",
      "metric-savings-kwh",
      "metric-service",
      "metric-spikes",
      "metric-spikes-copy",
      "metric-mae",
      "run-summary",
      "city-select",
      "demand-chart",
      "demand-chart-state",
      "demand-window",
      "demand-peak",
      "model-mae",
      "model-train-rows",
      "model-test-rows",
      "model-features",
      "model-rmse",
      "model-violations",
      "hour-chart",
      "hour-chart-state",
      "energy-always",
      "energy-always-detail",
      "energy-scheduled",
      "energy-scheduled-detail",
      "energy-adaptive",
      "energy-adaptive-detail",
      "download-csv",
      "download-note",
      "city-list",
      "tower-summary",
      "tower-table",
      "alert-count",
      "alert-list",
      "replay-panel",
      "replay-data-status",
      "replay-slider",
      "replay-play",
      "replay-next",
      "replay-position",
      "replay-scenario",
      "replay-demand",
      "replay-users",
      "replay-power",
      "replay-mode",
      "evaluation-note",
    ].forEach((id) => {
      elements[id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = $(id);
    });
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function setText(nodeOrId, value) {
    const node = typeof nodeOrId === "string" ? $(nodeOrId) : nodeOrId;
    if (node) node.textContent = value == null ? "" : String(value);
  }

  function toNumber(value, fallback = null) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") {
      return fallback;
    }
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function firstNumber(source, keys) {
    if (!source || typeof source !== "object") return null;
    for (const key of keys) {
      const value = toNumber(source[key]);
      if (value !== null) return value;
    }
    return null;
  }

  function formatNumber(value, decimals = 0) {
    const number = toNumber(value);
    if (number === null) return "—";
    return new Intl.NumberFormat("en-IN", {
      maximumFractionDigits: decimals,
      minimumFractionDigits: decimals,
    }).format(number);
  }

  function formatInteger(value) {
    const number = toNumber(value);
    return number === null ? "—" : formatNumber(Math.round(number), 0);
  }

  function formatPercent(value, decimals = 1) {
    const number = toNumber(value);
    return number === null ? "—" : `${formatNumber(number, decimals)}%`;
  }

  function formatKwh(value) {
    const number = toNumber(value);
    return number === null ? "—" : `${formatNumber(number, 1)} kWh`;
  }

  function parseTimestamp(value) {
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (value === null || value === undefined || value === "") return null;
    const raw = String(value).trim();
    if (!raw) return null;

    // Python's synthetic timeline is timezone-naive.  Interpret it as IST so
    // the browser's own local timezone cannot shift the chart by several hours.
    const hasExplicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
    const isoLike = /^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}/.test(raw);
    const candidate = isoLike && !hasExplicitZone
      ? `${raw.replace(" ", "T")}+05:30`
      : raw;
    const date = new Date(candidate);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function timeValue(value) {
    const date = parseTimestamp(value);
    return date ? date.getTime() : Number.NaN;
  }

  function formatDateTime(value, withZone = true) {
    const date = parseTimestamp(value);
    if (!date) return "Unknown time";
    const options = {
      timeZone: TIME_ZONE,
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    };
    if (withZone) options.timeZoneName = "short";
    return new Intl.DateTimeFormat("en-IN", options).format(date);
  }

  function formatDateOnly(value) {
    const date = parseTimestamp(value);
    if (!date) return "Unknown date";
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: TIME_ZONE,
      day: "2-digit",
      month: "short",
      year: "numeric",
    }).format(date);
  }

  function formatHour(hour) {
    const number = toNumber(hour);
    if (number === null) return "—";
    return `${String(Math.round(number)).padStart(2, "0")}:00`;
  }

  function valueOrDash(value, formatter) {
    return toNumber(value) === null ? "—" : formatter(value);
  }

  function setRunStatus(label, kind) {
    if (!elements.runStatus) return;
    elements.runStatus.className = `run-badge is-${kind}`;
    elements.runStatus.textContent = label;
  }

  function setChartState(node, message, visible) {
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("is-hidden", !visible);
  }

  function setProbabilityLabels() {
    const form = elements.simulationForm;
    if (!form) return;
    const event = toNumber(form.elements.event_probability && form.elements.event_probability.value);
    const anomaly = toNumber(form.elements.anomaly_probability && form.elements.anomaly_probability.value);
    const fault = toNumber(form.elements.fault_probability && form.elements.fault_probability.value);
    if (event !== null) setText(elements.eventProbabilityLabel, formatPercent(event * 100, 1));
    if (anomaly !== null) setText(elements.anomalyProbabilityLabel, formatPercent(anomaly * 100, 1));
    if (fault !== null) setText(elements.faultProbabilityLabel, formatPercent(fault * 100, 1));
  }

  function readFormConfig() {
    const form = elements.simulationForm;
    if (!form) throw new Error("Simulation controls are unavailable.");
    const output = {};

    for (const [name, limits] of Object.entries(FORM_LIMITS)) {
      const input = form.elements[name];
      const raw = input ? String(input.value).trim() : "";
      if (!raw || !Number.isFinite(Number(raw))) {
        throw new Error(`Enter a valid ${name.replaceAll("_", " ")}.`);
      }
      const number = Number(raw);
      if (name === "seed" || name === "days" || name === "cities" || name === "towers_per_city") {
        if (!Number.isInteger(number)) throw new Error(`${name.replaceAll("_", " ")} must be a whole number.`);
      }
      if (number < limits[0] || number > limits[1]) {
        throw new Error(`${name.replaceAll("_", " ")} must be between ${limits[0]} and ${limits[1]}.`);
      }
      output[name] = number;
    }
    return output;
  }

  function setControlsDisabled(disabled) {
    const form = elements.simulationForm;
    if (form) {
      Array.from(form.elements).forEach((control) => {
        if (control.tagName === "BUTTON" || control.tagName === "INPUT") {
          control.disabled = disabled;
          if (!disabled && control.id === "surge-demo-button") control.disabled = false;
        }
      });
    }
    if (elements.surgeDemoButton) elements.surgeDemoButton.disabled = disabled;
    if (elements.replayPanel) elements.replayPanel.setAttribute("aria-busy", String(disabled));
  }

  function setError(message) {
    setText(elements.formError, message || "");
    if (message) {
      setChartState(elements.demandChartState, message, true);
      setChartState(elements.hourChartState, message, true);
    }
  }

  async function postSimulation(options) {
    if (window.location.protocol === "file:") {
      throw new Error("Open this dashboard through dashboard.py, not by double-clicking index.html.");
    }
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeout = window.setTimeout(() => controller?.abort(), REQUEST_TIMEOUT_MS);
    let response;
    try {
      response = await fetch(SIMULATION_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(options),
        signal: controller?.signal,
      });
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new Error("The simulation took too long. Try fewer days, cities, or towers.");
      }
      throw new Error("The simulation server is unreachable. Run python3 dashboard.py and open its local URL.");
    } finally {
      window.clearTimeout(timeout);
    }

    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new Error(`The simulation server returned an unreadable response (${response.status}).`);
    }

    if (!response.ok) {
      const serverMessage = payload && typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`;
      throw new Error(serverMessage);
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("The simulation response was not an object.");
    }
    return payload;
  }

  async function runSimulation(options, isSurgeDemo = false) {
    if (state.running) return;
    state.running = true;
    stopReplay();
    updateReplayControls();
    state.config = options;
    setError("");
    setRunStatus("running", "running");
    setControlsDisabled(true);
    setChartState(elements.demandChartState, "Running simulation…", true);
    setChartState(elements.hourChartState, "Running simulation…", true);

    try {
      const payload = await postSimulation({ ...options, ...(isSurgeDemo ? { surge_demo: true } : {}) });
      state.data = payload;
      state.config = payload.config && typeof payload.config === "object" ? payload.config : options;
      renderData(payload, isSurgeDemo);
      setRunStatus("ready", "ready");
    } catch (error) {
      const message = error instanceof Error ? error.message : "The simulation failed.";
      setRunStatus("error", "error");
      setError(`Simulation unavailable: ${message}`);
      if (!state.data) {
        renderNoData(message);
      }
    } finally {
      state.running = false;
      setControlsDisabled(false);
      updateReplayControls();
    }
  }

  function renderNoData(message) {
    setChartState(elements.demandChartState, message || "Run a simulation to load demand.", true);
    setChartState(elements.hourChartState, message || "Run a simulation to load hourly profile.", true);
    setText(elements.replayPosition, "No replay loaded");
    setText(elements.replayDataStatus, "waiting for response");
    setText(elements.replayDemand, "—");
    setText(elements.replayUsers, "—");
    setText(elements.replayPower, "—");
    setText(elements.replayMode, "—");
    setText(elements.cityList, "Run a simulation to see city roll-ups.");
    setText(elements.coveragePercent, "—");
    setText(elements.coverageCovered, "—");
    setText(elements.coverageAtRisk, "—");
    setText(elements.coverageUncovered, "—");
    setText(elements.faultCount, "—");
    setText(elements.dispatchCount, "—");
    if (elements.coverageGrid) {
      clearNode(elements.coverageGrid);
      const coverageEmpty = document.createElement("div");
      coverageEmpty.className = "empty-state";
      coverageEmpty.textContent = "Coverage results will appear after the simulation.";
      elements.coverageGrid.appendChild(coverageEmpty);
    }
    if (elements.dispatchList) {
      clearNode(elements.dispatchList);
      const dispatchEmpty = document.createElement("div");
      dispatchEmpty.className = "empty-state";
      dispatchEmpty.textContent = "Operations results will appear after the simulation.";
      elements.dispatchList.appendChild(dispatchEmpty);
    }
    if (elements.downloadCsv) elements.downloadCsv.disabled = true;
  }

  function getCities(data) {
    if (Array.isArray(data && data.cities)) return data.cities.map(String);
    if (data && data.city_timelines && typeof data.city_timelines === "object") return Object.keys(data.city_timelines);
    if (Array.isArray(data && data.city_summary)) return data.city_summary.map((row) => String(row.city || "Unknown"));
    return [];
  }

  function populateCitySelect(data) {
    const select = elements.citySelect;
    if (!select) return;
    clearNode(select);
    const allOption = document.createElement("option");
    allOption.value = "__all__";
    allOption.textContent = "All cities";
    select.appendChild(allOption);
    getCities(data).forEach((city) => {
      const option = document.createElement("option");
      option.value = city;
      option.textContent = city;
      select.appendChild(option);
    });
    state.selectedCity = "__all__";
    select.value = state.selectedCity;
  }

  function renderMetrics(data) {
    const summary = data.summary && typeof data.summary === "object" ? data.summary : {};
    const model = data.model && typeof data.model === "object" ? data.model : {};
    const savingsPct = firstNumber(summary, ["energy_savings_pct", "energy_savings_percent", "savings_percent"]);
    const savingsKwh = firstNumber(summary, ["energy_savings_kwh", "savings_kwh"]);
    const always = firstNumber(summary, ["always_on_energy_kwh", "always_on_kwh", "always_on_energy"]);
    const adaptive = firstNumber(summary, ["adaptive_energy_kwh", "adaptive_kwh", "adaptive_energy"]);
    const derivedPct = savingsPct === null && always !== null && adaptive !== null && always > 0
      ? ((always - adaptive) / always) * 100
      : savingsPct;
    const derivedKwh = savingsKwh === null && always !== null && adaptive !== null ? always - adaptive : savingsKwh;
    const service = firstNumber(summary, ["service_level_pct"]);
    const spikes = firstNumber(summary, ["spike_count"]);
    const mae = firstNumber(model, ["test_mae_mbps"]) ?? firstNumber(summary, ["forecast_mae_mbps"]);

    setText(elements.metricSavings, formatPercent(derivedPct, 1));
    setText(elements.metricSavingsKwh, derivedKwh === null ? "energy comparison unavailable" : `${formatKwh(derivedKwh)} saved vs always-on`);
    setText(elements.metricService, formatPercent(service, 1));
    setText(elements.metricSpikes, formatInteger(spikes));
    setText(elements.metricSpikesCopy, spikes === null ? "spike count not returned" : spikes === 0 ? "no forecast residual spikes" : "forecast residual spike intervals");
    setText(elements.metricMae, mae === null ? "—" : `${formatNumber(mae, 1)} Mbps`);

    setText(elements.energyAlways, formatKwh(always));
    setText(elements.energyAlwaysDetail, always === null ? "baseline not returned" : "reference power profile");
    const scheduled = firstNumber(summary, ["scheduled_energy_kwh", "fixed_schedule_energy_kwh", "scheduled_kwh"]);
    const scheduledService = firstNumber(summary, ["scheduled_service_level_pct", "fixed_schedule_service_level_pct"]);
    setText(elements.energyScheduled, formatKwh(scheduled));
    setText(elements.energyScheduledDetail, scheduled === null
      ? "not returned by this run"
      : scheduledService === null ? "fixed schedule energy" : `${formatPercent(scheduledService, 1)} service level`);
    setText(elements.energyAdaptive, formatKwh(adaptive));
    setText(elements.energyAdaptiveDetail, adaptive === null
      ? "adaptive energy not returned"
      : service === null ? "controller result" : `${formatPercent(service, 1)} service level`);

    const config = data.config && typeof data.config === "object" ? data.config : state.config || {};
    const days = toNumber(config.days);
    const cities = toNumber(config.cities);
    const towers = toNumber(config.towers_per_city);
    const seed = toNumber(config.seed);
    const pieces = [];
    if (days !== null) pieces.push(`${formatInteger(days)} days`);
    if (cities !== null) pieces.push(`${formatInteger(cities)} cities`);
    if (towers !== null) pieces.push(`${formatInteger(towers)} towers/city`);
    if (seed !== null) pieces.push(`seed ${formatInteger(seed)}`);
    setText(elements.runSummary, pieces.length ? `Synthetic run · ${pieces.join(" · ")}` : "Synthetic run completed.");
  }

  function renderModel(data) {
    const model = data.model && typeof data.model === "object" ? data.model : {};
    const summary = data.summary && typeof data.summary === "object" ? data.summary : {};
    const mae = firstNumber(model, ["test_mae_mbps"]) ?? firstNumber(summary, ["forecast_mae_mbps"]);
    const trainRows = firstNumber(model, ["train_rows"]);
    const testRows = firstNumber(model, ["test_rows"]);
    const features = firstNumber(model, ["feature_count"]);
    const rmse = firstNumber(model, ["test_rmse_mbps"]) ?? firstNumber(summary, ["forecast_rmse_mbps"]);
    const violations = firstNumber(summary, ["safety_violation_count", "safety_violations"]);
    setText(elements.modelMae, mae === null ? "—" : `${formatNumber(mae, 1)} Mbps`);
    setText(elements.modelTrainRows, formatInteger(trainRows));
    setText(elements.modelTestRows, formatInteger(testRows));
    setText(elements.modelFeatures, formatInteger(features));
    setText(elements.modelRmse, rmse === null ? "—" : `${formatNumber(rmse, 1)} Mbps`);
    setText(elements.modelViolations, formatInteger(violations));
    if (elements.modelViolations) {
      elements.modelViolations.className = violations !== null && violations > 0 ? "warning-value" : "safe-value";
    }
  }

  function renderCityList(data) {
    const root = elements.cityList;
    if (!root) return;
    clearNode(root);
    const rows = Array.isArray(data.city_summary) ? data.city_summary : [];
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No city roll-up was returned for this run.";
      root.appendChild(empty);
      return;
    }
    const maxPct = Math.max(1, ...rows.map((row) => Math.max(0, toNumber(row.energy_savings_pct, 0))));
    rows.forEach((row) => {
      const article = document.createElement("article");
      article.className = "city-row";
      const top = document.createElement("div");
      top.className = "city-row-top";
      const title = document.createElement("h4");
      title.textContent = String(row.city || "Unknown city");
      const pct = document.createElement("span");
      pct.textContent = formatPercent(row.energy_savings_pct, 1);
      top.append(title, pct);
      const progress = document.createElement("div");
      progress.className = "city-progress";
      const fill = document.createElement("i");
      const pctNumber = Math.max(0, Math.min(100, (toNumber(row.energy_savings_pct, 0) / maxPct) * 100));
      fill.style.width = `${pctNumber}%`;
      progress.appendChild(fill);
      const meta = document.createElement("div");
      meta.className = "city-row-meta";
      const demand = document.createElement("span");
      demand.textContent = `avg ${formatNumber(row.average_demand_mbps, 0)} Mbps`;
      const peak = document.createElement("span");
      peak.textContent = `peak ${formatNumber(row.peak_demand_mbps, 0)} Mbps`;
      const savings = document.createElement("span");
      savings.textContent = `${formatKwh(row.energy_savings_kwh)} saved`;
      const alerts = document.createElement("span");
      alerts.textContent = `${formatInteger(row.spike_count)} spikes · ${formatInteger(row.tower_count)} towers`;
      meta.append(demand, peak, savings, alerts);
      article.append(top, progress, meta);
      root.appendChild(article);
    });
  }

  function renderAlerts(data) {
    const root = elements.alertList;
    if (!root) return;
    clearNode(root);
    const alerts = Array.isArray(data.alerts) ? data.alerts : [];
    setText(elements.alertCount, formatInteger(alerts.length));
    if (!alerts.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const violations = firstNumber(data.summary, ["safety_violation_count", "safety_violations"]);
      const overloads = firstNumber(data.summary, ["overload_count", "overloads"]);
      empty.textContent = violations !== null && violations > 0
        ? `No spike or event alerts; ${formatInteger(violations)} controller coverage violation(s) were returned.`
        : overloads !== null && overloads > 0
          ? `No spike or event alerts; ${formatInteger(overloads)} overload flag(s) were returned.`
        : "No spike or scheduled-event alerts returned for this run.";
      root.appendChild(empty);
      return;
    }
    alerts.forEach((alert) => {
      const item = document.createElement("article");
      item.className = "alert-item";
      const kind = String(alert.kind || "Alert");
      const mark = document.createElement("span");
      const lowerKind = kind.toLowerCase();
      mark.className = `alert-mark${lowerKind.includes("event") ? " event-mark" : lowerKind.includes("fault") ? " fault-mark" : ""}`;
      mark.textContent = lowerKind.includes("event") ? "✦" : lowerKind.includes("fault") ? "×" : "!";
      const copy = document.createElement("div");
      copy.className = "alert-copy";
      const title = document.createElement("strong");
      title.textContent = kind;
      const detail = document.createElement("p");
      detail.textContent = String(alert.detail || "The controller received an alert interval.");
      const metadata = document.createElement("small");
      metadata.textContent = `${String(alert.city || "Unknown city")} · ${String(alert.tower_id || "Unknown tower")} · ${formatDateTime(alert.timestamp)}`;
      copy.append(title, detail, metadata);
      item.append(mark, copy);
      root.appendChild(item);
    });
  }

  function coverageCity(data) {
    const coverage = data && data.coverage && typeof data.coverage === "object" ? data.coverage : null;
    if (!coverage || !coverage.cities || typeof coverage.cities !== "object") return null;
    if (state.selectedCity === "__all__") return null;
    return coverage.cities[state.selectedCity] || null;
  }

  function renderCoverage(data) {
    const coverage = data && data.coverage && typeof data.coverage === "object" ? data.coverage : null;
    const root = elements.coverageGrid;
    if (!coverage || !root) {
      setText(elements.coveragePercent, "—");
      setText(elements.coverageCovered, "—");
      setText(elements.coverageAtRisk, "—");
      setText(elements.coverageUncovered, "—");
      if (root) {
        clearNode(root);
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "Coverage data was not returned for this run.";
        root.appendChild(empty);
      }
      return;
    }

    const city = coverageCity(data);
    const latestByCity = coverage.cities && typeof coverage.cities === "object"
      ? Object.values(coverage.cities).map((item) => item && item.latest).filter(Boolean)
      : [];
    const latest = city && city.latest
      ? city.latest
      : latestByCity.reduce((total, item) => ({
        coverage_pct: (toNumber(total.coverage_pct, 0) + toNumber(item.coverage_pct, 0)),
        covered_cells: (toNumber(total.covered_cells, 0) + toNumber(item.covered_cells, 0)),
        at_risk_cells: (toNumber(total.at_risk_cells, 0) + toNumber(item.at_risk_cells, 0)),
        uncovered_cells: (toNumber(total.uncovered_cells, 0) + toNumber(item.uncovered_cells, 0)),
        _count: (total._count || 0) + 1,
      }), { coverage_pct: 0, covered_cells: 0, at_risk_cells: 0, uncovered_cells: 0, _count: 0 });
    const coveragePercent = city
      ? toNumber(latest.coverage_pct, 0)
      : latest._count ? latest.coverage_pct / latest._count : 0;
    setText(elements.coveragePercent, formatPercent(coveragePercent, 1));
    setText(elements.coverageCovered, formatInteger(latest.covered_cells));
    setText(elements.coverageAtRisk, formatInteger(latest.at_risk_cells));
    setText(elements.coverageUncovered, formatInteger(latest.uncovered_cells));
    setText(elements.coverageMode, String(coverage.mode || "synthetic grid"));
    setText(elements.coverageNote, city
      ? "Cells are assigned to the nearest synthetic tower. At-risk cells are served by backup capacity; uncovered cells require simulated response. This is not RF propagation."
      : "Choose a city in the demand chart selector to inspect its 5 × 5 cell matrix. Aggregate metrics above include all cities.");

    clearNode(root);
    if (!city) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "Select a city above to inspect its synthetic cell matrix.";
      root.appendChild(empty);
      return;
    }
    const cells = Array.isArray(city.latest?.cells) ? city.latest.cells : [];
    if (!cells.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No latest cell matrix was returned for this city.";
      root.appendChild(empty);
      return;
    }
    const grid = coverage.grid && typeof coverage.grid === "object" ? coverage.grid : {};
    root.style.setProperty("--coverage-cols", String(toNumber(grid.cols, 5)));
    cells.forEach((cell) => {
      const status = String(cell.status || "uncovered");
      const tile = document.createElement("div");
      tile.className = `coverage-cell ${status.replace(/[^a-z_]/g, "-")}`;
      const shortStatus = status === "covered" ? "OK" : status === "at_risk_backup" ? "BK" : "GAP";
      tile.textContent = shortStatus;
      tile.setAttribute("role", "img");
      tile.setAttribute("aria-label", `${String(cell.cell_id || "cell")}: ${status.replaceAll("_", " ")}`);
      tile.title = `${String(cell.cell_id || "cell")} · ${status.replaceAll("_", " ")} · ${formatNumber(cell.available_capacity_mbps, 1)} / ${formatNumber(cell.required_capacity_mbps, 1)} Mbps`;
      root.appendChild(tile);
    });
  }

  function renderOperations(data) {
    const operations = data && data.operations && typeof data.operations === "object" ? data.operations : null;
    const root = elements.dispatchList;
    if (!operations || !root) {
      setText(elements.faultCount, "—");
      setText(elements.dispatchCount, "—");
      if (root) {
        clearNode(root);
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "Operations data was not returned for this run.";
        root.appendChild(empty);
      }
      return;
    }
    const detector = operations.fault_detector && typeof operations.fault_detector === "object"
      ? operations.fault_detector
      : {};
    const summary = operations.summary && typeof operations.summary === "object" ? operations.summary : {};
    setText(elements.faultCount, formatInteger(summary.fault_event_count ?? detector.detected_fault_count ?? summary.faulted_tower_count));
    setText(elements.dispatchCount, formatInteger(summary.incident_count));
    setText(elements.operationsNote, operations.limitation || "Simulation-only response queue.");
    clearNode(root);
    const dispatches = Array.isArray(operations.dispatches) ? operations.dispatches : [];
    if (!dispatches.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No uncovered cells or synthetic telemetry faults required a response team.";
      root.appendChild(empty);
      return;
    }
    dispatches.forEach((dispatch) => {
      const item = document.createElement("article");
      item.className = "dispatch-item";
      const marker = document.createElement("span");
      marker.className = `dispatch-mark ${String(dispatch.priority || "high")}`;
      marker.textContent = String(dispatch.priority || "high").slice(0, 1).toUpperCase();
      const copy = document.createElement("div");
      copy.className = "dispatch-copy";
      const title = document.createElement("strong");
      title.textContent = `${String(dispatch.city || "Unknown")} · ${String(dispatch.team_id || "response team")}`;
      const detail = document.createElement("p");
      detail.textContent = `${String(dispatch.trigger || "incident").replaceAll("_", " ")} · ${formatInteger(dispatch.eta_minutes)} min ETA`;
      const meta = document.createElement("small");
      meta.textContent = `${String(dispatch.status || "simulated").replaceAll("_", " ")} · ${formatDateTime(dispatch.timestamp, false)} · ${formatInteger(Array.isArray(dispatch.cell_ids) ? dispatch.cell_ids.length : 0)} cells`;
      copy.append(title, detail, meta);
      item.append(marker, copy);
      root.appendChild(item);
    });
  }

  function evaluationSummary(data) {
    const evaluation = data.evaluation && typeof data.evaluation === "object" ? data.evaluation : null;
    const detector = data.detector && typeof data.detector === "object"
      ? data.detector
      : data.detector_metrics && typeof data.detector_metrics === "object"
        ? data.detector_metrics
        : data.spike_detector && typeof data.spike_detector === "object"
          ? data.spike_detector
          : evaluation && evaluation.detector_metrics && typeof evaluation.detector_metrics === "object" ? evaluation.detector_metrics : null;
    const calibration = data.calibration && typeof data.calibration === "object"
      ? data.calibration
      : data.detector_calibration && typeof data.detector_calibration === "object"
        ? data.detector_calibration
        : evaluation && evaluation.calibration && typeof evaluation.calibration === "object" ? evaluation.calibration : null;
    const parts = [];
    if (evaluation) {
      const interval = firstNumber(evaluation, ["interval_minutes"]);
      if (interval !== null) parts.push(`${formatInteger(interval)}-minute intervals`);
      if (evaluation.train_end) parts.push(`train through ${formatDateOnly(evaluation.train_end)}`);
      if (evaluation.test_start) parts.push(`test from ${formatDateOnly(evaluation.test_start)}`);
      if (evaluation.test_end) parts.push(`to ${formatDateOnly(evaluation.test_end)}`);
      if (evaluation.timezone) parts.push(`timezone ${String(evaluation.timezone)}`);
    }
    if (detector) {
      const detectorParts = [];
      ["precision", "precision_pct", "recall", "recall_pct", "f1", "f1_pct", "threshold", "alert_count"].forEach((key) => {
        const value = firstNumber(detector, [key]);
        if (value !== null) detectorParts.push(`${key} ${formatNumber(value, 2)}`);
      });
      if (detectorParts.length) parts.push(`detector ${detectorParts.join(", ")}`);
    }
    if (calibration) {
      const calibrationParts = [];
      ["threshold", "false_positive_rate", "false_negative_rate", "precision", "recall", "calibration_error"].forEach((key) => {
        const value = firstNumber(calibration, [key]);
        if (value !== null) calibrationParts.push(`${key.replaceAll("_", " ")} ${formatNumber(value, 2)}`);
      });
      if (calibrationParts.length) parts.push(`calibration ${calibrationParts.join(", ")}`);
    }
    return parts.length
      ? `Returned evaluation: ${parts.join(" · ")}.`
      : "Evaluation metadata was not returned by this response; the model card reflects the returned snapshot.";
  }

  function timelineForSelectedCity() {
    if (!state.data) return [];
    if (state.selectedCity !== "__all__" && state.data.city_timelines && Array.isArray(state.data.city_timelines[state.selectedCity])) {
      return state.data.city_timelines[state.selectedCity];
    }
    return Array.isArray(state.data.timeline) ? state.data.timeline : [];
  }

  function nearestTimelineRow(timestamp, timeline = timelineForSelectedCity()) {
    if (!Array.isArray(timeline) || !timeline.length) return null;
    const target = timeValue(timestamp);
    let best = timeline[0];
    let distance = Number.POSITIVE_INFINITY;
    timeline.forEach((row) => {
      const candidate = timeValue(row.timestamp);
      const currentDistance = Number.isFinite(target) && Number.isFinite(candidate)
        ? Math.abs(candidate - target)
        : 0;
      if (currentDistance < distance) {
        distance = currentDistance;
        best = row;
      }
    });
    return best;
  }

  function normalizeReplay(data) {
    const rawReplay = Array.isArray(data.replay) ? data.replay : [];
    const frames = rawReplay.map((frame) => {
      if (!frame || typeof frame !== "object") return null;
      const towers = Array.isArray(frame.towers) ? frame.towers.filter((tower) => tower && typeof tower === "object") : [];
      const timestamp = frame.timestamp || (towers[0] && towers[0].timestamp) || null;
      return { timestamp, towers, source: frame };
    }).filter(Boolean);

    if (frames.length) {
      frames.sort((left, right) => {
        const a = timeValue(left.timestamp);
        const b = timeValue(right.timestamp);
        if (!Number.isFinite(a) || !Number.isFinite(b)) return 0;
        return a - b;
      });
      state.replayFallback = false;
      return frames;
    }

    // Older dashboard.py responses have only a latest tower snapshot.  Keep
    // that limitation visible rather than pretending it is an interval feed.
    const snapshot = Array.isArray(data.tower_snapshot) ? data.tower_snapshot.filter((tower) => tower && typeof tower === "object") : [];
    const timeline = Array.isArray(data.timeline) ? data.timeline : [];
    const latestTimestamp = snapshot.reduce((latest, row) => {
      if (!latest) return row.timestamp;
      return timeValue(row.timestamp) > timeValue(latest) ? row.timestamp : latest;
    }, timeline.length ? timeline[timeline.length - 1].timestamp : null);
    if (snapshot.length || latestTimestamp) {
      state.replayFallback = true;
      return [{ timestamp: latestTimestamp, towers: snapshot, source: null }];
    }
    state.replayFallback = true;
    return [];
  }

  function sampleMinutes() {
    const evaluation = state.data && state.data.evaluation;
    return firstNumber(evaluation, ["interval_minutes"]) || SAMPLE_MINUTES_FALLBACK;
  }

  function updateReplayControls() {
    const count = state.replayFrames.length;
    const hasReplay = count > 0;
    if (elements.replaySlider) {
      elements.replaySlider.min = "0";
      elements.replaySlider.max = String(Math.max(0, count - 1));
      elements.replaySlider.value = String(Math.min(state.replayIndex, Math.max(0, count - 1)));
      elements.replaySlider.disabled = !hasReplay || count < 2 || state.running;
    }
    if (elements.replayPlay) {
      elements.replayPlay.disabled = !hasReplay || count < 2 || state.running;
      elements.replayPlay.textContent = state.replayTimer ? "Pause replay" : "Play replay";
    }
    if (elements.replayNext) elements.replayNext.disabled = !hasReplay || count < 2 || state.running;
    if (elements.replayDataStatus) {
      elements.replayDataStatus.textContent = hasReplay
        ? state.replayFallback
          ? `latest snapshot · ${sampleMinutes()}-min sample`
          : `${count} intervals · ${sampleMinutes()}-min samples`
        : "no replay returned";
    }
  }

  function frameTowers(frame) {
    const rows = frame && Array.isArray(frame.towers) && frame.towers.length
      ? frame.towers
      : state.data && Array.isArray(state.data.tower_snapshot) ? state.data.tower_snapshot : [];
    return state.selectedCity === "__all__"
      ? rows
      : rows.filter((row) => String(row.city || "") === state.selectedCity);
  }

  function aggregateFrame(frame) {
    const rows = frameTowers(frame);
    const sums = {
      demand_mbps: 0,
      active_users: 0,
      adaptive_power_kw: 0,
      always_on_power_kw: 0,
    };
    const present = {
      demand_mbps: false,
      active_users: false,
      adaptive_power_kw: false,
      always_on_power_kw: false,
    };
    rows.forEach((row) => {
      ["demand_mbps", "active_users", "adaptive_power_kw", "always_on_power_kw"].forEach((key) => {
        const number = toNumber(row[key]);
        if (number !== null) {
          sums[key] += number;
          present[key] = true;
        }
      });
    });
    const timeline = nearestTimelineRow(frame && frame.timestamp, timelineForSelectedCity());
    ["demand_mbps", "active_users", "adaptive_power_kw", "always_on_power_kw"].forEach((key) => {
      if (!present[key] && timeline) sums[key] = toNumber(timeline[key]);
    });
    const modes = {};
    let overloads = 0;
    rows.forEach((row) => {
      const mode = String(row.mode || row.controller_mode || "unknown").toLowerCase();
      modes[mode] = (modes[mode] || 0) + 1;
      if (row.safety_violation || row.overload || row.overloaded || toNumber(row.overload_mbps, 0) > 0 || toNumber(row.unmet_demand_mbps, 0) > 0) overloads += 1;
    });
    const modeText = Object.entries(modes).sort((a, b) => b[1] - a[1]).map(([mode, count]) => `${mode} ${count}`).join(" · ");
    return { ...sums, modeText: modeText || "no tower mode", overloads, towerCount: rows.length };
  }

  function renderReplayFrame() {
    const frame = state.replayFrames[state.replayIndex];
    if (!frame) {
      setText(elements.replayPosition, "No replay loaded");
      setText(elements.replayDemand, "—");
      setText(elements.replayUsers, "—");
      setText(elements.replayPower, "—");
      setText(elements.replayMode, "—");
      renderTowerTable([]);
      updateReplayControls();
      return;
    }
    const aggregate = aggregateFrame(frame);
    const timestamp = frame.timestamp;
    setText(elements.replayPosition, `Interval ${state.replayIndex + 1} of ${state.replayFrames.length} · ${formatDateTime(timestamp)}`);
    setText(elements.replayDemand, valueOrDash(aggregate.demand_mbps, (value) => `${formatNumber(value, 0)} Mbps`));
    setText(elements.replayUsers, valueOrDash(aggregate.active_users, (value) => formatInteger(value)));
    setText(elements.replayPower, valueOrDash(aggregate.adaptive_power_kw, (value) => `${formatNumber(value, 1)} kW`));
    setText(elements.replayMode, aggregate.modeText);
    renderTowerTable(frameTowers(frame));
    const timeline = nearestTimelineRow(timestamp, timelineForSelectedCity());
    const peakSignal = timeline && (toNumber(timeline.spike_count, 0) > 0 || toNumber(timeline.event_count, 0) > 0) ? " · alert interval" : "";
    const overloadSignal = aggregate.overloads ? ` · ${aggregate.overloads} coverage gap(s)` : " · coverage floor protected";
    setText(elements.towerSummary, `${formatDateTime(timestamp)} · ${aggregate.towerCount || "No"} towers${peakSignal}${overloadSignal}`);
    updateReplayControls();
    drawCharts();
  }

  function stopReplay() {
    if (state.replayTimer) {
      window.clearInterval(state.replayTimer);
      state.replayTimer = null;
    }
  }

  function advanceReplay() {
    if (state.replayFrames.length < 2) return;
    if (state.replayIndex >= state.replayFrames.length - 1) {
      stopReplay();
      state.replayIndex = 0;
    } else {
      state.replayIndex += 1;
    }
    if (elements.replaySlider) elements.replaySlider.value = String(state.replayIndex);
    renderReplayFrame();
  }

  function toggleReplay() {
    if (state.replayFrames.length < 2 || state.running) return;
    if (state.replayTimer) {
      stopReplay();
      updateReplayControls();
      return;
    }
    if (state.replayIndex >= state.replayFrames.length - 1) state.replayIndex = 0;
    state.replayTimer = window.setInterval(advanceReplay, 900);
    updateReplayControls();
    renderReplayFrame();
  }

  function renderTowerTable(rows) {
    const body = elements.towerTable;
    if (!body) return;
    clearNode(body);
    const sorted = Array.isArray(rows) ? rows.slice().sort((left, right) => {
      const cityCompare = String(left.city || "").localeCompare(String(right.city || ""));
      return cityCompare || String(left.tower_id || "").localeCompare(String(right.tower_id || ""));
    }) : [];
    if (!sorted.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "empty-cell";
      cell.textContent = "No per-tower interval state was returned.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    sorted.forEach((tower) => {
      const tr = document.createElement("tr");
      const towerCell = document.createElement("td");
      const name = document.createElement("div");
      name.className = "tower-name";
      const strong = document.createElement("strong");
      strong.textContent = String(tower.tower_id || "Unknown tower");
      const city = document.createElement("small");
      city.textContent = String(tower.city || "Unknown city");
      name.append(strong, city);
      towerCell.appendChild(name);

      const demandCell = document.createElement("td");
      demandCell.textContent = `${formatNumber(tower.demand_mbps, 0)} / ${formatNumber(tower.forecast_mbps ?? tower.predicted_demand_mbps, 0)} Mbps`;

      const capacityCell = document.createElement("td");
      capacityCell.className = "capacity-cell";
      const capacityText = document.createElement("span");
      capacityText.textContent = `${formatNumber(tower.active_capacity_mbps, 0)} / ${formatNumber(tower.capacity_mbps, 0)} Mbps`;
      const capacityBar = document.createElement("span");
      capacityBar.className = "capacity-bar";
      const capacityFill = document.createElement("i");
      const capacity = toNumber(tower.capacity_mbps, 0);
      const active = toNumber(tower.active_capacity_mbps, 0);
      capacityFill.style.width = `${Math.max(0, Math.min(100, capacity > 0 ? (active / capacity) * 100 : 0))}%`;
      capacityBar.appendChild(capacityFill);
      capacityCell.append(capacityText, capacityBar);

      const powerCell = document.createElement("td");
      powerCell.textContent = `${formatNumber(tower.adaptive_power_kw, 1)} / ${formatNumber(tower.always_on_power_kw, 1)} kW`;

      const modeCell = document.createElement("td");
      const mode = String(tower.mode || tower.controller_mode || "normal");
      const modeKey = ["eco", "normal", "boost", "sleep"].includes(mode.toLowerCase()) ? mode.toLowerCase() : "normal";
      const modeBadge = document.createElement("span");
      modeBadge.className = `mode-badge ${modeKey}`;
      modeBadge.textContent = mode;
      if (tower.controller_reason) modeBadge.title = String(tower.controller_reason);
      modeCell.appendChild(modeBadge);

      const signalCell = document.createElement("td");
      const signal = document.createElement("span");
      const unmet = toNumber(tower.unmet_demand_mbps, 0);
      const overloaded = Boolean(tower.safety_violation || tower.overload || tower.overloaded) || toNumber(tower.overload_mbps, 0) > 0 || unmet > 0;
      signal.className = `signal-text${tower.fault || overloaded || tower.spike || tower.event ? " is-alert" : ""}`;
      signal.textContent = tower.fault ? "telemetry fault" : overloaded ? "coverage gap" : tower.spike ? "demand spike" : tower.event ? "scheduled event" : "stable";
      signalCell.appendChild(signal);

      tr.append(towerCell, demandCell, capacityCell, powerCell, modeCell, signalCell);
      body.appendChild(tr);
    });
  }

  function closestReplayIndex(timestamp) {
    if (!state.replayFrames.length) return 0;
    const target = timeValue(timestamp);
    let best = 0;
    let distance = Number.POSITIVE_INFINITY;
    state.replayFrames.forEach((frame, index) => {
      const candidate = timeValue(frame.timestamp);
      const currentDistance = Number.isFinite(target) && Number.isFinite(candidate)
        ? Math.abs(candidate - target)
        : index === 0 ? 0 : Number.POSITIVE_INFINITY;
      if (currentDistance < distance) {
        distance = currentDistance;
        best = index;
      }
    });
    return best;
  }

  function applyScenarioSelection(data) {
    const scenario = data.scenario && typeof data.scenario === "object" ? data.scenario : null;
    if (!scenario || scenario.enabled !== true) {
      setText(elements.replayScenario, "");
      return;
    }
    const towerId = scenario.tower_id == null ? "" : String(scenario.tower_id);
    const timestamp = scenario.timestamp || null;
    let city = "";
    state.replayFrames.forEach((frame) => {
      frameTowers(frame).forEach((tower) => {
        if (!city && String(tower.tower_id || "") === towerId) city = String(tower.city || "");
      });
    });
    if (city && elements.citySelect && Array.from(elements.citySelect.options).some((option) => option.value === city)) {
      state.selectedCity = city;
      elements.citySelect.value = city;
    }
    if (timestamp) state.replayIndex = closestReplayIndex(timestamp);
    setText(elements.replayScenario, `Surge demo · ${towerId || "tower"}${timestamp ? ` at ${formatDateTime(timestamp)}` : ""}`);
  }

  function renderReplay(data, isSurgeDemo) {
    stopReplay();
    state.replayFrames = normalizeReplay(data);
    state.replayIndex = Math.max(0, state.replayFrames.length - 1);
    setText(elements.replayScenario, "");
    updateReplayControls();
    if (isSurgeDemo || (data.scenario && data.scenario.enabled === true)) applyScenarioSelection(data);
    renderReplayFrame();
  }

  function updateSelectedCityReplay() {
    if (!state.data || !state.replayFrames.length) return;
    renderReplayFrame();
  }

  function renderEvaluation(data) {
    setText(elements.evaluationNote, evaluationSummary(data));
  }

  function renderData(data, isSurgeDemo) {
    populateCitySelect(data);
    renderMetrics(data);
    renderModel(data);
    renderCityList(data);
    renderAlerts(data);
    renderCoverage(data);
    renderOperations(data);
    renderEvaluation(data);
    renderReplay(data, isSurgeDemo);
    if (elements.downloadCsv) elements.downloadCsv.disabled = !Array.isArray(data.timeline) || data.timeline.length === 0;
    if (elements.downloadNote) elements.downloadNote.textContent = Array.isArray(data.timeline) && data.timeline.length
      ? "Client-generated aggregate timeline; not full raw tower telemetry."
      : "No aggregate timeline was returned for export.";
    setChartState(elements.demandChartState, "", false);
    setChartState(elements.hourChartState, "", false);
  }

  function colors() {
    const styles = getComputedStyle(document.documentElement);
    return {
      ink: styles.getPropertyValue("--ink").trim() || "#eaf1f4",
      muted: styles.getPropertyValue("--muted").trim() || "#71858e",
      grid: styles.getPropertyValue("--line").trim() || "rgba(170,202,212,.14)",
      coral: styles.getPropertyValue("--coral").trim() || "#f08b78",
      cyan: styles.getPropertyValue("--cyan").trim() || "#68d5d0",
      amber: styles.getPropertyValue("--amber").trim() || "#f4c56e",
      lime: styles.getPropertyValue("--lime").trim() || "#c2e56f",
    };
  }

  function setupCanvas(canvas) {
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(10, Math.floor(rect.width));
    const height = Math.max(10, Math.floor(rect.height));
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const context = canvas.getContext("2d");
    if (!context) return null;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    return { context, width, height };
  }

  function scheduleFrame(callback) {
    return typeof window.requestAnimationFrame === "function"
      ? window.requestAnimationFrame(callback)
      : window.setTimeout(callback, 0);
  }

  function cancelFrame(frame) {
    if (!frame) return;
    if (typeof window.cancelAnimationFrame === "function") window.cancelAnimationFrame(frame);
    else window.clearTimeout(frame);
  }

  function drawDemandChart() {
    const setup = setupCanvas(elements.demandChart);
    if (!setup) return;
    const { context: ctx, width, height } = setup;
    const timeline = timelineForSelectedCity();
    if (!timeline.length) {
      setChartState(elements.demandChartState, "No demand timeline for this selection.", true);
      setText(elements.demandWindow, "—");
      setText(elements.demandPeak, "Peak demand —");
      return;
    }
    setChartState(elements.demandChartState, "", false);
    const palette = colors();
    const margin = { top: 18, right: 16, bottom: 37, left: 50 };
    const plotWidth = Math.max(1, width - margin.left - margin.right);
    const plotHeight = Math.max(1, height - margin.top - margin.bottom);
    const actualValues = timeline.map((row) => toNumber(row.demand_mbps, 0));
    const forecastValues = timeline.map((row) => toNumber(row.predicted_demand_mbps, 0));
    const maximum = Math.max(1, ...actualValues, ...forecastValues) * 1.1;
    const x = (index) => margin.left + (timeline.length === 1 ? plotWidth / 2 : (index / (timeline.length - 1)) * plotWidth);
    const y = (value) => margin.top + plotHeight - (Math.max(0, value) / maximum) * plotHeight;
    const labelFont = "10px SFMono-Regular, Consolas, monospace";
    const drawGrid = () => {
      ctx.font = labelFont;
      ctx.lineWidth = 1;
      ctx.fillStyle = palette.muted;
      ctx.strokeStyle = palette.grid;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let index = 0; index <= 4; index += 1) {
        const value = (maximum / 4) * index;
        const yPosition = y(value);
        ctx.beginPath();
        ctx.moveTo(margin.left, yPosition);
        ctx.lineTo(width - margin.right, yPosition);
        ctx.stroke();
        ctx.fillText(formatNumber(value, 0), margin.left - 8, yPosition);
      }
      ctx.save();
      ctx.translate(12, margin.top + plotHeight / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.fillText("Mbps", 0, 0);
      ctx.restore();
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const tickCount = Math.min(5, timeline.length);
      for (let tick = 0; tick < tickCount; tick += 1) {
        const index = tickCount === 1 ? 0 : Math.round((tick / (tickCount - 1)) * (timeline.length - 1));
        ctx.fillText(formatDateTime(timeline[index].timestamp, false), x(index), height - margin.bottom + 12);
      }
    };
    drawGrid();

    function drawLine(key, color, dashed) {
      ctx.save();
      ctx.beginPath();
      timeline.forEach((row, index) => {
        const value = toNumber(row[key], 0);
        if (index === 0) ctx.moveTo(x(index), y(value));
        else ctx.lineTo(x(index), y(value));
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = dashed ? 1.5 : 2.2;
      if (dashed) ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.restore();
    }
    drawLine("demand_mbps", palette.coral, false);
    drawLine("predicted_demand_mbps", palette.cyan, true);

    timeline.forEach((row, index) => {
      if (toNumber(row.spike_count, 0) <= 0 && toNumber(row.event_count, 0) <= 0) return;
      ctx.beginPath();
      ctx.arc(x(index), y(toNumber(row.demand_mbps, 0)), 3.5, 0, Math.PI * 2);
      ctx.fillStyle = palette.amber;
      ctx.fill();
    });

    const frame = state.replayFrames[state.replayIndex];
    if (frame && frame.timestamp) {
      const target = timeValue(frame.timestamp);
      let closest = 0;
      let distance = Number.POSITIVE_INFINITY;
      timeline.forEach((row, index) => {
        const candidate = timeValue(row.timestamp);
        const currentDistance = Number.isFinite(target) && Number.isFinite(candidate) ? Math.abs(candidate - target) : index;
        if (currentDistance < distance) {
          distance = currentDistance;
          closest = index;
        }
      });
      ctx.save();
      ctx.strokeStyle = palette.lime;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(x(closest), margin.top);
      ctx.lineTo(x(closest), margin.top + plotHeight);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = palette.lime;
      ctx.font = "9px SFMono-Regular, Consolas, monospace";
      ctx.textAlign = closest > timeline.length * 0.75 ? "right" : "left";
      ctx.fillText("selected", x(closest) + (closest > timeline.length * 0.75 ? -5 : 5), margin.top + 9);
      ctx.restore();
    }

    const peak = Math.max(...actualValues);
    setText(elements.demandWindow, `${formatDateTime(timeline[0].timestamp, false)} → ${formatDateTime(timeline[timeline.length - 1].timestamp, false)} · IST`);
    setText(elements.demandPeak, `Peak demand ${formatNumber(peak, 0)} Mbps`);
  }

  function drawHourChart() {
    const setup = setupCanvas(elements.hourChart);
    if (!setup) return;
    const { context: ctx, width, height } = setup;
    const profile = state.data && Array.isArray(state.data.hour_profile) ? state.data.hour_profile : [];
    if (!profile.length) {
      setChartState(elements.hourChartState, "No hourly profile was returned.", true);
      return;
    }
    setChartState(elements.hourChartState, "", false);
    const palette = colors();
    const margin = { top: 18, right: 47, bottom: 35, left: 45 };
    const plotWidth = Math.max(1, width - margin.left - margin.right);
    const plotHeight = Math.max(1, height - margin.top - margin.bottom);
    const demandValues = profile.map((row) => toNumber(row.demand_mbps, 0));
    const powerValues = profile.map((row) => toNumber(row.adaptive_power_kw, 0));
    const alwaysValues = profile.map((row) => toNumber(row.always_on_power_kw, 0));
    const demandMax = Math.max(1, ...demandValues) * 1.1;
    const powerMax = Math.max(1, ...powerValues, ...alwaysValues) * 1.1;
    const x = (index) => margin.left + (profile.length === 1 ? plotWidth / 2 : (index / (profile.length - 1)) * plotWidth);
    const yDemand = (value) => margin.top + plotHeight - (Math.max(0, value) / demandMax) * plotHeight;
    const yPower = (value) => margin.top + plotHeight - (Math.max(0, value) / powerMax) * plotHeight;
    const labelFont = "10px SFMono-Regular, Consolas, monospace";
    ctx.font = labelFont;
    ctx.lineWidth = 1;
    ctx.strokeStyle = palette.grid;
    ctx.fillStyle = palette.muted;
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (let index = 0; index <= 4; index += 1) {
      const demand = (demandMax / 4) * index;
      const yPosition = yDemand(demand);
      ctx.beginPath();
      ctx.moveTo(margin.left, yPosition);
      ctx.lineTo(width - margin.right, yPosition);
      ctx.stroke();
      ctx.fillText(formatNumber(demand, 0), margin.left - 7, yPosition);
      ctx.textAlign = "left";
      ctx.fillText(formatNumber((powerMax / 4) * index, 0), width - margin.right + 7, yPosition);
      ctx.textAlign = "right";
    }
    ctx.save();
    ctx.translate(12, margin.top + plotHeight / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center";
    ctx.fillText("Mbps", 0, 0);
    ctx.restore();
    ctx.save();
    ctx.translate(width - 10, margin.top + plotHeight / 2);
    ctx.rotate(Math.PI / 2);
    ctx.textAlign = "center";
    ctx.fillText("kW", 0, 0);
    ctx.restore();
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const tickCount = Math.min(8, profile.length);
    for (let tick = 0; tick < tickCount; tick += 1) {
      const index = tickCount === 1 ? 0 : Math.round((tick / (tickCount - 1)) * (profile.length - 1));
      ctx.fillText(formatHour(profile[index].hour), x(index), height - margin.bottom + 11);
    }

    function drawLine(values, yFunction, color, widthValue, dashed) {
      ctx.save();
      ctx.beginPath();
      values.forEach((value, index) => {
        if (index === 0) ctx.moveTo(x(index), yFunction(value));
        else ctx.lineTo(x(index), yFunction(value));
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = widthValue;
      if (dashed) ctx.setLineDash([4, 4]);
      ctx.stroke();
      ctx.restore();
    }
    drawLine(demandValues, yDemand, palette.coral, 2.2, false);
    drawLine(powerValues, yPower, palette.cyan, 2.2, false);
    if (alwaysValues.some((value) => value > 0)) drawLine(alwaysValues, yPower, palette.amber, 1.1, true);

    const frame = state.replayFrames[state.replayIndex];
    const frameDate = frame && parseTimestamp(frame.timestamp);
    if (frameDate) {
      const hour = Number(new Intl.DateTimeFormat("en-GB", { timeZone: TIME_ZONE, hour: "2-digit", hour12: false }).format(frameDate));
      let closest = 0;
      let distance = Number.POSITIVE_INFINITY;
      profile.forEach((row, index) => {
        const currentDistance = Math.abs(toNumber(row.hour, 0) - hour);
        if (currentDistance < distance) {
          distance = currentDistance;
          closest = index;
        }
      });
      ctx.save();
      ctx.strokeStyle = palette.lime;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(x(closest), margin.top);
      ctx.lineTo(x(closest), margin.top + plotHeight);
      ctx.stroke();
      ctx.restore();
    }
  }

  function drawCharts() {
    if (state.chartFrame) cancelFrame(state.chartFrame);
    state.chartFrame = scheduleFrame(() => {
      state.chartFrame = 0;
      drawDemandChart();
      drawHourChart();
    });
  }

  function csvCell(value) {
    if (value === null || value === undefined) return "";
    const text = String(value);
    return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  }

  function downloadCsv() {
    if (!state.data || !Array.isArray(state.data.timeline) || !state.data.timeline.length) return;
    const columns = [
      "timestamp",
      "demand_mbps",
      "predicted_demand_mbps",
      "active_users",
      "capacity_mbps",
      "active_capacity_mbps",
      "always_on_power_kw",
      "adaptive_power_kw",
      "energy_savings_kwh",
      "spike_count",
      "event_count",
      "utilization_pct",
    ];
    const lines = [columns.join(",")];
    state.data.timeline.forEach((row) => lines.push(columns.map((column) => csvCell(row[column])).join(",")));
    const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const seed = toNumber(state.config && state.config.seed, 0);
    link.href = url;
    link.download = `gridpulse-aggregate-timeline-seed-${Math.round(seed)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function bindEvents() {
    const requiredControls = [
      ["simulation form", elements.simulationForm],
      ["surge demo button", elements.surgeDemoButton],
      ["city selector", elements.citySelect],
      ["replay slider", elements.replaySlider],
      ["replay play button", elements.replayPlay],
      ["replay next button", elements.replayNext],
      ["CSV download button", elements.downloadCsv],
    ];
    const missing = requiredControls.filter(([, element]) => !element).map(([name]) => name);
    if (missing.length) {
      setRunStatus("error", "error");
      setError(`Dashboard markup is incomplete: missing ${missing.join(", ")}. Reload the supplied index.html.`);
      return false;
    }
    elements.simulationForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.running) return;
      try {
        runSimulation(readFormConfig(), false);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Check the simulation controls.";
        setRunStatus("error", "error");
        setError(`Invalid input: ${message}`);
      }
    });
    elements.surgeDemoButton.addEventListener("click", () => {
      if (state.running) return;
      try {
        runSimulation(readFormConfig(), true);
      } catch (error) {
        const message = error instanceof Error ? error.message : "Check the simulation controls.";
        setRunStatus("error", "error");
        setError(`Invalid input: ${message}`);
      }
    });
    elements.citySelect.addEventListener("change", () => {
      state.selectedCity = elements.citySelect.value || "__all__";
      drawCharts();
      updateSelectedCityReplay();
      renderCoverage(state.data || {});
      renderOperations(state.data || {});
    });
    ["event_probability", "anomaly_probability"].forEach((name) => {
      const input = elements.simulationForm.elements[name];
      if (input) input.addEventListener("input", setProbabilityLabels);
    });
    const faultInput = elements.simulationForm.elements.fault_probability;
    if (faultInput) faultInput.addEventListener("input", setProbabilityLabels);
    elements.replaySlider.addEventListener("input", () => {
      if (state.running) return;
      stopReplay();
      state.replayIndex = Math.max(0, Math.min(state.replayFrames.length - 1, Number(elements.replaySlider.value)));
      renderReplayFrame();
    });
    elements.replayPlay.addEventListener("click", toggleReplay);
    elements.replayNext.addEventListener("click", () => {
      if (state.running || state.replayFrames.length < 2) return;
      stopReplay();
      advanceReplay();
    });
    elements.downloadCsv.addEventListener("click", downloadCsv);
    window.addEventListener("resize", drawCharts, { passive: true });
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(drawCharts);
      if (elements.demandChart && elements.demandChart.parentElement) observer.observe(elements.demandChart.parentElement);
      if (elements.hourChart && elements.hourChart.parentElement) observer.observe(elements.hourChart.parentElement);
    }
    return true;
  }

  function init() {
    cacheElements();
    if (!bindEvents()) {
      renderNoData("The dashboard controls could not be loaded.");
      return;
    }
    setProbabilityLabels();
    renderNoData("Loading simulation…");
    try {
      runSimulation(readFormConfig(), false);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Check the simulation controls.";
      setRunStatus("error", "error");
      setError(`Invalid initial input: ${message}`);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
