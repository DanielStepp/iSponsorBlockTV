import { h } from "./dom.js";

/**
 * Show one checkbox per SponsorBlock category.
 * Categories that are selected in the config but unknown to the UI are shown as well, so
 * that saving never silently drops them.
 */
export function renderCategories(container, available, selected) {
  const known = new Set(available.map((category) => category.id));
  const unknown = selected
    .filter((id) => !known.has(id))
    .map((id) => ({ id, label: id }));

  container.replaceChildren();
  for (const category of [...available, ...unknown]) {
    const checkbox = h("input", { type: "checkbox", value: category.id });
    checkbox.checked = selected.includes(category.id);
    container.append(h("label", { class: "check" }, checkbox, category.label));
  }
}

/** The ids of all checked categories. */
export function selectedCategories(container) {
  return [...container.querySelectorAll("input:checked")].map((input) => input.value);
}
