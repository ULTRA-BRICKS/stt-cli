// The Vercel function behind /api/dictionaries and /api/dictionaries/<id> (vercel.json rewrites).
// The catalog is data/, copied from stt_cli/dictionaries/ by build.mjs and bundled through
// `includeFiles`; loaded once per instance.
//
// Vercel hands a rewritten function the URL that was requested, not the rewrite destination:
// on the deployment, /api/dictionaries?id=gaming returns the index, which it could not if the
// function saw /api/index?id=gaming. So routing is on req.url as-is, exactly as in serve.mjs.

import { fileURLToPath } from "node:url";

import { handleApi, loadCatalog, send } from "../lib/catalog.mjs";

const catalog = loadCatalog(fileURLToPath(new URL("../data", import.meta.url)));

export default function handler(req, res) {
  const url = new URL(req.url, "https://stt-dicts-marketplace.vercel.app");
  send(res, req.method, handleApi(catalog, req.method, url), { "cache-control": "public, max-age=0, s-maxage=300" });
}
