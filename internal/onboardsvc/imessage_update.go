package onboardsvc

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/imageconv"
	"github.com/joestump/msgbrowse/internal/imessage"
	"github.com/joestump/msgbrowse/internal/onboard"
	"github.com/joestump/msgbrowse/internal/store"
	_ "modernc.org/sqlite"
)

// localIMessageUpdater refreshes the rolling year from the Mac hosting the UI.
// The overlap is intentional: an iCloud attachment may arrive after its message,
// and GUID identity makes repeated paired imports safe. No Apple file is written.
type localIMessageUpdater struct {
	st  *store.Store
	cfg *config.Config
	log *slog.Logger
	// sourceRoot and now are test seams; production uses the current Mac/user.
	sourceRoot string
	now        func() time.Time
}

// Update runs one paired, owner-filtered update from a consistent source copy.
// Staging is private and disposable; the SQLite archive and retained media are
// append-only. The mixed orphaned transcript is held for separate review.
func (u *localIMessageUpdater) Update(ctx context.Context, tool string, env []string,
	exec onboard.ExecRunner, progress func(onboard.Phase, string)) (onboard.ImportResult, error) {
	var result onboard.ImportResult
	if runtime.GOOS != "darwin" && u.sourceRoot == "" {
		return result, fmt.Errorf("local Messages updates require macOS")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return result, fmt.Errorf("locate this Mac's Messages: %w", err)
	}
	appleRoot := filepath.Join(home, "Library", "Messages")
	if u.sourceRoot != "" {
		appleRoot = u.sourceRoot
	}
	sourceDB := filepath.Join(appleRoot, "chat.db")
	if _, err := os.Stat(sourceDB); err != nil {
		return result, fmt.Errorf("read this Mac's Messages database (Full Disk Access may be required): %w", err)
	}
	stage, err := os.MkdirTemp(u.cfg.DataDir, ".imessage-update-")
	if err != nil {
		return result, fmt.Errorf("prepare private update staging: %w", err)
	}
	defer func() {
		if err := os.RemoveAll(stage); err != nil {
			u.log.Warn("could not clear iMessage update staging", "error", err)
		}
	}()
	if err := os.Chmod(stage, 0o700); err != nil {
		return result, fmt.Errorf("protect update staging: %w", err)
	}
	snapshot := filepath.Join(stage, "chat.db")
	if err := snapshotMessages(ctx, sourceDB, snapshot); err != nil {
		return result, err
	}
	zone := time.Local
	clock := time.Now
	if u.now != nil {
		clock = u.now
	}
	now := clock().In(zone)
	day := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, zone)
	start, end := day.AddDate(-1, 0, 0), day.AddDate(0, 0, 1)
	// A long pause between manual updates must not leave an unscanned gap.
	// Revisit the last archived Apple message with a month of overlap; the
	// ordinary one-year window still applies when it reaches farther back.
	var lastUnix sql.NullInt64
	if err := u.st.DB().QueryRowContext(ctx, `SELECT MAX(ts_unix) FROM messages
		WHERE source='imessage' AND source_guid IS NOT NULL`).Scan(&lastUnix); err != nil {
		return result, fmt.Errorf("find latest archived iMessage: %w", err)
	}
	if lastUnix.Valid {
		last := time.Unix(lastUnix.Int64, 0).In(zone)
		if last.Before(day) {
			lastDay := time.Date(last.Year(), last.Month(), last.Day(), 0, 0, 0, 0, zone)
			if overlap := lastDay.AddDate(0, 0, -30); overlap.Before(start) {
				start = overlap
			}
		}
	}
	txt, html := filepath.Join(stage, "txt"), filepath.Join(stage, "html")
	for _, dir := range []string{txt, html} {
		if err := os.Mkdir(dir, 0o700); err != nil {
			return result, fmt.Errorf("create update export directory: %w", err)
		}
	}
	for _, format := range []struct{ name, copyMethod, dest string }{
		{"txt", "clone", txt}, {"html", "disabled", html},
	} {
		progress(onboard.PhaseExporting, "Exporting this Mac's recent Messages ("+format.name+")…")
		args := []string{"-a", "macOS", "-p", snapshot, "-r", appleRoot,
			"-f", format.name, "-c", format.copyMethod, "-o", format.dest,
			"-s", start.Format("2006-01-02"), "-e", end.Format("2006-01-02"), "--no-progress"}
		if _, err := exec(ctx, tool, env, args...); err != nil {
			return result, fmt.Errorf("%s export failed: %w", format.name, err)
		}
	}
	// The exporter combines unrelated, unlinked messages in one orphaned file.
	// Importing that file would reintroduce owner-excluded spam or assign an
	// invented shared conversation to new messages. Keep it out of this update.
	for _, path := range []string{filepath.Join(txt, "orphaned.txt"), filepath.Join(html, "orphaned.html")} {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return result, fmt.Errorf("hold mixed orphaned transcript: %w", err)
		}
	}
	progress(onboard.PhaseImporting, "Importing new messages and retaining attachments…")
	run, err := imessage.Run(ctx, u.st, imessage.Options{
		ArchiveRoot: txt, GUIDHTMLRoot: html, AppleFilterDB: snapshot,
		RequireAppleFilter:   u.cfg.IMessageExcludeAppleFilters,
		ExcludedAppleSenders: u.cfg.IMessageExcludedAppleSenders,
		Exclusions: imessage.ExclusionPolicy{
			ShortCodes: u.cfg.IMessageExcludeShortCodes, FilteredSMS: u.cfg.IMessageExcludeFilteredSMS,
			AppointmentReminders: u.cfg.IMessageExcludeAppointmentReminders,
			OptOutThreads:        u.cfg.IMessageExcludeOptOutThreads,
			EmailReminders:       u.cfg.IMessageExcludeEmailReminders, Rules: u.cfg.IMessageExclusions,
		},
	})
	if err != nil {
		return result, fmt.Errorf("paired iMessage import: %w", err)
	}
	if run.Errors != 0 {
		return result, fmt.Errorf("paired iMessage import reported %d errors; archive may be partially updated", run.Errors)
	}
	result.ConversationsChanged, result.MessagesAdded, result.MessagesTotal =
		run.ConversationsChanged, run.MessagesAdded, run.MessagesTotal
	progress(onboard.PhaseImporting, "Checking this Mac's native attachments and link previews…")
	assets, err := imessage.ImportSourceAssets(ctx, u.st, imessage.SourceAssetOptions{
		Platform: "macOS", Source: snapshot, AttachmentRoot: appleRoot,
	})
	if err != nil {
		return result, fmt.Errorf("retain native iMessage attachments: %w", err)
	}
	result.MediaRetained = assets.AssetsAdded
	previews, err := imessage.ImportLinkPreviews(ctx, u.st, imessage.PreviewOptions{
		SourceDB: snapshot, AttachmentRoot: appleRoot, Start: start, End: end, Location: zone,
	})
	if err != nil {
		return result, fmt.Errorf("retain iMessage link previews: %w", err)
	}
	result.PreviewAssets = previews.AssetsAdded
	if previews.Unmatched+previews.Ambiguous+previews.MissingFiles+previews.Unsupported != 0 {
		u.log.Warn("some iMessage link previews were unavailable or ambiguous",
			"unmatched", previews.Unmatched, "ambiguous", previews.Ambiguous,
			"missing", previews.MissingFiles, "unsupported", previews.Unsupported)
	}
	if conv, err := imageconv.Run(ctx, u.st, imageconv.Options{
		DataDir: u.cfg.DataDir, IMessageArchiveRoot: txt, Logger: u.log,
	}); err != nil {
		u.log.Warn("iMessage image conversion failed", "error", err)
	} else {
		result.MediaConverted, result.MediaSkipped, result.MediaFailed = conv.Converted, conv.Skipped, conv.Failed
	}
	return result, nil
}

// snapshotMessages copies a live SQLite database with SQLite's online VACUUM
// INTO operation. The source connection is read-only and the copy is checked
// before the exporter reads it, so TXT, HTML, and filter metadata agree.
func snapshotMessages(ctx context.Context, source, target string) error {
	u := url.URL{Scheme: "file", Path: source}
	db, err := sql.Open("sqlite", u.String()+"?mode=ro")
	if err != nil {
		return fmt.Errorf("open Apple Messages read-only: %w", err)
	}
	defer db.Close()
	if _, err := db.ExecContext(ctx, fmt.Sprintf("VACUUM INTO %q", target)); err != nil {
		return fmt.Errorf("copy Apple Messages read-only (Full Disk Access may be required): %w", err)
	}
	copyDB, err := sql.Open("sqlite", "file:"+target+"?mode=ro")
	if err != nil {
		return err
	}
	defer copyDB.Close()
	var integrity string
	if err := copyDB.QueryRowContext(ctx, "PRAGMA integrity_check").Scan(&integrity); err != nil {
		return fmt.Errorf("check Messages copy: %w", err)
	}
	if integrity != "ok" {
		return fmt.Errorf("Messages copy failed SQLite integrity check")
	}
	return nil
}
