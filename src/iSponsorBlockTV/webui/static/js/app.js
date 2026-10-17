import * as api from "./api.js";
import { renderCategories, selectedCategories } from "./categories.js";
import { byId } from "./dom.js";
import { createRowList } from "./rows.js";

const CHECKBOXES = ["skip_ads", "mute_ads", "auto_play", "skip_count_tracking", "use_proxy"];

const devices = createRowList(byId("devices"), {
  rowClass: "row-device",
  columns: [
    { key: "name", placeholder: "Name", label: "Name" },
    { key: "screen_id", placeholder: "Screen ID", label: "Screen ID" },
    { key: "offset", placeholder: "0", label: "Offset in ms", type: "number" },
  ],
  emptyText: "No device added yet.",
  removeLabel: "Remove device",
});

const whitelist = createRowList(byId("whitelist"), {
  rowClass: "row-channel",
  columns: [
    { key: "id", placeholder: "Channel ID", label: "Channel ID" },
    { key: "name", placeholder: "Name", label: "Channel name" },
  ],
  removeLabel: "Remove channel",
});

const categoriesBox = byId("categories");
const status = byId("status");
const saveButtons = [byId("save"), byId("save-restart")];
let availableCategories = [];

function showStatus(text, kind = "") {
  status.textContent = text;
  status.className = kind;
}

/** Run an action and show its error, if any. Disables `buttons` while it runs. */
async function guarded(action, buttons = []) {
  buttons.forEach((button) => (button.disabled = true));
  try {
    await action();
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    buttons.forEach((button) => (button.disabled = false));
  }
}

// --- config <-> form ------------------------------------------------------------------

function fillForm(config) {
  devices.set(config.devices);
  whitelist.set(config.channel_whitelist);
  renderCategories(categoriesBox, availableCategories, config.skip_categories);
  for (const id of CHECKBOXES) {
    byId(id).checked = Boolean(config[id]);
  }
  byId("minimum_skip_length").value = config.minimum_skip_length;
  byId("join_name").value = config.join_name;
  byId("sponsorblock_api_url").value = config.sponsorblock_api_url;
  byId("apikey").value = "";
  byId("clear_apikey").checked = false;
  byId("apikey-hint").textContent = config.apikey_set
    ? "A key is saved. Leave the field empty to keep it."
    : "No key saved.";
}

function readForm() {
  const config = {
    devices: devices.get(),
    channel_whitelist: whitelist.get(),
    skip_categories: selectedCategories(categoriesBox),
    minimum_skip_length: Number(byId("minimum_skip_length").value),
    join_name: byId("join_name").value,
    sponsorblock_api_url: byId("sponsorblock_api_url").value,
    apikey: byId("apikey").value,
    clear_apikey: byId("clear_apikey").checked,
  };
  for (const id of CHECKBOXES) {
    config[id] = byId(id).checked;
  }
  return config;
}

// --- actions --------------------------------------------------------------------------

async function load() {
  const data = await api.loadConfig();
  availableCategories = data.categories;
  fillForm(data.config);
  byId("save-restart").hidden = !data.can_restart;
  byId("pairing").hidden = !data.can_pair;
  byId("warning").hidden = data.password_protected;
}

async function save({ restart }) {
  await api.saveConfig(readForm());
  if (restart) {
    await api.restartProgram();
    showStatus("Saved. The program is restarting …", "ok");
  } else {
    showStatus("Saved. Applies after the next restart.", "ok");
    await load();
  }
}

async function pair() {
  const input = byId("pairing-code");
  const code = input.value.trim();
  if (!code) return;
  const device = await api.pairDevice(code);
  if (devices.has("screen_id", device.screen_id)) {
    showStatus("This device is already added.", "error");
    return;
  }
  devices.add({ screen_id: device.screen_id, name: device.name, offset: 0 });
  input.value = "";
  showStatus("Device added. Now save.", "ok");
}

// --- wiring ---------------------------------------------------------------------------

byId("save").addEventListener("click", () => guarded(() => save({ restart: false }), saveButtons));
byId("save-restart").addEventListener("click", () => {
  if (confirm("Save and restart the program now?")) {
    guarded(() => save({ restart: true }), saveButtons);
  }
});
byId("add-device").addEventListener("click", () =>
  devices.add({ screen_id: "", name: "", offset: 0 }),
);
byId("add-channel").addEventListener("click", () => whitelist.add({ id: "", name: "" }));
byId("pair").addEventListener("click", () => guarded(pair, [byId("pair")]));

guarded(load);
