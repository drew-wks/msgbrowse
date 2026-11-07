package imessage

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

// ErrArchiveNotFound is returned when the iMessage archive directory is missing.
// It is a sentinel so the CLI can attach an actionable hint.
var ErrArchiveNotFound = errors.New("imessage archive directory not found")

// Options configures an iMessage import run.
type Options struct {
	// ArchiveRoot is the imessage-exporter output directory: a flat set of
	// <ChatName>.txt files plus an attachments/ folder. Read-only.
	ArchiveRoot string
	// Exclusions are applied before attachment retention and SQLite insertion.
	Exclusions ExclusionPolicy
	// Full forces every conversation to be re-parsed, ignoring incremental state.
	Full bool
	// Now supplies the current time; defaults to time.Now.
	Now func() time.Time
	// Logger receives progress; defaults to slog.Default().
	Logger *slog.Logger
}

// Run imports the iMessage archive into st and returns the recorded summary.
// Changed conversations append new records. Existing messages and media remain.
func Run(ctx context.Context, st *store.Store, opts Options) (store.IngestRun, error) {
	if err := ValidateExclusions(opts.Exclusions); err != nil {
		return store.IngestRun{}, err
	}
	now := time.Now
	if opts.Now != nil {
		now = opts.Now
	}
	log := opts.Logger
	if log == nil {
		log = slog.Default()
	}
	start := now()
	run := store.IngestRun{Source: source.IMessage, StartedAt: start}
	log.Info("importing imessage archive", "archive", opts.ArchiveRoot, "full", opts.Full)

	entries, err := os.ReadDir(opts.ArchiveRoot)
	if err != nil {
		if os.IsNotExist(err) {
			return run, fmt.Errorf("%w at %s", ErrArchiveNotFound, opts.ArchiveRoot)
		}
		return run, fmt.Errorf("read imessage archive: %w", err)
	}
	if err := PreserveExistingAttachments(ctx, st, opts.ArchiveRoot); err != nil {
		return run, err
	}

	// Diagnose the common "0 imported" case up front: the importer only reads
	// top-level <ChatName>.txt files (imessage-exporter `-f txt` output). If there
	// are none, say so loudly with what we DID see, so a nested or wrong-format
	// archive doesn't silently look like a successful no-op.
	var txtCount, dirCount int
	for _, e := range entries {
		switch {
		case e.IsDir():
			dirCount++
		case strings.HasSuffix(e.Name(), ".txt"):
			txtCount++
		}
	}
	if txtCount == 0 {
		log.Warn("no <ChatName>.txt files found at the top level of the imessage archive — "+
			"point --imessage-archive-root at the directory imessage-exporter wrote with `-f txt` "+
			"(flat .txt files; nested subfolders are not scanned)",
			"archive", opts.ArchiveRoot, "entries", len(entries), "subdirs", dirCount)
	}

	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".txt") {
			continue
		}
		name := strings.TrimSuffix(e.Name(), ".txt")
		path := filepath.Join(opts.ArchiveRoot, e.Name())
		info, statErr := os.Stat(path)
		if statErr != nil {
			continue
		}
		run.ConversationsScanned++

		changed, added, skipped, cerr := importConversation(ctx, st, opts, log, name, path, info, now())
		if cerr != nil {
			log.Error("imessage conversation import failed", "conversation", name, "error", cerr)
			run.Errors++
			continue
		}
		run.SkippedLines += skipped
		if changed {
			run.ConversationsChanged++
			run.MessagesAdded += added
			log.Info("imported conversation", "conversation", name, "messages", added, "skipped_lines", skipped)
		}
	}

	total, err := st.CountMessages(ctx)
	if err != nil {
		return run, err
	}
	run.MessagesTotal = total

	run.FinishedAt = now()
	run.DurationMS = run.FinishedAt.Sub(run.StartedAt).Milliseconds()
	if _, err := st.RecordIngestRun(ctx, run); err != nil {
		return run, err
	}
	// Fold re-imported identities back onto their merged person (ADR-0024 /
	// SPEC-0018). Idempotent, local-only; nil resolver = no address book.
	// Best-effort: the import is committed and hash-idempotent, and reconcile
	// re-runs next import, so a failure is logged rather than failing the import.
	// Heal rows written under the old name-as-identifier rule before matching
	// runs, so an existing archive fixes itself on its next import rather than
	// needing a reimport (issue #363). Best-effort for the same reason
	// reconcile is: the import is already committed and idempotent.
	if rep, rerr := st.RepairContactIdentities(ctx); rerr != nil {
		log.Error("contact identity repair failed (import committed; will retry next run)", "error", rerr)
		run.Errors++
	} else if rep.Changed() {
		log.Info("repaired contact identities",
			"groups_marked", rep.GroupsMarked,
			"identifiers_rewritten", rep.IdentifiersRewritten,
			"contacts_orphaned", rep.ContactsOrphaned)
	}
	if err := st.ReconcileContacts(ctx, nil); err != nil {
		log.Error("contact reconcile failed (import committed; will retry next run)", "error", err)
		run.Errors++
	}
	log.Info("imessage import complete",
		"scanned", run.ConversationsScanned, "changed", run.ConversationsChanged,
		"messages_added", run.MessagesAdded, "skipped_lines", run.SkippedLines,
		"errors", run.Errors, "duration_ms", run.DurationMS)
	if run.Errors > 0 {
		return run, fmt.Errorf("iMessage import had %d error(s); archive is incomplete", run.Errors)
	}
	return run, nil
}

