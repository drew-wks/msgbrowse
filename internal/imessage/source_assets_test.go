package imessage

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

func addArchivedGUID(t *testing.T, st *store.Store) {
	t.Helper()
	ctx := context.Background()
	id, err := st.UpsertConversation(ctx, source.IMessage, "Synthetic")
	if err != nil {
		t.Fatal(err)
	}
	m := signal.Message{Conversation: "Synthetic", Timestamp: time.Date(2020, 5, 20, 9, 0, 0, 0, time.UTC),
		TimestampRaw: "2020-05-20 09:00:00", Sender: "Me", Body: "test", SourceGUID: guidOne}
	if _, err := st.AppendIMessageMessages(ctx, id, []signal.Message{m}); err != nil {
		t.Fatal(err)
	}
}

func createSourceDB(t *testing.T, path, filename, hiddenFilename string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, stmt := range []string{
		`CREATE TABLE message (guid TEXT)`,
		`CREATE TABLE attachment (guid TEXT, filename TEXT, hide_attachment INTEGER, mime_type TEXT, transfer_name TEXT)`,
		`CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER)`,
		`INSERT INTO message(guid) VALUES ('` + guidOne + `')`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatal(err)
		}
	}
	for _, item := range []struct {
		guid, name, mime string
		hidden           int
	}{{guidOne, filename, "image/jpeg", 0}, {guidTwo, hiddenFilename, "", 1}} {
		result, err := db.Exec(`INSERT INTO attachment(guid, filename, hide_attachment, mime_type, transfer_name)
			VALUES (?, ?, ?, ?, '')`, item.guid, item.name, item.hidden, item.mime)
		if err != nil {
			t.Fatal(err)
		}
		id, err := result.LastInsertId()
		if err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`INSERT INTO message_attachment_join(message_id,attachment_id) VALUES (1,?)`, id); err != nil {
			t.Fatal(err)
		}
	}
}

func TestSourceAssetsMacOSVisibleAndHiddenRetention(t *testing.T) {
	st := newStore(t)
	addArchivedGUID(t, st)
	root := t.TempDir()
	mediaRoot := filepath.Join(root, "Messages")
	visible := filepath.Join(mediaRoot, "Attachments", "photo.jpg")
	hidden := filepath.Join(mediaRoot, "Attachments", "payload.pluginPayloadAttachment")
	if err := os.MkdirAll(filepath.Dir(visible), 0o700); err != nil {
		t.Fatal(err)
	}
	for path, body := range map[string]string{visible: "image-bytes", hidden: "opaque-bytes"} {
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	dbPath := filepath.Join(root, "chat.db")
	createSourceDB(t, dbPath, "~/Library/Messages/Attachments/photo.jpg",
		"~/Library/Messages/Attachments/payload.pluginPayloadAttachment")
	opts := SourceAssetOptions{Platform: "macOS", Source: dbPath, AttachmentRoot: mediaRoot}
	opts.DryRun = true
	dry, err := ImportSourceAssets(context.Background(), st, opts)
	if err != nil || dry.Available != 2 || dry.AssetsAdded != 0 {
		t.Fatalf("dry run = %+v, %v", dry, err)
	}
	opts.DryRun = false
	first, err := ImportSourceAssets(context.Background(), st, opts)
	if err != nil || first.AssetsAdded != 2 || first.DisplayAdded != 1 || first.Coverage.Unavailable != 0 {
		t.Fatalf("first pass = %+v, %v", first, err)
	}
	second, err := ImportSourceAssets(context.Background(), st, opts)
	if err != nil || second.AssetsAdded != 0 || second.DisplayAdded != 0 {
		t.Fatalf("repeat pass = %+v, %v", second, err)
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_source_assets`); got != 2 {
		t.Fatalf("source assets = %d", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_attachment_observations`); got != 2 {
		t.Fatalf("catalog observations = %d", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM attachments`); got != 1 {
		t.Fatalf("visible attachment cards = %d", got)
	}
	if err := os.RemoveAll(mediaRoot); err != nil {
		t.Fatal(err)
	}
	rows, err := st.DB().Query(`SELECT rel_path FROM imessage_source_assets`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	retainedBodies := make(map[string]bool)
	for rows.Next() {
		var rel string
		if err := rows.Scan(&rel); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(st.DataDir(), "media", "imessage", strings.TrimPrefix(rel, "permanent/"))
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		retainedBodies[string(data)] = true
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if !retainedBodies["image-bytes"] || !retainedBodies["opaque-bytes"] {
		t.Fatalf("source deletion lost a retained visible or hidden file")
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_source_assets`); got != 2 {
		t.Fatalf("source deletion removed asset records: %d", got)
	}
}

func TestSourceAssetsIOSManifestResolution(t *testing.T) {
	st := newStore(t)
	addArchivedGUID(t, st)
	root := t.TempDir()
	fileID := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	file := filepath.Join(root, "aa", fileID)
	if err := os.MkdirAll(filepath.Dir(file), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte("iphone-image"), 0o600); err != nil {
		t.Fatal(err)
	}
	manifest, err := sql.Open("sqlite", filepath.Join(root, "Manifest.db"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manifest.Exec(`CREATE TABLE Files(fileID TEXT, domain TEXT, relativePath TEXT);
		INSERT INTO Files VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','MediaDomain',
		'Library/SMS/Attachments/AB/photo.jpg')`); err != nil {
		t.Fatal(err)
	}
	_ = manifest.Close()
	dbPath := filepath.Join(root, iosSMSDatabaseFileID[:2], iosSMSDatabaseFileID)
	createSourceDB(t, dbPath, "~/Library/SMS/Attachments/AB/photo.jpg", "~/Library/SMS/Attachments/AB/missing.pluginPayloadAttachment")
	report, err := ImportSourceAssets(context.Background(), st, SourceAssetOptions{Platform: "iOS", Source: root})
	if err != nil || report.Available != 1 || report.Missing != 1 || report.DisplayAdded != 1 ||
		report.Coverage.Observed != 2 || report.Coverage.Unavailable != 1 {
		t.Fatalf("iOS source pass = %+v, %v", report, err)
	}
}
