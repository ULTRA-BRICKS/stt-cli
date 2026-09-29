#!/usr/bin/env node
// Build the static site from the catalog stt bundles (spec section 8: one source of truth).
//
//   node build.mjs            reads ../stt_cli/dictionaries, writes public/ and data/
//   STT_DICTIONARIES=<dir>    reads another catalog directory (tests, publish walkthrough)
//
// public/  pages, assets, and /dicts/<id>.json downloads (the committed bytes)
// data/    the catalog files the API function serves, copied unchanged
//
// No dependencies: Node's standard library only. A catalog whose files do not match their
// index hashes fails the build (loadCatalog), so a stale index can never deploy.

import { copyFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { INDEX, TAGS, loadCatalog } from "./lib/catalog.mjs";
import { renderCatalog, renderDetail, renderNotFound, renderPublish } from "./lib/render.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(process.env.STT_DICTIONARIES ?? join(here, "..", "stt_cli", "dictionaries"));
const out = join(here, "public");
const data = join(here, "data");

const catalog = loadCatalog(source);

for (const dir of [out, data]) rmSync(dir, { recursive: true, force: true });
for (const dir of [join(out, "d"), join(out, "dicts"), join(out, "assets"), data]) mkdirSync(dir, { recursive: true });

writeFileSync(join(out, "index.html"), renderCatalog(catalog));
writeFileSync(join(out, "publish.html"), renderPublish(catalog));
writeFileSync(join(out, "404.html"), renderNotFound());
for (const entry of catalog.index.dictionaries) {
  writeFileSync(join(out, "d", `${entry.id}.html`), renderDetail(catalog, entry));
  writeFileSync(join(out, "dicts", `${entry.id}.json`), catalog.files.get(entry.id));
  writeFileSync(join(data, `${entry.id}.json`), catalog.files.get(entry.id));
}
for (const name of [INDEX, TAGS]) copyFileSync(join(source, name), join(data, name));
for (const name of ["app.js", "style.css"]) copyFileSync(join(here, "assets", name), join(out, "assets", name));

process.stdout.write(`built ${catalog.index.dictionaries.length} dictionaries from ${source}\n`);
