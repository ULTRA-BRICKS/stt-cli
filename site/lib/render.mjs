// HTML for the three pages of spec section 8, rendered once at build time.
//
// Every page is complete without JavaScript: the catalog lists every card and JS only hides
// the ones a filter excludes; the detail page lists every term and JS only adds the copy
// button's clipboard write. All catalog text is escaped — notes in particular reach the
// language-model pass and are treated as untrusted (spec section 4), so they are never markup.

import { searchText } from "./catalog.mjs";

export const SITE_NAME = "stt dictionaries";
export const REPO = "https://git.hyperide.ai/ultrabricks/stt-cli";

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

const LANGUAGE_NAMES = new Intl.DisplayNames(["en"], { type: "language" });

function languageName(code) {
  return LANGUAGE_NAMES.of(code) ?? code;
}

function page({ title, description, body, path }) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<meta name="description" content="${esc(description)}">
<link rel="canonical" href="https://stt-dicts-marketplace.vercel.app${esc(path)}">
<link rel="stylesheet" href="/assets/style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 16 16%22><text y=%2213%22 font-size=%2213%22>📖</text></svg>">
<script src="/assets/app.js" defer></script>
</head>
<body>
<header class="top">
  <a class="brand" href="/">${esc(SITE_NAME)}</a>
  <nav><a href="/">Catalog</a><a href="/publish">Publish</a><a href="/api/dictionaries">API</a><a href="${REPO}">Source</a></nav>
</header>
<main>
${body}
</main>
<footer class="foot">
  Terminology dictionaries for <a href="${REPO}">stt</a>, the speech-to-text command-line tool.
  Every file here is the one stt bundles, byte for byte. No accounts, no install counts, no telemetry.
</footer>
</body>
</html>
`;
}

function copyBlock(command, label) {
  return `<div class="copy" data-copy="${esc(command)}">
  <code>${esc(command)}</code>
  <button type="button" class="copy-button" aria-label="${esc(label)}">Copy</button>
</div>`;
}

function tagChip(catalog, tag, { link = true } = {}) {
  const { kind, label } = catalog.tags[tag];
  const inner = esc(label);
  return link
    ? `<a class="tag ${esc(kind)}" href="/?tag=${esc(tag)}" title="${esc(tag)}">${inner}</a>`
    : `<span class="tag ${esc(kind)}" title="${esc(tag)}">${inner}</span>`;
}

function byTitle(a, b) {
  return a.title.localeCompare(b.title, "en");
}

export function renderCatalog(catalog) {
  const entries = [...catalog.index.dictionaries].sort(byTitle);
  const used = new Map();
  for (const entry of entries) for (const tag of entry.tags) used.set(tag, (used.get(tag) ?? 0) + 1);
  const group = (kind) =>
    [...used.keys()]
      .filter((tag) => catalog.tags[tag].kind === kind)
      .sort((a, b) => catalog.tags[a].label.localeCompare(catalog.tags[b].label, "en"))
      .map(
        (tag) =>
          `<button type="button" class="tag ${esc(kind)}" data-tag="${esc(tag)}" aria-pressed="false">${esc(
            catalog.tags[tag].label,
          )} <span class="count">${used.get(tag)}</span></button>`,
      )
      .join("\n");
  const cards = entries
    .map((entry) => {
      const langs = entry.languages.map(languageName).join(", ");
      return `<li class="card" data-id="${esc(entry.id)}" data-tags="${esc(entry.tags.join(" "))}" data-search="${esc(
        searchText(catalog, entry),
      )}">
  <a class="card-title" href="/d/${esc(entry.id)}">${esc(entry.title)}</a>
  <div class="meta"><code>${esc(entry.id)}</code> · v${esc(entry.version)} · ${esc(entry.term_count)} terms · ${esc(langs)}</div>
  <div class="tags">${entry.tags.map((tag) => tagChip(catalog, tag, { link: false })).join(" ")}</div>
</li>`;
    })
    .join("\n");
  const total = entries.length;
  const terms = entries.reduce((sum, entry) => sum + entry.term_count, 0);
  const body = `<section class="hero">
  <h1>Dictionaries that teach stt your vocabulary</h1>
  <p>${total} curated dictionaries, ${esc(terms)} terms: product names, drug names, legal and financial vocabulary,
  Russian IT slang — the words a speech model gets wrong. Each one is a list of correct spellings and the
  mis-hearings stt fixes on the spot.</p>
</section>
<section class="filters" aria-label="Filter the catalog">
  <input id="search" type="search" placeholder="Search titles, tags and terms — e.g. kubernetes, ибупрофен" autocomplete="off" aria-label="Search dictionaries">
  <div class="tag-group"><span class="group-label">Topics</span>
${group("topic")}
  </div>
  <div class="tag-group"><span class="group-label">Facets</span>
${group("facet")}
  </div>
  <p class="status" id="status" aria-live="polite">${total} dictionaries</p>
</section>
<ul class="cards" id="cards">
${cards}
</ul>
<p class="empty" id="empty" hidden>No dictionary matches. <button type="button" id="clear">Clear filters</button></p>`;
  return page({
    title: `${SITE_NAME} — terminology for speech-to-text`,
    description: `${total} curated terminology dictionaries for stt, installable with one command.`,
    body,
    path: "/",
  });
}

export function renderDetail(catalog, entry) {
  const dictionary = catalog.dictionaries.get(entry.id);
  const install = `stt dict install ${entry.id}`;
  const rows = dictionary.terms
    .map(
      (term) => `<tr id="t-${esc(term.id)}">
  <td class="term">${esc(term.term)}</td>
  <td class="aka">${(term.aka ?? []).map((a) => `<span>${esc(a)}</span>`).join("")}</td>
  <td class="note">${esc(term.note ?? "")}</td>
