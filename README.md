# Calendar Booking

[![Calendar Booking](media/thumbnail.webp)](media/cover.png)

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

Find shared availability in **Google Calendar**, prepare a local booking draft and create a one-off meeting only when its exact draft is approved. Searching and previewing do not invite anyone. This is an independent Google Calendar adapter, not a universal CalDAV connector.

## Setup

Requirements: Linux, Python 3.10+, installed IANA time-zone data and `httpx>=0.28,<1` in the runtime interpreter. Google Calendar API access and an existing OAuth grant are required. The block does not register an OAuth app, run an authorization callback or install dependencies.

Put one of these JSON objects in an unlocked wallet secret, then configure only its reference, for example `secret://workspace/google_calendar`:

```json
{"client_id":"YOUR_CLIENT_ID","client_secret":"YOUR_CLIENT_SECRET","refresh_token":"YOUR_REFRESH_TOKEN"}
```

Alternatively, use `{"access_token":"YOUR_ACCESS_TOKEN"}` for a short-lived bearer grant. These are placeholders, not real credentials. The refresh-token form obtains an access token for each remote operation. Neither form is persisted in the block journal. DPoP-bound grants, service-account key files and environment-variable credentials are not supported.

The grant must cover availability reads and event creation/readback, for example `https://www.googleapis.com/auth/calendar.events.freebusy` plus `https://www.googleapis.com/auth/calendar.events`, with account permissions on each selected calendar. Use an explicit booking calendar ID, not `primary` or a calendar web URL. See Google's [free/busy scopes](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query), [event permissions](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert) and [refresh-token flow](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

Configure an allowlist of participants:

```json
[
  {"id":"alice","email":"alice@example.com","calendar_id":"alice@example.com"}
]
```

Each participant binds an input ID to an invitation address and an availability calendar. Confirm that these bindings represent the intended people. Group expansion is refused. An empty list supports bookings without guests. Availability is always checked on the booking calendar as well.

Remote access and booking both default **off**. Enable calendar access for searches; enable approved bookings separately to permit creation. Run preparation and Play alone never call Google. The same explicit inputs perform real remote operations in One Shot and Active Runtime; One Shot is not a dry run.

## Ports and workflow

| Port | Direction | Contents |
| --- | --- | --- |
| `request` | Input | JSON search, select/draft, local status or read-only reconciliation |
| `decision` | Input | Exact draft approval or rejection |
| `slots` | Output | Proposed UTC/local-offset slots; not reservations |
| `draft` | Output | Frozen event content, selected slot, expiry and approval identity |
| `receipt` | Output | Correlated state; an actual `event_id` when provider state has been obtained |

Send one operation per activation and wait for its response before the next. Ports are matched by their stable IDs/names, not visual order. A decision never borrows a stale value from `request`.

### 1. Search

Send to `request`, substituting future dates within the configured horizon:

```json
{
  "request_id":"meeting-001",
  "operation":"search",
  "participants":["alice"],
  "period":{"start":"2026-10-12T09:00:00+02:00","end":"2026-10-12T17:00:00+02:00"},
  "duration_minutes":30,
  "summary":"Project review",
  "description":"Review the agreed milestones.",
  "location":"Meeting room"
}
```

The response contains `options`, each with a `slot_id`, UTC `start`/`end`, local times with explicit offsets, duration and IANA time zone. Only configured participant IDs are allowed. Missing, denied or malformed calendar availability is an error, never treated as free time.

### 2. Select the exact slot

Send to `request` with a returned slot ID:

```json
{"request_id":"meeting-001","operation":"draft","slot_id":"RETURNED_SLOT_ID"}
```

The `draft` includes its `draft_id`, `revision`, slot, summary, plain-text description, location, attendees, calendar ID and `expires_at`. Selecting a different slot produces a new draft identity and invalidates the earlier approval. Repeating the same selection returns the same draft. Preview creates no remote event.

### 3. Approve or reject

After reviewing the draft, send its unmodified identity to `decision`:

```json
{"request_id":"meeting-001","draft_id":"RETURNED_DRAFT_ID","revision":"RETURNED_REVISION","decision":"approve"}
```

