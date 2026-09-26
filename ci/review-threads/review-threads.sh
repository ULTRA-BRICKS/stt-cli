#!/usr/bin/env bash
# Merge gate: FAIL if a PR has unresolved review threads.
#
# FORGEJO-NATIVE (stt-cli#53): this repo is hosted on Forgejo (git.hyperide.ai), which has
# no GitHub-compatible GraphQL API. The previous version of this script shelled out to
# `gh api graphql` against github.com's reviewThreads query — that fails on every PR here:
# `gh auth status` only knows github.com, and there is no github.com API for a repo that
# only exists on Forgejo (reproduced: `gh api graphql` against this repo's PRs failed with
# "could not query review threads (gh api failed) — failing closed" even on a PR with zero
# comments). Forgejo's REST API has no GraphQL and no single "unresolved thread count"
# endpoint, so this walks the same data GitHub's reviewThreads query walks, via Forgejo's
# REST equivalents:
#   GET /repos/{owner}/{repo}/pulls/{pr}/reviews                     (paginated)
#   GET /repos/{owner}/{repo}/pulls/{pr}/reviews/{id}/comments        (paginated)
# Each inline review comment carries a `resolver` field (a User, or null/absent). Forgejo's
# "Resolve conversation" button sets the resolving user on every comment that shares a
# conversation's (path, position) — the same anchor GitHub's reviewThreads groups by — so
# comments are grouped into threads by (path, position) and a thread counts as unresolved
# if ANY comment in its group is still unresolved (fail toward "still open", never toward
# "resolved", matching the previous script's fail-closed posture on a query error).
# PENDING reviews (drafts not yet submitted) are skipped: their comments aren't real
# threads yet, matching GitHub's reviewThreads semantics. Every review that's actually
# SUBMITTED is walked for comments regardless of any comment-count field the API reports
# (see FAIL-CLOSED below) — an extra empty request for a comment-less review is cheap
# insurance against exactly the schema drift that field would otherwise be fragile to.
#
# FAIL-CLOSED, for real: `set -e` alone does NOT make this safe. A failing command inside a
# function whose call site is itself being TESTED (`if ! x="$(f)"`, `f || …`, `! f`) runs
# with `errexit` suspended for the WHOLE function body — a known bash gotcha — so a `jq`
# parse failure buried inside `paginate` would otherwise be swallowed, leave a stale/partial
# value in a shell variable, and let the caller's `if !` see a *successful* return. Every
# `jq`/`curl` call below is therefore followed by an explicit `|| return 1` / `|| exit 1` —
# never relying on `errexit` to catch it — and every page is validated as a JSON array
# before being trusted, so a malformed response (an HTML login/error page, a truncated body)
# fails the gate instead of silently counting as zero threads.
#
# Requires: curl + jq (both present on the runner already — jq is used by the CodeQL
# self-gate script in this same repo). No auth token is required for a PUBLIC repo's
# read-only PR data; set FORGEJO_TOKEN (the workflow passes the runner's own auto-token,
# which — unlike `gh`'s github.com-only token — authenticates against THIS Forgejo
# instance's own API) to avoid anonymous rate limits. The token is written to a private
# (mode 600) curl config file and read via `curl -K`, never placed on the process argv —
# matching this repo's own credential-hygiene rule (AGENTS.md: keep a secret "out of an
# argument vector that `curl` would expose") — which also sidesteps a stock-macOS-bash-3.2
# landmine: bash < 4.4 raises "unbound variable" for a *declared-but-empty* array expanded
# under `set -u`, so a bash-array-based `-H` flag broke the local/no-token path on this
# repo's own target platform. A one-line (or empty) config file needs no array at all.
#
# Knobs (env):
#   PR_NUMBER      PR number (or pass as $1).
#   FORGEJO_HOST   e.g. https://git.hyperide.ai (default: GITHUB_SERVER_URL, which Forgejo
#                  Actions populates with the Forgejo instance's own URL — never
#                  github.com — falling back to parsing `origin`'s remote URL for a local/
#                  manual run outside CI).
#   FORGEJO_REPO   owner/repo (default: GITHUB_REPOSITORY, same fallback logic).
#   FORGEJO_TOKEN  optional bearer token (sent as `Authorization: token …`).
#
# Usage (this script is bash, not POSIX sh — it uses `local` and arithmetic expansion):
#   bash ci/review-threads/review-threads.sh 123
#   PR_NUMBER=123 bash ci/review-threads/review-threads.sh
set -euo pipefail

