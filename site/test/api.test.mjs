// The site's half of the contract test (spec section 8): the shared fixture set that stt's
// source-parser tests read, served through the API, must come back byte for byte.
//   node --test test/

import assert from "node:assert/strict";
import { cpSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { MAX_QUERY_CHARS, handleApi, loadCatalog, renderIndex, sha256 } from "../lib/catalog.mjs";
import { renderCatalog, renderDetail } from "../lib/render.mjs";

const repo = fileURLToPath(new URL("../../", import.meta.url));
const FIXTURE = join(repo, "tests", "fixtures", "dictionaries");
const BUNDLED = join(repo, "stt_cli", "dictionaries");
const fixture = loadCatalog(FIXTURE);

function get(catalog, path, method = "GET") {
  return handleApi(catalog, method, new URL(path, "https://example.test"));
}

function ids(response) {
  return JSON.parse(Buffer.from(response.body).toString("utf8")).dictionaries.map((row) => row.id);
}

test("the index endpoint with no parameters serves the committed index.json byte for byte", () => {
  const response = get(fixture, "/api/dictionaries");
  assert.equal(response.status, 200);
  assert.match(response.headers["content-type"], /^application\/json/);
  assert.deepEqual(Buffer.from(response.body), readFileSync(join(FIXTURE, "index.json")));
});

test("a dictionary endpoint serves the file's committed bytes, whose hash is the index entry's", () => {
  const index = JSON.parse(readFileSync(join(FIXTURE, "index.json"), "utf8"));
  for (const row of index.dictionaries) {
    const response = get(fixture, `/api/dictionaries/${row.id}`);
    assert.equal(response.status, 200, row.id);
    assert.deepEqual(Buffer.from(response.body), readFileSync(join(FIXTURE, `${row.id}.json`)));
    assert.equal(sha256(response.body), row.sha256);
  }
});

test("an unknown or malformed id is a 404, never a file outside the catalog", () => {
  for (const id of ["nope-nope", "..%2Findex", "index", "tags", "%2E%2E%2F%2E%2E%2Fpackage", "A", "%zz", "%"]) {
    const response = get(fixture, `/api/dictionaries/${id}`);
    assert.equal(response.status, 404, id);
    assert.ok(JSON.parse(response.body).error, id);
  }
});

test("tag and q filter the index list and keep the document's shape", () => {
  assert.deepEqual(ids(get(fixture, "/api/dictionaries?tag=gaming")), ["fixture-games"]);
  // q matches term text and aliases, not only titles: the website's search (spec section 8).
  assert.deepEqual(ids(get(fixture, "/api/dictionaries?q=HYPERIDE")), ["fixture-tools"]);
  assert.deepEqual(ids(get(fixture, "/api/dictionaries?q=vigma")), ["fixture-tools"]);
  assert.deepEqual(ids(get(fixture, "/api/dictionaries?q=fixture")), ["fixture-games", "fixture-tools"]);
  assert.deepEqual(ids(get(fixture, "/api/dictionaries?tag=gaming&q=figma")), []);
  // The fixture has no Cyrillic term, so case-insensitive Cyrillic search is checked on the bundled set.
  assert.ok(ids(get(loadCatalog(BUNDLED), "/api/dictionaries?q=ДЕПЛОЙ")).includes("russian-it-anglicisms"));
  const filtered = JSON.parse(get(fixture, "/api/dictionaries?tag=vendor-names").body);
  const committed = JSON.parse(readFileSync(join(FIXTURE, "index.json"), "utf8"));
  assert.equal(filtered.format, committed.format);
  assert.deepEqual(filtered.dictionaries, [committed.dictionaries[1]]);
  // Both filters narrow: q alone matches both fixtures, the tag keeps one.
  assert.deepEqual(ids(get(fixture, "/api/dictionaries?tag=gaming&q=fixture")), ["fixture-games"]);
  assert.deepEqual(JSON.parse(get(fixture, "/api/dictionaries?tag=no-such-tag").body), { ...committed, dictionaries: [] });
});

test("an overlong query is refused instead of scanned", () => {
  const response = get(fixture, `/api/dictionaries?q=${"a ".repeat(MAX_QUERY_CHARS)}`);
  assert.equal(response.status, 400);
  assert.equal(get(fixture, `/api/dictionaries?q=${"a".repeat(MAX_QUERY_CHARS)}`).status, 200);
});

test("the index is re-rendered in exactly stt's format, so a filtered response is too", () => {
  for (const directory of [FIXTURE, BUNDLED]) {
    const bytes = readFileSync(join(directory, "index.json"));
    assert.equal(renderIndex(JSON.parse(bytes.toString("utf8"))), bytes.toString("utf8"), directory);
  }
});

test("only GET and HEAD are served", () => {
  const response = get(fixture, "/api/dictionaries", "POST");
  assert.equal(response.status, 405);
  assert.equal(response.headers.allow, "GET, HEAD");
});

test("a dictionary file that does not match its index hash refuses to load", () => {
  const directory = mkdtempSync(join(tmpdir(), "stt-site-"));
  cpSync(FIXTURE, directory, { recursive: true });
  const path = join(directory, "fixture-games.json");
  writeFileSync(path, readFileSync(path, "utf8").replace('"version": 1', '"version": 2'));
  assert.throws(() => loadCatalog(directory), /fixture-games\.json: SHA-256 .* does not match index\.json/);
});

test("a catalog with no dictionaries, or a tag kind stt would reject, refuses to load", () => {
  const empty = mkdtempSync(join(tmpdir(), "stt-site-"));
  cpSync(FIXTURE, empty, { recursive: true });
  writeFileSync(join(empty, "index.json"), renderIndex({ dictionaries: [], format: 1 }));
  assert.throws(() => loadCatalog(empty), /index\.json: lists no dictionaries/);

  const hostile = mkdtempSync(join(tmpdir(), "stt-site-"));
  cpSync(FIXTURE, hostile, { recursive: true });
  const tags = JSON.parse(readFileSync(join(hostile, "tags.json"), "utf8"));
  tags.gaming.kind = 'facet" onclick="alert(1)';
  writeFileSync(join(hostile, "tags.json"), JSON.stringify(tags));
  assert.throws(() => loadCatalog(hostile), /tags\.json: gaming has kind .*, not "topic" or "facet"/);
});

test("pages escape catalog text instead of rendering it as markup", () => {
  const catalog = loadCatalog(FIXTURE);
  const entry = { ...catalog.index.dictionaries[0], title: '<img src=x onerror="alert(1)">' };
  const hostile = structuredClone(catalog.dictionaries.get(entry.id));
  hostile.terms[0].note = "</script><script>alert(2)</script>";
  catalog.dictionaries.set(entry.id, hostile);
  catalog.index = { ...catalog.index, dictionaries: [entry] };
  for (const html of [renderCatalog(catalog), renderDetail(catalog, entry)]) {
    assert.ok(!html.includes("<img src=x"), "title rendered as markup");
    assert.ok(!html.includes("<script>alert"), "note rendered as markup");
  }
});
