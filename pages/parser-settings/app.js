const bridge = window.AstrBotPluginPage;
const parsersPanel = document.getElementById("parsers-panel");
const skinsPanel = document.getElementById("skins-panel");
const parsersElement = document.getElementById("parsers");
const skinsElement = document.getElementById("skins");
const skinMessage = document.getElementById("skin-message");
const statusElement = document.getElementById("status");

function setStatus(message) {
  statusElement.textContent = message;
}

function createField(field, value) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  const label = document.createElement("label");
  label.textContent = field.label;
  wrapper.append(label);

  let input;
  if (field.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
  } else if (field.type === "select") {
    input = document.createElement("select");
    for (const option of field.options) {
      const item = document.createElement("option");
      item.value = option;
      item.textContent = option;
      item.selected = option === value;
      input.append(item);
    }
  } else if (field.type === "text") {
    input = document.createElement("textarea");
    input.rows = 4;
    input.value = value ?? "";
  } else if (field.type === "list") {
    input = document.createElement("textarea");
    input.rows = 3;
    input.value = Array.isArray(value) ? value.join("\n") : "";
    input.placeholder = "每行一个选项";
  } else {
    input = document.createElement("input");
    input.type = field.type === "int" || field.type === "float" ? "number" : "text";
    input.value = value ?? "";
  }
  input.dataset.key = field.key;
  input.dataset.type = field.type;
  wrapper.append(input);
  if (field.hint) {
    const hint = document.createElement("small");
    hint.textContent = field.hint;
    wrapper.append(hint);
  }
  return wrapper;
}

function readField(input) {
  if (input.dataset.type === "bool") return input.checked;
  if (input.dataset.type === "int") return Number.parseInt(input.value, 10);
  if (input.dataset.type === "float") return Number.parseFloat(input.value);
  if (input.dataset.type === "list") {
    return input.value
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return input.value;
}

function renderParsers(parsers) {
  parsersElement.replaceChildren();
  for (const parser of parsers) {
    const card = document.createElement("article");
    card.className = "parser";
    const title = document.createElement("h2");
    title.textContent = parser.display_name;
    const enabledLabel = document.createElement("label");
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = parser.enabled;
    enabled.dataset.role = "enabled";
    enabledLabel.append(enabled, " 启用");
    card.append(title, enabledLabel);
    for (const field of parser.fields) {
      card.append(createField(field, parser.values[field.key]));
    }
    const save = document.createElement("button");
    save.textContent = "保存";
    save.addEventListener("click", async () => {
      const values = {};
      card.querySelectorAll("[data-key]").forEach((input) => {
        values[input.dataset.key] = readField(input);
      });
      setStatus(`正在保存 ${parser.display_name}...`);
      try {
        await bridge.apiPost(`parser-settings/parser/${parser.name}`, {
          enabled: enabled.checked,
          values,
        });
        setStatus(`${parser.display_name} 已保存`);
        await loadParsers();
      } catch (error) {
        setStatus(`保存失败: ${error.message}`);
      }
    });
    card.append(save);
    parsersElement.append(card);
  }
}

async function loadParsers() {
  const data = await bridge.apiGet("parser-settings/parsers");
  renderParsers(data.parsers || []);
}

async function loadSkins() {
  const data = await bridge.apiGet("parser-settings/skins");
  skinMessage.textContent = data.message || "";
  skinsElement.replaceChildren();
  for (const skin of data.skins || []) {
    const label = document.createElement("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "skin";
    radio.value = skin.name;
    radio.checked = skin.name === data.current;
    radio.disabled = !data.editable;
    label.append(radio, ` ${skin.display_name}`);
    skinsElement.append(label);
  }
}

document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    const parsersActive = button.dataset.tab === "parsers";
    parsersPanel.hidden = !parsersActive;
    skinsPanel.hidden = parsersActive;
  });
});

await bridge.ready();
try {
  await Promise.all([loadParsers(), loadSkins()]);
} catch (error) {
  setStatus(`加载失败: ${error.message}`);
}