PR="${1:-${PR_NUMBER:-}}"
[ -n "$PR" ] || { echo "Usage: $0 <pr-number>  (or set PR_NUMBER)" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "curl not found" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "jq not found" >&2; exit 2; }
command -v mktemp >/dev/null 2>&1 || { echo "mktemp not found" >&2; exit 2; }

# Prefer the CI-provided, host-agnostic env vars (correct under Forgejo Actions — they
# point at THIS instance, never github.com); fall back to parsing `origin` for a local run.
# Trailing-slash-normalize whichever source wins, so a `GITHUB_SERVER_URL` (or manual env)
# ending in `/` can't produce a doubled `//api/v1/...` and a confusing 404 downstream.
: "${FORGEJO_HOST:=${GITHUB_SERVER_URL:-}}"
FORGEJO_HOST="${FORGEJO_HOST%/}"
: "${FORGEJO_REPO:=${GITHUB_REPOSITORY:-}}"
if [ -z "$FORGEJO_HOST" ] || [ -z "$FORGEJO_REPO" ]; then
  origin_url="$(git remote get-url origin 2>/dev/null || true)"
  # https://[user@]host/owner/repo[.git]  or  git@host:owner/repo[.git] — greedy capture
  # (avoid lazy quantifiers: not every `sed -E` build honors them) then strip a trailing
  # `.git` with plain shell suffix removal, which is unambiguous either way.
  parsed="$(printf '%s' "$origin_url" | sed -E \
    -e 's#^[A-Za-z][A-Za-z0-9+.-]*://([^/@]*@)?([^/]+)/(.+)$#\2 \3#' \
    -e 's#^[^@]+@([^:]+):(.+)$#\1 \2#')"
  parsed="${parsed%.git}"
  parsed="${parsed%/}"
  [ -z "$FORGEJO_HOST" ] && FORGEJO_HOST="https://$(printf '%s' "$parsed" | cut -d' ' -f1)"
  [ -z "$FORGEJO_REPO" ] && FORGEJO_REPO="$(printf '%s' "$parsed" | cut -d' ' -f2-)"
fi
if [ -z "$FORGEJO_HOST" ] || [ -z "$FORGEJO_REPO" ] || [ "$FORGEJO_HOST" = "https://" ]; then
  echo "::error::review-threads: could not derive FORGEJO_HOST/FORGEJO_REPO — set them explicitly." >&2
  exit 2
fi

AUTH_CFG="$(mktemp)"
chmod 600 "$AUTH_CFG"
TMP_COMMENTS="$(mktemp)"
printf '[]\n' >"$TMP_COMMENTS"   # seed so a zero-review PR still slurps to `[]`, never `null`
cleanup() { rm -f "$AUTH_CFG" "$TMP_COMMENTS"; }
trap cleanup EXIT
[ -n "${FORGEJO_TOKEN:-}" ] && printf 'header = "Authorization: token %s"\n' "$FORGEJO_TOKEN" >"$AUTH_CFG"

# api <path-with-query> -> prints the response body. Fails CLOSED via `curl -f`: a non-2xx
# response (bad host/repo/PR, auth error, instance hiccup) is a hard error here, never a
# silently-empty "[]" that would masquerade as "no unresolved threads". `--max-time`/
# `--retry` keep a stalled connection from wedging this required merge check until the whole
# job times out.
api() {
  curl -fsSL --max-time 30 --retry 3 --retry-connrefused -K "$AUTH_CFG" "${FORGEJO_HOST}/api/v1${1}"
}

