// A code someone reads out or passes on, as on a ticket: a saved result's ID on Test, the
// access code on History. Set apart in a box; a click (or Enter) copies it and says so.
import { h } from "./dom";

const SHOWN_MS = 1200; // how long "Copied" stays

export function codeChip(code: string, hint: string): HTMLElement {
  const chip = h("span", { class: "code-chip", role: "button", tabindex: 0, title: `${hint} Click to copy.` }, code);
  const copy = () => {
    // Where the clipboard is refused, the click has still selected the code, for Ctrl+C.
    void navigator.clipboard?.writeText(code).then(() => {
      window.getSelection()?.removeAllRanges();
      chip.querySelector(".code-copied")?.remove();
      const note = h("span", { class: "code-copied", role: "status" }, "Copied");
      chip.append(note);
      setTimeout(() => note.remove(), SHOWN_MS);
    });
  };
  chip.addEventListener("click", copy);
  chip.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    copy();
  });
  return chip;
}
