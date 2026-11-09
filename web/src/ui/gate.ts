// The access code screen (docs/access.md): shown before anything else when this server
// asks visitors for the service desk's code, and again when the pass it bought runs out.
// A visitor this server lets in without one can still be asked by another address the
// page tests, such as this server's IPv4-only name: then over the page, in a dialog.
import { type Endpoint, setPass } from "../engine/net";
import type { AccessPass, AccessStatus } from "../types";
import { $, clear, h, save, store } from "./dom";

/** The pass as the page keeps it. */
interface Pass {
  pass: string;
  expires: number;
}

const KEY = "t2s.pass";
let expires = 0; // when the pass held runs out, in seconds since the epoch; 0 for none
let declined = false; // "Not now" in the dialog: asked again only when the visitor chooses to
let asking: Promise<AccessPass | null> | null = null;
let note = ""; // the site's line under the code box (access_note)
let screened = false; // the code screen was shown since the page loaded

export function forgetPass(): void {
  save(KEY, { pass: "", expires: 0 });
  expires = 0;
}

/** Whether a pass is held that hasn't run out. */
export function passHeld(): boolean {
  return expires * 1000 > Date.now();
}

/** Whether the code screen was shown since the page loaded (admit). */
export function codeScreened(): boolean {
  return screened;
}

/**
 * Resolves once this visitor may test: no code is needed, a saved pass still holds, or a
 * right code was entered. Null from a server that has no access codes (or didn't answer).
 */
export async function admit(origin: Endpoint): Promise<AccessStatus | null> {
  const saved = store<Pass>(KEY, { pass: "", expires: 0 });
  expires = saved.pass ? saved.expires : 0;
  // Even past the time it came with: how long a pass lasts is each server's
  // access_pass_hours now, which may have been raised since. The server says.
  if (saved.pass) setPass(saved.pass);
  // A site that shows the code screen alone hid the header and footer before first paint,
  // in case the code is asked for (index.html); they're back unless it is.
  const root = document.documentElement;
  let status: AccessStatus;
  try {
    status = await origin.json<AccessStatus>("/access");
  } catch {
    root.classList.remove("gated");
    return null;
  }
  note = status.note ?? "";
  if (saved.pass && status.pass) {
    if (status.pass.valid && status.pass.expires) {
      expires = status.pass.expires; // as the setting is now
      save(KEY, { pass: saved.pass, expires });
    } else if (!status.pass.valid) {
      // Ran out, or was ended: not sent on, and the code is asked for again.
      setPass("");
      expires = Math.min(expires, Math.floor(Date.now() / 1000));
    }
  }
  if (!status.required || status.pass?.valid) {
    root.classList.remove("gated");
    return status;
  }
  setPass("");
  expires = 0;
  const got = await ask(origin, saved.pass !== "");
  // The status was read before the code was in: the pass it bought says what it is now.
  return { ...status, pass: { valid: true, expires: got.expires }, support: got.scope === "support" };
}

function ask(origin: Endpoint, again: boolean): Promise<AccessPass> {
  screened = true;
  document.documentElement.classList.add("gated");
  const view = $("#view");
  clear(view);
  return new Promise((resolve) => {
    const help = again
      ? "Access expired. Contact the service desk."
      : "Use of this system is limited to authorized users with an assigned access code.";
    const { card, input } = codeCard(origin, "h1", help, (got) => {
      document.documentElement.classList.remove("gated");
      resolve(got);
    });
    view.append(h("section", { class: "gate" }, card));
    input.focus();
  });
}

/**
 * The code asked for over the page, which stays as it is underneath: the pass once a right
 * code has bought one, null for "Not now". `over` begins its sentence with where the code
 * is asked for, as "Over IPv4, "; `also` follows it, where given. Asked on its own only
 * until the visitor says "Not now"; `chosen` asks anyway.
 */
export function askOver(origin: Endpoint, over: string, chosen = false, also = ""): Promise<AccessPass | null> {
  if (asking) return asking;
  if (declined && !chosen) return Promise.resolve(null);
  const again = expires > 0 && !passHeld();
  const help = sentence(
    over,
    again
      ? "access expired. Contact the service desk."
      : "this function requires organization approval. Access is limited to authorized users.",
  );
  asking = new Promise<AccessPass | null>((resolve) => {
    const later = h("button", { class: "btn small", type: "button" }, "Not now");
    const dialog = h("dialog", { class: "code-dialog", "aria-labelledby": "access-title" });
    let bought: AccessPass | null = null;
    const { card, input } = codeCard(
      origin,
      "h2",
      also ? `${help} ${also}` : help,
      (got) => {
        declined = false;
        bought = got;
        dialog.close("pass");
      },
      h("div", { class: "gate-later" }, later),
    );
    later.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      dialog.remove();
      asking = null;
      if (!bought) declined = true;
      resolve(bought);
    });
    dialog.append(card);
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
  return asking;
}

/** The card with the code form; `done` runs once a right code has bought a pass. */
function codeCard(origin: Endpoint, heading: "h1" | "h2", help: string, done: (got: AccessPass) => void, ...more: HTMLElement[]): { card: HTMLElement; input: HTMLInputElement } {
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
        const got = await origin.post<AccessPass>("/access", { code: input.value });
        save(KEY, { pass: got.pass, expires: got.expires }); // the pass alone, as ever
        setPass(got.pass);
        expires = got.expires;
        done(got);
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
    note ? h("p", { class: "gate-note" }, note) : null,
    ...more,
  );
  return { card, input };
}

/** A sentence that begins with `over`, as "Over IPv4, ", or with `rest` when that's empty. */
export function sentence(over: string, rest: string): string {
  return over ? over + rest : rest.charAt(0).toUpperCase() + rest.slice(1);
}
