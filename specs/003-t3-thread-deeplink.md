# 003 — Open the exact T3 Code thread on click

**Status**: shipped
**Requested by**: Andy Trouvé
**Date**: 2026-10-19

## Problem

Claude sessions driven by T3 Code (desktop app) show up in the widget, but clicking one only raises
the T3 window. With several T3 threads running, the user lands on whatever thread T3 last displayed
and has to hunt for the right one by hand.

## Solution

Clicking a T3 Code session (left click, context menu "Focus", or Enter) raises T3 **and** switches it
to the thread that owns that Claude session. If the thread cannot be resolved, behaviour stays exactly
as today: T3 is raised, nothing else.

Mechanism (verified live on T3 Code beta, 2026-10-19):

- T3 registers `x-scheme-handler/t3code`; `t3code://threads/<environmentId>/<threadId>` navigates the
  running instance to that thread.
- `environmentId`: `<t3 home>/userdata/environment-id`.
- `threadId`: `<t3 home>/userdata/statev2.sqlite`, table `orchestration_v2_projection_provider_threads`,
  `payload_json.nativeThreadRef.nativeId` = Claude session id → `payload_json.appThreadId`.
- T3 session marker: env `CHROME_DESKTOP=com.t3tools.T3Code.desktop` on the `claude` process.
- `<t3 home>`: parent of `bin/` in env `T3CODE_CLI_PATH` (`~/.t3/bin/t3`), fallback `~/.t3`.

## Scope

- GTK widget only (`claude-watcher-gtk.py`).
- Tag local T3 sessions at scan time (marker + T3 home + Claude session id), no I/O added to the tick
  beyond the env already read.
- At click time: resolve thread id (read-only SQLite), then open the deep link in-process with
  `Gio.AppInfo.launch_default_for_uri` (no external binary), non-blocking.
- Window raise (`wmctrl`/`xdotool`) unchanged and still runs first.

## Out of scope

- TUI (`../tui`) — separate spec if wanted.
- Remote sessions (no local T3 to drive).
- Showing a "T3" badge in the row.
- Other GUI frontends (Claude desktop, IDE extensions).
- Codex threads inside T3 (only `claudeAgent` provider rows are matched, implicitly via the Claude
  session id).

## Acceptance criteria

- [x] **AC1** Given a local session whose env has `CHROME_DESKTOP=com.t3tools.T3Code.desktop`, when the
  user clicks it, then `t3code://threads/<environmentId>/<appThreadId>` is handed to the default
  `x-scheme-handler/t3code` via `Gio.AppInfo.launch_default_for_uri`, after the window raise.
- [x] **AC2** Given a non-T3 session (terminal, kitty, daemon, remote), when clicked, then no deep link
  is opened and behaviour is byte-identical to today.
- [x] **AC3** Given a T3 session whose Claude session id has no row in `statev2.sqlite` (or the DB /
  `environment-id` is missing, unreadable, locked, or has an unexpected schema, or no handler is
  registered for `t3code:` — `GLib.Error`), when clicked, then no
  deep link is opened, no exception escapes, and the window raise still happens.
- [x] **AC4** The thread lookup opens the DB read-only (`mode=ro` URI) and never writes or creates
  files under the T3 home.
- [x] **AC5** The lookup runs only on click, never in the periodic scan; the URI launch spawns the handler without
  waiting on it, so the GTK main loop is never blocked by T3.
- [x] **AC6** If several rows map to the same Claude session id, the most recently updated
  (`updated_at`) thread wins.
- [x] **AC7** Shared detection symbols (`tests/test_core_parity.py`) stay untouched: parity test green.

## Phases

### Phase 1 — Tag T3 sessions + resolve and open the thread
- Work: in `scan_local_sessions`, add `t3_home` (None for non-T3) and `session_id` to the row; add pure
  helpers `t3_thread_id(t3_home, session_id)` and `t3_deeplink(t3_home, session_id)`; in
  `focus_terminal`, after the existing raise, open the deep link when available. Tests with a temp
  SQLite fixture mimicking the T3 table.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6, AC7
- **Data model impact**: none (reads a third-party DB; no schema of ours)
- **DoD**: `uv run pytest` green (new `tests/test_t3.py` + parity); `pre-commit run --all-files` green;
  manual check — clicking each of two live T3 sessions switches T3 to the matching thread.

## Data model impact (summary)

None. Reads T3's `statev2.sqlite` read-only; adds two keys to the in-memory session row.

## Open questions

- (none)

## Decisions

- GTK only, TUI later — user choice.
- Session id taken from the Claude session registry already resolved by `get_session_state` (equals
  `--session-id` in argv, checked live) — no extra argv parsing, `scan_proc` (shared) untouched.
- Lookup via `json_extract(payload_json, '$.nativeThreadRef.nativeId')` with a short busy timeout
  (~200 ms) — T3 writes the DB in WAL mode; a locked read degrades to AC3 rather than freezing the UI.
- Deep link via `Gio.AppInfo.launch_default_for_uri` rather than `xdg-open` (xdg-utils, not always
  installed) or `gio open` (extra process): GLib/Gio is already a hard dependency (PyGObject), and it
  resolves the same `x-scheme-handler/t3code` mimeapps entry. Not the AppImage directly — its path
  varies, the scheme does not.
- Spec numbered 003: `specs/002` is cited in tests but not in this repo; avoid reusing the number.
- Undocumented T3 internals (beta): any mismatch must fall back to AC3, never break the click.