Use `reject` to close the draft without contacting Google. Creation requires the original configuration scope, an unexpired draft, the minimum advance notice and a fresh free/busy check. Successful creation requests invitation updates for all guests. `confirmed` means the organizer's event exists with the approved content; **it does not mean guests have accepted or received notifications**. The block does not create recurring meetings, video conferences or reminders, and does not reschedule/delete events.

The decision port is a trusted workflow control signal, not proof of a human identity or a built-in approval dialog. Wire it only to an authorized decision source. Do not connect an unreviewed model output directly to it if human approval is required.

### Inspect an outcome

```json
{"request_id":"meeting-001","operation":"status"}
```

`status` reads the local journal without credentials or network access. To resolve a prior creation attempt against Google, send `{"request_id":"meeting-001","operation":"reconcile"}`. Reconciliation performs only an event GET; it never creates, changes or deletes a meeting. Even a 404 is not permission to insert again.

## Duplicate and failure handling

Before inserting an event, the block durably reserves a single attempt for that request ID in its node-private SQLite journal. Repeated approvals, a restarted Run or a lost HTTP response cannot cause this node to automatically send another creation request for that ID. After Stop during submission, the receipt remains `uncertain` until readback can establish the outcome. A confirmed removal stays terminal and is not recreated.

Other useful receipt states are `proposed`, `no_slots`, `draft`, `expired`, `rejected`, `conflict`, `provider_rejected`, `external_change`, `interrupted`, `search_failed` and `error`. `duplicate` reports a replay. `attempted_event_id` identifies a reserved attempt, **not proof of creation**. Never automatically change the request ID to bypass an uncertain outcome. Reusing an ID with different search content is refused.

These are per-node safeguards, not global exactly-once delivery. Different nodes/request IDs can request the same meeting. Google's free/busy check and event insertion are separate operations: another booking client can race between them. Local overlapping confirmed or uncertain attempts remain reserved conservatively until reconciled; the block cannot lock external calendars. Source-to-calendar permissions and the meaning of an approval remain deployment responsibilities.

## Scheduling, storage and UI

Working hours use `mon` through `sun`, each with at most four non-overlapping `["HH:MM","HH:MM"]` intervals. Missing days are closed. `24:00` is an end only; split overnight periods. The default is 09:00–17:00 Monday–Friday in `Europe/Paris`. Durations are elapsed minutes, not wall-clock subtraction; missing and repeated daylight-saving times are handled with explicit offsets.

The main form contains access switches, calendar ID, wallet reference, zone and participants. Scheduling rules and quotas are grouped under an expandable section. The modal and inspector use Apply; Cancel discards unsaved modal edits. Changing settings requires preparing a new Run and may invalidate pending drafts.

`context.services["get_block_storage_dir"]()` owns `calendar.sqlite3` and `calendar.lock`. Records survive Runs and contain meeting content, attendee addresses and wallet references, but never OAuth credentials. Protect this private directory; SQLite content is not encrypted by the block. No automatic pruning, reset or migration discards duplicate-protection history. The request-count and database-size limits refuse new work when full. Deleting/restoring this journal changes the duplicate-protection guarantee.

Only fixed Google HTTPS endpoints are used, with certificate validation, no redirects, no environment proxies, bounded response size and a total operation deadline. No automatic network retries occur. Stop cancels owned HTTP work. Errors return a receipt without intentionally terminating the whole workflow.

## Tests and qualification

The owned suites use a loopback Google-protocol fixture and disposable wallets: no real account, calendar or invitation. They exercise DST/buffers, exact approval, scope changes, expired drafts, conflicts, denied access, response loss, cancellation, storage quotas and read-only reconciliation. Integration tests cover both runtimes and bundled/managed/linked installations, reversed port order, persistence and actual Stop while the provider response is pending. Browser tests cover English/French modal and inspector, Apply/Cancel, JSON replacement and 320–1440 px layouts.

These checks qualify the block integration, not live Google account policy or actual notification delivery. The proprietary framework test harness is not distributed in this repository.

## License

Block code: Apache-2.0. Google Calendar is an external service governed by its own terms. No framework code, account secrets or provider SDK is bundled.
