// The access code screen (docs/access.md): shown before anything else when this server
// asks visitors for the service desk's code, and again when the pass it bought runs out.
// A visitor this server lets in without one can still be asked by another address the
// page tests, such as this server's IPv4-only name: then over the page, in a dialog.
import { type Endpoint, setPass } from "../engine/net";
import type { AccessStatus } from "../types";
import { $, clear, h, save, store } from "./dom";

interface Pass {
  pass: string;
  expires: number;
}

const KEY = "t2s.pass";
let expires = 0; // when the pass held runs out, in seconds since the epoch; 0 for none
let declined = false; // "Not now" in the dialog: asked again only when the visitor chooses to
let asking: Promise<boolean> | null = null;

export function forgetPass(): void {
  save(KEY, { pass: "", expires: 0 });
  expires = 0;
}

/** Whether a pass is held that hasn't run out. */
export function passHeld(): boolean {
  return expires * 1000 > Date.now();
}

/**
 * Resolves once this visitor may test: no code is needed, a saved pass still holds, or a
 * right code was entered. Null from a server that has no access codes (or didn't answer).
 */
export async function admit(origin: Endpoint): Promise<AccessStatus | null> {
  const saved = store<Pass>(KEY, { pass: "", expires: 0 });
  expires = saved.pass ? saved.expires : 0; // one that ran out: the code is asked for again
  if (passHeld()) setPass(saved.pass);
  let status: AccessStatus;
  try {
    status = await origin.json<AccessStatus>("/access");
  } catch {
    return null;
  }
  if (!status.required || status.pass?.valid) return status;
  setPass("");
  expires = 0;
  await ask(origin, saved.pass !== "");
  return status;
}

function ask(origin: Endpoint, again: boolean): Promise<void> {
  document.body.classList.add("gated");
  const view = $("#view");
  clear(view);
  return new Promise((resolve) => {
    const help = again
      ? "Your access has run out. Enter the current code from the service desk."
      : "The service desk gives you a code of words and digits, such as maple.river.42.";
    const { card, input } = codeCard(origin, "h1", help, () => {
      document.body.classList.remove("gated");
      resolve();
    });
    view.append(h("section", { class: "gate" }, card));
    input.focus();
  });
}

/**
 * The code asked for over the page, which stays as it is underneath: true once a right code
 * has bought a pass, false for "Not now". `what` says who asks, as "Over IPv4, this
 * server". Asked on its own only until the visitor says "Not now"; `chosen` asks anyway.
 */
export function askOver(origin: Endpoint, what: string, chosen = false): Promise<boolean> {
  if (asking) return asking;
  if (declined && !chosen) return Promise.resolve(false);
  const again = expires > 0 && !passHeld();
  const help = again
    ? `${what} asks for the code again: your access has run out. Enter the current code from the service desk.`
    : `${what} asks for the access code. The service desk gives you a code of words and digits, such as maple.river.42.`;
  asking = new Promise<boolean>((resolve) => {
    const later = h("button", { class: "btn small", type: "button" }, "Not now");
    const dialog = h("dialog", { class: "code-dialog", "aria-labelledby": "access-title" });
    const { card, input } = codeCard(
      origin,
      "h2",
      help,
      () => {
        declined = false;
        dialog.close("pass");
      },
      h("div", { class: "gate-later" }, later),
    );
    later.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      dialog.remove();
      asking = null;
      const got = dialog.returnValue === "pass";
      if (!got) declined = true;
      resolve(got);
    });
    dialog.append(card);
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
  return asking;
}

/** The card with the code form; `done` runs once a right code has bought a pass. */
function codeCard(origin: Endpoint, heading: "h1" | "h2", help: string, done: () => void, ...more: HTMLElement[]): { card: HTMLElement; input: HTMLInputElement } {
  const input = h("input", {
    class: "code-input",
    id: "access-code",
    type: "text",
    name: "code",
    autocomplete: "off",
    autocapitalize: "none",
    autocorrect: "off",
    spellcheck: "false",
    enterkeyhint: "go",
    maxlength: 100,
    "aria-describedby": "access-help",
    required: true,
  });
  const button = h("button", { class: "btn primary big", type: "submit" }, "Continue");
  const error = h("p", { class: "gate-error", role: "alert" });
  const form = h("form", { class: "gate-form" }, h("label", { class: "visually-hidden", for: "access-code" }, "Access code"), input, button);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    void (async () => {
      button.disabled = true;
      error.textContent = "";
      try {
        const got = await origin.post<Pass>("/access", { code: input.value });
        save(KEY, got);
        setPass(got.pass);
        expires = got.expires;
        done();
      } catch (err) {
        const why = err instanceof Error ? err.message : String(err);
        // After too many wrong codes the server refuses every code the same way, the right
        // one too, so this message stands for that as well.
        error.textContent = why.startsWith("403")
          ? "That isn't the current code. Check it with the service desk."
          : `The server didn't answer: ${why}`;
        input.select();
      } finally {
        button.disabled = false;
      }
    })();
  });
  const card = h(
    "div",
    { class: "card gate-card" },
    h(heading, { id: "access-title" }, "Enter access code"),
    h("p", { class: "lede", id: "access-help" }, help),
    form,
    error,
    h("p", { class: "gate-note" }, "Type it here, not in the browser's address bar. Capitals, spaces and dots don't matter."),
    ...more,
  );
  return { card, input };
}
