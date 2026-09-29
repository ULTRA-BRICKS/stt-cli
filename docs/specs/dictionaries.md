# Dictionaries: distributable format, catalog, install, overlays, marketplace, suggestions

## 1. Status and scope

This document is the contract for the dictionary marketplace epic,
[#90](https://git.hyperide.ai/ultrabricks/stt-cli/issues/90), and its five sub-tickets. Every `file:line`
citation was verified against main [e10967b](https://git.hyperide.ai/ultrabricks/stt-cli/commit/e10967bd61)
(2026-09-29); when code moves, fix the citation in the pull request that moves it. What exists
today is one hand-maintained personal dictionary: `dictionary.json` under the application home
(`stt_cli/dictionary.py:43`, `:438-441`), shaped `{"terms": [{term, aka, note}]}`
(`stt_cli/dictionary.py:149-166`), keyed only by casefolded term text (`stt_cli/dictionary.py:252-254`),
and edited through `stt dict list/add/rm/import/check` (`stt_cli/commands/dict_cmd.py:34-48`). None of
the following is built yet: the distributable format, the `stt_cli/dictionaries/` content and its
catalog index, any dictionary source, the `search`/`install`/`installed`/`remove`/`show` commands,
installed-dictionary merging, overlays, the marketplace website, and usage-based suggestions. Build order,
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
`tags` are checked against ISO 639-1, the SPDX list and the tag vocabulary.

**Versioning: an integer.** [#81](https://git.hyperide.ai/ultrabricks/stt-cli/issues/81)'s prose says "a semantic version", but its own sketch writes
`"version": 1` and [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84)'s overlay writes `base_version: 3`. The only consumers ask "is the installed copy
older than the catalog's" and "was this overlay written against the installed version", both plain
comparisons. Semantic versioning's major/minor/patch split serves dependency resolution, which
dictionaries do not have, and would make every publisher decide whether renaming an alias is minor or
patch for no reader's benefit.

## 4. Catalog and tags

**Where files live.** `stt_cli/dictionaries/`: one `<id>.json` per dictionary, `tags.json` (the tag
vocabulary) and `index.json` (the catalog index). They ship inside the package, so `pyproject.toml` must
gain a `[tool.setuptools.package-data]` entry for `dictionaries/*.json`; today it has only
`packages.find` (`pyproject.toml:50-51`), and the wheel would ship without them.

**Catalog index.** Produced by a standard-library function in the package (run as a module by
publishers); a test regenerates it and compares it byte for byte with the committed file.

```json
{"format": 1, "dictionaries": [
  {"id": "medical-general", "version": 1, "title": "General medicine", "tags": ["medical-general"],
   "languages": ["en"], "license": "CC0-1.0", "term_count": 42, "sha256": "<hex of the file's bytes>"}]}
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
| `stt dict installed` | Lists installed dictionaries (id, version, term count, overlay or not); warns when the effective glossary exceeds the 500-term phonetic screen or merging dropped entries (section 6). Offline. |
| `stt dict remove <id>` | Deletes the installed file; leaves the overlay (the user's own writing) and says where it is. |
| `stt dict show <id>` | The effective terms of one installed dictionary, each marked base, replaced or added; disabled entries separately; every overlay warning (section 7). Before [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84), base terms only. |

`remove` is a working name. Section 11 recommends `uninstall` instead, because `stt dict rm` already
deletes a personal term and the two are one typo apart; whichever name #83 settles replaces `remove`
everywhere in this document.

**On-disk layout (decided on [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) and
[#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84), 2026-09-29, reversible): split by
who writes the file.**

| File | Path | Written by |
| --- | --- | --- |
| Installed dictionary | `<app_home>/dicts/installed/<id>.json` | stt (`install`, `remove`) |
| Overlay | `~/.config/stt-cli/dicts/overlays/<dict_id>.yaml`; `$XDG_CONFIG_HOME/stt-cli/dicts/overlays/<dict_id>.yaml` when `XDG_CONFIG_HOME` is set | the user, by hand |

`<app_home>` is `config.app_home()`: `STT_HOME` if set, otherwise
`~/Library/Application Support/stt-cli` (`stt_cli/config.py:64-69`), which already holds `config.json`
(`stt_cli/config.py:88-92`), the personal `dictionary.json` (`stt_cli/dictionary.py:438-441`) and the
archive (`stt_cli/config.py:72-96`). An installed dictionary is a downloaded artefact stt writes and
replaces itself, so it sits beside the personal dictionary it is merged with and keeps `STT_HOME`'s test
isolation. An overlay is the one dictionary file a person writes by hand and expects to find, edit and
keep, so it goes in the conventional configuration directory Alex named; an upgrade or `stt dict remove`
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
| `remove` or `show` of an id not installed | `unknown_item`, exit 4 | `stt dict installed` |

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
| `stt_cli/live/dictation.py:220` (ad-hoc session), `:835` (daemon burst) | effective glossary, loaded once per session or burst, as today |
| `stt_cli/live/status.py:218` (`example_hint`, called once per burst from `stt_cli/live/dictation.py:358`) | takes the glossary the same session or burst already loaded instead of reading it again; keeps the built-in fallback when there is none (`stt_cli/live/status.py:217-220`), so the hint can name an installed term when the personal dictionary is empty. A burst loads the effective glossary once, not twice, before the microphone opens. |
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
| Install or remove a dictionary contributing at least one surviving entry | changes |
| Edit an overlay so the effective entries change | changes |
| Reformat an installed or overlay file; bump `version` with identical terms | unchanged (words, not presentation, `AGENTS.md:132-134`) |
| Personal dictionary only, nothing installed | identical to today's digest, so existing archives stay valid |
| `settings.dictionary` off | `""`, as today |

`archive.DECODE_REVISION` (`stt_cli/archive.py:113`) is not bumped: decoding is unchanged, and a bump would
invalidate every archived run of users who install nothing.

## 7. Overlay YAML schema

Path: `~/.config/stt-cli/dicts/overlays/<dict_id>.yaml`, or under `$XDG_CONFIG_HOME` when it is set
(section 5). The schema is [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84)'s: a named-field object per entry instead of Alex's
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
- An overlay for a dictionary that is not installed: ignored, warned.

An unparseable file, an unknown `op`, a missing required field, an id that appears twice under `entries:`
or an entry over a limit is a `UsageError` naming the line and field, as `load()` treats a malformed
personal file (`stt_cli/dictionary.py:466-477`).

**The parsing dependency: a decision [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84) must record here.** stt has no required runtime dependency
(`AGENTS.md:118-119`, `pyproject.toml:15-19`), and overlays are read on every transcription.

| Option | For | Against |
| --- | --- | --- |
| A. Standard-library parser for a documented subset: `#` comments; space-indented block mappings; plain, single- and double-quoted scalars; integers; flow lists `[a, "b"]` and block lists `- a`. Refused with a line number: anchors, aliases, tags, multiple documents, block scalars, flow mappings, tabs, a key repeated in one mapping. Plain `yes`/`no`/`on`/`off` stay strings. | Same result on every machine; nothing to install; errors point at the exact line; the subset is exactly what the schema needs. | About two hundred lines to write and test; advanced YAML is refused rather than read. |
| B. PyYAML as an optional extra, with the overlay also accepted as JSON when PyYAML is missing. | Full YAML, little parsing code. | The same file loads on one machine and fails on another, so one recording gets a different glossary and cache key depending on unrelated installs; two formats to document. |

Recommendation: option A. An overlay changes the words, so how it parses must not depend on which optional
packages happen to be present.

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

**Publish flow (from [#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86); open).** (a) A hosted publish API with its own authentication, modeled on
boards-app's `server/identity.mjs`, backed by a Postgres/Neon table of dictionary versions. (b) A pull
request adding `stt_cli/dictionaries/<id>.json`; its CI runs section 3's validation and the index
regeneration test, a maintainer reviews terms and notes, and the merge redeploys the site; `/publish`
documents the steps. Recommendation: (b). The committed catalog stays the only source of truth, no
database or login system is needed, and the review of notes (section 4) has a natural place. Option (a)
makes a database a second catalog the bundled source never sees.

**Contract test.** One fixture set, `tests/fixtures/dictionaries/` (an `index.json` and one or more
dictionaries), feeds stt's source-parser tests and the site's API tests, which assert byte-identical serving.

## 9. Usage-based suggestions (laya)

**What laya is.** The PyPI package `laya` is a local classifier: given text and developer-defined typed
questions (`choice`, `score`, `noul`), it returns calibrated probabilities. It does not discover topics,
degrades past about 20 options per question, and reads about 4,000 tokens reliably
([#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88)). The feature sorts transcripts into fixed topic tags; it is not open-ended topic discovery.

**Eligibility.** From the archive index, which records `created_at` per run (`stt_cli/archive.py:54`,
indexed `:66`). `Archive.recent` has no date filter (`stt_cli/archive.py:335-343`), so [#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88) adds a query
for the earliest `created_at` and a windowed count.

- At least 30 days since the first archived transcript.
- At least 10 archived transcripts in the trailing 30 days: the minimum [#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88) asks this spec to fix, so one
  topical recording cannot decide alone.
- Only file transcriptions count today. Live dictation is not archived
  ([#15](https://git.hyperide.ai/ultrabricks/stt-cli/issues/15)); mic sessions count only after [#15](https://git.hyperide.ai/ultrabricks/stt-cli/issues/15) lands,
  and only the sessions the user chose to archive.

**Scoring.** Candidates are the topic tags of catalog dictionaries not installed, plus "none of these".
Each transcript, split into pieces under the input limit, gets one `choice` question per piece via
`predict_batch` (`predict_shortlist` if candidates ever exceed 20). A transcript matches a tag when its best
piece scores at least 0.6 for it; a tag qualifies when at least 5 transcripts and at least a quarter of the
window match it. These three numbers are code constants, tuned on real data by [#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88).

**Opt-in, off by default.** A new setting `dict_suggest: bool = False`, not in `archive.FINGERPRINT_KEYS`
(it changes no transcript). Off: no archive text is read and laya is never imported. This deliberately
departs from the "default most things on" audit
([#47](https://git.hyperide.ai/ultrabricks/stt-cli/issues/47)), because it reads a month of the user's
speech into a model. Everything runs locally. On: the check runs at most once a day, after a file
transcription has written its result and printed its output, in a detached child process, so it can never
affect a transcript or delay the command's exit; its state (last check, dictionaries already suggested)
lives in `<app_home>/dicts/suggestions.json`.

**Exactly one notification, never an install.** At most one per check, for the highest-scoring qualifying
dictionary, never twice for the same one. It names the dictionary, the reason and the command:
`Dictionary "General medicine" matches 7 of your last 23 transcripts (tag medical-general). Install: stt
dict install medical-general`. The suggestion code path contains no install call.

**Mechanism.** Today: the osascript banner, `_banner` (`stt_cli/live/status.py:243-261`), fire-and-forget
and unable to carry a button (`stt_cli/live/status.py:1-15`), so the notification is informational and the
user runs the command. With the signed helper app of
[#99](https://git.hyperide.ai/ultrabricks/stt-cli/issues/99) (not built), it carries an Install action;
stt runs `stt dict install <id>` only after the helper reports the click, and does nothing on dismissal.

**Optional extra.** laya needs torch and transformers and downloads a checkpoint of about 400 million
parameters from the Hugging Face Hub on first use, so it is an optional extra (for example
`suggest = ["laya"]` beside `mlx` and `diarize`, `pyproject.toml:21-27`), imported lazily inside the scoring
function with the failure cached, following task-cli's `tasklib/laya_autoinstall.py`. Missing laya or
checkpoint: the feature does nothing, raises nothing, and no other command is affected. The download goes
through `resources.py`'s disk guard like every large download.

## 10. Platform deferrals

Platform-independent, and in scope wherever stt starts: the distributable format, catalog and tag
vocabulary, both sources, install/remove/show, overlays, the effective glossary and fingerprint, the
website, and section 9's eligibility and scoring. Deferred: notifications and microphone capture outside
macOS. Linux needs `notify-send` or a D-Bus notification action (oh-my-pi's
`packages/tui/src/desktop-notify.ts` is the one precedent found) plus capture, tracked in
[#60](https://git.hyperide.ai/ultrabricks/stt-cli/issues/60) and
[#64](https://git.hyperide.ai/ultrabricks/stt-cli/issues/64), with Linux engines in
[#61](https://git.hyperide.ai/ultrabricks/stt-cli/issues/61). Windows needs a toast notification (no
precedent found) on top of [#76](https://git.hyperide.ai/ultrabricks/stt-cli/issues/76)'s start-up support.
Until then the suggestion check prints its result to the terminal instead of posting a notification.

## 11. Open decisions

| Decision | Recommendation here | Owner |
| --- | --- | --- |
| Uninstall command name, given `stt dict rm` already removes a personal term | `uninstall` instead of `remove`: pairs with `install`, cannot be mistyped as `rm` | [#83](https://git.hyperide.ai/ultrabricks/stt-cli/issues/83) |
| Overlay parser: standard-library subset or optional PyYAML with JSON fallback | standard-library subset (section 7) | [#84](https://git.hyperide.ai/ultrabricks/stt-cli/issues/84), recorded in this file |
| Publish flow: hosted API or pull request | pull request (section 8) | [#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86) |
| Site placement: `site/` here or a separate repository | `site/` in this repository (section 8) | [#86](https://git.hyperide.ai/ultrabricks/stt-cli/issues/86) |
| Suggestion thresholds (10 transcripts; 0.6 per transcript; 5 and a quarter of the window) | start here, tune on real archives | [#88](https://git.hyperide.ai/ultrabricks/stt-cli/issues/88) |

Decided here, not open: the version is an integer (section 3); index, API shapes and merge precedence as
above. Decided on 2026-09-29 and recorded on the tickets (each reversible): the on-disk layout, installed
dictionaries under `<app_home>` and overlays under `~/.config/stt-cli/` (section 5, #83, #84); the site
name `stt-dicts-marketplace` at `https://stt-dicts-marketplace.vercel.app` (section 8, #86).
