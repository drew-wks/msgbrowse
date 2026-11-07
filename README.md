# msgbrowse

> **Fork objective:** Permanent, offline archiving of Apple iMessages and their
> attachments while Apple Messages in iCloud remains the active messaging system.
> This fork builds on [upstream msgbrowse](https://github.com/stump-wtf/msgbrowse)
> and aims to retain imported history when source messages are later deleted.
> **Append-only iMessage retention is implemented.** Synthetic tests and a
> limited real-data export, repeat import, and restore check passed. Imported
> iMessages remain in SQLite when later exports omit them, and referenced
> attachments are copied into archive-controlled storage. Full-history
> completeness and compatibility across exporter versions remain unverified.
> Do not delete messages from iCloud based on the limited sample alone.
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
a limited real-data import, independent repeat export, and restore check;
full-history and cross-version compatibility remain unverified. Record the
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

The following settings in your local, uncommitted `config.yaml` opt in to two
ingest filters. A short code is **exactly five or six digits** in the export's
sender or conversation field; ordinary phone numbers are unaffected. The email
rule matches an incoming message when its sender or conversation is an email
address and its body contains the literal phrase `appointment reminder`
(case-insensitive). Neither rule uses a contact-list heuristic.

```yaml
imessage_exclude_short_codes: true
imessage_exclude_email_reminders: true
imessage_exclusions:
  - name: named-contact-reminder
    conversation: "Example Contact"
    body_contains: "appointment reminder"
```

Each `imessage_exclusions` entry requires a unique `name` and an exact
`conversation` matching a `<ChatName>.txt` filename. With `body_contains`, only
matching messages are skipped; without it, incoming and outgoing messages in
that entire conversation are skipped. Use a named-contact rule only after
reviewing its match count. Rules apply to `imessage-import`, the combined
`import` and `sync` commands, and Providers imports.

Preview counts before import, without opening or writing the SQLite archive or
copying media:

```sh
./bin/msgbrowse --config /absolute/path/to/config.yaml imessage-import --preview-exclusions
```

The preview reports counts by rule, not message bodies. Counts cover the current
TXT export and may include messages already archived. Filtering happens before
attachment copying and SQLite insertion, but the replaceable exporter staging
files still contain the original messages until separately removed. A policy
change rescans an unchanged TXT file. Adding a rule does **not** erase messages
or media already archived; this is an append-only archive. The Apple Messages
“Unknown Senders” flag is not available in this TXT import, so it is not an
automatic exclusion. Add exact conversation rules for such threads after
reviewing the export names. If the exporter resolves an address to a contact
name, the automatic address rules may not recognize it; the preview counts
make that limitation visible only for matched messages.

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

You can also let msgbrowse invoke the same exporter. With only the iMessage root
configured, run:

```sh
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" export
./bin/msgbrowse --data-dir "$DATA_DIR" --imessage-archive-root "$EXPORT_DIR" imessage-import
```

`export` looks for
`imessage-exporter` on `PATH`; set `imessage_exporter_bin` in `config.yaml` or
pass `--imessage-exporter-bin /absolute/path/to/imessage-exporter` after
`export` to use a specific binary. It invokes `-f txt -c clone -o "$EXPORT_DIR"`
and preserves legacy attachment references before replacing export files.

Alternatively, start `serve`, open **Settings → Providers**, and Enable or
Refresh iMessage. The CLI server finds the exporter on `PATH` or through
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
