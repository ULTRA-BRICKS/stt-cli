# Durable benchmark progress

Follow-up to [the incomplete second comparison](voz-comparison-attempt-2.md).
The earlier journal limitation described in that historical report is now addressed
for `stt bench`. Existing pilot results have not been relabelled or recomputed.

```sh
stt bench corpus.json --engine voz:voz --engine whispercpp:large-v3-turbo \
  --repeats 3 --timeout 90 --max-seconds 600 -o comparison.json
```

This creates `comparison.json.events.jsonl` before model preparation. Each complete
line is flushed and `fsync`ed before continuing. The file is exclusively created
with mode 0600 and never overwrites another run or follows a pre-existing symlink.
It contains references and recognized speech: treat it as private, like the final
report. The existing complete-report filename is still protected against clobbering.

Events identify the environment, input hashes and references, preparation starts
and results, and every trial's engine, case, phase and repetition. Completed trials
include their hypothesis, error counts, wall time and native metrics. The last
unmatched `trial_started` identifies where work was interrupted. An exception or
cooperative cancellation appends an `aborted` event with its exception type, not
arbitrary exception text. SIGKILL or loss of the machine cannot append that event;
ignore a partial final line and keep the preceding complete lines.

`completed` means the comparison loop finished, not that every engine worked: its
`complete` boolean and per-engine failures are authoritative. A timed-out whole run
does not publish a misleading successful final report. Earlier trial lines remain
available even when the final JSON could not be produced. This is not automatic
resume: retries use a new output name and do not append to an earlier experiment.

`--timeout` remains the per-trial limit. `--max-seconds` is an optional whole-run
cooperative deadline including model preparation. It should be shorter than an
outer CI job limit, leaving time to preserve diagnostics. As with asyncio deadlines,
synchronous work and cleanup may delay delivery of cancellation; this is not a
replacement for an external hard process/job timeout. Journal I/O is outside the
per-trial wall timer, but is included in the whole-run budget. Timing and scoring
rules from the original benchmark are unchanged.

The Python API accepts `on_event=journal.emit` for custom experiment scripts. Existing
custom callers that omit the callback, including the historical pilot script, do
not automatically gain persistence. Use `stt bench` or explicitly attach a Journal.

## Verified in this follow-up

The recovered branch first reproduced 551 passing tests and one skip. Six new tests
exercise durable in-flight evidence, cancellation after completed trials, failed
preparation, exclusive/private file creation, symlink rejection and the CLI-wide
budget. The full local suite then passed 557 tests with one skip. These are software
regression checks, not fresh speech-model or Apple Neural Engine measurements.

The GitHub branch and PR already existed before this follow-up: the earlier chat
answer claiming no integration or PR had been created was incorrect. PR #41 contains
the native adapter, benchmark tools, prior measured pilot and documented limitations.
The physical-Mac ANE comparison and representative Russian/meeting evaluation remain
unverified; no VM or unit-test timing is substituted for them.
