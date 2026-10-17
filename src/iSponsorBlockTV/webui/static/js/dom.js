/** Small helpers for building DOM elements without innerHTML. */

export const byId = (id) => document.getElementById(id);

/**
 * Create an element. Attributes starting with "on" become event listeners, children are
 * appended as text or nodes (so user supplied text is never parsed as HTML).
 */
export function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name.startsWith("on")) {
      element.addEventListener(name.slice(2), value);
    } else {
      element.setAttribute(name, value);
    }
  }
  element.append(...children);
  return element;
}