</tr>`,
    )
    .join("\n");
  // Every value is escaped here, at the point of use. A third element shows the value as code,
  // with that string as the code element's class ("" for none).
  const facts = [
    ["Id", entry.id, ""],
    ["Version", entry.version],
    ["Terms", entry.term_count],
    ["Languages", entry.languages.map((code) => `${languageName(code)} (${code})`).join(", ")],
    ["License", entry.license],
    ["Source", entry.source],
    ["SHA-256", entry.sha256, "hash"],
  ]
    .map(([k, v, codeClass]) => {
      const value =
        codeClass === undefined ? esc(v) : `<code${codeClass ? ` class="${codeClass}"` : ""}>${esc(v)}</code>`;
      return `<dt>${k}</dt><dd>${value}</dd>`;
    })
    .join("\n");
  const body = `<p class="crumbs"><a href="/">Catalog</a> / <code>${esc(entry.id)}</code></p>
<h1>${esc(entry.title)}</h1>
<div class="tags">${entry.tags.map((tag) => tagChip(catalog, tag)).join(" ")}</div>
<section class="install" aria-label="Install">
  <h2>Install</h2>
  ${copyBlock(install, `Copy the install command for ${entry.id}`)}
  <p>Or download <a class="download" href="/dicts/${esc(entry.id)}.json" download="${esc(entry.id)}.json">${esc(
    entry.id,
  )}.json</a> — the exact bytes of the catalog, SHA-256 <code class="hash">${esc(entry.sha256.slice(0, 16))}…</code>.
  The page never writes to your disk; stt does, after checking that hash.</p>
</section>
<dl class="facts">
${facts}
</dl>
<section aria-label="Terms">
  <h2>Terms <span class="count">${esc(entry.term_count)}</span></h2>
  <p class="hint">Top to bottom is priority: stt feeds the first terms to the decoder as a hint.
  Misheard spellings are replaced as facts; anything else close to a term is only flagged.</p>
  <div class="table-wrap"><table class="terms">
    <thead><tr><th>Term</th><th>Misheard as</th><th>Note</th></tr></thead>
    <tbody>
${rows}
    </tbody>
  </table></div>
</section>`;
  return page({
    title: `${entry.title} — ${SITE_NAME}`,
    description: `${entry.term_count} terms. Install with: ${install}`,
    body,
    path: `/d/${entry.id}`,
  });
}

export function renderPublish(catalog) {
  const example = catalog.index.dictionaries[0].id;
  const body = `<h1>Publish a dictionary</h1>
<p>The catalog is the <code>stt_cli/dictionaries/</code> directory of the
<a href="${REPO}">stt-cli repository</a>. Publishing is a pull request: no account here, no database, and the
same review every stt change gets. When it merges, stt bundles your dictionary and this site redeploys
from the same commit, so both serve the same bytes.</p>
<ol class="steps">
  <li><strong>Write the file.</strong> Add <code>stt_cli/dictionaries/&lt;id&gt;.json</code> — copy
  <a href="/dicts/${esc(example)}.json"><code>${esc(example)}.json</code></a> as a starting point. The id is a
  lowercase slug and equals the file name. Pick exactly one topic tag and any facets from
  <a href="${REPO}/src/branch/main/stt_cli/dictionaries/tags.json"><code>tags.json</code></a>.</li>
  <li><strong>Only real mis-hearings.</strong> Each <code>aka</code> is a wrong spelling a speech model actually
  produces, never an ordinary word — aliases are replaced without asking. Say in <code>source</code> how you
  found them.</li>
  <li><strong>Regenerate the index.</strong> This validates every file exactly as <code>stt dict add</code> would
  and rewrites the index with your file's hash:
  ${copyBlock("python -m stt_cli.catalog", "Copy the index command")}</li>
  <li><strong>Open a pull request</strong> with both files. CI re-runs the validation and checks the committed
  index byte for byte; a maintainer reviews the terms and notes.</li>
  <li><strong>Merged.</strong> The site redeploys and your dictionary appears here at
  <code>/d/&lt;id&gt;</code>, installable with <code>stt dict install &lt;id&gt;</code>.</li>
</ol>
<h2>Format</h2>
<p>The full contract — fields, limits, id rules — is section 3 of
<a href="${REPO}/src/branch/main/docs/specs/dictionaries.md">docs/specs/dictionaries.md</a>.</p>
<h2>API</h2>
<p><code>GET /api/dictionaries?tag=&lt;tag&gt;&amp;q=&lt;query&gt;</code> returns the catalog index (with no
parameters, byte-identical to the committed <code>index.json</code>);
<code>GET /api/dictionaries/&lt;id&gt;</code> returns a dictionary file's bytes, whose SHA-256 matches its index
entry.</p>`;
  return page({
    title: `Publish — ${SITE_NAME}`,
    description: "How to add a dictionary to the stt catalog: a reviewed pull request.",
    body,
    path: "/publish",
  });
}

export function renderNotFound() {
  return page({
    title: `Not found — ${SITE_NAME}`,
    description: "No such page.",
    body: `<h1>Not found</h1><p>No such page or dictionary. <a href="/">Back to the catalog</a>.</p>`,
    path: "/404",
  });
}
