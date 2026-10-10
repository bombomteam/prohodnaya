// Dependency-free DOM reconciliation: preserve live forms, FileList and camera.
function identity(node) {
  return node.nodeType === 1 ? `${node.tagName}:${node.id || node.getAttribute("class") || ""}` : `node:${node.nodeType}`;
}
function reconcile(parent, desired) {
  const available = new Map();
  for (const node of [...parent.childNodes]) {
    const key = identity(node); if (!available.has(key)) available.set(key, []);
    available.get(key).push(node);
  }
  let cursor = parent.firstChild;
  const retained = new Set();
  for (const next of [...desired.childNodes]) {
    const current = available.get(identity(next))?.shift();
    const node = current || next.cloneNode(true);
    if (current) {
      if (node.nodeType === 3) node.textContent = next.textContent;
      else if (node.nodeType === 1) {
        const value = ["INPUT","TEXTAREA","SELECT"].includes(node.tagName) ? node.value : undefined;
        for (const attr of [...node.attributes]) if (!next.hasAttribute(attr.name) && !(["DIALOG", "DETAILS"].includes(node.tagName) && attr.name === "open")) node.removeAttribute(attr.name);
        for (const attr of next.attributes) if (node.getAttribute(attr.name) !== attr.value) node.setAttribute(attr.name, attr.value);
        if (node.tagName !== "TEXTAREA") reconcile(node, next);
        if (value !== undefined && node.type !== "file") node.value = value;
      }
    }
    if (node !== cursor) parent.insertBefore(node, cursor);
    retained.add(node); cursor = node.nextSibling;
  }
  for (const node of [...parent.childNodes]) if (!retained.has(node)) node.remove();
}
export function createView(element) {
  return html => {
    const template = document.createElement("template"); template.innerHTML = html;
    reconcile(element, template.content);
  };
}
