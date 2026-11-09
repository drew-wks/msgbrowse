# msgbrowse

> **Fork objective:** Permanent, offline archiving of Apple iMessages and their
> attachments while Apple Messages in iCloud remains the active messaging system.
> This fork builds on [upstream msgbrowse](https://github.com/stump-wtf/msgbrowse)
> and aims to retain imported history when source messages are later deleted.
> **Append-only iMessage retention is implemented.** Synthetic tests and a
> seven-snapshot real-data GUID union, repeat import, and separate restore
> check passed. Additional Drew-labeled 2010–2014 iPhone backups have been
> merged into the configured archive. Imported
> iMessages remain in SQLite when later exports omit them, and referenced
> attachments are copied into archive-controlled storage. Complete attachment
> recovery and compatibility across exporter versions remain unverified.
> Do not delete messages from iCloud based on this validation alone.
> Read the [fork architecture and implementation status](docs/ARCHITECTURE.md)
> before using it for archival decisions.

[![CI](https://github.com/joestump/msgbrowse/actions/workflows/ci.yml/badge.svg)](https://github.com/joestump/msgbrowse/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-joestump.github.io%2Fmsgbrowse-blue)](https://joestump.github.io/msgbrowse/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Self-hosted, **local-only** browser, search engine, and AI-editorialized
> journal over your personal message archives — **Signal, Apple iMessage, and
> WhatsApp**. A calm, private reading room for everything you've ever said.

[![msgbrowse](docs-site/static/img/hero-screenshot.png)](https://joestump.github.io/msgbrowse/)

msgbrowse renders a fast local UI over on-disk exports from
[`signal-export`](https://github.com/carderne/signal-export),
[`imessage-exporter`](https://github.com/ReagentX/imessage-exporter), and
[`WhatsApp-Chat-Exporter`](https://github.com/KnugiHK/WhatsApp-Chat-Exporter),
adds keyword + semantic search, and exposes an **MCP server** so Claude can
answer natural-language questions over your history — every answer traceable to
source messages.

Nothing leaves your machine. The web UI binds loopback only, archives are
read-only, and the only internet egress is to the OpenAI-compatible LLM endpoint
**you** configure. See [`SECURITY.md`](SECURITY.md) for the threat model.

> [!WARNING]
> **Alpha software** — expect rough edges while this is under active testing.

## Install

**Homebrew — CLI** (preferred; builds from source, so no Gatekeeper quarantine):

```sh
brew tap stump-wtf/tap
brew install msgbrowse
```

**Homebrew — desktop app** (macOS 11+): the native `.app` — same embedded server
as `msgbrowse serve`, plus bundled exporters, so a fresh Mac with no Homebrew
Python can export and import offline:

```sh
brew install --cask msgbrowse-desktop
```

The formula and the cask are **different artifacts of the same project** —
installing one does not get you the other. `msgbrowse` builds the CLI from
source; `msgbrowse-desktop` downloads the prebuilt universal `.app` into
`/Applications`. Homebrew forbids a formula from installing an `.app` into
`/Applications`, which is why they ship under separate tokens rather than one.
They coexist fine. Releases are ad-hoc signed pending a
Developer ID, so the cask's `postflight` strips `com.apple.quarantine` for you —
no manual `xattr` step, and the app's embedded exporters actually run.

**`go install`** — needs Go 1.25+ and nothing else; the SQLite driver is pure Go
(FTS5 built in), so there's no C toolchain and no build tag:

```sh
go install github.com/joestump/msgbrowse/cmd/msgbrowse@latest
```

Reading live iMessage data requires **Full Disk Access** for the app executing
the exporter — a separate macOS permission from Gatekeeper that no install
method avoids. Docker and the upstream exporters are covered in the
[installation guide](https://joestump.github.io/msgbrowse/docs/getting-started/installation/).

### iMessage exporter dependency on macOS

[`imessage-exporter`](https://github.com/ReagentX/imessage-exporter) is a
separate open-source project. It reads the local Apple Messages database and,
with `-f txt -c clone`, produces TXT transcripts and copied attachments. msgbrowse then
imports those files into its own SQLite database and permanent media directory.
The **msgbrowse CLI does not install `imessage-exporter`**. Install and check the
standalone binary before creating an export ([upstream installation and CLI
reference](https://github.com/ReagentX/imessage-exporter/blob/4.2.0/imessage-exporter/README.md)):

```sh
brew install imessage-exporter
which imessage-exporter
imessage-exporter --version
```

In **System Settings → Privacy & Security → Full Disk Access**, add and enable
the application that runs the exporter. For commands in Terminal, grant Terminal
access (or grant your other terminal app access). For the desktop Providers
workflow, grant the msgbrowse desktop app access. Reopen the application after
changing the setting. See [Apple's Full Disk Access
instructions](https://support.apple.com/guide/mac-help/change-privacy-security-settings-on-mac-mchl211c911f/mac).

The parser targets the **4.2.0 TXT layout**, and the desktop build pins exporter
4.2.0. The CLI uses whichever version is installed. Exporter **5.0.1** passed
a seven-snapshot real-data GUID union, repeat import, attachment recovery, and
restore check; other versions remain unverified. Record the
output of `imessage-exporter --version` and validate a disposable export before
relying on an upgrade. There is no proven minimum supported version or
compatibility range.

## Quickstart

```sh
# point at whichever archives you have (any subset works):
msgbrowse --data-dir ./data \
  --archive-root          ~/"Managed Files/Signal-Archive" \
  --imessage-archive-root ~/"Managed Files/iMessage-Archive" \
  --whatsapp-archive-root ~/"Managed Files/WhatsApp-Archive" \
  import

msgbrowse --data-dir ./data doctor   # setup diagnostics: roots, media health, exporters
msgbrowse --data-dir ./data embed    # optional; needs an LLM endpoint
msgbrowse --data-dir ./data serve    # auto-opens http://127.0.0.1:8787
```

On this Mac, run `./msgbrowse_run` from the repository to build if sources have
changed, start the configured server, and open the browser. The launcher then
returns to the shell while msgbrowse keeps running; running it again restarts
that same server. Its log is `msgbrowse-server.log` beside the config file
(`/Users/drew_wilkins/Documents/Messages/msgbrowse-server.log` with the default
config); set `MSGBROWSE_RUN_LOG` to override it. If you set a custom
`listen_addr` or pass `--port`, set `MSGBROWSE_RUN_URL` to the matching local URL
so the launcher can check startup and restart the correct server.

Imports share one database and skip unchanged exports. iMessage imports append
newly observed messages and retain earlier rows; Signal and WhatsApp retain their
existing import behavior. iMessage attachment bytes are copied to
`<data-dir>/media/imessage` and no longer depend on the export directory once
imported. Use `imessage-import --full` to recheck attachments when an export's
text is unchanged. Browsing and keyword search work without any LLM. Prefer
clicking? **Settings → Providers** in the web UI runs export + import per source
with one click. See the
[first import guide](https://joestump.github.io/msgbrowse/docs/getting-started/first-import/).

### Exclude unwanted iMessage texts before import

The following settings opt in to ingest filters. A short code is four to six
digits in the export's sender or conversation field, optionally followed by an
exporter-added name in parentheses. Ordinary phone numbers are unaffected.
`(smsfp)` and `(smsft)` conversation suffixes are separately excluded, including
outgoing replies in those conversations. The appointment rule excludes incoming
messages containing the literal phrase `appointment reminder`
(case-insensitive), regardless of sender address. Neither rule uses a
contact-list heuristic.
The opt-out rule excludes every message in a TXT conversation when an outgoing
message consists only of `stop` or `quit`, ignoring case and surrounding
whitespace. It deliberately does not treat the exporter's mixed `orphaned`
bucket as a single conversation.

```yaml
imessage_exclude_short_codes: true
imessage_exclude_filtered_sms: true
imessage_exclude_appointment_reminders: true
imessage_exclude_email_reminders: true
imessage_exclude_opt_out_threads: true
imessage_exclude_apple_filters: true
imessage_excluded_apple_senders: ["+15555550101", "+442079460000", "12345", "alerts@example.com"] # optional exact source handles
imessage_exclusions:
  - name: named-contact-reminder
    conversation: "Example Contact"
    body_contains: "appointment reminder"
  - name: blocked-transactional-sender
    conversation: "Example Brand - 1234"
```

Each `imessage_exclusions` entry requires a unique `name` and an exact
`conversation` matching a `<ChatName>.txt` filename. With `body_contains`, only
matching messages are skipped; without it, incoming and outgoing messages in
that entire conversation are skipped. Use a named-contact rule only after
reviewing its match count. Rules apply to `imessage-import`, the combined
`import` and `sync` commands, and Providers imports.
Exact conversation rules also keep a known opt-out thread blocked if a later
export omits its `stop`/`quit` reply. This Mac's local config includes exact
blocks for the identified Verizon, Lowe's, United Airlines, AT&T, and opt-out
threads, plus verified numeric and RCS sender aliases. New aliases or renamed export files need
review and additional exact rules.

Preview counts before import, without opening or writing the SQLite archive or
copying media:

```sh
./bin/msgbrowse --config /absolute/path/to/config.yaml imessage-import --preview-exclusions
```

The preview reports counts by TXT rule, not message bodies. It does not include
Apple database filter values. Counts cover the current TXT export and may
include messages already archived. Filtering happens before
attachment copying and SQLite insertion, but the replaceable exporter staging
files still contain the original messages until separately removed. A policy
change rescans an unchanged TXT file. Adding a rule does **not** erase messages
or media already archived; deliberate cleanup must first make a backup and
record removed message identities in the SQLite exclusion ledger. This prevents
overlapping GUID-aware imports from recreating those messages. The Apple Messages
“Unknown Senders” flag is not available in TXT. With
`imessage_exclude_apple_filters: true`, import requires paired TXT and HTML
exports plus `--apple-filter-db` pointing to the **same** read-only Apple
Messages database snapshot. The importer excludes exact `chat.is_filtered`
values **1, 2, 3, 4, and 52**, verified against this owner's iPhone filter
screenshots, and records each new excluded Apple GUID in the durable ledger.
`imessage_excluded_apple_senders` also excludes new GUIDs whose source sender
handle is an exact listed phone number, short code, or email address, even when
the exporter changes the contact name. The list applies to incoming messages from those handles; it
requires the same paired Apple source database and does not delete old rows.
Other nonzero values hold new messages for review; existing archived messages
are preserved. A source without this optional Apple column cannot provide
native filter classification, so only the TXT rules apply. The flag values are
private Apple metadata and are not a general guarantee of iPhone-category
coverage. When enabled, ordinary TXT-only `imessage-import`, combined
`import`, `sync`, and Providers imports stop with an error. Use the paired
reconstruction workflow below for future iMessage imports. The local archive's
known GUIDs were reviewed against Apple's read-only `chat.db` on 2026-11-07;
see [validation results](docs/ARCHITECTURE.md#owner-directed-exclusion-cleanup-2026-11-07).

The owner-directed transactional cleanup command is
[`scripts/transactional_cleanup.py`](scripts/transactional_cleanup.py). It scans
the current SQLite archive locally, protects substantive two-way and group
conversations, an owner-reviewed named-sender keep list, and person-shaped
sender labels. It writes a private identity-only plan. Applying a plan requires
a new SQLite-only checkpoint and
records exact source hashes plus Apple GUID/TXT fingerprints before deleting
rows. Schema v29 rejects reinsertions of those exact identities from any
importer. This is a conservative sweep of the current archive, not an automatic
classifier for future unseen messages; ambiguous one-way texts require review.

### Reassign messages from the exporter’s orphaned bucket

The exporter puts a message in `orphaned.txt` when its source database has no
chat link for that message. A sender name can still be present. A later Apple
database may provide a unique chat link for the same message GUID, while the
append-only archive still holds the message under its first imported label.
The metadata-only recovery script audits those GUIDs against one or more
read-only Apple `chat.db` snapshots, then reassigns only messages with one
nonconflicting source chat and archive destination. It does
not read message text, delete messages, or modify Apple's database:

```sh
DATA_DIR="$HOME/msgbrowse-data"
python3 scripts/imessage_reassign_orphaned.py \
  --source-db "$HOME/Library/Messages/chat.db" \
  --archive-db "$DATA_DIR/msgbrowse.sqlite"
```

Review the aggregate dry-run count and stop the local server before
applying. A metadata-only move needs a consistent SQLite checkpoint, not a
new copy of permanent media. Verify that checkpoint and pass the reviewed
move count to guard against a source change:

```sh
DATA_DIR="$HOME/msgbrowse-data"
CHECKPOINT="$DATA_DIR/orphan-pre-$(date +%Y%m%d-%H%M%S).sqlite"
sqlite3 "$DATA_DIR/msgbrowse.sqlite" ".backup '$CHECKPOINT'"
sqlite3 -readonly "$CHECKPOINT" "PRAGMA integrity_check"
python3 scripts/imessage_reassign_orphaned.py \
  --source-db "$HOME/Library/Messages/chat.db" \
  --archive-db "$DATA_DIR/msgbrowse.sqlite" \
  --apply --expect-moves COUNT_FROM_DRY_RUN
```

The script keeps message IDs, GUIDs, attachment paths, links, and reactions;
it updates their conversation references in one checked SQLite transaction.
When no existing archive conversation has unambiguous GUID evidence, it uses
Apple's unique chat identifier for a separate recovered conversation rather
than guessing from a sender name. Unlinked messages stay in `orphaned` for
later review. Paired GUID imports are the reliable way to identify overlap
across historical exports; TXT-only exports cannot always resolve the same
message after a conversation reassignment.

### Build and create a local iMessage archive

Run this example from this repository on a Mac with Messages history. The export
directory is **replaceable input**; the separate data directory holds the
permanent SQLite archive and retained attachments. Choose locations with enough
free space, outside Apple's `~/Library/Messages` directory:

```sh
make build
EXPORT_DIR="$HOME/msgbrowse-imessage-export"
DATA_DIR="$HOME/msgbrowse-data"
mkdir -p "$EXPORT_DIR" "$DATA_DIR"
imessage-exporter -f txt -c clone -o "$EXPORT_DIR"
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" imessage-import
./bin/msgbrowse --data-dir "$DATA_DIR" imessage-previews --all --dry-run
./bin/msgbrowse --data-dir "$DATA_DIR" imessage-previews --all
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" doctor
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" serve
```

The exporter command uses its [documented `-f txt`, `-c clone`, and `-o`
options](https://github.com/ReagentX/imessage-exporter/blob/4.2.0/imessage-exporter/README.md):
`clone` copies attachments without converting them.
`imessage-import` also creates cached JPEG previews for HEIC/HEIF/TIFF images
when a local converter such as macOS `sips` is available. If the archive was
imported before conversion ran, or a restored archive has no preview cache,
run `./bin/msgbrowse --data-dir "$DATA_DIR" media` and refresh the browser.
The original image stays in permanent media storage; JPEG previews are
regenerable files under `<data-dir>/derived`. After import, open the local
interface, compare expected conversations and message counts, search for
known messages, and open representative attachments. Run
`imessage-exporter --diagnostics` to check the local source for missing files,
and resolve import errors before treating the archive as complete. `doctor`
samples media health; it does not prove every historical file is present.
Exporter 5.0.1's TXT output omits local URL balloon thumbnail files. The optional
`imessage-previews` step reads Apple's local Messages database and attachment
files **read-only**, then copies images for uniquely matched, already-imported
links into permanent media storage. It requires Full Disk Access for the app
running msgbrowse. Use `--participant PHONE_OR_EMAIL --start-date YYYY-MM-DD
--end-date YYYY-MM-DD --timezone America/Toronto` to limit a scan; the end date
is exclusive. `--source-db` and `--attachment-root` can point to a copied source.
Run it while those source files are still available. It reports unmatched,
ambiguous, missing, or unsupported assets and exits unsuccessfully if any occur.
It never downloads images from the internet. The transcript shows retained
thumbnails when available and omits long URL text; the link and copy button
still retain the complete URL. Other apps' link metadata may not be covered.
Create a [backup](docs/ARCHITECTURE.md#data-model-and-storage) with
`./bin/msgbrowse --data-dir "$DATA_DIR" backups create`; verify a restore on a
separate copy before considering any deletion from Apple Messages or iCloud.

### Update the local archive from this Mac

Open msgbrowse and click **Update database** on Home (also available in
**Settings → Providers → iMessage**). The manual update reads the Messages
database belonging to the Mac user running msgbrowse; it does not connect to
iCloud directly or pull from other devices. Messages must first sync or download
to this Mac, and the app or terminal running msgbrowse needs Full Disk Access.
The configured `imessage-exporter` binary must be available through its explicit
path or `PATH`.

Each click makes a consistent, read-only copy of that Mac's `chat.db`, exports
from the earlier of one year ago or 30 days before the latest archived Apple
message through today as paired TXT and HTML, and checks Apple's
filter metadata from the same copy. It then appends new message GUIDs, copies
available attachments and URL preview images into permanent storage, and
discards the temporary export. Rechecking the overlap lets late-downloaded
attachments be retained; previously imported messages are not duplicated or
deleted. The exporter's mixed `orphaned` transcript is held out of automatic
updates because it combines unrelated senders. The button reports new-message
and attachment-link counts when it finishes. Missing or ambiguous preview
files remain gaps; the update cannot obtain bytes absent from the Mac.

The update does **not** create an archive backup. Back up the SQLite database
and permanent media separately before relying on this as your only copy. On a
different Mac, the button reads that Mac's local Messages database and adds
only what its source supplies; it cannot reach another device's local-only data.

### Reconstruct history from overlapping backups

For multiple iPhone backups or copied Mac databases, use the GUID-aware
workflow. `imessage-exporter` 5.0.1 produces **both** TXT and HTML exports from
the *same database snapshot*: TXT supplies text and cloned attachments; HTML
supplies each Apple message GUID. msgbrowse unions messages by GUID, so a
message found in several backups is one archived message. Each pass can add
attachment bytes that were missing from an earlier snapshot. Source order is
not important; keep each backup's staging directory separate. The original
backups and Apple Messages database are read only.

On this Mac, the seven-source reconstruction and subsequent Drew-labeled
Seagate sources are in the configured archive at
`/Users/drew_wilkins/Documents/Messages/archive`. The browser launcher reads
`/Users/drew_wilkins/Documents/Messages/config.yaml`, and new
archive backups go to `/Users/drew_wilkins/Documents/Messages/archive-backups`.
For the next mounted source, use a new label, keep the existing reconstruction
workspace for staging, and set `DATA_DIR` to the configured archive path. The
former `/Users/drew_wilkins/Documents/Messages/data` remains a rollback copy.
The configured archive currently has 72,870 messages after overlapping
historical imports and owner-directed cleanup. Import-only
`legacy_sms_review` and `legacy_mms_review` conversations keep recovered old
material visible without assigning uncertain Apple GUIDs, contacts, or
directions. The synthetic `iChat review (unassigned)` conversation holds 118
additional Apple GUIDs without asserting a historical chat. Two embedded
images are retained permanently; other `.ichat` attachment markers still need
byte recovery. Another 119 five- or six-digit sender GUIDs are recorded in the
exclusion ledger. Of 14,628 cataloged iMessage attachment identifiers, 884 still
lack bytes in the sources examined. A current full media backup and independent
restore are still due; do not delete Apple/iCloud copies on the basis of this
coverage.

```sh
WORKSPACE="$HOME/Documents/Messages/reconstruction/2026-11-06-backups"
DATA_DIR="$HOME/Documents/Messages/archive"
CONFIG="$HOME/Documents/Messages/config.yaml"
./scripts/imessage-reconstruct iOS \
  "/absolute/path/to/one/MobileSync/Backup/DEVICE_ID" \
  "iphone-2024-07-21" "$WORKSPACE" "$DATA_DIR" "$CONFIG"
# Repeat with a different label and path for every distinct backup snapshot.
```

The script checks for exporter 5.0.1, builds msgbrowse, creates separate TXT
and HTML staging, imports with `--guid-html-root` and `--apple-filter-db`, then
reads the same source
database and backup manifest in read-only mode to retain native attachment
files omitted by TXT. It keeps private logs under `$WORKSPACE/reports`. A label
is tied to the backup manifest checksum; an
incomplete staging directory stops for review rather than being reused. For a
Mac snapshot, pass `macOS`, the path to a consistent **copy** of `chat.db`, and
the original Messages attachment root as the seventh argument. Do not point
the script at a live changing database: sequential TXT and HTML runs could see
different messages. The ordinary `msgbrowse export` and Providers flows still
produce TXT alone; use this paired workflow when the Apple filter guard is
enabled and for overlapping historical
snapshots. `--guid-html-root` is validated against exporter 5.0.1, not an
unbounded exporter compatibility range. The current native iPhone recovery path
requires an **unencrypted** MobileSync backup; the script rejects an encrypted
one before exporting. A copied Mac database can use an attachment root copied
from another Mac.

After owner-directed archive deletion, use paired TXT/HTML imports for any
overlapping historical snapshot. The deletion ledger blocks matching Apple
GUIDs across renamed exports. For the 2026-11-07 owner-reviewed orphan cleanup,
a TXT-only import also blocks the same archived hash or a matching
message-second-and-body fingerprint, even if the conversation or sender
display label changes. Changed message times or text can evade this fallback,
so paired GUIDs are required for a full identity guarantee.

The script requires the newer `Manifest.db` backup layout. Older flat backups
with `Manifest.mbdb` or `.mddata` files need a separately validated conversion; see the
[Seagate historical import](docs/ARCHITECTURE.md#seagate-historical-import-2026-11-07-local-time).

If a network-mounted iPhone backup cannot be opened by the exporter, make a
local copy of its manifest, `sms.db`, contacts database, and the Messages
attachment files named by `Manifest.db`, then point the script at that copy.
Keep the copied manifest and file set from the same backup snapshot. Missing
files remain recorded as gaps; they do not prevent later backups from filling
them. The four-source isolated validation used this approach for one network
backup.

The archive keeps the first imported text for a GUID and appends later media,
links, and reactions. Visible native attachments become ordinary media;
hidden plugin payloads are retained as raw assets without appearing in the
gallery. The native attachment catalog reports distinct identifiers still
missing bytes across *all backups ingested so far*. A changed body under the
same GUID is not yet exposed as a separate revision in the browser. Files
missing from every source remain unrecoverable by this workflow; compare the
catalog and archive media before deleting anything from Apple Messages or iCloud.

If `imessage_exclude_apple_filters` is disabled, msgbrowse can invoke the
exporter with only the iMessage root configured:

```sh
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" export
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" imessage-import
```

`export` looks for
`imessage-exporter` on `PATH`; set `imessage_exporter_bin` in `config.yaml` or
pass `--imessage-exporter-bin /absolute/path/to/imessage-exporter` after
`export` to use a specific binary. It invokes `-f txt -c clone -o "$EXPORT_DIR"`
and preserves legacy attachment references before replacing export files.

Alternatively, start `serve`, open **Settings → Providers**, and Enable iMessage
or click **Update database**. The CLI server finds the exporter on `PATH` or through
`imessage_exporter_bin`; the desktop app uses its bundled exporter. Providers
exports to app-managed staging, adopts it at `<data-dir>/archives/imessage` on
success, then imports it. The Providers export directory is also replaceable;
permanent attachments live under `<data-dir>/media/imessage`. Providers cannot
Disable the permanent iMessage archive.

## Commands

| Command | What it does |
| --- | --- |
| `msgbrowse sync` | One-command pipeline: export → import → media → embed → facts. |
| `msgbrowse export` | Run the upstream exporters into the configured roots. |
| `msgbrowse import` | Import every configured archive into one DB (also `signal-import`, `imessage-import`, `whatsapp-import`). |
| `msgbrowse doctor` | Read-only setup diagnostics; hints name the exact fix. `--check-llm` probes the endpoint. |
| `msgbrowse embed` | Compute embeddings for semantic search. |
| `msgbrowse facts` | Extract cited AI facts about each contact. |
| `msgbrowse media` | Transcode HEIC/TIFF to cached JPEGs for the gallery. |
| `msgbrowse journal` | Build the per-day journal rollup + optional LLM digest. |
| `msgbrowse spam` | Build an evidence record of unsolicited contact and export per-sender dossiers. Local, deterministic, **no network egress**. |
| `msgbrowse serve` | Run the local HTMX web UI (default `127.0.0.1:8787`). |
| `msgbrowse mcp` | Run the MCP server (stdio; `--http` for streamable HTTP). |
| `msgbrowse devices` | Manage LAN-only device-sync peers: `list`, `status`, `unpair`. |
| `msgbrowse version` | Print version. |

Full flags for every command: [CLI reference](https://joestump.github.io/msgbrowse/docs/reference/cli/).

## Configuration

Resolved low→high: defaults < `config.yaml` < `MSGBROWSE_*` env < flags. See
[`config.example.yaml`](config.example.yaml) and the
[full reference](https://joestump.github.io/msgbrowse/docs/reference/configuration/).

| Env | Default | Notes |
| --- | --- | --- |
| `MSGBROWSE_ARCHIVE_ROOT` | — | read-only signal-export archive |
| `MSGBROWSE_IMESSAGE_ARCHIVE_ROOT` | — | read-only imessage-exporter archive |
| `MSGBROWSE_WHATSAPP_ARCHIVE_ROOT` | — | read-only WhatsApp-Chat-Exporter output |
| `MSGBROWSE_DATA_DIR` | `./data` | writable DB/embeddings dir |
| `MSGBROWSE_LISTEN_ADDR` | `127.0.0.1:8787` | loopback by default |
| `MSGBROWSE_LLM_BASE_URL` | `http://127.0.0.1:4000/v1` | the only internet egress |
| `MSGBROWSE_LLM_API_KEY` | — | env wins; never commit |
| `MSGBROWSE_LLM_CHAT_MODEL` | `local-chat` | RAG + digests |
| `MSGBROWSE_LLM_EMBED_MODEL` | `local-embed` | embeddings |
| `MSGBROWSE_DEVICE_SYNC_ENABLED` | `false` | opt-in LAN-only device sync |
| `MSGBROWSE_LOG_LEVEL` | `info` | debug/info/warn/error |

## Connecting Claude (MCP)

Run `msgbrowse embed` first so semantic search has vectors, then add:

```json
{
  "mcpServers": {
    "msgbrowse": {
      "command": "msgbrowse",
      "args": ["--data-dir", "/absolute/path/to/data", "mcp"]
    }
  }
}
```

Use the absolute binary path if it isn't on the client's `PATH`. Tools, prompts,
and the Docker variant: [MCP server docs](https://joestump.github.io/msgbrowse/docs/features/mcp-server/).

## Documentation

Everything lives at
**[joestump.github.io/msgbrowse](https://joestump.github.io/msgbrowse/)**:

- [What is msgbrowse](https://joestump.github.io/msgbrowse/docs/getting-started/what-is-msgbrowse/) · [Installation](https://joestump.github.io/msgbrowse/docs/getting-started/installation/) · [Exporting your archives](https://joestump.github.io/msgbrowse/docs/getting-started/exporting-your-archives/) · [First import](https://joestump.github.io/msgbrowse/docs/getting-started/first-import/)
- [Browsing](https://joestump.github.io/msgbrowse/docs/features/browsing/) · [Search](https://joestump.github.io/msgbrowse/docs/features/search/) · [Media gallery](https://joestump.github.io/msgbrowse/docs/features/media-gallery/) · [AI features](https://joestump.github.io/msgbrowse/docs/features/ai-features/) · [MCP server](https://joestump.github.io/msgbrowse/docs/features/mcp-server/) · [Device sync](https://joestump.github.io/msgbrowse/docs/features/device-sync/) · [Status & backups](https://joestump.github.io/msgbrowse/docs/features/status-and-backups/)
- [CLI](https://joestump.github.io/msgbrowse/docs/reference/cli/) · [Configuration](https://joestump.github.io/msgbrowse/docs/reference/configuration/) · [Security model](https://joestump.github.io/msgbrowse/docs/reference/security-model/) · [Troubleshooting](https://joestump.github.io/msgbrowse/docs/reference/troubleshooting/)
- [Architecture](https://joestump.github.io/msgbrowse/architecture) — ADRs in [`docs/adr/`](docs/adr/), specs in [`docs/openspec/specs/`](docs/openspec/specs/)

## Development

### Build and run this checkout on macOS, including Apple Silicon

The core CLI is pure Go. Install the toolchain specified by `go.mod`
(`go 1.25.8`, with `toolchain go1.27.0`), then run from this repository:

```sh
make build
./bin/msgbrowse --data-dir ./data --imessage-archive-root /absolute/path/to/imessage-export doctor
./bin/msgbrowse --data-dir ./data --imessage-archive-root /absolute/path/to/imessage-export imessage-import
./bin/msgbrowse --data-dir ./data --imessage-archive-root /absolute/path/to/imessage-export serve
```

For the complete export-to-archive example, permission steps, and exporter
version guidance, see [Build and create a local iMessage
archive](#build-and-create-a-local-imessage-archive). The iMessage root must
contain top-level `<ChatName>.txt` files and the referenced copied
`attachments/` tree. The CLI needs the exporter to create fresh exports;
browsing an already populated local DB and intact media tree works offline.
`./data/msgbrowse.sqlite` is the default database path. The desktop shell has
separate Wails/cgo build requirements.

The [fork architecture](docs/ARCHITECTURE.md) explains the storage layout,
identity limits, synthetic test coverage, and remaining real-history validation.

See the
[local development guide](https://joestump.github.io/msgbrowse/docs/development/local-development/)
and the
[signing & notarization runbook](https://joestump.github.io/msgbrowse/docs/development/release-signing/).

```sh
make build   # build ./bin/msgbrowse
make test    # run the test suite
make check   # gofmt + go vet + tests (the CI gate)
make css     # rebuild internal/web/static/app.css (Tailwind + daisyUI)
```

The built `app.css` is committed and `go:embed`-served, so the runtime needs no
Node toolchain — CI fails if it's stale. Contributions should keep `make check`
green and add tests for new ingest/search/MCP behavior.

## License

[MIT](LICENSE) © Joe Stump
