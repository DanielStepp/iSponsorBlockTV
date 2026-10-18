/*
 * iSponsorBlockTV web UI.
 *
 * Loads the config from the server, lets the user edit it and saves it back.
 * The form is the single source of truth: nothing is mirrored in JavaScript state,
 * the values are read from the page when saving.
 *
 * Markup conventions (see index.html):
 *   data-setting="name"  a control that holds the config value "name"
 *   data-field="name"    an input inside a row (device or channel)
 *   data-action="remove" a button that removes its row
 */

"use strict";

// Must match CSRF_HEADER / CSRF_VALUE in security.py
const POST_HEADERS = {
  "Content-Type": "application/json",
  "X-Requested-With": "iSponsorBlockTV",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

/* ---------- Server API ---------- */

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Error ${response.status}`);
  }
  return data;
}

const api = {
  getConfig: () => request("/api/config"),
  saveConfig: (config) => request("/api/config", config),
  pair: (code) => request("/api/pair", { code }),
  restart: () => request("/api/restart", {}),
};

/* ---------- Status bar ---------- */

function showStatus(text, kind = "") {
  const status = $("#status");
  status.textContent = text;
  status.className = kind; // "success", "error" or ""
}

function setSaveButtonsDisabled(disabled) {
  $("#save").disabled = disabled;
  $("#save-restart").disabled = disabled;
}

/* ---------- Simple controls (data-setting) ---------- */

function setControlValue(control, value) {
  if (control.type === "checkbox") {
    control.checked = Boolean(value);
  } else {
    control.value = value ?? "";
  }
}

function readControlValue(control) {
  if (control.type === "checkbox") return control.checked;
  if (control.type === "number") return Number(control.value);
  return control.value;
}

function fillSettings(config) {
  for (const control of $$("[data-setting]")) {
    // Settings the server does not send (API key, "remove key") are reset
    setControlValue(control, config[control.dataset.setting]);
  }
}

function readSettings() {
  const settings = {};
  for (const control of $$("[data-setting]")) {
    settings[control.dataset.setting] = readControlValue(control);
  }
  return settings;
}

/* ---------- Rows (devices and channels) ---------- */

function cloneTemplate(selector) {
  return $(selector).content.firstElementChild.cloneNode(true);
}

function addRow(container, template, values = {}) {
  const row = cloneTemplate(template);
  for (const input of row.querySelectorAll("[data-field]")) {
    input.value = values[input.dataset.field] ?? "";
  }
  $(container).append(row);
  updateEmptyState();
}

function readRows(container) {
  return $$(`${container} .row`).map((row) => {
    const values = {};
    for (const input of row.querySelectorAll("[data-field]")) {
      values[input.dataset.field] = input.type === "number" ? Number(input.value) || 0 : input.value;
    }
    return values;
  });
}

function fillRows(container, template, rows) {
  $(container).replaceChildren();
  for (const values of rows) addRow(container, template, values);
  updateEmptyState();
}

function updateEmptyState() {
  $("#no-devices").hidden = $$("#devices .row").length > 0;
}

/* ---------- Categories ---------- */

function fillCategories(options, selected) {
  // Categories in the config that this page does not know are kept, labelled by their id
  const known = new Set(options.map((option) => option.id));
  const unknown = selected.filter((id) => !known.has(id)).map((id) => ({ id, label: id }));

  const container = $("#categories");
  container.replaceChildren();
  for (const { id, label } of [...options, ...unknown]) {
    const item = cloneTemplate("#tpl-category");
    const checkbox = item.querySelector("input");
    checkbox.value = id;
    checkbox.checked = selected.includes(id);
    item.querySelector("span").textContent = label;
    container.append(item);
  }
}

function readCategories() {
  return $$("#categories input:checked").map((checkbox) => checkbox.value);
}

/* ---------- Load and save ---------- */

async function loadConfig() {
  const data = await api.getConfig();
  const { config } = data;

  fillSettings(config);
  fillRows("#devices", "#tpl-device", config.devices);
  fillRows("#channels", "#tpl-channel", config.channel_whitelist);
  fillCategories(data.categories, config.skip_categories);

  $("#api-key-hint").textContent = config.apikey_set
    ? "A key is saved. Leave the field empty to keep it."
    : "No key saved.";
  $("#password-warning").hidden = data.password_protected;
  $("#pair-box").hidden = !data.can_pair;
  $("#save-restart").hidden = !data.can_restart;
}

function collectConfig() {
  return {
    ...readSettings(),
    skip_categories: readCategories(),
    devices: readRows("#devices"),
    channel_whitelist: readRows("#channels"),
  };
}

async function save({ restart }) {
  setSaveButtonsDisabled(true);
  try {
    await api.saveConfig(collectConfig());
    if (restart) {
      await api.restart();
      showStatus("Saved. The program is restarting …", "success");
    } else {
      showStatus("Saved. Applies after the next restart.", "success");
      await loadConfig();
    }
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    setSaveButtonsDisabled(false);
  }
}

/* ---------- Pairing ---------- */

async function pairDevice() {
  const input = $("#pair-code");
  const code = input.value.trim();
  if (!code) return;

  $("#pair-button").disabled = true;
  try {
    const { screen_id, name } = await api.pair(code);
    if (readRows("#devices").some((device) => device.screen_id === screen_id)) {
      showStatus("This device is already added.", "error");
    } else {
      addRow("#devices", "#tpl-device", { screen_id, name, offset: 0 });
      input.value = "";
      showStatus("Device added. Now save.", "success");
    }
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    $("#pair-button").disabled = false;
  }
}

/* ---------- Start ---------- */

function bindEvents() {
  $("#save").addEventListener("click", () => save({ restart: false }));
  $("#save-restart").addEventListener("click", () => {
    if (confirm("Save and restart the program now?")) save({ restart: true });
  });
  $("#add-device").addEventListener("click", () => addRow("#devices", "#tpl-device"));
  $("#add-channel").addEventListener("click", () => addRow("#channels", "#tpl-channel"));
  $("#pair-button").addEventListener("click", pairDevice);

  // One listener for the "Remove" buttons of all rows, including rows added later
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action='remove']");
    if (!button) return;
    button.closest(".row").remove();
    updateEmptyState();
  });
}

bindEvents();
loadConfig().catch((error) => showStatus(error.message, "error"));