// importConversation imports one .txt file if it changed since the last run.
func importConversation(
	ctx context.Context, st *store.Store, opts Options, log *slog.Logger,
	name, path string, info os.FileInfo, at time.Time,
) (changed bool, added, skipped int, err error) {
	var convID int64
	err = st.DB().QueryRowContext(ctx,
		`SELECT id FROM conversations WHERE source = ? AND name = ?`, source.IMessage, name).Scan(&convID)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return false, 0, 0, err
	}
	var prev *store.IngestState
	if convID != 0 {
		prev, err = st.GetIngestState(ctx, convID)
		if err != nil {
			return false, 0, 0, err
		}
	}
	mtime, size := info.ModTime().Unix(), info.Size()

	// Hash even when mtime and size match: exporters can replace a file with
	// different same-length content within the filesystem's time resolution.
	contentHash, err := hashFile(path)
	if err != nil {
		return false, 0, 0, err
	}
	contentHash, err = exclusionStateHash(contentHash, opts.Exclusions)
	if err != nil {
		return false, 0, 0, err
	}
	if !opts.Full && prev != nil && prev.ContentHash == contentHash {
		prev.MTimeUnix, prev.SizeBytes, prev.LastIngestedAt = mtime, size, at
		if serr := st.SetIngestState(ctx, *prev); serr != nil {
			return false, 0, 0, serr
		}
		return false, 0, 0, nil
	}

	msgs, skips, perr := parseFile(name, path, log)
	if perr != nil {
		return false, 0, 0, perr
	}
	msgs, excluded := FilterExcluded(msgs, opts.Exclusions)
	for rule, count := range excluded {
		log.Info("excluded imessage messages", "conversation", name, "rule", rule, "count", count)
	}
	if len(msgs) == 0 && convID == 0 {
		// A wholly excluded new transcript should not leave an empty thread in
		// the browser. It has no ingest state, so a later policy change can add it.
		return true, 0, skips, nil
	}
	if convID == 0 {
		convID, err = st.UpsertConversation(ctx, source.IMessage, name)
		if err != nil {
			return false, 0, 0, err
		}
	}
	if err := retainMessageAttachments(ctx, st, opts.ArchiveRoot, msgs); err != nil {
		return false, 0, 0, err
	}
	added, err = st.AppendIMessageMessages(ctx, convID, msgs)
	if err != nil {
		return false, 0, 0, err
	}
	if err = st.SetIngestState(ctx, store.IngestState{
		ConversationID: convID,
		RelPath:        name + ".txt",
		MTimeUnix:      mtime,
		SizeBytes:      size,
		ContentHash:    contentHash,
		MessageCount:   len(msgs),
		LastIngestedAt: at,
	}); err != nil {
		return false, 0, 0, err
	}
	return true, added, skips, nil
}

// parseFile streams a .txt file and returns its parsed messages plus a count of
// skipped (pre-timestamp) lines.
func parseFile(name, path string, log *slog.Logger) ([]signal.Message, int, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	var (
		msgs    []signal.Message
		skipped int
	)
	err = Parse(name, f,
		func(m signal.Message) error { msgs = append(msgs, m); return nil },
		func(line int, text string) {
			skipped++
			log.Warn("skipped pre-timestamp line", "conversation", name, "line", line)
		},
	)
	return msgs, skipped, err
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
