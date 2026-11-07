package imessage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"mime"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
	_ "modernc.org/sqlite"
)

const iosSMSDatabaseFileID = "3d0d7e5fb2ce288813306e4d4636395e047a3d28"

var backupFileID = regexp.MustCompile(`^[0-9a-fA-F]{40}$`)

// SourceAssetOptions selects one immutable Apple Messages source. iOS Source
// is an unencrypted MobileSync backup root; macOS Source is a copied chat.db.
type SourceAssetOptions struct {
	Platform       string
	Source         string
	AttachmentRoot string
	DryRun         bool
}

// SourceAssetReport contains counts only; it never exposes message contents.
type SourceAssetReport struct {
	Observed     int
	Archived     int
	Available    int
	Missing      int
	NoPath       int
	NoGUID       int
	Hidden       int
	Visible      int
	AssetsAdded  int
	DisplayAdded int
	Coverage     store.IMessageAttachmentCoverage
}

func openSourceSQLite(path string) (*sql.DB, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	u := url.URL{Scheme: "file", Path: absolute}
	db, err := sql.Open("sqlite", u.String()+"?mode=ro&_pragma=query_only(ON)")
	if err != nil {
		return nil, err
	}
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	return db, nil
}

// loadBackupManifest maps iPhone attachment paths to their hashed backup file.
func loadBackupManifest(ctx context.Context, backupRoot string) (map[string]string, error) {
	db, err := openSourceSQLite(filepath.Join(backupRoot, "Manifest.db"))
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.QueryContext(ctx, `SELECT relativePath, fileID FROM Files
		WHERE domain='MediaDomain' AND relativePath LIKE 'Library/SMS/%'`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]string)
	for rows.Next() {
		var path, id string
		if err := rows.Scan(&path, &id); err != nil {
			return nil, err
		}
		if backupFileID.MatchString(id) {
			out[path] = id
		}
	}
	return out, rows.Err()
}

// sourceAssetPath resolves an attachment while confining reads to its source.
func sourceAssetPath(opts SourceAssetOptions, root string, manifest map[string]string, name string) (string, error) {
	if opts.Platform == "macOS" {
		if strings.HasPrefix(name, "~/Library/Messages/") {
			name = filepath.Join(root, strings.TrimPrefix(name, "~/Library/Messages/"))
		}
		return previewSourcePath(root, name)
	}
	if !strings.HasPrefix(name, "~/Library/SMS/") {
		return "", fmt.Errorf("iOS attachment path is not under Library/SMS")
	}
	id := manifest[strings.TrimPrefix(name, "~/")]
	if id == "" {
		return "", os.ErrNotExist
	}
	path, err := filepath.EvalSymlinks(filepath.Join(root, id[:2], id))
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("iOS attachment escaped backup root")
	}
	return path, nil
}

func sourceAssetKind(mimeType, name string) signal.AttachmentKind {
	ext := strings.ToLower(filepath.Ext(name))
	if strings.HasPrefix(mimeType, "image/") || imageExts[ext] {
		if ext != ".svg" {
			return signal.KindImage
		}
	}
	if strings.HasPrefix(mimeType, "video/") || strings.HasPrefix(mime.TypeByExtension(ext), "video/") {
		return signal.KindVideo
	}
	return signal.KindFile
}

