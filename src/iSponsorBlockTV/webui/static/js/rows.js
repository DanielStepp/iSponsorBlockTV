import { h } from "./dom.js";

/**
 * An editable list of rows with a "Remove" button per row.
 *
 * `columns` describes the inputs of one row: { key, placeholder, label, type }.
 * The rows are plain objects; the list keeps them in sync with the inputs.
 */
export function createRowList(container, { rowClass, columns, emptyText, removeLabel }) {
  let rows = [];

  function render() {
    container.replaceChildren();
    rows.forEach((row, index) => container.append(renderRow(row, index)));
    if (rows.length === 0 && emptyText) {
      container.append(h("p", { class: "hint" }, emptyText));
    }
  }

  function renderRow(row, index) {
    const remove = h(
      "button",
      {
        type: "button",
        class: "danger",
        "aria-label": removeLabel,
        onclick: () => {
          rows.splice(index, 1);
          render();
        },
      },
      "Remove",
    );
    return h("div", { class: `row ${rowClass}` }, ...columns.map((c) => renderInput(row, c)), remove);
  }

  function renderInput(row, { key, placeholder, label, type = "text" }) {
    const input = h("input", { type, placeholder, "aria-label": label });
    input.value = row[key];
    input.addEventListener("input", () => {
      row[key] = type === "number" ? Number(input.value) || 0 : input.value;
    });
    return input;
  }

  return {
    /** Replace all rows. */
    set(newRows) {
      rows = newRows.map((row) => ({ ...row }));
      render();
    },
    add(row) {
      rows.push({ ...row });
      render();
    },
    /** A copy of the current rows. */
    get: () => rows.map((row) => ({ ...row })),
    has: (key, value) => rows.some((row) => row[key] === value),
  };
}
