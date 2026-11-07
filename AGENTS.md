# Development instructions for this fork

- Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before architecture, ingestion, storage, or media changes. Treat its project objective and retention requirements as authoritative; keep its implementation status and decisions current.
- Preserve implemented append-only iMessage retention. Never let source deletion, export refresh, or provider controls delete archived messages or attachment bytes.
- Treat `imessage-exporter` as an external dependency. Keep ingestion compatible with its documented TXT and copied-attachment format; review exporter version changes before changing parsing or export invocation.
- Historical reconstruction pairs exporter 5.0.1 TXT and HTML from the same immutable source snapshot. Preserve Apple GUID identity across renamed conversations and append media found in later snapshots; fail closed when pairing is incomplete. Keep each backup's staging distinct and private.
- After paired import, catalog native attachment GUIDs from the same read-only source. Retain available files under archive-controlled media, including hidden opaque payloads; keep opaque assets out of ordinary media cards. Never treat a zero-message import as proof that all attachment bytes are present.
- Treat intermediate exporter output and Providers staging as replaceable input, never permanent storage. Preserve append-only retention when modifying exporter integration.
- Keep `README.md` installation/setup guidance and `docs/ARCHITECTURE.md` integration, version, and retention guidance accurate when changing this pipeline.
- Keep active Apple Messages/iCloud separate from the local historical archive. Do not write back to Apple's database or sync archive deletions to it.
- The optional `imessage-previews` command may read Apple's local database only in read-only mode to retain URL balloon images that TXT exports omit. Keep it explicit, scoped, fail-closed on ambiguous matches, and append-only; never rely on Apple-owned preview files as permanent storage.
- Reuse msgbrowse components and make minimal, targeted changes. Avoid new services, databases, frameworks, or dependencies without demonstrated need.
- Add regression tests for ingestion, storage, attachment, or retention changes, including repeated import and source deletion. Distinguish verified behavior from assumptions and do not claim archival safety without tests and real-history validation.
- Follow `~/.codex/AGENTS.md` for environment, test, type-check, and Git workspace rules. Direct user instructions take precedence.