# paginate <path> -> prints a concatenated, id-deduplicated JSON array across every page.
# Termination is NOT a simple "stop once a page is empty" or "stop once a page is shorter
# than `limit`" — this Forgejo instance's own endpoints don't agree on pagination behavior
# (confirmed live against git.hyperide.ai): the PR reviews LIST properly empties on the
# page past the end (`limit=50` covering all 5 reviews on a real PR -> page 2 is `[]`), but
# `/reviews/{id}/comments` IGNORES `page` entirely and returns the SAME full result set
# every time (confirmed: requesting pages 1-5 of a real 4-comment review returns the
# identical 4 comments each time, in a possibly different order) — trusting "shorter than
# `limit`" alone to stop would never advance past page 1 there, which is fine, but trusting
# "keep going until empty" alone (as an earlier version of this fix did) means it NEVER
# terminates against that endpoint — reproduced: it ran to page 463 before being killed.
# So this stops on whichever fires first: (a) an empty page, or (b) this page's id set being
# IDENTICAL to the immediately preceding page's (sorted, so a reordered-but-same-content
# repeat — as observed above — still counts). Each page is checked against the previous
# one and, unless it's that exact duplicate, appended BEFORE the next page is fetched and
# compared against it in turn — so the one true page from an ignore-paging endpoint (its
# first) is always kept, and only the redundant repeat that follows it is dropped; the
# final `unique_by(.id)` sweep makes any accidental double-fetch harmless either way.
# Deliberately does NOT stop merely because a page is
# shorter than `limit`: an instance whose admin-configured page-size cap is below the
# requested `limit` would otherwise silently truncate (false PASS — later items never
# fetched). A hard 500-page ceiling is a last-resort circuit breaker against any other
# not-yet-seen pagination misbehavior; hitting it fails the gate closed rather than looping
# forever or under-counting. Pages accumulate in a temp file (flattened by one `jq -s` at
# the end) rather than being re-parsed through `--argjson` on every iteration: besides being
# O(n^2), a large PR (long diff hunks, many comments) can push `--argjson`'s value past the
# OS's argv size limit (E2BIG) and kill the gate outright — a file has no such ceiling.
paginate() {
  local path="$1" page=1 sep chunk n tmp rc ids prev_ids=''
  tmp="$(mktemp)" || return 1
  printf '[]\n' >"$tmp"   # seed so a zero-item result still slurps to `[]`, never `null`
  while :; do
    if [ "$page" -gt 500 ]; then
      echo "::error::paginate: ${path} did not terminate after 500 pages — refusing to loop forever. Failing closed." >&2
      rm -f "$tmp"
      return 1
    fi
    case "$path" in *'?'*) sep='&' ;; *) sep='?' ;; esac
    chunk="$(api "${path}${sep}limit=50&page=${page}")" || { rm -f "$tmp"; return 1; }
    # Validate the shape before trusting it: a login/error HTML page or truncated body would
    # otherwise fail `jq` in a context (`if ! x="$(paginate …)"`) where `errexit` is
    # suspended for this whole function — see the FAIL-CLOSED header note — so the check is
    # explicit here, never implicit.
    printf '%s' "$chunk" | jq -e 'type == "array"' >/dev/null 2>&1 || { rm -f "$tmp"; return 1; }
    n="$(printf '%s' "$chunk" | jq 'length')" || { rm -f "$tmp"; return 1; }
    [ "$n" -eq 0 ] && break
    ids="$(printf '%s' "$chunk" | jq -c '[.[].id] | sort')" || { rm -f "$tmp"; return 1; }
    if [ "$page" -gt 1 ] && [ "$ids" = "$prev_ids" ]; then
      # A repeat page of exactly `limit` (50) items is AMBIGUOUS, not a safe stop: it looks
      # identical whether (a) the true total is exactly 50 on an ignore-`page` endpoint (safe
      # to stop), or (b) the endpoint is silently CAPPING at 50 and truncating a larger real
      # set (items 51+ would be dropped, undercounting unresolved threads — fail-open). A
      # repeat page smaller than `limit` can only be case (a): fewer than `limit` items were
      # returned, so there is structurally nothing left to truncate. Only that case is a safe
      # stop; the ambiguous 50-item case fails closed instead of guessing.
      if [ "$n" -eq 50 ]; then
        echo "::error::paginate: ${path} returned an identical, full (50-item) page twice in a row — cannot tell a genuinely-50-item result apart from a silently-capped/truncated one on an endpoint that ignores 'page'. Failing closed rather than risk under-counting." >&2
        rm -f "$tmp"
        return 1
      fi
      break
    fi
    printf '%s\n' "$chunk" >>"$tmp"
    prev_ids="$ids"
    page=$((page + 1))
  done
  jq -cs 'add | unique_by(.id)' "$tmp"
  rc=$?
  rm -f "$tmp"
  return "$rc"
}

