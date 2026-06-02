import { placeholderText } from "./placeholder";

function renderShell(root: HTMLElement): void {
  const heading = document.createElement("h1");
  heading.textContent = "hestia";

  const placeholder = document.createElement("p");
  placeholder.textContent = placeholderText();

  root.replaceChildren(heading, placeholder);
}

const root = document.querySelector<HTMLElement>("#app");

if (root === null) {
  throw new Error("Missing #app root");
}

renderShell(root);
