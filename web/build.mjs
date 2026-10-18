// Bundles src/main.ts and copies public/ into the Python package's static directory,
// so the server can be installed and run without Node. Usage: node build.mjs [--watch]
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "..", "src", "texas2step", "static");
const watch = process.argv.includes("--watch");

function stamp() {
  // Cache-bust assets by content hash; index.html itself is served no-cache.
  const hash = (file) =>
    createHash("sha256").update(readFileSync(join(out, "assets", file))).digest("hex").slice(0, 10);
  const html = readFileSync(join(here, "public", "index.html"), "utf8")
    .replace("assets/app.js", `assets/app.js?v=${hash("app.js")}`)
    .replace("assets/app.css", `assets/app.css?v=${hash("app.css")}`);
  writeFileSync(join(out, "index.html"), html);
}

rmSync(out, { recursive: true, force: true });
mkdirSync(join(out, "assets"), { recursive: true });
cpSync(join(here, "public"), out, { recursive: true });

const options = {
  entryPoints: { app: join(here, "src", "main.ts") },
  outdir: join(out, "assets"),
  entryNames: "[name]",
  bundle: true,
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  target: ["chrome110", "firefox115", "safari16.4", "edge110"],
  format: "esm",
  logLevel: "info",
  legalComments: "none",
  plugins: [{ name: "stamp", setup: (b) => b.onEnd((r) => r.errors.length || stamp()) }],
};

if (watch) {
  await (await esbuild.context(options)).watch();
} else {
  await esbuild.build(options);
}
