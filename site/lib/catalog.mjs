// The catalog as the site sees it, and the two API endpoints over it.
//
// Everything here reads the directory stt bundles (stt_cli/dictionaries/, spec section 4)
// unchanged: index.json is served byte for byte, dictionary files byte for byte, and a file
// whose SHA-256 does not match its index entry refuses to load at all, so the site can never
// serve a hash the command-line tool would reject. The same module backs the build, the
// Vercel function and the local server, so there is one implementation of the API.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

export const INDEX = "index.json";
export const TAGS = "tags.json";
export const INDEX_FORMAT = 1;

// Same slug rule as stt_cli/catalog.py `_SLUG` and SLUG_LENGTHS: a request path can only
// ever name a file this way, never walk out of the directory.
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export function isSlug(value) {
  return typeof value === "string" && value.length >= 2 && value.length <= 64 && SLUG.test(value);
}

export function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

/**
 * Load and cross-check a catalog directory.
 * @param {string} directory holds index.json, tags.json and <id>.json files
 */
export function loadCatalog(directory) {
  const indexBytes = readFileSync(join(directory, INDEX));
  const index = JSON.parse(indexBytes.toString("utf8"));
  if (index.format !== INDEX_FORMAT) {
    throw new Error(`${directory}/${INDEX}: format ${index.format}, this site reads ${INDEX_FORMAT}`);
  }
  if (!Array.isArray(index.dictionaries) || !index.dictionaries.length) {
    throw new Error(`${directory}/${INDEX}: lists no dictionaries; run \`python -m stt_cli.catalog\``);
  }
  const tags = JSON.parse(readFileSync(join(directory, TAGS), "utf8"));
  for (const [tag, spec] of Object.entries(tags)) {
    // Same rule as stt_cli/catalog.py read_tags; the kind also becomes a CSS class on the page.
    if (spec?.kind !== "topic" && spec?.kind !== "facet") {
      throw new Error(`${directory}/${TAGS}: ${tag} has kind ${JSON.stringify(spec?.kind)}, not "topic" or "facet"`);
    }
  }
  const files = new Map();
  const dictionaries = new Map();
  for (const entry of index.dictionaries) {
    if (!isSlug(entry.id)) throw new Error(`${INDEX}: ${JSON.stringify(entry.id)} is not a dictionary id`);
    const bytes = readFileSync(join(directory, `${entry.id}.json`));
    const actual = sha256(bytes);
    if (actual !== entry.sha256) {
      throw new Error(
        `${entry.id}.json: SHA-256 ${actual} does not match ${INDEX} (${entry.sha256}); ` +
          "run `python -m stt_cli.catalog` and commit the index",
      );
    }
    for (const tag of entry.tags) {
      if (!(tag in tags)) throw new Error(`${entry.id}: tag ${tag} is not in ${TAGS}`);
    }
    files.set(entry.id, bytes);
    dictionaries.set(entry.id, JSON.parse(bytes.toString("utf8")));
  }
  const catalog = { index, indexBytes, tags, files, dictionaries };
  // The catalog never changes after loading, so the API matches against text built once here
  // rather than rebuilding every dictionary's term list on every query.
  catalog.search = new Map(index.dictionaries.map((entry) => [entry.id, searchText(catalog, entry)]));
  return catalog;
}

/** The text a query is matched against: id, title, tags with their labels, terms and aliases. */
export function searchText(catalog, entry) {
  const terms = catalog.dictionaries.get(entry.id).terms.flatMap((t) => [t.term, ...(t.aka ?? [])]);
  const labels = entry.tags.map((tag) => catalog.tags[tag].label);
  return [entry.id, entry.title, ...entry.tags, ...labels, ...terms].join("\n").toLowerCase();
}

/** The query as lowercase words; a dictionary matches when every word occurs in its text. */
function queryWords(query) {
  return query.toLowerCase().split(/\s+/).filter(Boolean);
}

// A search box query is a few words. The cap bounds what one unauthenticated request can cost:
// matching is words × catalog text.
export const MAX_QUERY_CHARS = 200;

// stt_cli/catalog.py render_index: json.dumps(sort_keys=True, ensure_ascii=False, indent=2)
// plus a newline. JSON.stringify with two-space indent emits the same bytes for this shape once
// keys are sorted, which the API test checks against the committed file.
function sortedKeys(value) {
  if (Array.isArray(value)) return value.map(sortedKeys);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sortedKeys(value[key])]),
    );
  }
  return value;
}

export function renderIndex(document) {
  return `${JSON.stringify(sortedKeys(document), null, 2)}\n`;
}

const JSON_TYPE = "application/json; charset=utf-8";

function reply(status, body, extra = {}) {
  return {
    status,
    headers: { "content-type": JSON_TYPE, "access-control-allow-origin": "*", ...extra },
    body,
  };
}

function notFound(what, how) {
  return reply(404, `${JSON.stringify({ error: what, how })}\n`);
}

/**
 * The API of spec section 8.
 *   GET /api/dictionaries?tag=<t>&q=<query>  the index document; no parameters = committed bytes
 *   GET /api/dictionaries/<id>               the dictionary file's committed bytes
 * @param {URL} url
 */
export function handleApi(catalog, method, url) {
  if (method !== "GET" && method !== "HEAD") {
    return reply(405, `${JSON.stringify({ error: `${method} is not supported`, how: "use GET" })}\n`, {
      allow: "GET, HEAD",
    });
  }
  const path = url.pathname.replace(/\/+$/, "");
  if (path === "/api/dictionaries") {
    const tag = url.searchParams.get("tag") ?? "";
    const query = url.searchParams.get("q") ?? "";
    if (!tag && !query) return reply(200, catalog.indexBytes);
    if (query.length > MAX_QUERY_CHARS) {
      return reply(
        400,
        `${JSON.stringify({ error: `q is ${query.length} characters`, how: `search with at most ${MAX_QUERY_CHARS}` })}\n`,
      );
    }
    const words = queryWords(query);
    const rows = catalog.index.dictionaries.filter((entry) => {
      const text = catalog.search.get(entry.id);
      return (!tag || entry.tags.includes(tag)) && words.every((word) => text.includes(word));
    });
    return reply(200, renderIndex({ ...catalog.index, dictionaries: rows }));
  }
  const prefix = "/api/dictionaries/";
  if (path.startsWith(prefix)) {
    let id = "";
    try {
      id = decodeURIComponent(path.slice(prefix.length));
    } catch {
      // A malformed escape (/api/dictionaries/%zz) names no dictionary; 404 below.
    }
    const bytes = isSlug(id) ? catalog.files.get(id) : undefined;
    if (bytes) return reply(200, bytes);
    return notFound(`no dictionary with id ${JSON.stringify(id)}`, "GET /api/dictionaries lists every id");
  }
  return notFound(`no endpoint ${url.pathname}`, "GET /api/dictionaries or /api/dictionaries/<id>");
}

/** Write a handleApi result to a Node response; shared by the Vercel function and serve.mjs. */
export function send(res, method, { status, headers, body }, extraHeaders = {}) {
  res.writeHead(status, { ...headers, ...extraHeaders });
  res.end(method === "HEAD" ? undefined : body);
}
