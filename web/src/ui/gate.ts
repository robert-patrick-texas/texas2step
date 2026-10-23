// The access code screen (docs/access.md): shown before anything else when this server
// asks visitors for the service desk's code, and again when the pass it bought runs out.
import { type Endpoint, setPass } from "../engine/net";
import type { AccessStatus } from "../types";
import { $, clear, h, save, store } from "./dom";

interface Pass {
  pass: string;
  expires: number;
}

const KEY = "t2s.pass";

export function forgetPass(): void {
  save(KEY, { pass: "", expires: 0 });
}

/**
 * Resolves once this visitor may test: no code is needed, a saved pass still holds, or a
 * right code was entered. Null from a server that has no access codes (or didn't answer).
 */
export async function admit(origin: Endpoint): Promise<AccessStatus | null> {
  const saved = store<Pass>(KEY, { pass: "", expires: 0 });
  const held = saved.pass && saved.expires * 1000 > Date.now();
  if (held) setPass(saved.pass);
  let status: AccessStatus;
  try {
    status = await origin.json<AccessStatus>("/access");
  } catch {
    return null;
  }
  if (!status.required || status.pass?.valid) return status;
  setPass("");
  await ask(origin, saved.pass !== "");
  return status;
}

function ask(origin: Endpoint, again: boolean): Promise<void> {
  document.body.classList.add("gated");
  const view = $("#view");
  clear(view);
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
  const help = again
    ? "Your access has run out. Enter the current code from the service desk."
    : "The service desk gives you a code of words and digits, such as maple.river.42.";
  view.append(
    h(
      "section",
      { class: "gate" },
      h(
        "div",
        { class: "card gate-card" },
        h("h1", {}, "Enter your access code"),
        h("p", { class: "lede", id: "access-help" }, help),
        form,
        error,
        h("p", { class: "gate-note" }, "Type it here, not in the browser's address bar. Capitals, spaces and dots don't matter."),
      ),
    ),
  );
  input.focus();
  return new Promise((resolve) => {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      void (async () => {
        button.disabled = true;
        error.textContent = "";
        try {
          const got = await origin.post<Pass>("/access", { code: input.value });
          save(KEY, got);
          setPass(got.pass);
          document.body.classList.remove("gated");
          resolve();
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
  });
}
