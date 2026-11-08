package imessage

import (
	"context"
	"database/sql"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	_ "modernc.org/sqlite"
)

func writeFilterSource(t *testing.T, path string, values map[string]int) {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, ddl := range []string{
		`CREATE TABLE message (guid TEXT, handle_id INTEGER)`,
		`CREATE TABLE chat (is_filtered INTEGER)`,
		`CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER)`,
		`CREATE TABLE handle (id TEXT)`,
	} {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatal(err)
		}
	}
	for guid, value := range values {
		msg, err := db.Exec(`INSERT INTO message(guid) VALUES (?)`, guid)
		if err != nil {
			t.Fatal(err)
		}
		chat, err := db.Exec(`INSERT INTO chat(is_filtered) VALUES (?)`, value)
		if err != nil {
			t.Fatal(err)
		}
		mid, _ := msg.LastInsertId()
		cid, _ := chat.LastInsertId()
		if _, err := db.Exec(`INSERT INTO chat_message_join(message_id,chat_id) VALUES (?,?)`, mid, cid); err != nil {
			t.Fatal(err)
		}
	}
}

func TestAppleFilterImportGuardsRepeatedAndChangedSource(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	txt, htmlRoot := filepath.Join(root, "txt"), filepath.Join(root, "html")
	sourceDB := filepath.Join(root, "chat.db")
	date := "May 20, 2020  9:00:00 AM"
	writeArchiveFixture(t, txt, "Blocked", date+"\nAutomated\ntransaction\nattachments/receipt.jpg\n")
	if err := os.MkdirAll(filepath.Join(txt, "attachments"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(txt, "attachments", "receipt.jpg"), []byte("sample image bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeGUIDHTML(t, htmlRoot, "Blocked", guidOne, date, "Automated")
	writeFilterSource(t, sourceDB, map[string]int{guidOne: 2})
	opts := Options{ArchiveRoot: txt, GUIDHTMLRoot: htmlRoot, AppleFilterDB: sourceDB,
		RequireAppleFilter: true, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	for i := 0; i < 2; i++ {
		run, err := Run(context.Background(), st, opts)
		if err != nil || run.MessagesAdded != 0 {
			t.Fatalf("filtered import %d: added=%d err=%v", i, run.MessagesAdded, err)
		}
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_excluded_messages`); got != 1 {
		t.Fatalf("GUID guards = %d, want 1", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM messages`); got != 0 {
		t.Fatalf("filtered message imported: %d", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM attachments`); got != 0 {
		t.Fatalf("filtered attachment retained: %d", got)
	}
	sourceConn, err := sql.Open("sqlite", sourceDB)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := sourceConn.Exec(`UPDATE chat SET is_filtered=0`); err != nil {
		t.Fatal(err)
	}
	sourceConn.Close()
	if err := os.RemoveAll(txt); err != nil {
		t.Fatal(err)
	}
	// The source can later omit the excluded message without changing the
	// archive. Recreated exports with the flag cleared still hit its GUID guard.
	if got := scalar(t, st, `SELECT count(*) FROM messages`); got != 0 {
		t.Fatalf("source deletion changed archive: %d", got)
	}
	writeArchiveFixture(t, txt, "Blocked", date+"\nAutomated\ntransaction\n")
	run, err := Run(context.Background(), st, opts)
	if err != nil || run.MessagesAdded != 0 {
		t.Fatalf("changed source reimport: added=%d err=%v", run.MessagesAdded, err)
	}
	convID, err := st.UpsertConversation(context.Background(), source.IMessage, "Guard check")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB().Exec(`INSERT INTO messages(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body)
		VALUES ('new-hash',?,?,?,'date',1,'Automated','transaction')`, convID, source.IMessage, guidOne); err == nil || !strings.Contains(err.Error(), "owner-excluded message") {
		t.Fatalf("excluded GUID bypassed store guard: %v", err)
	}
}

func TestAppleFilterRequiresPairedSourceAndHoldsUnknownValues(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	txt, htmlRoot := filepath.Join(root, "txt"), filepath.Join(root, "html")
	sourceDB := filepath.Join(root, "chat.db")
	date := "May 20, 2020  9:00:00 AM"
	writeArchiveFixture(t, txt, "Protected", date+"\nKnown contact\nhello\n")
	writeGUIDHTML(t, htmlRoot, "Protected", guidTwo, date, "Known contact")
	writeFilterSource(t, sourceDB, map[string]int{guidTwo: 68})
	if _, err := Run(context.Background(), st, Options{ArchiveRoot: txt, RequireAppleFilter: true}); err == nil {
		t.Fatal("TXT-only import bypassed required Apple filter")
	}
	opts := Options{ArchiveRoot: txt, GUIDHTMLRoot: htmlRoot, AppleFilterDB: sourceDB,
		RequireAppleFilter: true, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	if _, err := Run(context.Background(), st, opts); err == nil {
		t.Fatal("new unreviewed filter value was imported")
	}
	if got := scalar(t, st, `SELECT count(*) FROM messages`); got != 0 {
		t.Fatalf("unreviewed message count = %d", got)
	}
	sourceConn, err := sql.Open("sqlite", sourceDB)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := sourceConn.Exec(`UPDATE chat SET is_filtered=0`); err != nil {
		t.Fatal(err)
	}
	sourceConn.Close()
	run, err := Run(context.Background(), st, opts)
	if err != nil || run.MessagesAdded != 1 {
		t.Fatalf("clear flag import: added=%d err=%v", run.MessagesAdded, err)
	}
	sourceConn, err = sql.Open("sqlite", sourceDB)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := sourceConn.Exec(`UPDATE chat SET is_filtered=68`); err != nil {
		t.Fatal(err)
	}
	sourceConn.Close()
	run, err = Run(context.Background(), st, opts)
	if err != nil || run.MessagesAdded != 0 || scalar(t, st, `SELECT count(*) FROM messages`) != 1 {
		t.Fatalf("existing personal row changed: added=%d err=%v", run.MessagesAdded, err)
	}
}

func TestAppleFilterVerifiedValues(t *testing.T) {
	st := newStore(t)
	guids := []string{
		"11111111-1111-1111-1111-111111111111",
		"22222222-2222-2222-2222-222222222222",
		"33333333-3333-3333-3333-333333333333",
		"44444444-4444-4444-4444-444444444444",
		"55555555-5555-5555-5555-555555555555",
	}
	values := map[string]int{}
	msgs := make([]signal.Message, 0, len(guids))
	for i, value := range []int{1, 2, 3, 4, 52} {
		values[guids[i]] = value
		msgs = append(msgs, signal.Message{SourceGUID: guids[i]})
	}
	kept, count, err := filterAppleValues(context.Background(), st, msgs, values)
	if err != nil || count != len(guids) || len(kept) != 0 {
		t.Fatalf("verified values: kept=%d excluded=%d err=%v", len(kept), count, err)
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_excluded_messages`); got != len(guids) {
		t.Fatalf("durable GUID guards = %d, want %d", got, len(guids))
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_excluded_messages WHERE reason LIKE 'owner_apple_filter_value_%'`); got != len(guids) {
		t.Fatalf("audited filter values = %d, want %d", got, len(guids))
	}
}

func TestAppleFilterReadsLiveWAL(t *testing.T) {
	path := filepath.Join(t.TempDir(), "chat.db")
	writeFilterSource(t, path, map[string]int{guidOne: 1})
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`PRAGMA journal_mode=WAL`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE chat SET is_filtered=3`); err != nil {
		t.Fatal(err)
	}
	values, hasColumn, err := loadAppleFilterValues(context.Background(), path)
	if err != nil || !hasColumn || values[guidOne] != 3 {
		t.Fatalf("live WAL flag: value=%d has_column=%t err=%v", values[guidOne], hasColumn, err)
	}
}

func TestBlockedAppleSenderIgnoresExporterContactName(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	sourceDB := filepath.Join(root, "chat.db")
	writeFilterSource(t, sourceDB, map[string]int{guidOne: 0})
	db, err := sql.Open("sqlite", sourceDB)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO handle(id) VALUES ('+1 (551) 266-5526')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE message SET handle_id=1 WHERE guid=?`, guidOne); err != nil {
		t.Fatal(err)
	}
	db.Close()
	date := "May 20, 2020  9:00:00 AM"
	for _, name := range []string{"Tamar Asayan", "CarePlus"} {
		txt := filepath.Join(root, name+"-txt")
		htmlRoot := filepath.Join(root, name+"-html")
		writeArchiveFixture(t, txt, name, date+"\n"+name+"\nappointment notice\n")
		writeGUIDHTML(t, htmlRoot, name, guidOne, date, name)
		run, err := Run(context.Background(), st, Options{
			ArchiveRoot: txt, GUIDHTMLRoot: htmlRoot, AppleFilterDB: sourceDB,
			ExcludedAppleSenders: []string{"+15512665526"},
			Logger:               slog.New(slog.NewTextHandler(io.Discard, nil)),
		})
		if err != nil || run.MessagesAdded != 0 {
			t.Fatalf("sender block after label %q: added=%d err=%v", name, run.MessagesAdded, err)
		}
	}
	if got := scalar(t, st, `SELECT count(*) FROM messages`); got != 0 {
		t.Fatalf("blocked sender imported %d messages", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM imessage_excluded_messages`); got != 1 {
		t.Fatalf("sender GUID guards = %d, want 1", got)
	}
}

func TestBlockedAppleSenderInternationalShortCodeAndEmail(t *testing.T) {
	path := filepath.Join(t.TempDir(), "chat.db")
	handles := map[string]string{
		"international-guid": "+44 20 7946 0000",
		"short-guid":         "12345",
		"email-guid":         "Alerts@Example.com",
	}
	values := make(map[string]int, len(handles))
	for guid := range handles {
		values[guid] = 0
	}
	writeFilterSource(t, path, values)
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	for guid, handle := range handles {
		result, err := db.Exec(`INSERT INTO handle(id) VALUES (?)`, handle)
		if err != nil {
			t.Fatal(err)
		}
		id, err := result.LastInsertId()
		if err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`UPDATE message SET handle_id=? WHERE guid=?`, id, guid); err != nil {
			t.Fatal(err)
		}
	}
	db.Close()
	blocked, err := loadBlockedAppleSenders(context.Background(), path,
		[]string{"+442079460000", "12345", "alerts@example.com"})
	if err != nil || len(blocked) != len(handles) {
		t.Fatalf("blocked handles = %v, err = %v", blocked, err)
	}
	for guid := range handles {
		if blocked[guid] == "" {
			t.Fatalf("source handle for %s was not blocked", guid)
		}
	}
	if _, err := loadBlockedAppleSenders(context.Background(), path,
		[]string{"+442079460000", "+44 20 7946 0000"}); err == nil {
		t.Fatal("duplicate normalized sender accepted")
	}
}
