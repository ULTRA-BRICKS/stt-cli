# Dictionaries: distributable format, catalog, install, overlays, marketplace, suggestions

## 1. Status and scope

This document is the contract for the dictionary marketplace epic,
[#90](https://git.hyperide.ai/ultrabricks/stt-cli/issues/90), and its five sub-tickets. Every `file:line`
citation was verified against main [e10967b](https://git.hyperide.ai/ultrabricks/stt-cli/commit/e10967bd61)
(2026-09-29); when code moves, fix the citation in the pull request that moves it. What exists
today is one hand-maintained personal dictionary: `dictionary.json` under the application home
(`stt_cli/dictionary.py:43`, `:438-441`), shaped `{"terms": [{term, aka, note}]}`
(`stt_cli/dictionary.py:149-166`), keyed only by casefolded term text (`stt_cli/dictionary.py:252-254`),
and edited through `stt dict list/add/rm/import/check` (`stt_cli/commands/dict_cmd.py:34-48`).
**Shipped by [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81):** the distributable format
and its validator (`stt_cli/catalog.py`), the 21 curated dictionaries of section 4 with `tags.json`, and
the committed catalog index. **Shipped by [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83):**
both sources (`stt_cli/sources.py`), the index reader (`catalog.parse_index`), the
`search`/`install`/`installed`/`show`/`uninstall` commands, installed-dictionary merging and the
fingerprint (`stt_cli/installed.py`), read by file transcription and `stt dict check`. **Shipped by
[#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84):** overlays (`stt_cli/overlays.py`) and
their standard-library YAML-subset reader (`stt_cli/yamlsubset.py`), applied inside
`installed.load_effective`, so everything that reads the effective glossary reads them. Live dictation
still reads only the personal dictionary; switching it is
[#113](https://git.hyperide.ai/ultrabricks/stt-cli/issues/113). **Shipped by
[#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86):** the marketplace website of section 8
(`site/`), deployed at `https://stt-dicts-marketplace.vercel.app`, whose API tests read the same contract
fixture `tests/fixtures/dictionaries/` as the stt side. **Shipped by
[#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88):** usage-based suggestions (section 9,
`stt_cli/suggest.py`). Build order,
as recorded on the epic: [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81) (format, catalog
contract, curated content), then [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) (sources,
install, effective glossary, cache fingerprint), then
[#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84) (overlays).
[#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86) (the website, built and deployed) runs in
parallel with [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) and [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84) against the catalog contract [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81) commits.
[#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88) (suggestions) starts after [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83), because it
needs "installed" and "not installed" to exist.

## 2. Terms

| Term | Meaning |
| --- | --- |
| Personal dictionary | The user's own `dictionary.json` under the application home, written only by `stt dict add/rm/import` through `dictionary.editing()` (`stt_cli/dictionary.py:551-573`). Unchanged by this epic. |
| Distributable dictionary | One JSON file in the format of section 3: an envelope with an id, a version and tags, wrapping terms that each carry a stable id. |
| Catalog index | The committed `index.json` listing every distributable dictionary with its metadata and the SHA-256 hash of its file (section 4). The command-line tool and the website both read it unchanged. |
| Installed dictionary | A distributable dictionary copied byte for byte into `<app_home>/dicts/installed/` by `stt dict install` (section 5). |
| Overlay | A local YAML file in `~/.config/stt-cli/dicts/overlays/` that disables, replaces or adds entries of one installed dictionary without editing the installed file (section 7). |
| Effective glossary | The single `Dictionary` every consumer receives: installed dictionaries with their overlays applied, merged with the personal dictionary, which wins every conflict (section 6). |

## 3. Distributable dictionary format

```json
{
  "id": "software-engineering-tools",
  "version": 3,
  "title": "Software engineering tool and vendor names",
  "tags": ["software-engineering-tools", "vendor-names"],
  "languages": ["en"],
  "license": "CC0-1.0",
  "source": "curated by hand from mis-transcriptions seen in stt output",
  "terms": [
    {"id": "conloca", "term": "ConLoca", "aka": ["ConLog", "Conloca", "ConLoka"], "note": "a project name"},
    {"id": "hyperide", "term": "HyperIDE", "aka": ["hyper ide"]}
  ]
}
```

| Field | Required | Purpose |
| --- | --- | --- |
| `id` | yes | Stable slug naming the dictionary everywhere: file name, install command, overlay file name, website URL. Never changes after publication. |
| `version` | yes | Positive integer, raised by one on every published change. Lets an overlay detect that its base moved and lets `stt dict install` report an upgrade. |
| `title` | yes | Human-readable name for listings and the notification, at most 80 characters. |
| `tags` | yes | Tags from the committed vocabulary (section 4), exactly one of them a topic tag. Drives search, the website filter and suggestion scoring. |
| `languages` | yes | ISO 639-1 codes of the languages the terms are spoken in, for example `["ru", "en"]`. |
| `license` | yes | SPDX license identifier of the content, for example `CC0-1.0`. |
| `source` | yes | One line: where the terms came from and how they were checked (section 4). |
| `terms` | yes | Ordered list. Order is priority, as in the personal dictionary (`stt_cli/dictionary.py:231-232`), because the decoder prompt takes terms from the top. |
| `terms[].id` | yes | Stable slug for this entry, independent of its text. |
| `terms[].term` | yes | The correct spelling. |
| `terms[].aka` | no | Recorded wrong spellings, substituted as facts. Default `[]`. |
| `terms[].note` | no | One line for the language-model correction pass. Default `""`. |

**Why each term has its own id.** An overlay has to say "disable or replace the entry that used to be
called X" and keep saying it after the next base version edits that entry's text or aliases; a lookup by
casefolded text, today's only key, breaks the moment the text changes. The id is never added to `Term` or
`Term.to_dict` (`stt_cli/dictionary.py:141-166`): that would change the personal file's shape and the
digest of every archived run. Ids live in the distributable layer only.

**Id rules.** Dictionary and term ids are ASCII slugs matching `^[a-z0-9]+(-[a-z0-9]+)*$`, 2 to 64
characters (a Russian term gets a transliterated id: `деплой` has id `deploy`). A dictionary id equals its
file name without `.json` and is unique across the catalog. A term id is unique within its file and is
never reused for a different concept. No two entries of one file share a casefolded term text: `add` would
merge the second into the first (`stt_cli/dictionary.py:293-306`) and two ids would name one entry, so this
is a separate check, new code with its own tests.

**Validation is the same as for a hand-typed entry.** A file is valid only if every term, fed in file
order through `Dictionary.add` (`stt_cli/dictionary.py:256-306`) into a fresh `Dictionary`, is accepted and
equals itself after `normalized` (`stt_cli/dictionary.py:177-206`). Calling `add` rather than re-listing
its checks is what guarantees an installed dictionary never holds an entry a typist would have been
refused. That call enforces:

| Limit | Where |
| --- | --- |
| Non-empty term after separators are stripped | `stt_cli/dictionary.py:265-270` |
| At most `MAX_TERM_WORDS` = 8 words and `MAX_TERM_CHARS` = 80 characters, term and every alias | `_within_limits` `stt_cli/dictionary.py:746-757` via `_fits` `:741-743`; constants `:86-87` |
| At most `MAX_ALIASES` = 24 aliases | `stt_cli/dictionary.py:108`, checked `:279-284` |
| No alias that is another entry's name or alias | `_refuse_an_ambiguous_alias`, `stt_cli/dictionary.py:308-334` |
| At most `MAX_SCREENED_TERMS` = 500 terms | `stt_cli/dictionary.py:99`, checked `:286-292` |
| Note is one line of at most `NOTE_CHARS` = 120 characters, no control characters | `stt_cli/dictionary.py:132`, `:135-138`, `:216-226` |

"Equals itself after `normalized`" means a published file never holds a comma, `|`, `~` or control
character the loader would silently rewrite. In addition: the file is at most `MAX_DICTIONARY_BYTES`
(`stt_cli/dictionary.py:124`); unknown fields are an error — stricter than the personal file, whose
loader refuses a malformed entry (`stt_cli/dictionary.py:470-477`) but ignores keys it does not know
(`Term.from_dict`, `:158-166`), so this check is new code with its own tests; `languages`, `license` and
`tags` are checked against ISO 639-1, the SPDX list and the tag vocabulary; `title`, `source` and tag
labels must be one line with no control characters, because they are printed on listings.

**Versioning: an integer.** [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81)'s prose says "a semantic version", but its own sketch writes
`"version": 1` and [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84)'s overlay writes `base_version: 3`. The only consumers ask "is the installed copy
older than the catalog's" and "was this overlay written against the installed version", both plain
comparisons. Semantic versioning's major/minor/patch split serves dependency resolution, which
dictionaries do not have, and would make every publisher decide whether renaming an alias is minor or
patch for no reader's benefit.

## 4. Catalog and tags

**Where files live.** `stt_cli/dictionaries/`: one `<id>.json` per dictionary, `tags.json` (the tag
vocabulary) and `index.json` (the catalog index). They ship inside the package through the
`[tool.setuptools.package-data]` entry for `dictionaries/*.json` (`pyproject.toml:57-58`); a test builds
a wheel and fails if any of the files is missing from it.

**Catalog index.** Produced by `catalog.write_index` (`python -m stt_cli.catalog`, run by publishers);
a test regenerates it and compares it byte for byte with the committed file. It refuses a directory with
no dictionaries rather than writing an empty index.

```json
{"format": 1, "dictionaries": [
  {"id": "medical-general", "version": 1, "title": "General medicine", "tags": ["medical-general"],
   "languages": ["en"], "license": "CC0-1.0", "source": "curated by hand from ...", "term_count": 42,
   "sha256": "<hex of the file's bytes>"}]}
```

Determinism: entries sorted by `id`; written with `json.dumps(..., sort_keys=True, ensure_ascii=False,
indent=2)` plus a trailing newline; `sha256` over the dictionary file's committed bytes; no timestamps.
`format` lets a future index shape be refused with "upgrade stt" instead of misread.

**Tag vocabulary.** `tags.json` maps each tag slug to `{"kind": "topic" | "facet", "label": "..."}`.
Each dictionary has exactly one topic tag and any number of facets (`vendor-names`, `drug-names`,
`anglicisms`). Topic tags are the scoring labels of section 9; 18 keeps one question under the ceiling of
about 20 options of laya, the local classifier section 9 uses. `label` is shown by the website filter and
used in the scoring question.

**Target content (the 2026-09-29 criterion on [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81)).** At least 20 dictionaries of at least 30 terms each,
covering these 18 topic tags, with a Russian companion where the field is dictated in both languages:

| Topic tag | Dictionary ids (proposed) |
| --- | --- |
| `software-engineering-tools` | `software-engineering-tools` |
| `ai-ml`, `devops-cloud`, `web-frontend`, `product-design-ux` | one each, id equal to the tag |
| `russian-it-anglicisms` | `russian-it-anglicisms` (`ru`: деплой, пулреквест, коммит) |
| `medical-general` | `medical-general`, `medical-general-ru` |
| `pharmacology` | `pharmacology` |
| `legal-contracts` | `legal-contracts`, `legal-contracts-ru` |
| `personal-finance-banking` | `personal-finance-banking`, `personal-finance-banking-ru` |
| `accounting-tax`, `marketing-analytics`, `science-math`, `education` | one each, id equal to the tag |
| `real-estate`, `automotive`, `music-production`, `gaming` | one each, id equal to the tag |

That is 21 dictionaries. The ids follow the 2026-09-29 list, which supersedes [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81)'s original four names:
`medical` becomes `medical-general` and `legal` becomes `legal-contracts`, so [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83)'s
`stt dict install medical` example reads as `stt dict install medical-general`.

**Content quality rule.** Every term is real vocabulary a speech model plausibly gets wrong. The `source`
field records the method: a mis-transcription seen in stt output, a named published or open glossary, or a
domain reviewer. An `aka` must be a wrong spelling that actually occurs and never an ordinary word in the
dictionary's languages, because aliases are substituted without asking (`stt_cli/dictionary.py:15-18`):
`"aka": ["fig"]` on Figma would rewrite every honest "fig". Notes reach the language-model pass and are a
prompt-injection surface (`stt_cli/dictionary.py:127-132`), so review treats them as untrusted text.

## 5. Install and sources

**Sources.** One protocol, two implementations; the command-line surface does not branch on which:

```python
class DictionarySource(Protocol):
    def index(self) -> Catalog: ...  # the parsed index.json of section 4
    def fetch(self, entry: CatalogEntry) -> bytes: ...  # exact file bytes, SHA-256 verified
```

| Source | Reads | Notes |
| --- | --- | --- |
| Bundled | `stt_cli/dictionaries/` in the installed package | Offline; usable before the website exists. Verifies SHA-256 too, which catches a hand-edited package. |
| Remote marketplace | `GET <base>/api/dictionaries` (index), `GET <base>/api/dictionaries/<id>` (bytes); `<base>` is `https://stt-dicts-marketplace.vercel.app` (section 8) | Standard-library `urllib`, synchronous, with the request handling of `hf._request` (`stt_cli/hf.py:472-500`); HTTPS only; a timeout; reads capped at `MAX_DICTIONARY_BYTES`. The SHA-256 must equal the index entry before anything is written. |

**Startup cost.** Importing `stt_cli.cli` does not load `urllib.request` today: `hf.py` imports it at
module level (`stt_cli/hf.py:27-28`), but nothing on the startup path imports `hf`. The remote source keeps
it that way — either `commands/dict_cmd.py` imports the source module inside the function that uses it,
or the source module imports `urllib.request` inside `fetch`/`index`. This is a startup-cost rule,
stricter than `AGENTS.md`'s import-clean rule, which allows any standard-library module at module level.
The only existing startup-import test checks a single module (`tests/test_cli_and_llm.py:247-268`), so
[#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) adds a fresh-interpreter test that importing
`stt_cli.cli` loads no third-party module and not `urllib.request`. The network is touched only by `search`
and `install` with the remote source, never by a transcription or a dictation session.

**Command-line surface.** `stt dict list` keeps listing personal terms, with byte-identical output
(`stt_cli/commands/dict_cmd.py:55-65`). New subcommands:

| Command | Does |
| --- | --- |
| `stt dict search [query] [--tag T] [--remote]` | Lists id, version, title, tags, languages and term count of entries whose id, title or tags contain the query, case-insensitively; marks installed ones. It reads only the catalog index, which holds no terms, so unlike the website's search (section 8) it does not match term text. |
| `stt dict install <id> [--remote]` | Fetches, verifies, validates (section 3), writes. Re-installing upgrades, printing old and new version, and warns if an overlay exists for it. |
| `stt dict installed` | Lists installed dictionaries (id, version, term count after the overlay, overlay or not); warns when the effective glossary exceeds the 500-term phonetic screen, merging dropped entries (section 6), or an overlay could not do something (section 7). Offline. |
| `stt dict uninstall <id>` | Deletes the installed file; leaves the overlay (the user's own writing) and says where it is. |
| `stt dict show <id>` | The effective terms of one installed dictionary, each marked base, replaced or added with its term id; disabled entries separately; the overlay's path and `base_version`, or where one would go; every overlay warning (section 7). |

The uninstall command is `uninstall`, settled by [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83)
(section 11): `stt dict rm` already deletes a personal term, and `remove` was one typo away from it.

**On-disk layout (decided on [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) and
[#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84), 2026-09-29, reversible): split by
who writes the file.**

| File | Path | Written by |
| --- | --- | --- |
| Installed dictionary | `<app_home>/dicts/installed/<id>.json` | stt (`install`, `uninstall`) |
| Overlay | `~/.config/stt-cli/dicts/overlays/<dict_id>.yaml`; `$XDG_CONFIG_HOME/stt-cli/dicts/overlays/<dict_id>.yaml` when `XDG_CONFIG_HOME` is set | the user, by hand |

`<app_home>` is `config.app_home()`: `STT_HOME` if set, otherwise
`~/Library/Application Support/stt-cli` (`stt_cli/config.py:64-69`), which already holds `config.json`
(`stt_cli/config.py:88-92`), the personal `dictionary.json` (`stt_cli/dictionary.py:438-441`) and the
archive (`stt_cli/config.py:72-96`). An installed dictionary is a downloaded artefact stt writes and
replaces itself, so it sits beside the personal dictionary it is merged with and keeps `STT_HOME`'s test
isolation. An overlay is the one dictionary file a person writes by hand and expects to find, edit and
keep, so it goes in the conventional configuration directory Alex named; an upgrade or `stt dict uninstall`
can then never touch it. stt keeps none of its own files under `~/.config` today (its one write there is
`install-skill`'s blurb for the opencode harness, `stt_cli/install.py:96-101`; the `$XDG_STATE_HOME` pid
file named in `stt_cli/commands/mic.py:592-594` belongs to the external service library), so the overlay
directory is a deliberate second root: one function resolves it, and every test that loads the effective
glossary redirects `XDG_CONFIG_HOME` the way it redirects `STT_HOME`. The Linux platform layer
([#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60)) moves `<app_home>` to XDG directories in
one place.

Writes go through `archive.write_atomic` (`stt_cli/archive.py:391`) under a `portable.lock`, the pattern of
`dictionary.editing` (`stt_cli/dictionary.py:562-573`), byte for byte as fetched so the hash stays
comparable with the index.

**Errors.** Every failure is an `SttError` with what, why and how (`stt_cli/_errors.py:45-62`):

| Case | Class and exit code | How (the fix line) |
| --- | --- | --- |
| Unknown dictionary id | `unknown_item` (`stt_cli/_errors.py:106`), exit 4 | the closest ids, and `stt dict search` |
| Remote file's hash does not match the index | `NetworkError` (`stt_cli/_errors.py:77`), exit 7 | nothing was installed; retry, report it if it repeats |
| Bundled file's hash does not match the index | `SttError` (`stt_cli/_errors.py:45`), exit 1: the installed package is damaged, not the network | nothing was installed; reinstall stt |
| Site unreachable, timeout, HTTP error | `NetworkError`, exit 7 | check the network, or install without `--remote` |
| File fails validation | `UsageError` (`stt_cli/_errors.py:65`), exit 2 | names the entry and the limit it broke |
| Index `format` newer than this stt | `UsageError`, exit 2 | upgrade stt |
| `uninstall` or `show` of an id not installed | `unknown_item`, exit 4 | `stt dict installed` |

## 6. Effective glossary and cache fingerprint

**Merge order.** `load_effective(settings)` returns a `Dictionary` and a list of warnings. With
`settings.dictionary` off (`stt_cli/config.py:188`) it returns an empty dictionary and reads nothing,
installed dictionaries included, as today's transcription and dictation call sites do.

1. Start with the personal dictionary's terms, in their order.
2. For each installed dictionary in ascending id order: parse it, apply its overlay ([#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84)), append its
   entries.
3. An appended entry whose casefolded name equals a name or alias already in the list is dropped whole:
   the personal entry, or the earlier installed one, wins ([#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83)'s criterion). Unlike `Dictionary.add`
   (`stt_cli/dictionary.py:293-306`), which merges a later entry's aliases and note into the existing one,
   nothing from the dropped entry is kept.
4. An appended alias already claimed by an earlier entry is dropped from the later entry. Otherwise
   `_alias_index` skips the alias for both (`stt_cli/dictionary.py:626-642`), silently switching off a
   correction the user wrote down.
5. Every drop is a warning naming both dictionaries, shown by `stt dict installed`, `stt dict show` and
   once per transcription through the pipeline reporter (flag first, `AGENTS.md:135`).

The result is built as `Dictionary(terms=...)`, as `load()` does (`stt_cli/dictionary.py:514`), not
through `add`, which refuses a 501st term (`stt_cli/dictionary.py:286-292`); several installed dictionaries
can exceed 500. Past 500 the phonetic screen is capped (`stt_cli/dictionary.py:736-738`); alias
correction stops at `MAX_ALIAS_BRANCHES` = 500 × 24 = 12,000 aliases (`stt_cli/dictionary.py:114`,
`:644-650`), and the language-model glossary at `LLM_GLOSSARY_CHARS` = 4,000 characters
(`stt_cli/dictionary.py:55`, `:380-400`). A single dictionary built through the commands never reaches
the screen or the branch cap (`add` refuses the 501st term and the 25th alias, `:279-292`); several
installed ones can, and each of those two caps reached is a warning in the same place as a merge drop
(step 5), naming how many entries it left out. The glossary budget is different: one large personal
dictionary can already exceed it, and `glossary()` then ends the prompt's list with
`... and N more term(s), omitted` (`:396`); that stays the behaviour for the effective glossary. The
decoder prompt takes the first 24 (`stt_cli/dictionary.py:49`, `:353`), which are the personal ones
first. Installing is an explicit act of adopting a dictionary's `aka` spellings, so they apply as facts;
near-matches to installed terms stay flags (`AGENTS.md:149-151`).

**Call sites.** Each switches from `dictionary.load()` to `load_effective` unless noted. The tickets cite
`stt_cli/live/dictation.py:221` and `:644`; on main they are `:220` and `:835`.

| Call site | Change |
| --- | --- |
| `stt_cli/pipeline.py:247` (`_resolve`) | effective glossary, loaded once; the snapshot feeds every later stage (`stt_cli/pipeline.py:230-234`) |
| `stt_cli/live/dictation.py:220` (ad-hoc session), `:835` (daemon burst) | effective glossary, loaded once per session or burst, as today. Not switched by [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83), whose change left `stt_cli/live/` to concurrent work; tracked in [#113](https://git.hyperide.ai/ultrabricks/stt-cli/issues/113). |
| `stt_cli/live/status.py:218` (`example_hint`, called once per burst from `stt_cli/live/dictation.py:358`) | takes the glossary the same session or burst already loaded instead of reading it again; keeps the built-in fallback when there is none (`stt_cli/live/status.py:217-220`), so the hint can name an installed term when the personal dictionary is empty. A burst loads the effective glossary once, not twice, before the microphone opens. [#113](https://git.hyperide.ai/ultrabricks/stt-cli/issues/113). |
| `stt_cli/commands/dict_cmd.py:217` (`_check`) | effective glossary: the command explains what the pipeline does. Today `_check` loads the personal dictionary even with `settings.dictionary` off; after the switch it reports an empty glossary then, as the pipeline would use none. |
| `stt_cli/commands/dict_cmd.py:56` (`_list`) | unchanged: personal terms, byte-identical output |
| `dictionary.editing` (`stt_cli/dictionary.py:551-573`; `add`, `rm`, `import`) | unchanged: writes the personal file only |

Downstream consumers take the object they are handed and need no change: the decoder prompt
(`stt_cli/pipeline.py:760`), `dictionary.apply` (`:772`), variant correction (`:870`), the language-model
glossary (`:905`), and the live session (`stt_cli/live/session.py:461`, `:486`).

**Fingerprint.** No new key. `_resolve` writes the loaded dictionary's `digest()` into
`settings.dict_digest` before the fingerprint is computed (`stt_cli/pipeline.py:253`; field
`stt_cli/config.py:194-196`); `dict_digest` enters the fingerprint whenever non-empty
(`stt_cli/archive.py:90-100`, `:148-157`); `digest()` hashes the ordered terms' `term`, `aka` and `note`
(`stt_cli/dictionary.py:363-378`). Handing `_resolve` the effective glossary, and passing that object to
`_without_dead_dictionary_knobs` (`stt_cli/pipeline.py:257-268`), keeps "anything that changes the words
has to be settled before the fingerprint is computed" (`AGENTS.md:152-156`) true. Required properties,
each a test:

| Change | Fingerprint |
| --- | --- |
| Install or uninstall a dictionary contributing at least one surviving entry | changes |
| Edit an overlay so the effective entries change | changes |
| Reformat an installed or overlay file; bump `version` with identical terms | unchanged (words, not presentation, `AGENTS.md:132-134`) |
| Personal dictionary only, nothing installed | identical to today's digest, so existing archives stay valid |
| `settings.dictionary` off | `""`, as today |

`archive.DECODE_REVISION` (`stt_cli/archive.py:113`) is not bumped: decoding is unchanged, and a bump would
invalidate every archived run of users who install nothing.

## 7. Overlay YAML schema

Path: `~/.config/stt-cli/dicts/overlays/<dict_id>.yaml`, or under `$XDG_CONFIG_HOME` when it is set to
an absolute path; a relative value (a `~` included, since nothing expands it there) counts as unset, as the XDG spec says, so the words of a transcript
never depend on the directory stt was started from (section 5). The schema is [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84)'s: a named-field object per entry instead of Alex's
positional `["replace", "<value>"]` sketch, because a replace changes several fields a positional pair
cannot name, and a named field lets an error say which one is wrong.

```yaml
# ~/.config/stt-cli/dicts/overlays/software-engineering-tools.yaml
base_version: 3          # the installed version this overlay was written against
entries:
  conloca:               # a term id from the base dictionary
    op: replace
    term: "ConLoca"
    aka: ["ConLog", "Conloca", "our-internal-nickname"]
    note: "our fork, not the upstream project"
  hyperide:
    op: disable
  my-codename:           # a new id, local to this overlay
    op: add
    term: "ProjNova"
    aka: ["Proj Nova"]
    note: "internal codename"
```

| `op` | Target id | Fields | Effect |
| --- | --- | --- | --- |
| `disable` | exists in the base | none | the entry is dropped |
| `replace` | exists in the base | at least one of `term`, `aka`, `note`; each given field replaces the base value whole (`aka` is not merged) | the entry keeps its position and id with the new values |
| `add` | not in the base | `term` required; `aka`, `note` optional | appended after the base entries, in overlay order |

Every entry of the composed dictionary passes section 3's per-entry checks. Composition is a pure function
of an in-memory base and a parsed overlay, with no file access, as
[#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84) requires.

**Warnings, never errors.** Printed once per load, in the shape of `stt dict check`'s near misses, and
listed by `stt dict show`:

- A `replace` or `disable` whose id the installed base no longer has: skipped, warned.
- `base_version` differs from the installed `version`: one warning naming both numbers.
- An `add` whose id the base has since gained: the overlay's entry is kept (the user wrote it), warned,
  suggesting `op: replace`.
- A `replace` or `add` whose term equals another entry's term or alias: skipped, so the base entry stays
  as it was; an overlay alias equal to another entry's term or alias: dropped from the overlay's entry.
  Each is warned naming the overlay line. The same collision between two dictionaries is section 6's
  steps 3 and 4; a colliding name or alias in an overlay never stops a transcription.
- An overlay for a dictionary that is not installed: ignored, warned. So is a `<dict_id>.yml` file,
  since only `.yaml` is read.

Details settled by [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84):

- `base_version` is required, a positive integer: without it the staleness warning could never fire.
  `entries` may be missing or empty (`entries:` with nothing under it), which changes nothing.
- Entry ids follow section 3's id rules; an `add` id is local to the overlay.
- An `add` whose id the base has since gained takes that entry's place, exactly as a `replace` of every
  field would (its `aka` and `note` default to empty, not to the base's), and is marked replaced.
- Order: disables first, so a spelling a disabled entry held is free for a replace or an add; then
  replaces, then adds, each in overlay order and each checked against the entries as they stand at that
  moment. Warnings are listed in the file's line order.
- At most `MAX_SCREENED_TERMS` = 500 entries and `MAX_DICTIONARY_BYTES` per overlay file.
- `stt dict install` of a dictionary that has an overlay prints the overlay's warnings against the
  newly installed version, so a `base_version` that no longer matches is said at the moment it
  stops matching. A broken overlay is printed as a warning there too, not an error: the install has
  already been written, and a script re-running `install` to upgrade must not read a failure.
- A replace that renames an entry to one of the spellings it keeps drops that spelling from the
  entry's `aka`, since it is now the name.
- Only regular files, or links to them, are overlays; a directory or a FIFO is not. The name must
  match exactly: on a case-insensitive disk `Fixture-Tools.yaml` would open as
  `fixture-tools.yaml`, and it is ignored and warned about as not installed, not applied. The
  not-installed and `.yml` warnings quote the file name.
  `stt dict show <id>` names a `<id>.yml` next to its "overlay: none".
- `stt dict show <id>` fails only on `<id>`'s own broken overlay. Another dictionary's broken
  overlay is a warning there, and that dictionary is counted without it for the section 6 drops;
  a transcription still stops on it.

An unparseable file, an unknown `op`, a missing required field, an id that appears twice under `entries:`
or an entry over a limit is a `UsageError` naming the line and field, as `load()` treats a malformed
personal file (`stt_cli/dictionary.py:466-477`).

**The parsing dependency: a decision [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84) must record here.** stt has no required runtime dependency
(`AGENTS.md:118-119`, `pyproject.toml:15-19`), and overlays are read on every transcription.

| Option | For | Against |
| --- | --- | --- |
| A. Standard-library parser for a documented subset: `#` comments; space-indented block mappings; plain, single- and double-quoted scalars; integers; flow lists `[a, "b"]` and block lists `- a`. Refused with a line number: anchors, aliases, tags, multiple documents, block scalars, flow mappings, tabs, a key repeated in one mapping. Plain `yes`/`no`/`on`/`off` stay strings. | Same result on every machine; nothing to install; errors point at the exact line; the subset is exactly what the schema needs. | About two hundred lines to write and test; advanced YAML is refused rather than read. |
| B. PyYAML as an optional extra, with the overlay also accepted as JSON when PyYAML is missing. | Full YAML, little parsing code. | The same file loads on one machine and fails on another, so one recording gets a different glossary and cache key depending on unrelated installs; two formats to document. |

**Decision ([#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84)): option A**, implemented in
`stt_cli/yamlsubset.py`. An overlay changes the words, so how it parses must not depend on which
optional packages happen to be present. Beyond the table's list: one leading `---` is accepted, since
editors and templates write it; plain `null`, `~`, `true` and `false` are refused with "quote it"
rather than read as text or as a non-text value, because a full YAML reader disagrees with any
answer; a trailing comma in a flow list is accepted; a list item that is itself a list or a mapping is
refused. `stt dict --help` prints the schema and the subset in one screen.

## 8. Marketplace site contract

**Deployment (name decided on [#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86),
2026-09-29, reversible).** A Vercel project named `stt-dicts-marketplace`, served at
`https://stt-dicts-marketplace.vercel.app`; that URL is the remote source's `<base>` (section 5). Renaming
later means a new Vercel domain and that one constant. Vercel's built-in Git integration
covers GitHub, GitLab, Bitbucket and Azure DevOps, not Forgejo, and points other providers at the Vercel
command-line tool (https://vercel.com/docs/git). Deployment is therefore a CI job in this repository (beside
the existing `.github/workflows/`) running `vercel build` and `vercel deploy --prebuilt`: production on
`main`, previews on pull requests, reusing the configuration [#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86) names (tasks-history-release-20260927's
`vercel.json`).

**Single source of truth: a build-time read of this repository.** The site lives here under `site/`
(outside `stt_cli*`, so not in the wheel, `pyproject.toml:50-51`), and its build reads
`stt_cli/dictionaries/` from the same checkout. A copied catalog drifts from the one the command-line tool
bundles; a fetch from a separate site repository needs a pinned reference and a second redeploy trigger.
Reading the same commit gives every dictionary change a preview in its own pull request, and the site
serves exactly the hashes the bundled source verifies.

| Page | Content |
| --- | --- |
| `/` | Catalog: tag filter (topics first, then facets, labels from `tags.json`), text search over title, tags and terms, sorted by title. No install counts: the command-line tool sends no telemetry. |
| `/d/<id>` | Title, id, version, tags, languages, license, source, term count, a sample of terms, one-click copy of `stt dict install <id>`, and a download of `<id>.json` whose SHA-256 equals the index entry. The browser never writes to the user's disk. |
| `/publish` | The chosen publish flow. |

| Endpoint | Returns (exactly the [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81) shapes) |
| --- | --- |
| `GET /api/dictionaries?tag=<t>&q=<query>` | The catalog index document of section 4; `tag` and `q` only filter its list. With no parameters it is byte-identical to the committed `index.json`. |
| `GET /api/dictionaries/<id>` | The dictionary file's committed bytes, so its SHA-256 matches the index; 404 for an unknown id. |

**Publish flow (decided on [#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86), 2026-09-29, reversible: (b)).** (a) A hosted publish API with its own authentication, modeled on
boards-app's `server/identity.mjs`, backed by a Postgres/Neon table of dictionary versions. (b) A pull
request adding `stt_cli/dictionaries/<id>.json`; its CI runs section 3's validation and the index
regeneration test, a maintainer reviews terms and notes, and the merge redeploys the site; `/publish`
documents the steps. Chosen: (b). The committed catalog stays the only source of truth, no
database or login system is needed, and the review of notes (section 4) has a natural place. Option (a)
makes a database a second catalog the bundled source never sees.

**Contract test.** One fixture set, `tests/fixtures/dictionaries/` (an `index.json` and one or more
dictionaries), feeds stt's source-parser tests and the site's API tests, which assert byte-identical serving.

## 9. Usage-based suggestions (laya)

**What laya is.** The PyPI package `laya` (Convai Innovations, Apache-2.0) is a local classifier: given
text and developer-defined typed questions (`choice`, `score`, `noul`), it returns calibrated
probabilities. It does not discover topics, degrades past about 20 options per question, and reads a few
hundred tokens per state (512 on the English checkpoint, 1,024 on the multilingual one). The feature sorts
transcripts into fixed topic tags; it is not open-ended topic discovery.

**Eligibility.** From the archive index's `created_at`: `Archive.earliest` (the first run) and
`Archive.since` (runs in the window, one per recording, so a file transcribed twice counts once).

- At least 30 days since the first archived transcript (`MIN_AGE_DAYS`).
- At least 10 archived transcripts with words in the trailing 30 days (`MIN_TRANSCRIPTS`,
  `WINDOW_DAYS`), so one topical recording cannot decide alone. The newest 60 are classified
  (`MAX_TRANSCRIPTS`).
- Only file transcriptions count today. Live dictation is not archived
  ([#15](https://git.hyperide.ai/ultrabricks/stt-cli/issues/15)); mic sessions count only after
  [#15](https://git.hyperide.ai/ultrabricks/stt-cli/issues/15) lands, and only the sessions the user chose
  to archive.

**Scoring.** Candidates are the topic tags of the bundled catalog's dictionaries that are neither
installed nor already suggested, plus `other` ("everyday talk, or any subject not listed here"). Each
option reads as the tag's label followed by three of the topic's terms: measured on the multilingual
checkpoint on 2026-09-30, the bare label "Russian IT anglicisms" gave Russian small talk about a weekend
0.69, because the word "Russian" was the match; with three terms after it the same text scored 0.51 and
two other pieces of Russian small talk 0.14, while dictated medicine in English and Russian stayed at 0.99
and above. Each transcript is split into pieces of about 1,000 characters and at most three of them,
spread over the text, are scored with one `choice` question per group of at most 19 topics plus `other`
through `Router.predict_batch` (the Router sends English to the English checkpoint and everything else to
the multilingual one). A transcript matches a tag when its best piece scores at least 0.6 for it; a tag
qualifies when at least 5 transcripts and at least a quarter of the window match it. Among qualifying tags
the one with the most matching transcripts wins, then the higher mean probability. The dictionary named is
the tag's candidate in the language most of the matching transcripts were dictated in; with none in their
language the tag does not qualify.

**On by default, opted out with one setting (changed by #88 from the opt-in drafted here).** The setting
is `dict_suggest: bool = True`, neither in `archive.FINGERPRINT_KEYS` nor in `ENRICHMENTS` (it changes no
transcript). Off: no process starts and nothing is read; the detached check asks the setting again before
it reads anything, so turning it off while a check waits still counts. The consent that matters is a different step:
no transcript text is read by a model until the user ran `stt dict suggest --setup`, which fetches laya
and its checkpoints visibly, after a disk check and a confirmation. That keeps the ticket's privacy
argument (a month of the user's speech read into a model) while letting a user who has never heard of the
marketplace hear about it once: after a month of use without the setup, one notification says what the
feature is, how to set it up and how to turn it off. The #47 "default most things on" audit applies to the
setting; the explicit step applies to the reading.

**Where it runs.** `stt_cli/suggest.py`. After a file transcription has written its result and printed its
output, `suggest.after_transcription` stamps `<app_home>/dicts/suggestions.json` and starts
`stt dict __suggest` in a session of its own, which it never waits for, so the check can neither change a
transcript nor delay the command. The child takes a lock (one check at a time), lowers its priority, and
asks `suggest_worker.py` — run by stt's own Python when the `suggest` extra is installed, otherwise by
`uv run --offline --with laya` — for probabilities, sending the pieces on standard input. The worker sets
`HF_HUB_OFFLINE=1` before anything imports `huggingface_hub`, loads both checkpoints from the local cache,
and runs on the CPU with one checkpoint resident, because a model on Metal is wired memory (#106) and this
runs beside speech models. Everything is local: the candidates come from the bundled catalog, not the
website, and the transcripts never leave the machine.

**Beside the speech models (#143, `stt_cli/suggest.py::_slot`).** Running on the CPU spares Metal's wired
memory, not the machine's memory, and the check starts right after a transcription: it can load laya's
checkpoints beside any number of concurrent whisper decodes, the shape of the 2026-09-29 hard reset
(#106). Scoring therefore holds a `model_gate` slot, passed to `proc.run` as `hold=` so the model process
itself carries the lock and is tethered to the check. It is sized `LAYA_BYTES` = 1,487,000,000, both
checkpoints (843 MB + 644 MB: one is resident at a time, but a swap holds both for a moment and torch comes on
top of the weights), which is about what large-v3-turbo weighs, so laya and a large-v3-turbo decode count
against the same slots. Only scoring takes one; the readiness probe imports laya and looks at files and loads no
checkpoint. The slot is taken once, with `model_gate.attempt`, and **never waited for**: a decode that
arrives while the check holds the last slot already waits for it (for as long as it runs, at most
`SCORE_TIMEOUT`, 25 minutes, which stays under the 30 a decode waits), and a background job must not
also queue behind the transcriptions somebody is waiting for. `resources.check_memory` is asked first,
and its warning (the checkpoints over 70% of
physical memory) is a reason to skip, since nobody is there to read a warning. When either refuses,
`suggest.Deferred` ends the check without reading anything into a model: no notification, the stored outcome
(shown by `stt dict suggest`) says `skipped, the next daily check will try again: <reason>`, and the check
counts as served, so the next one starts after `CHECK_INTERVAL` like any other. The slot count follows the
model's size (`model_gate.count`: a sixth of memory over the file size; each model probes its slots from
the top down), so laya is turned away when the slots within its own range are full: two large-v3-turbo
decodes on a 24 GB Mac, or one on a 16 GB Mac, which has a single slot for laya and so checks only while no
such decode is running. Where `stt mic` keeps one loaded all day
([#134](https://git.hyperide.ai/ultrabricks/stt-cli/issues/134)) the check is skipped until that changes.
The gate caps each model size separately, not their sum (unless `RIG_GPU_SLOTS` fixes one count for every
size, when laya and all other models share it), so models of other sizes can run beside laya whenever their
own slots are free ([#144](https://git.hyperide.ai/ultrabricks/stt-cli/issues/144)).

**Rate limits.** A check starts at most once a day (`CHECK_INTERVAL`), a notification reaches the user at
most once a week (`NOTIFY_INTERVAL`), a dictionary is named at most once ever, and the setup notification
is shown once; after it, nothing more runs until the setup is done. A stamp in the future (the clock set
back) holds nothing off. `stt dict suggest` shows the setting, whether the classifier is ready, the last
check's outcome and what was already suggested.

**Exactly one notification, never an install.** At most one per check, for the best qualifying
dictionary: `Dictionary "General medicine (Russian)" matches 7 of your last 23 transcripts (tag
medical-general). Install: stt dict install medical-general-ru. Turn off: stt config set dict_suggest
false`. A notification the platform did not accept is not recorded as a suggestion, and counts against the
weekly limit so a machine with no way to show one does not classify a month of transcripts every day. The
record is read and written under a lock by all three writers (a finishing transcription, the check, the
setup), and the check lays its findings over the record as it is when it ends. The suggestion code
path contains no install call.

**Mechanism.** `stt_cli/notify.py`, one command per platform (section 10). On macOS it is the osascript
banner `live/status.py` already shows, fire-and-forget and unable to carry a button, so the notification is
informational and the user runs the command. With the signed helper app of
[#99](https://git.hyperide.ai/ultrabricks/stt-cli/issues/99) it can carry an Install action; stt would run
`stt dict install <id>` only after the helper reports the click, and do nothing on dismissal.

**Optional extra.** laya needs torch and transformers, and its two checkpoints are 843 MB (English) and
644 MB (multilingual) in the `convaiinnovations/laya` repository. It is the `suggest` extra
(`laya>=0.3.20,<0.4`, pinned below 0.4 because the worker reads laya's result envelope), never imported by
stt itself. Without the extra, `stt dict suggest --setup` fetches it into a uv environment, as `stt diarize
install` does for pyannote, so nothing is installed into an interpreter the user owns. Missing laya or a
checkpoint: the check does nothing, raises nothing, and no other command is affected.

## 10. Platform deferrals

Platform-independent, and in scope wherever stt starts: the distributable format, catalog and tag
vocabulary, both sources, install/uninstall/show, overlays, the effective glossary and fingerprint, the
website, and section 9's eligibility, scoring and notification. Suggestion notifications are built for all
three platforms in `stt_cli/notify.py`, and only macOS was seen working on a real machine (2026-09-30):

| Platform | Command | Verified |
| --- | --- | --- |
| macOS | `osascript -e 'display notification … with title "stt"'` | live: a throwaway `STT_HOME` with a seeded month, the real detached check and laya on the CPU; usernoted logged the request from `com.apple.ScriptEditor2`, not suppressed |
| Linux | `notify-send --app-name=stt -- <title> <body>`, else `gdbus call … org.freedesktop.Notifications.Notify` (the order of oh-my-pi's `packages/tui/src/desktop-notify.ts`) | the command's shape, unit-tested |
| Windows | a toast through Windows PowerShell 5.1's WinRT projection, attributed to PowerShell's application id; the text travels in environment variables, never in the script | the command's shape, unit-tested |

Microphone capture outside macOS stays deferred: Linux in
[#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60) and
[#64](https://git.hyperide.ai/ultrabricks/stt-cli/issues/64), with Linux engines in
[#61](https://git.hyperide.ai/ultrabricks/stt-cli/issues/61); Windows on top of
[#76](https://git.hyperide.ai/ultrabricks/stt-cli/issues/76)'s start-up support.

## 11. Open decisions

| Decision | Recommendation here | Owner |
| --- | --- | --- |
| Uninstall command name, given `stt dict rm` already removes a personal term | decided on [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83): `uninstall`, which pairs with `install` and cannot be mistyped as `rm` | [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) |
| Overlay parser: standard-library subset or optional PyYAML with JSON fallback | decided on [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84): standard-library subset (section 7) | [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84) |
| Suggestion thresholds (10 transcripts; 0.6 per transcript; 5 and a quarter of the window) | kept after the 2026-09-30 measurement in section 9: topical dictation mostly 0.97 to 1.0, small talk at most 0.51, and two topical Russian pieces (a lease, developer slang) under the bar at 0.52 and 0.41, so it errs toward silence; retune when real archives disagree | [#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88) |

Decided here, not open: the version is an integer (section 3); index, API shapes and merge precedence as
above. Decided on 2026-09-29 and recorded on the tickets (each reversible): the on-disk layout, installed
dictionaries under `<app_home>` and overlays under `~/.config/stt-cli/` (section 5, #83, #84); the site
name `stt-dicts-marketplace` at `https://stt-dicts-marketplace.vercel.app` (section 8, #86). Decided on
[#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86) when the site was built: publishing is a pull
request, and the site lives in `site/` in this repository (section 8).