if ! reviews="$(paginate "/repos/${FORGEJO_REPO}/pulls/${PR}/reviews")"; then
  echo "::error::could not query reviews for PR #$PR (Forgejo API failed) — failing closed." >&2
  echo "FAIL: review query failed for PR #$PR." >&2
  exit 1
fi

# Every SUBMITTED review (skip PENDING drafts — not real threads yet). Deliberately NOT
# pre-filtered on a comment-count field: Forgejo has renamed/omitted such fields across
# versions, and `(.comments_count // 0) > 0` would silently fail OPEN — every review
# skipped, zero comments ever fetched, gate reports PASS — the moment that field is absent
# or named differently. Fetching (possibly empty) comments for every submitted review costs
# one cheap extra request and cannot under-count.
review_ids="$(printf '%s' "$reviews" | jq -r '.[] | select(.state != "PENDING") | select(.id != null) | .id')" || {
  echo "::error::could not parse reviews for PR #$PR — failing closed." >&2
  echo "FAIL: review query failed for PR #$PR." >&2
  exit 1
}

for rid in $review_ids; do
  if ! chunk="$(paginate "/repos/${FORGEJO_REPO}/pulls/${PR}/reviews/${rid}/comments")"; then
    echo "::error::could not query comments for review $rid on PR #$PR (Forgejo API failed) — failing closed." >&2
    echo "FAIL: review-comment query failed for PR #$PR." >&2
    exit 1
  fi
  printf '%s\n' "$chunk" >>"$TMP_COMMENTS"
done

# Group by (path, position) — falling back to original_position when position is null
# (a comment can go position-less once the diff context it was anchored to changes) — the
# same anchor a "conversation" is keyed by. A thread is unresolved if ANY comment in its
# group has no resolver: resolving sets it on every comment sharing that anchor, so this is
# normally equivalent to "every comment unresolved" — ANY is the fail-toward-open choice if
# that invariant is ever violated. NOTE: this keys by anchor, not by conversation identity —
# two independent reviewers' separate comments on the same (path, position), or two
# position-less comments on the same file after a force-push strips their diff context, merge
# into one group. The gate's PASS/FAIL bit is never wrong from this (merging only ever
# reduces the printed count, never flips a real unresolved comment to resolved), but the
# printed "N unresolved" count can undercount distinct conversations.
UNRESOLVED="$(jq -cs '
  add
  | group_by([.path, (.position // .original_position)])
  | map(select(any(.[]; .resolver == null)))
  | length
' "$TMP_COMMENTS")" || {
  echo "::error::could not compute unresolved threads for PR #$PR — failing closed." >&2
  echo "FAIL: review-comment parse failed for PR #$PR." >&2
  exit 1
}

if [ "${UNRESOLVED:-0}" != "0" ]; then
  echo "::error::PR #$PR has $UNRESOLVED unresolved review thread(s) — resolve every thread before merging." >&2
  echo "FAIL: $UNRESOLVED unresolved review thread(s) on PR #$PR." >&2
  exit 1
fi
echo "PASS: PR #$PR has no unresolved review threads."
