(() => {
  "use strict";

  const EVENT_ROW_HEIGHT = 104;
  const EVENT_OVERSCAN = 3;
  const PLAYBACK_STEP_MS = 700;
  const LOCAL_ONLY_MESSAGE = "Local UI/status only — does not contact or direct real people, vehicles, emergency services, network equipment, or external systems.";
  const prefersReducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

  const form = document.getElementById("lab-form");
  const status = document.getElementById("lab-status");
  const error = document.getElementById("lab-error");
  const citySelect = document.getElementById("city-select");
  const coverageGrid = document.getElementById("coverage-grid");

  const state = {
    data: null,
    city: "",
    loading: false,
    frames: [],
    frameIndex: 0,
    playing: false,
    playbackAnimation: 0,
    playbackLastAt: 0,
    playbackAccumulator: 0,
    speed: 1,
    coverageByCityTime: new Map(),
    coverageIssues: [],
    dispatchByCityTime: new Map(),
    frameTowersByCityTime: new Map(),
    events: [],
    eventWindowStart: -1,
    eventNodes: [],
    towerRows: new Map(),
    towerOrder: [],
    deployedHelp: new Set(),
    deployMessage: "",
    coverageCellNodes: [],
    profileResizeAnimation: 0,
  };

  const $ = (id) => document.getElementById(id);

  function numeric(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function number(value, fallback = 0) {
    const result = numeric(value);
    return result === null ? fallback : result;
  }

  function format(value, digits = 0) {
    const result = numeric(value);
    if (result === null) return "—";
    return result.toLocaleString("en-IN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function percent(value) {
    const result = numeric(value);
    return result === null ? "—" : `${format(result, 1)}%`;
  }

  function text(value, fallback = "—") {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function parseTimestamp(value) {
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (value === null || value === undefined || value === "") return null;
    const raw = String(value).trim();
    if (!raw) return null;
    const hasExplicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
    const isoLike = /^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}/.test(raw);
    const candidate = isoLike && !hasExplicitZone ? `${raw.replace(" ", "T")}+05:30` : raw;
    const date = new Date(candidate);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function timeValue(value) {
    const date = parseTimestamp(value);
    return date ? date.getTime() : Number.NaN;
  }

  function timestampKey(value) {
    const time = timeValue(value);
    return Number.isFinite(time) ? String(time) : text(value, "unknown");
  }

  function rawTimestamp(value) {
    return text(value, "unknown timestamp");
  }

  function timestamp(value) {
    const date = parseTimestamp(value);
    if (!date) return rawTimestamp(value);
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZoneName: "short",
    }).format(date);
  }

  function cleanLabel(value) {
    return text(value, "unknown").replaceAll("_", " ");
  }

  function cityTimeKey(city, value) {
    return `${text(city, "Unknown")}::${timestampKey(value)}`;
  }

  function setText(nodeOrId, value) {
    const node = typeof nodeOrId === "string" ? $(nodeOrId) : nodeOrId;
    if (node) node.textContent = value === null || value === undefined ? "" : String(value);
  }

  function setStatus(label, kind) {
    status.textContent = label;
    status.className = `lab-status ${kind}`;
  }

  function setPlaybackStatus(label) {
    const node = $("playback-status");
    if (node) node.textContent = label;
  }

  function setButtonLabel(button, label) {
    if (!button) return;
    const textNode = Array.from(button.childNodes).find((node) => node.nodeType === Node.TEXT_NODE);
    if (textNode) textNode.nodeValue = `${label} `;
    else button.prepend(document.createTextNode(`${label} `));
  }

  function readOptions() {
    const options = {};
    ["seed", "days", "cities", "towers_per_city"].forEach((name) => {
      const input = form.elements[name];
      const value = Number(input.value);
      if (!Number.isInteger(value) || value < Number(input.min) || value > Number(input.max)) {
        throw new Error(`Enter a valid ${name.replaceAll("_", " ")}.`);
      }
      options[name] = value;
    });
    const faultProbability = Number(form.elements.fault_probability.value);
    if (!Number.isFinite(faultProbability) || faultProbability < 0 || faultProbability > 0.05) {
      throw new Error("Enter a valid telemetry fault chance.");
    }
    options.fault_probability = faultProbability;
    return options;
  }

  function updateFaultLabel() {
    const value = number(form.elements.fault_probability.value);
    $("fault-label").textContent = percent(value * 100);
  }

  function setFormDisabled(disabled) {
    form.querySelectorAll("input, button").forEach((control) => {
      control.disabled = disabled;
    });
  }

  function scheduleAnimation(callback) {
    if (typeof window.requestAnimationFrame === "function") return window.requestAnimationFrame(callback);
    return window.setTimeout(() => callback(Date.now()), 16);
  }

  function cancelAnimation(animation) {
    if (!animation) return;
    if (typeof window.cancelAnimationFrame === "function") window.cancelAnimationFrame(animation);
    else window.clearTimeout(animation);
  }

  function stopPlayback() {
    state.playing = false;
    cancelAnimation(state.playbackAnimation);
    state.playbackAnimation = 0;
    state.playbackLastAt = 0;
    state.playbackAccumulator = 0;
  }

  function updatePlaybackControls() {
    const hasReplay = state.frames.length > 0 && !state.loading;
    const start = $("playback-start");
    const pause = $("playback-pause");
    const reset = $("playback-reset");
    const speed = $("playback-speed");
    const scrubber = $("playback-scrubber");
    if (start) {
      start.disabled = !hasReplay || state.playing;
      setButtonLabel(start, state.frameIndex >= state.frames.length - 1 && hasReplay ? "Start again" : "Start");
    }
    if (pause) pause.disabled = !hasReplay || !state.playing;
    if (reset) reset.disabled = !hasReplay;
    if (speed) {
      speed.disabled = !hasReplay;
      speed.value = String(state.speed);
    }
    if (scrubber) {
      scrubber.disabled = !hasReplay || state.frames.length < 2;
      scrubber.min = "0";
      scrubber.max = String(Math.max(0, state.frames.length - 1));
      scrubber.value = String(Math.min(state.frameIndex, Math.max(0, state.frames.length - 1)));
    }
  }

  function updatePlaybackStatus() {
    if (!state.frames.length) {
      setPlaybackStatus(state.loading ? "running simulation" : "waiting");
      return;
    }
    const speedText = `${state.speed}×`;
    if (state.playing) setPlaybackStatus(`playing · ${speedText}`);
    else if (state.frameIndex >= state.frames.length - 1) setPlaybackStatus("complete");
    else setPlaybackStatus("paused");
  }

  function normalizeFrames(data) {
    const replay = Array.isArray(data.replay) ? data.replay : [];
    const frames = replay
      .filter((frame) => frame && typeof frame === "object")
      .map((frame) => {
        const towers = Array.isArray(frame.towers) ? frame.towers.filter((tower) => tower && typeof tower === "object") : [];
        return {
          timestamp: frame.timestamp || towers[0]?.timestamp || null,
          towers,
        };
      })
      .filter((frame) => frame.timestamp || frame.towers.length);
    frames.sort((left, right) => {
      const a = timeValue(left.timestamp);
      const b = timeValue(right.timestamp);
      if (!Number.isFinite(a) || !Number.isFinite(b)) return 0;
      return a - b;
    });
    if (frames.length) return frames;

    const snapshot = Array.isArray(data.tower_snapshot) ? data.tower_snapshot : [];
    if (!snapshot.length) return [];
    const latest = snapshot.reduce((value, tower) => {
      if (!value || timeValue(tower.timestamp) > timeValue(value)) return tower.timestamp;
      return value;
    }, null);
    return [{ timestamp: latest, towers: snapshot }];
  }

  function frameTowersForCityTime(city, frameTimestamp) {
    return state.frameTowersByCityTime.get(cityTimeKey(city, frameTimestamp)) || [];
  }

  function buildDispatchIndex(data) {
    state.dispatchByCityTime = new Map();
    const dispatches = Array.isArray(data.operations?.dispatches) ? data.operations.dispatches : [];
    dispatches.forEach((dispatch) => {
      const key = cityTimeKey(dispatch.city, dispatch.timestamp);
      if (!state.dispatchByCityTime.has(key)) state.dispatchByCityTime.set(key, dispatch);
    });
  }

  function buildCoverageIndex(data) {
    state.coverageByCityTime = new Map();
    state.coverageIssues = [];
    const rows = Array.isArray(data.coverage?.timeline) ? data.coverage.timeline : [];
    rows.forEach((row) => {
      if (!row || typeof row !== "object") return;
      const key = cityTimeKey(row.city, row.timestamp);
      state.coverageByCityTime.set(key, row);
      const dispatchNeeded = number(row.dispatch_needed_cells) > 0;
      const uncovered = number(row.uncovered_cells) > 0;
      const faulted = Array.isArray(row.faulted_tower_ids) && row.faulted_tower_ids.length > 0;
      if (!dispatchNeeded && !uncovered && !faulted) return;
      const frameTowers = frameTowersForCityTime(row.city, row.timestamp);
      const inferredTowers = frameTowers
        .filter((tower) => tower.fault || tower.fault_detected || tower.overload || number(tower.unmet_demand_mbps) > 0)
        .map((tower) => text(tower.tower_id, "unknown tower"));
      const towerIds = Array.from(new Set([
        ...(Array.isArray(row.faulted_tower_ids) ? row.faulted_tower_ids.map((tower) => String(tower)) : []),
        ...inferredTowers,
      ]));
      state.coverageIssues.push({
        key,
        timestamp: row.timestamp,
        city: text(row.city, "Unknown city"),
        towerIds: towerIds.length ? towerIds : ["city grid"],
        covered: number(row.covered_cells),
        atRisk: number(row.at_risk_cells),
        uncovered: number(row.uncovered_cells),
        dispatchNeeded: number(row.dispatch_needed_cells),
        row,
        dispatch: state.dispatchByCityTime.get(key) || null,
      });
    });
  }

  function remedyFor(kind, context) {
    const tower = context.towerIds || "affected tower";
    if (kind === "fault") {
      return `Hold ${tower} in fail-safe boost; validate ${cleanLabel(context.reason || "telemetry") } before returning to adaptive mode.`;
    }
    if (kind === "coverage") {
      const team = context.dispatch?.team_id || "the deterministic help team";
      return `Keep backup capacity active and stage ${team} for ${format(context.dispatchNeeded || context.uncovered || 0)} cells; review at the next interval.`;
    }
    if (kind === "spike") {
      return `Hold boost headroom on ${tower} and monitor the next interval until demand returns below forecast.`;
    }
    return `Pre-stage deterministic capacity headroom on ${tower}; return to the controller's normal mode after the event.`;
  }

  function buildEvents(data) {
    const events = [];
    const order = { fault: 0, coverage: 1, event: 2, spike: 3 };
    let sequence = 0;

    state.frames.forEach((frame) => {
      const frameTimestamp = frame.timestamp;
      frame.towers.forEach((tower) => {
        const city = text(tower.city, "Unknown city");
        const towerId = text(tower.tower_id, "unknown tower");
        const base = {
          timestamp: frameTimestamp || tower.timestamp,
          city,
          towerIds: towerId,
          rawTimestamp: rawTimestamp(frameTimestamp || tower.timestamp),
          sequence: sequence += 1,
          mode: text(tower.mode || tower.controller_mode, "unknown"),
        };
        if (tower.fault || tower.fault_detected) {
          events.push({
            ...base,
            kind: "Telemetry fault",
            tone: "fault",
            mark: "×",
            severity: cleanLabel(tower.fault_severity || "high"),
            detail: `${city} · ${towerId} · ${cleanLabel(tower.fault_reason || "telemetry validation failed")}`,
            remedy: remedyFor("fault", { towerIds: towerId, reason: tower.fault_reason }),
            issueKey: cityTimeKey(city, frameTimestamp),
            order: order.fault,
          });
        }
        if (tower.event) {
          events.push({
            ...base,
            kind: "Scheduled event",
            tone: "event",
            mark: "✦",
            severity: "event",
            detail: `${city} · ${towerId} · controller headroom event`,
            remedy: remedyFor("event", { towerIds: towerId }),
            issueKey: "",
            order: order.event,
          });
        }
        if (tower.spike) {
          events.push({
            ...base,
            kind: "Demand spike",
            tone: "spike",
            mark: "!",
            severity: "alert",
            detail: `${city} · ${towerId} · ${format(tower.demand_mbps)} Mbps actual vs ${format(tower.forecast_mbps)} Mbps forecast`,
            remedy: remedyFor("spike", { towerIds: towerId }),
            issueKey: "",
            order: order.spike,
          });
        }
      });
    });

    state.coverageIssues.forEach((issue) => {
      const dispatch = issue.dispatch;
      const towerIds = issue.towerIds.join(", ");
      events.push({
        timestamp: issue.timestamp,
        city: issue.city,
        towerIds,
        rawTimestamp: rawTimestamp(issue.timestamp),
        sequence: sequence += 1,
        kind: "Coverage issue",
        tone: "coverage",
        mark: "◇",
        severity: issue.uncovered > 0 ? "critical" : "high",
        detail: `${issue.city} · ${towerIds} · ${format(issue.uncovered)} uncovered · ${format(issue.atRisk)} at risk · ${format(issue.dispatchNeeded)} dispatch cells`,
        remedy: remedyFor("coverage", {
          towerIds,
          dispatch,
          dispatchNeeded: issue.dispatchNeeded,
          uncovered: issue.uncovered,
        }),
        issueKey: issue.key,
        order: order.coverage,
      });
    });

    events.sort((left, right) => {
      const leftTime = timeValue(left.timestamp);
      const rightTime = timeValue(right.timestamp);
      if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) return leftTime - rightTime;
      if (left.order !== right.order) return left.order - right.order;
      return left.sequence - right.sequence;
    });
    events.forEach((event, index) => {
      event.key = `${timestampKey(event.timestamp)}::${event.kind}::${event.city}::${event.towerIds}::${index}`;
    });
    return events;
  }

  function prepareData(data) {
    stopPlayback();
    state.frames = normalizeFrames(data);
    state.frameIndex = 0;
    state.deployedHelp = new Set();
    state.deployMessage = "";
    state.towerRows = new Map();
    state.towerOrder = [];
    state.frameTowersByCityTime = new Map();
    state.frames.forEach((frame) => {
      const byCity = new Map();
      frame.towers.forEach((tower) => {
        const city = text(tower.city, "Unknown city");
        if (!byCity.has(city)) byCity.set(city, []);
        byCity.get(city).push(tower);
      });
      byCity.forEach((towers, city) => {
        state.frameTowersByCityTime.set(cityTimeKey(city, frame.timestamp), towers);
      });
    });
    buildDispatchIndex(data);
    buildCoverageIndex(data);
    state.events = buildEvents(data);
    state.eventWindowStart = -1;
    state.eventNodes = [];
  }

  function populateCities(data) {
    const cities = Array.isArray(data.cities)
      ? data.cities.map(String)
      : Object.keys(data.coverage?.cities || {});
    const previous = state.city;
    citySelect.replaceChildren();
    cities.forEach((city) => {
      const option = document.createElement("option");
      option.value = city;
      option.textContent = city;
      citySelect.appendChild(option);
    });
    state.city = cities.includes(previous) ? previous : cities[0] || "";
    citySelect.value = state.city;
  }

  function renderMetrics(data) {
    const summary = data.summary || {};
    const operations = data.operations || {};
    const coverageSummary = data.coverage?.summary || {};
    const operationSummary = operations.summary || {};
    $("metric-savings").textContent = percent(summary.energy_savings_pct || 0);
    $("metric-coverage").textContent = percent(coverageSummary.latest_coverage_pct ?? 0);
    $("metric-faults").textContent = format(operationSummary.fault_event_count ?? operations.fault_detector?.detected_fault_count ?? 0);
    $("metric-incidents").textContent = format(operationSummary.incident_count || 0);
    $("fault-count").textContent = format(operationSummary.fault_event_count ?? 0);
    $("incident-count").textContent = format(operationSummary.incident_count || 0);
    const interval = data.evaluation?.interval_minutes || 60;
    const runStart = state.frames[0]?.timestamp;
    const runEnd = state.frames[state.frames.length - 1]?.timestamp;
    $("run-detail").textContent = `${format(data.model?.test_rows || 0)} heldout rows · ${interval}-min samples${runStart ? ` · ${timestamp(runStart)} → ${timestamp(runEnd)}` : ""}`;
  }

  function renderCoverageGrid(data) {
    const cityData = data.coverage?.cities?.[state.city];
    const latest = cityData?.latest;
    const cells = Array.isArray(latest?.cells) ? latest.cells : [];
    state.coverageCellNodes = [];
    coverageGrid.replaceChildren();
    coverageGrid.style.setProperty("--coverage-cols", String(number(data.coverage?.grid?.cols, 5)));
    if (!cells.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No city-local coverage grid was returned.";
      coverageGrid.appendChild(empty);
      return;
    }
    cells.forEach((cell) => {
      const tile = document.createElement("div");
      const className = cell.status === "at_risk_backup" ? "at-risk" : text(cell.status, "uncovered");
      tile.className = `coverage-cell ${className}`;
      tile.textContent = cell.status === "covered" ? "OK" : cell.status === "at_risk_backup" ? "BK" : "GAP";
      tile.setAttribute("role", "img");
      tile.setAttribute("aria-label", `${text(cell.cell_id, "cell")}: ${cleanLabel(cell.status)}`);
      tile.title = `${text(cell.cell_id, "cell")} · ${cleanLabel(cell.status)} · ${format(cell.available_capacity_mbps, 1)} / ${format(cell.required_capacity_mbps, 1)} Mbps`;
      coverageGrid.appendChild(tile);
      state.coverageCellNodes.push({ tile, cell });
    });
  }

  function updateCoverageGrid(row) {
    const statuses = typeof row?.cell_statuses === "string" ? row.cell_statuses : "";
    if (!statuses || statuses.length !== state.coverageCellNodes.length) return;
    const labels = { c: ["covered", "OK"], b: ["at_risk_backup", "BK"], u: ["uncovered", "GAP"] };
    state.coverageCellNodes.forEach(({ tile, cell }, index) => {
      const [status, label] = labels[statuses[index]] || labels.u;
      tile.className = `coverage-cell ${status === "at_risk_backup" ? "at-risk" : status}`;
      tile.textContent = label;
      tile.setAttribute("aria-label", `${text(cell.cell_id, "cell")}: ${cleanLabel(status)}`);
      tile.title = `${text(cell.cell_id, "cell")} · ${cleanLabel(status)}`;
    });
  }

  function coverageAt(city, frameTimestamp) {
    return state.coverageByCityTime.get(cityTimeKey(city, frameTimestamp)) || null;
  }

  function updateLiveCoverage(frameTimestamp) {
    const cityData = state.data?.coverage?.cities?.[state.city];
    const latest = cityData?.latest;
    const live = coverageAt(state.city, frameTimestamp) || latest;
    if (!live) {
      setText("covered-cells", "—");
      setText("risk-cells", "—");
      setText("uncovered-cells", "—");
      setText("live-coverage", "—");
      setText("live-coverage-detail", "coverage unavailable");
      setText("coverage-note", "No synthetic coverage result was returned for this city.");
      return;
    }
    setText("covered-cells", format(live.covered_cells));
    setText("risk-cells", format(live.at_risk_cells));
    setText("uncovered-cells", format(live.uncovered_cells));
    setText("live-coverage", percent(live.coverage_pct));
    setText("live-coverage-detail", `${format(live.covered_cells)} covered · ${format(live.at_risk_cells)} at risk · ${format(live.uncovered_cells)} gap`);
    $("metric-coverage").textContent = percent(live.coverage_pct);
    updateCoverageGrid(live);
    setText("coverage-note", `${state.city}: ${percent(live.coverage_pct)} available at ${timestamp(live.timestamp)}. The grid is a latest city snapshot; values follow the live cursor. Synthetic capacity/fault proxy only, not RF propagation.`);
  }

  function renderDispatches(data) {
    const root = $("dispatches");
    root.replaceChildren();
    const dispatches = Array.isArray(data.operations?.dispatches) ? data.operations.dispatches : [];
    if (!dispatches.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No synthetic faults or uncovered cells required a response queue.";
      root.appendChild(empty);
      return;
    }
    dispatches.forEach((dispatch) => {
      const item = document.createElement("article");
      item.className = "dispatch";
      const mark = document.createElement("span");
      mark.className = "dispatch-mark";
      mark.textContent = dispatch.priority === "critical" ? "!" : "→";
      const copy = document.createElement("div");
      copy.className = "dispatch-copy";
      const title = document.createElement("strong");
      title.textContent = `${text(dispatch.city, "Unknown city")} · ${text(dispatch.team_id, "help team")}`;
      const detail = document.createElement("p");
      detail.textContent = `${cleanLabel(dispatch.trigger)} · ETA ${format(dispatch.eta_minutes)} min`;
      const meta = document.createElement("small");
      const deployed = state.deployedHelp.has(cityTimeKey(dispatch.city, dispatch.timestamp));
      meta.textContent = `${cleanLabel(dispatch.status)} · ${rawTimestamp(dispatch.timestamp)} · ${timestamp(dispatch.timestamp)} · ${format((dispatch.cell_ids || []).length)} cells${deployed ? " · local help staged" : ""}`;
      copy.append(title, detail, meta);
      item.append(mark, copy);
      root.appendChild(item);
    });
  }

  function createEventNode() {
    const item = document.createElement("article");
    item.className = "event-item";
    item.setAttribute("role", "listitem");
    const mark = document.createElement("span");
    mark.className = "event-mark";
    const copy = document.createElement("div");
    copy.className = "event-copy";
    const title = document.createElement("strong");
    const detail = document.createElement("p");
    const meta = document.createElement("small");
    const remedy = document.createElement("span");
    remedy.className = "event-remedy";
    const remedyLabel = document.createElement("b");
    remedyLabel.textContent = "Remedy · ";
    const remedyText = document.createElement("span");
    remedy.append(remedyLabel, remedyText);
    copy.append(title, detail, meta, remedy);
    item.append(mark, copy);
    return { item, mark, title, detail, meta, remedyText };
  }

  function updateEventNode(node, event, index) {
    const deployed = event.issueKey && state.deployedHelp.has(event.issueKey);
    const currentTimestamp = state.frames[state.frameIndex]?.timestamp;
    const isCurrent = timestampKey(event.timestamp) === timestampKey(currentTimestamp);
    node.item.className = `event-item${isCurrent ? " is-current" : ""}${deployed ? " is-deployed" : ""}`;
    node.item.dataset.eventKey = event.key;
    node.item.dataset.index = String(index);
    node.item.title = `${event.kind} · ${event.rawTimestamp}`;
    node.mark.className = `event-mark ${event.tone}`;
    node.mark.textContent = event.mark;
    node.title.textContent = `${event.kind} · ${event.severity}`;
    node.detail.textContent = event.detail;
    node.meta.textContent = `${event.city} · affected tower: ${event.towerIds} · ${event.rawTimestamp} · ${timestamp(event.timestamp)}${deployed ? " · local help staged" : ""}`;
    node.remedyText.textContent = event.remedy;
  }

  function renderEventWindow() {
    const root = $("event-stream");
    const list = $("event-stream-list");
    const spacer = $("event-stream-spacer");
    const empty = $("event-stream-empty");
    if (!state.events.length) {
      spacer.style.height = "0px";
      list.replaceChildren();
      empty.hidden = false;
      state.eventWindowStart = -1;
      return;
    }
    empty.hidden = true;
    spacer.style.height = `${state.events.length * EVENT_ROW_HEIGHT}px`;
    const visibleRows = Math.max(5, Math.ceil(root.clientHeight / EVENT_ROW_HEIGHT));
    const start = Math.max(0, Math.floor(root.scrollTop / EVENT_ROW_HEIGHT) - EVENT_OVERSCAN);
    const end = Math.min(state.events.length, start + visibleRows + EVENT_OVERSCAN * 2);
    if (start === state.eventWindowStart) {
      state.eventNodes.forEach((node) => {
        const index = Number(node.item.dataset.index);
        if (Number.isInteger(index) && state.events[index]) updateEventNode(node, state.events[index], index);
      });
      return;
    }
    state.eventWindowStart = start;
    const fragment = document.createDocumentFragment();
    const visibleNodes = [];
    for (let index = start; index < end; index += 1) {
      const node = state.eventNodes[index - start] || createEventNode();
      updateEventNode(node, state.events[index], index);
      visibleNodes.push(node);
      fragment.appendChild(node.item);
    }
    state.eventNodes = visibleNodes;
    list.style.transform = `translateY(${start * EVENT_ROW_HEIGHT}px)`;
    list.replaceChildren(fragment);
  }

  function renderEventStream() {
    setText("event-count", `${format(state.events.length)} records`);
    setText("event-stream-summary", state.events.length
      ? `${format(state.events.length)} chronological records · every replay fault, event, spike, and coverage issue is retained locally.`
      : "No relevant synthetic fault, event, spike, or coverage issue was returned for this run.");
    $("event-stream").scrollTop = 0;
    state.eventWindowStart = -1;
    renderEventWindow();
  }

  function revealCurrentEvent() {
    if (!state.playing || !state.events.length) return;
    const frame = state.frames[state.frameIndex];
    if (!frame) return;
    const frameKey = timestampKey(frame.timestamp);
    const eventIndex = state.events.findIndex((event) => timestampKey(event.timestamp) === frameKey);
    if (eventIndex < 0) return;
    const root = $("event-stream");
    const visibleStart = Math.floor(root.scrollTop / EVENT_ROW_HEIGHT);
    const visibleEnd = visibleStart + Math.max(1, Math.ceil(root.clientHeight / EVENT_ROW_HEIGHT)) - 1;
    if (eventIndex < visibleStart || eventIndex > visibleEnd) {
      root.scrollTop = Math.max(0, eventIndex - 1) * EVENT_ROW_HEIGHT;
    }
  }

  function createTowerRow() {
    const row = document.createElement("tr");
    const cells = Array.from({ length: 5 }, () => document.createElement("td"));
    cells.forEach((cell) => row.appendChild(cell));
    return { row, cells };
  }

  function updateTowerRow(entry, tower) {
    const fault = Boolean(tower.fault || tower.fault_detected);
    const gap = Boolean(tower.overload || tower.safety_violation || number(tower.overload_mbps) > 0 || number(tower.unmet_demand_mbps) > 0);
    const spike = Boolean(tower.spike);
    const event = Boolean(tower.event);
    const mode = text(tower.mode || tower.controller_mode, "unknown");
    entry.cells[0].textContent = `${text(tower.tower_id, "unknown tower")} · ${text(tower.city, "Unknown city")}`;
    entry.cells[1].textContent = `${format(tower.demand_mbps)} / ${format(tower.forecast_mbps ?? tower.predicted_demand_mbps)} Mbps`;
    entry.cells[2].textContent = `${format(tower.active_capacity_mbps)} / ${format(tower.capacity_mbps)} Mbps`;
    entry.cells[3].textContent = mode;
    entry.cells[3].className = `mode ${mode.toLowerCase().replace(/[^a-z0-9_-]/g, "-")}`;
    entry.cells[4].textContent = fault ? "telemetry fault" : gap ? "coverage gap" : spike ? "demand spike" : event ? "scheduled event" : "stable";
    entry.cells[4].className = `signal ${fault ? "fault" : gap ? "gap" : spike || event ? "alert" : ""}`;
    entry.row.title = rawTimestamp(tower.timestamp);
  }

  function renderTowers(towers) {
    const root = $("tower-rows");
    const sorted = Array.isArray(towers)
      ? towers.slice().sort((left, right) => `${text(left.city)}::${text(left.tower_id)}`.localeCompare(`${text(right.city)}::${text(right.tower_id)}`))
      : [];
    if (!sorted.length) {
      state.towerRows = new Map();
      state.towerOrder = [];
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "empty-cell";
      cell.textContent = "No tower state returned.";
      row.appendChild(cell);
      root.replaceChildren(row);
      return;
    }
    const order = sorted.map((tower) => `${text(tower.city)}::${text(tower.tower_id)}`);
    const orderChanged = order.length !== state.towerOrder.length || order.some((key, index) => key !== state.towerOrder[index]);
    const fragment = orderChanged ? document.createDocumentFragment() : null;
    sorted.forEach((tower, index) => {
      const key = order[index];
      let entry = state.towerRows.get(key);
      if (!entry) {
        entry = createTowerRow();
        state.towerRows.set(key, entry);
      }
      updateTowerRow(entry, tower);
      if (fragment) fragment.appendChild(entry.row);
    });
    Array.from(state.towerRows.keys()).forEach((key) => {
      if (!order.includes(key)) state.towerRows.delete(key);
    });
    if (fragment) root.replaceChildren(fragment);
    state.towerOrder = order;
  }

  function frameTowers(frame) {
    const towers = Array.isArray(frame?.towers) ? frame.towers : [];
    return state.city ? towers.filter((tower) => text(tower.city, "") === state.city) : towers;
  }

  function aggregateFrame(frame) {
    const rows = frameTowers(frame);
    const totals = { active_users: 0, demand_mbps: 0, forecast_mbps: 0, adaptive_power_kw: 0 };
    const present = { active_users: false, demand_mbps: false, forecast_mbps: false, adaptive_power_kw: false };
    const modes = {};
    rows.forEach((tower) => {
      ["active_users", "demand_mbps", "forecast_mbps", "adaptive_power_kw"].forEach((key) => {
        const value = numeric(tower[key]);
        if (value !== null) {
          totals[key] += value;
          present[key] = true;
        }
      });
      const mode = cleanLabel(tower.mode || tower.controller_mode || "unknown");
      modes[mode] = (modes[mode] || 0) + 1;
    });
    Object.keys(present).forEach((key) => {
      if (!present[key]) totals[key] = null;
    });
    const modeText = Object.entries(modes).sort((left, right) => right[1] - left[1]).map(([mode, count]) => `${mode} ${count}`).join(" · ");
    return { ...totals, towerCount: rows.length, modeText: modeText || "no tower mode" };
  }

  function profileTimeline() {
    const cityTimeline = state.data?.city_timelines?.[state.city];
    return Array.isArray(cityTimeline) && cityTimeline.length ? cityTimeline : (Array.isArray(state.data?.timeline) ? state.data.timeline : []);
  }

  function drawProfile() {
    const canvas = $("profile-chart");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(10, Math.floor(rect.width));
    const height = Math.max(10, Math.floor(rect.height));
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const timeline = profileTimeline();
    const styles = getComputedStyle(document.documentElement);
    const line = styles.getPropertyValue("--line").trim() || "rgba(255,255,255,.2)";
    const muted = styles.getPropertyValue("--muted").trim() || "#8e8e8e";
    if (!timeline.length) {
      context.fillStyle = muted;
      context.font = "11px SFMono-Regular, Consolas, monospace";
      context.fillText("Run a lab to draw the demand profile.", 14, height / 2);
      return;
    }
    const actual = timeline.map((row) => number(row.demand_mbps));
    const forecast = timeline.map((row) => number(row.predicted_demand_mbps));
    const maximum = Math.max(1, ...actual, ...forecast) * 1.08;
    const margin = { left: 12, right: 10, top: 12, bottom: 13 };
    const plotWidth = Math.max(1, width - margin.left - margin.right);
    const plotHeight = Math.max(1, height - margin.top - margin.bottom);
    const x = (index) => margin.left + (timeline.length === 1 ? plotWidth / 2 : (index / (timeline.length - 1)) * plotWidth);
    const y = (value) => margin.top + plotHeight - (Math.max(0, value) / maximum) * plotHeight;
    context.lineWidth = 1;
    context.strokeStyle = line;
    for (let index = 0; index <= 4; index += 1) {
      const yPosition = margin.top + (plotHeight / 4) * index;
      context.beginPath();
      context.moveTo(margin.left, yPosition);
      context.lineTo(width - margin.right, yPosition);
      context.stroke();
    }
    function drawLine(values, stroke, dash = []) {
      context.beginPath();
      context.setLineDash(dash);
      context.lineWidth = 1.6;
      context.strokeStyle = stroke;
      values.forEach((value, index) => {
        if (index === 0) context.moveTo(x(index), y(value));
        else context.lineTo(x(index), y(value));
      });
      context.stroke();
      context.setLineDash([]);
    }
    drawLine(actual, "#ffffff");
    drawLine(forecast, "#777777", [4, 4]);
    setText("profile-interval", `${format(timeline.length)} intervals · ${number(state.data?.evaluation?.interval_minutes, 60)}-min samples`);
    setText("profile-start", timestamp(timeline[0]?.timestamp));
    setText("profile-end", timestamp(timeline[timeline.length - 1]?.timestamp));
  }

  function updateProfileCursor(frameTimestamp) {
    const cursor = $("profile-cursor");
    const denominator = Math.max(1, state.frames.length - 1);
    const progress = Math.max(0, Math.min(1, state.frameIndex / denominator));
    if (cursor) {
      cursor.style.left = `${progress * 100}%`;
      cursor.title = rawTimestamp(frameTimestamp);
    }
    setText("profile-cursor-label", `cursor at ${timestamp(frameTimestamp)}`);
  }

  function issueForPlayback() {
    if (!state.coverageIssues.length) return null;
    const frame = state.frames[state.frameIndex];
    const currentKey = frame ? cityTimeKey(state.city, frame.timestamp) : "";
    return state.coverageIssues.find((issue) => issue.key === currentKey) || null;
  }

  function updateDeployControl() {
    const button = $("deploy-help-team");
    const issue = issueForPlayback();
    if (!button) return;
    button.disabled = state.loading || !state.data || !issue;
    button.title = issue ? `Stage local help-team simulation for ${issue.city} at ${rawTimestamp(issue.timestamp)}` : "No coverage-related issue was returned for this run.";
    if (state.deployMessage) {
      setText("deploy-status", state.deployMessage);
    } else if (issue) {
      setText("deploy-status", `Ready for ${issue.city} · ${issue.towerIds.join(", ")} at ${timestamp(issue.timestamp)}.`);
    } else {
      setText("deploy-status", "No coverage-related issue at the current interval. Scrub or play until one appears.");
    }
  }

  function updateFrame() {
    const frame = state.frames[state.frameIndex];
    if (!frame) {
      updatePlaybackControls();
      updatePlaybackStatus();
      return;
    }
    const aggregate = aggregateFrame(frame);
    const frameTimestamp = frame.timestamp;
    setText("playback-position", `Interval ${state.frameIndex + 1} of ${state.frames.length} · ${timestamp(frameTimestamp)}`);
    setText("live-timestamp", timestamp(frameTimestamp));
    $("live-timestamp").title = rawTimestamp(frameTimestamp);
    setText("live-users", format(aggregate.active_users));
    setText("live-demand", aggregate.demand_mbps === null ? "—" : `${format(aggregate.demand_mbps)} Mbps`);
    setText("live-forecast", aggregate.forecast_mbps === null ? "forecast —" : `forecast ${format(aggregate.forecast_mbps)} Mbps`);
    updateLiveCoverage(frameTimestamp);
    updateProfileCursor(frameTimestamp);
    renderTowers(frame.towers);
    updateDeployControl();
    revealCurrentEvent();
    renderEventWindow();
    updatePlaybackControls();
    updatePlaybackStatus();
  }

  function playbackTick(now) {
    if (!state.playing) return;
    if (!state.playbackLastAt) state.playbackLastAt = now;
    const elapsed = Math.min(500, Math.max(0, now - state.playbackLastAt));
    state.playbackLastAt = now;
    state.playbackAccumulator += elapsed * state.speed;
    const steps = Math.floor(state.playbackAccumulator / PLAYBACK_STEP_MS);
    if (steps > 0) {
      state.playbackAccumulator -= steps * PLAYBACK_STEP_MS;
      state.frameIndex = Math.min(state.frames.length - 1, state.frameIndex + steps);
      updateFrame();
      if (state.frameIndex >= state.frames.length - 1) {
        stopPlayback();
        updatePlaybackControls();
        updatePlaybackStatus();
        return;
      }
    }
    state.playbackAnimation = scheduleAnimation(playbackTick);
  }

  function startPlayback() {
    if (!state.frames.length || state.loading) return;
    if (state.frameIndex >= state.frames.length - 1) {
      state.frameIndex = 0;
      updateFrame();
    }
    state.playing = true;
    state.playbackLastAt = 0;
    state.playbackAccumulator = 0;
    updatePlaybackControls();
    updatePlaybackStatus();
    state.playbackAnimation = scheduleAnimation(playbackTick);
  }

  function pausePlayback() {
    if (!state.playing) return;
    stopPlayback();
    updatePlaybackControls();
    updatePlaybackStatus();
  }

  function resetPlayback() {
    stopPlayback();
    if (state.frames.length) {
      state.frameIndex = 0;
      updateFrame();
    }
    updatePlaybackControls();
    updatePlaybackStatus();
  }

  function deployHelpTeam() {
    const issue = issueForPlayback();
    if (!issue) {
      state.deployMessage = `No coverage-related issue is available in this run. ${LOCAL_ONLY_MESSAGE}`;
      updateDeployControl();
      return;
    }
    const dispatch = issue.dispatch || state.dispatchByCityTime.get(issue.key);
    const team = dispatch?.team_id || `${issue.city.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-team-01`;
    const currentTimestamp = state.frames[state.frameIndex]?.timestamp;
    const timing = timestampKey(currentTimestamp) === timestampKey(issue.timestamp) ? "current" : "next";
    state.deployedHelp.add(issue.key);
    state.deployMessage = `Simulation-only ${timing} help team ${team} staged for ${issue.city} · ${issue.towerIds.join(", ")} at ${rawTimestamp(issue.timestamp)}. ${LOCAL_ONLY_MESSAGE}`;
    renderDispatches(state.data);
    updateDeployControl();
    renderEventWindow();
  }

  function renderNoData(message) {
    state.frames = [];
    state.events = [];
    state.coverageIssues = [];
    state.coverageByCityTime = new Map();
    state.dispatchByCityTime = new Map();
    state.frameTowersByCityTime = new Map();
    state.towerRows = new Map();
    state.towerOrder = [];
    state.coverageCellNodes = [];
    state.eventWindowStart = -1;
    state.eventNodes = [];
    ["metric-savings", "metric-coverage", "metric-faults", "metric-incidents", "fault-count", "incident-count", "covered-cells", "risk-cells", "uncovered-cells", "live-timestamp", "live-users", "live-demand", "live-coverage", "profile-start", "profile-end", "profile-cursor-label"].forEach((id) => setText(id, "—"));
    setText("live-timezone", "Asia/Kolkata");
    setText("live-forecast", "forecast —");
    setText("live-coverage-detail", "waiting for interval");
    setText("playback-position", message || "No replay loaded");
    setText("profile-interval", "waiting for run");
    setText("coverage-note", "Synthetic availability proxy only. This does not estimate RF propagation, signal strength, geographic radius, subscriber reach, or an SLA.");
    setText("run-detail", "waiting for simulation");
    setText("deploy-status", state.loading ? "Loading a synthetic run…" : "A help-team simulation will become available for a coverage-related issue in the run.");
    setText("event-count", "0 records");
    setText("event-stream-summary", message || "Run the lab to inspect every relevant synthetic fault, event, spike, and coverage issue.");
    citySelect.replaceChildren(new Option("Run a lab first", ""));
    coverageGrid.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-state", textContent: "Run the lab to render the city-local coverage grid." }));
    $("dispatches").replaceChildren(Object.assign(document.createElement("p"), { className: "empty-state", textContent: "Response-team simulations will appear here." }));
    const emptyTowerRow = document.createElement("tr");
    const emptyTowerCell = document.createElement("td");
    emptyTowerCell.colSpan = 5;
    emptyTowerCell.className = "empty-cell";
    emptyTowerCell.textContent = "Run the lab to load tower states.";
    emptyTowerRow.appendChild(emptyTowerCell);
    $("tower-rows").replaceChildren(emptyTowerRow);
    $("event-stream-list").replaceChildren();
    $("event-stream-spacer").style.height = "0px";
    $("event-stream-empty").hidden = false;
    const canvas = $("profile-chart");
    if (canvas) {
      const context = canvas.getContext("2d");
      if (context) context.clearRect(0, 0, canvas.width, canvas.height);
    }
    const cursor = $("profile-cursor");
    if (cursor) cursor.style.left = "0%";
    updatePlaybackControls();
    updatePlaybackStatus();
    updateDeployControl();
  }

  async function run(options, surge = false) {
    stopPlayback();
    state.loading = true;
    state.data = null;
    renderNoData("Running simulation…");
    setStatus("running", "running");
    error.textContent = "";
    setFormDisabled(true);
    updatePlaybackControls();
    let shouldAutoplay = false;
    try {
      const response = await fetch("api/simulation", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ ...options, ...(surge ? { surge_demo: true } : {}) }),
      });
      let payload;
      try {
        payload = await response.json();
      } catch (_parseError) {
        throw new Error(`The simulation server returned an unreadable response (${response.status}).`);
      }
      if (!response.ok) {
        const serverMessage = payload && typeof payload.error === "string" ? payload.error : `HTTP ${response.status}`;
        throw new Error(serverMessage);
      }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("The simulation response was not an object.");
      state.data = payload;
      populateCities(payload);
      prepareData(payload);
      renderMetrics(payload);
      renderCoverageGrid(payload);
      renderDispatches(payload);
      renderEventStream();
      drawProfile();
      updateFrame();
      setStatus("ready", "ready");
      shouldAutoplay = state.frames.length > 1;
    } catch (runError) {
      setStatus("error", "error");
      error.textContent = runError instanceof Error ? runError.message : "The lab could not complete the simulation.";
      renderNoData("Simulation unavailable");
    } finally {
      state.loading = false;
      setFormDisabled(false);
      updatePlaybackControls();
      updatePlaybackStatus();
      updateDeployControl();
      if (shouldAutoplay && !prefersReducedMotion) startPlayback();
    }
  }

  function scheduleProfileRedraw() {
    if (state.profileResizeAnimation) return;
    state.profileResizeAnimation = scheduleAnimation(() => {
      state.profileResizeAnimation = 0;
      if (state.data) drawProfile();
    });
  }

  form.elements.fault_probability.addEventListener("input", updateFaultLabel);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    try { run(readOptions()); } catch (runError) { error.textContent = runError.message; }
  });
  $("lab-surge").addEventListener("click", () => {
    try { run(readOptions(), true); } catch (runError) { error.textContent = runError.message; }
  });
  $("playback-start").addEventListener("click", startPlayback);
  $("playback-pause").addEventListener("click", pausePlayback);
  $("playback-reset").addEventListener("click", resetPlayback);
  $("playback-speed").addEventListener("change", (event) => {
    state.speed = number(event.target.value, 1);
    updatePlaybackStatus();
  });
  $("playback-scrubber").addEventListener("input", (event) => {
    pausePlayback();
    state.frameIndex = Math.max(0, Math.min(state.frames.length - 1, Math.round(number(event.target.value))));
    updateFrame();
  });
  $("deploy-help-team").addEventListener("click", deployHelpTeam);
  citySelect.addEventListener("change", () => {
    state.city = citySelect.value;
    if (state.data) {
      renderCoverageGrid(state.data);
      drawProfile();
      updateFrame();
    }
  });
  $("event-stream").addEventListener("scroll", renderEventWindow, { passive: true });
  window.addEventListener("resize", scheduleProfileRedraw, { passive: true });

  updateFaultLabel();
  renderNoData("Run the lab to load a replay.");
  try {
    run(readOptions());
  } catch (runError) {
    setStatus("error", "error");
    error.textContent = runError.message;
  }
})();
