package imessage

import (
	"bytes"
	"context"
	"database/sql"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

// TestLinkPreviewRetention uses a fabricated Apple schema and image bytes only.
// It exercises backfill, repeat import, source disappearance, and ambiguity.
func TestLinkPreviewRetention(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "archive"), 0o700); err != nil {
		t.Fatal(err)
	}
	st, err := store.Open(filepath.Join(root, "archive", "msgbrowse.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	convID, err := st.UpsertConversation(ctx, source.IMessage, "Example")
	if err != nil {
		t.Fatal(err)
	}
	when := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	msg := signal.Message{
		Conversation: "Example", Timestamp: when, TimestampRaw: when.Format(signal.TimestampLayout),
		Sender: "Me", Body: "https://example.org/clip\nExample clip",
		Links: []signal.Link{{URL: "https://example.org/clip"}},
	}
	if n, err := st.AppendIMessageMessages(ctx, convID, []signal.Message{msg}); err != nil || n != 1 {
		t.Fatalf("append message: %d, %v", n, err)
	}
	sourceRoot := filepath.Join(root, "Apple")
	if err := os.MkdirAll(filepath.Join(sourceRoot, "Attachments"), 0o700); err != nil {
		t.Fatal(err)
	}
	photo := filepath.Join(sourceRoot, "Attachments", "image.pluginPayloadAttachment")
	icon := filepath.Join(sourceRoot, "Attachments", "icon.pluginPayloadAttachment")
	var b bytes.Buffer
	img := image.NewRGBA(image.Rect(0, 0, 40, 20))
	img.Set(0, 0, color.RGBA{R: 255, A: 255})
	if err := png.Encode(&b, img); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(photo, b.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(icon, []byte{0, 0, 1, 0, 1, 0, 16, 16}, 0o600); err != nil {
		t.Fatal(err)
	}
	sourcePath := filepath.Join(sourceRoot, "chat.db")
	db, err := sql.Open("sqlite", sourcePath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, stmt := range []string{
		`CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, text TEXT, payload_data BLOB, is_from_me INTEGER, associated_message_guid TEXT, balloon_bundle_id TEXT)`,
		`CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, hide_attachment INTEGER)`,
		`CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER)`,
		`CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)`,
		`CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER)`,
		`CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatal(err)
		}
	}
	appleDate := when.Sub(appleEpoch).Nanoseconds()
	if _, err := db.Exec(`INSERT INTO message VALUES (1, ?, ?, ?, 1, NULL, 'com.apple.messages.URLBalloonProvider')`,
		appleDate, "https://example.org/clip", []byte("https://example.org/clip")); err != nil {
		t.Fatal(err)
	}
	for _, stmt := range []string{
		`INSERT INTO attachment VALUES (1, '` + photo + `', 1)`,
		`INSERT INTO attachment VALUES (2, '` + icon + `', 1)`,
		`INSERT INTO message_attachment_join VALUES (1, 1), (1, 2)`,
		`INSERT INTO handle VALUES (1, '+12018389535')`,
		`INSERT INTO chat_message_join VALUES (1, 1)`,
		`INSERT INTO chat_handle_join VALUES (1, 1)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatal(err)
		}
	}
	opts := PreviewOptions{
		SourceDB: sourcePath, AttachmentRoot: sourceRoot, Participant: "2018389535",
		Start: when.Add(-time.Hour), End: when.Add(time.Hour), Location: time.UTC, DryRun: true,
	}
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.Matched != 1 || r.AssetsObserved != 2 || r.AssetsAdded != 0 {
		t.Fatalf("dry run = %+v, %v", r, err)
	}
	opts.DryRun = false
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.AssetsAdded != 2 {
		t.Fatalf("first run = %+v, %v", r, err)
	}
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.AssetsAdded != 0 {
		t.Fatalf("repeat run = %+v, %v", r, err)
	}
	if _, err := db.Exec(`UPDATE message SET text=NULL WHERE ROWID=1`); err != nil {
		t.Fatal(err)
	}
	opts.DryRun = true
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.Matched != 1 || r.WeakMatches != 1 {
		t.Fatalf("payload-only URL match = %+v, %v", r, err)
	}
	opts.DryRun = false
	page, err := st.GetMessages(ctx, convID, 0, 0, 10, false)
	if err != nil || len(page.Messages) != 1 || page.Messages[0].PreviewPath == "" || len(page.Messages[0].Attachments) != 0 {
		t.Fatalf("message preview view = %+v, %v", page, err)
	}
	retained, ok := archivepath.Resolve(source.IMessage,
		archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(st.DataDir())}, "", page.Messages[0].PreviewPath)
	if !ok {
		t.Fatal("preview path does not resolve")
	}
	if got, err := os.ReadFile(retained); err != nil || !bytes.Equal(got, b.Bytes()) {
		t.Fatalf("retained image changed: %v", err)
	}
	if err := os.Remove(photo); err != nil {
		t.Fatal(err)
	}
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.MissingFiles != 1 {
		t.Fatalf("missing source run = %+v, %v", r, err)
	}
	if got, err := os.ReadFile(retained); err != nil || !bytes.Equal(got, b.Bytes()) {
		t.Fatalf("source deletion damaged retained image: %v", err)
	}
	other := msg
	other.Body = "https://example.org/other"
	other.Links = []signal.Link{{URL: "https://example.org/other"}}
	if n, err := st.AppendIMessageMessages(ctx, convID, []signal.Message{other}); err != nil || n != 1 {
		t.Fatalf("append ambiguous candidate: %d, %v", n, err)
	}
	opts.DryRun = true
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.Ambiguous != 1 || r.AssetsObserved != 0 {
		t.Fatalf("ambiguous match = %+v, %v", r, err)
	}
	if _, err := db.Exec(`UPDATE message SET payload_data=NULL WHERE ROWID=1`); err != nil {
		t.Fatal(err)
	}
	if r, err := ImportLinkPreviews(ctx, st, opts); err != nil || r.Unmatched != 1 {
		t.Fatalf("no URL evidence = %+v, %v", r, err)
	}
}
