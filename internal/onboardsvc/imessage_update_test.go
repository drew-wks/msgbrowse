package onboardsvc

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/onboard"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
	_ "modernc.org/sqlite"
)

const updateTestGUID = "11111111-1111-1111-1111-111111111111"

// TestLocalIMessageUpdateRepeatAndSourceDeletion verifies the real paired
// importer is idempotent and never treats a vanished source row as a delete.
func TestLocalIMessageUpdateRepeatAndSourceDeletion(t *testing.T) {
	dataDir, appleRoot := t.TempDir(), t.TempDir()
	st, err := store.Open(filepath.Join(dataDir, store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	sourceDB, err := sql.Open("sqlite", filepath.Join(appleRoot, "chat.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sourceDB.Close() })
	_, err = sourceDB.Exec(`CREATE TABLE chat (is_filtered INTEGER);
		CREATE TABLE message (guid TEXT, date INTEGER, text TEXT, payload_data BLOB,
			is_from_me INTEGER, associated_message_guid TEXT, balloon_bundle_id TEXT);
		CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
		CREATE TABLE attachment (guid TEXT, filename TEXT, hide_attachment INTEGER,
			mime_type TEXT, transfer_name TEXT);
		CREATE TABLE message_attachment_join (attachment_id INTEGER, message_id INTEGER);
		INSERT INTO chat(is_filtered) VALUES (0);
		INSERT INTO message(guid,date,text,is_from_me) VALUES ('` + updateTestGUID + `',1,'hello',1);
		INSERT INTO chat_message_join(chat_id,message_id) VALUES (1,1);
		INSERT INTO attachment(guid,filename,hide_attachment,mime_type,transfer_name)
		VALUES ('attachment-guid','~/Library/Messages/Attachments/photo.jpg',0,'image/jpeg','photo.jpg');
		INSERT INTO message_attachment_join(attachment_id,message_id) VALUES (1,1);`)
	if err != nil {
		t.Fatal(err)
	}
	appleMedia := filepath.Join(appleRoot, "Attachments", "photo.jpg")
	if err := os.MkdirAll(filepath.Dir(appleMedia), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(appleMedia, []byte("original-photo-bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	updater := &localIMessageUpdater{st: st, cfg: &config.Config{
		DataDir: dataDir, IMessageExcludeAppleFilters: true,
	}, log: discardLogger(), sourceRoot: appleRoot,
		now: func() time.Time { return time.Date(2026, 11, 9, 12, 0, 0, 0, time.Local) }}
	showSource := true
	formats := make([]string, 0, 6)
	var exportStart string
	export := func(_ context.Context, _ string, _ []string, args ...string) (string, error) {
		var format, dest string
		for i := 0; i+1 < len(args); i++ {
			switch args[i] {
			case "-f":
				format = args[i+1]
			case "-o":
				dest = args[i+1]
			case "-s":
				exportStart = args[i+1]
			}
		}
		formats = append(formats, format)
		if !showSource {
			return "", nil
		}
		date := "Nov 9, 2026  9:00:00 AM"
		if format == "txt" {
			media := filepath.Join(dest, "attachments", "photo.jpg")
			if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
				return "", err
			}
			if err := os.WriteFile(media, []byte("original-photo-bytes"), 0o600); err != nil {
				return "", err
			}
			return "", os.WriteFile(filepath.Join(dest, "Solo.txt"), []byte(date+"\nMe\nhello\nattachments/photo.jpg\n"), 0o600)
		}
		body := `<p><span class="timestamp"><a title="Reveal in Messages app" href="sms://open?message-guid=` + updateTestGUID + `">` + date + `</a></span><span class="sender">Me</span></p>`
		return "", os.WriteFile(filepath.Join(dest, "Solo.html"), []byte(body), 0o600)
	}
	update := func() onboard.ImportResult {
		t.Helper()
		res, err := updater.Update(context.Background(), "fake-exporter", nil, export,
			func(onboard.Phase, string) {})
		if err != nil {
			t.Fatal(err)
		}
		return res
	}
	if got := update().MessagesAdded; got != 1 {
		t.Fatalf("first update added %d messages, want 1", got)
	}
	if got := update().MessagesAdded; got != 0 {
		t.Fatalf("repeat update added %d duplicate messages", got)
	}
	var rel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments WHERE kind='image' LIMIT 1`).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	permanent, ok := archivepath.Resolve(source.IMessage,
		archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(dataDir)}, "", rel)
	if !ok {
		t.Fatalf("retained media path did not resolve: %s", rel)
	}
	if _, err := sourceDB.Exec(`DELETE FROM chat_message_join; DELETE FROM message`); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(appleMedia); err != nil {
		t.Fatal(err)
	}
	showSource = false
	updater.now = func() time.Time { return time.Date(2028, 1, 1, 12, 0, 0, 0, time.Local) }
	if got := update().MessagesAdded; got != 0 {
		t.Fatalf("empty source update added %d messages", got)
	}
	if exportStart != "2026-10-10" {
		t.Fatalf("long-gap update starts at %s, want overlap before last archived message", exportStart)
	}
	if n, err := st.CountMessages(context.Background()); err != nil || n != 1 {
		t.Fatalf("source deletion changed archive: count=%d err=%v", n, err)
	}
	if got, err := os.ReadFile(permanent); err != nil || string(got) != "original-photo-bytes" {
		t.Fatalf("source deletion lost retained photo: bytes=%q err=%v", got, err)
	}
	if got := strings.Join(formats, ","); got != "txt,html,txt,html,txt,html" {
		t.Fatalf("paired exporter formats = %s", got)
	}
	if left, err := filepath.Glob(filepath.Join(dataDir, ".imessage-update-*")); err != nil || len(left) != 0 {
		t.Fatalf("private staging remains: %v, %v", left, err)
	}
}
