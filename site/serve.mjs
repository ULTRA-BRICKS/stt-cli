#!/usr/bin/env node
// Local preview of the built site, with Vercel's routing for this project: clean URLs
// (/d/<id> -> d/<id>.html), the API through the same handler the function uses, 404.html.
//
//   node build.mjs && node serve.mjs [port]      default port 4086

import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { handleApi, loadCatalog, send } from "./lib/catalog.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "public");
const catalog = loadCatalog(join(here, "data"));
const port = Number(process.argv[2] ?? process.env.PORT ?? 4086);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

// Only a file inside public/ is ever served: the path is resolved first, then checked.
function file(pathname) {
  let clean;
  try {
    clean = decodeURIComponent(pathname);
  } catch {
    return null; // a malformed escape (/%zz) names no file
  }
  for (const candidate of [clean, `${clean}.html`, `${clean}/index.html`]) {
    const path = resolve(root, `.${candidate}`);
    if (path.startsWith(root + sep) && existsSync(path) && statSync(path).isFile()) return path;
  }
  return null;
}

createServer((req, res) => {
  // A fixed base: only the path and query are read, and a degenerate Host header must not throw.
  const url = new URL(req.url, "http://localhost");
  if (url.pathname === "/api/dictionaries" || url.pathname.startsWith("/api/dictionaries/")) {
    send(res, req.method, handleApi(catalog, req.method, url));
    return;
  }
  const path = file(url.pathname);
  const target = path ?? join(root, "404.html");
  res.writeHead(path ? 200 : 404, { "content-type": TYPES[extname(target)] ?? "application/octet-stream" });
  if (req.method === "HEAD") {
    res.end();
    return;
  }
  createReadStream(target)
    .on("error", () => res.destroy())
    .pipe(res);
}).listen(port, "127.0.0.1", () => process.stdout.write(`http://localhost:${port}\n`));
