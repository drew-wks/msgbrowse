# Development instructions for this fork

- Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) before architecture, ingestion, storage, or media changes. Treat its project objective and retention requirements as authoritative; keep its implementation status and decisions current.
- Preserve implemented append-only iMessage retention. Never let source deletion, export refresh, or provider controls delete archived messages or attachment bytes.
- Keep active Apple Messages/iCloud separate from the local historical archive. Do not write back to Apple's database or sync archive deletions to it.
- Reuse msgbrowse components and make minimal, targeted changes. Avoid new services, databases, frameworks, or dependencies without demonstrated need.
- Add regression tests for ingestion, storage, attachment, or retention changes, including repeated import and source deletion. Distinguish verified behavior from assumptions and do not claim archival safety without tests and real-history validation.
- Follow `~/.codex/AGENTS.md` for environment, test, type-check, and Git workspace rules. Direct user instructions take precedence.