// ImportSourceAssets copies native attachment bytes omitted by TXT export.
// It uses GUIDs already archived in msgbrowse and reads its Apple source and
// backup manifest only in SQLite query-only mode. Hidden payload bytes are
// retained in imessage_source_assets without appearing in the media gallery.
func ImportSourceAssets(ctx context.Context, st *store.Store, opts SourceAssetOptions) (SourceAssetReport, error) {
	var report SourceAssetReport
	if opts.Platform != "iOS" && opts.Platform != "macOS" {
		return report, fmt.Errorf("platform must be iOS or macOS")
	}
	if opts.Source == "" || opts.Platform == "macOS" && opts.AttachmentRoot == "" {
		return report, fmt.Errorf("source and macOS attachment root are required")
	}
	rootPath := opts.Source
	if opts.Platform == "macOS" {
		rootPath = opts.AttachmentRoot
	}
	root, err := filepath.EvalSymlinks(rootPath)
	if err != nil {
		return report, err
	}
	dbPath := opts.Source
	manifest := make(map[string]string)
	if opts.Platform == "iOS" {
		dbPath = filepath.Join(root, iosSMSDatabaseFileID[:2], iosSMSDatabaseFileID)
		manifest, err = loadBackupManifest(ctx, root)
		if err != nil {
			return report, fmt.Errorf("read iOS backup manifest: %w", err)
		}
	}
	sourceDB, err := openSourceSQLite(dbPath)
	if err != nil {
		return report, fmt.Errorf("read Apple Messages database: %w", err)
	}
	defer sourceDB.Close()
	rows, err := sourceDB.QueryContext(ctx, `SELECT a.guid, a.filename, COALESCE(a.hide_attachment,0),
		COALESCE(a.mime_type,''), COALESCE(a.transfer_name,''), m.guid
		FROM attachment a JOIN message_attachment_join j ON j.attachment_id=a.ROWID
		JOIN message m ON m.ROWID=j.message_id WHERE m.guid IS NOT NULL`)
	if err != nil {
		return report, err
	}
	defer rows.Close()
	for rows.Next() {
		if err := ctx.Err(); err != nil {
			return report, err
		}
		var attachmentGUID, messageGUID, mimeType, transferName string
		var filename sql.NullString
		var hidden int
		if err := rows.Scan(&attachmentGUID, &filename, &hidden, &mimeType, &transferName, &messageGUID); err != nil {
			return report, err
		}
		report.Observed++
		if attachmentGUID == "" {
			report.NoGUID++
			continue
		}
		var messageID int64
		err := st.DB().QueryRowContext(ctx, `SELECT id FROM messages WHERE source=? AND source_guid=?`,
			source.IMessage, messageGUID).Scan(&messageID)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return report, err
		}
		report.Archived++
		if !opts.DryRun {
			if err := st.ObserveIMessageAttachment(ctx, messageID, attachmentGUID, hidden != 0, mimeType); err != nil {
				return report, err
			}
		}
		if !filename.Valid || filename.String == "" {
			report.NoPath++
			continue
		}
		path, err := sourceAssetPath(opts, root, manifest, filename.String)
		if err != nil {
			report.Missing++
			continue
		}
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() {
			report.Missing++
			continue
		}
		report.Available++
		if hidden != 0 {
			report.Hidden++
		} else {
			report.Visible++
		}
		if opts.DryRun {
			continue
		}
		name := filepath.Base(transferName)
		if name == "." || name == "" {
			name = filepath.Base(filename.String)
		}
		ext := strings.ToLower(filepath.Ext(filename.String))
		if ext == "" {
			ext = ".bin"
		}
		retained, err := retainSourceFile(path, st.DataDir(), ext)
		if err != nil {
			return report, fmt.Errorf("retain source attachment: %w", err)
		}
		kind := sourceAssetKind(mimeType, filename.String)
		added, displayed, err := st.AppendIMessageSourceAsset(ctx, messageID, store.IMessageSourceAsset{
			SourceAttachmentGUID: attachmentGUID, RelPath: retained,
			OriginalName: name, Kind: kind, Hidden: hidden != 0,
		})
		if err != nil {
			return report, err
		}
		report.AssetsAdded += added
		report.DisplayAdded += displayed
	}
	if err := rows.Err(); err != nil {
		return report, err
	}
	if err := rows.Close(); err != nil {
		return report, err
	}
	if !opts.DryRun {
		report.Coverage, err = st.IMessageAttachmentCoverage(ctx)
		if err != nil {
			return report, err
		}
	}
	return report, nil
}
