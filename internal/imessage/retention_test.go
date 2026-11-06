package imessage

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

const archiveFirst = "May 20, 2020  9:00:00 AM\nMe\nold searchable text\nattachments/AB/photo.jpg\n"
const archiveSecond = "May 20, 2020  9:01:00 AM\nMJ\nsecond message\n"
const archiveThird = "May 20, 2020  9:02:00 AM\nMe\nnew after deletion\n"

// writeArchiveFixture creates only synthetic exporter data under a test directory.
func writeArchiveFixture(t *testing.T, root, conversation, body string) {
	t.Helper()
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, conversation+".txt"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// runArchiveFixture imports a synthetic root with quiet diagnostics.
func runArchiveFixture(t *testing.T, st *store.Store, root string) store.IngestRun {
	t.Helper()
	run, err := Run(context.Background(), st, Options{ArchiveRoot: root, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	return run
}

func TestAppendOnlyRetainsMessagesMediaAndSearch(t *testing.T) {
	ctx := context.Background()
	st := newStore(t)
	root := t.TempDir()
	media := filepath.Join(root, "attachments", "AB", "photo.jpg")
	if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(media, []byte("synthetic-photo-one"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeArchiveFixture(t, root, "Solo", archiveFirst+"\n"+archiveSecond)
	if run := runArchiveFixture(t, st, root); run.MessagesAdded != 2 {
		t.Fatalf("first import added %d, want 2", run.MessagesAdded)
	}
	var rel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments LIMIT 1`).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(rel, archivepath.IMessagePermanentPrefix) {
		t.Fatalf("attachment not retained: %q", rel)
	}
	retained, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(st.DataDir())}, "Solo", rel)
	if !ok {
		t.Fatal("retained path did not resolve")
	}
	if got, err := os.ReadFile(retained); err != nil || string(got) != "synthetic-photo-one" {
		t.Fatalf("retained bytes = %q, %v", got, err)
	}
	if run := runArchiveFixture(t, st, root); run.MessagesAdded != 0 {
		t.Fatalf("identical import added %d messages", run.MessagesAdded)
	}
	// The replacement export omits both old messages and the original media.
	if err := os.Remove(media); err != nil {
		t.Fatal(err)
	}
	writeArchiveFixture(t, root, "Solo", archiveThird)
	if run := runArchiveFixture(t, st, root); run.MessagesAdded != 1 {
		t.Fatalf("post-deletion import added %d, want 1", run.MessagesAdded)
	}
	if n, err := st.CountMessages(ctx); err != nil || n != 3 {
		t.Fatalf("message count = %d, %v; want 3", n, err)
	}
	if got, err := os.ReadFile(retained); err != nil || string(got) != "synthetic-photo-one" {
		t.Fatalf("retained media changed after source removal: %q, %v", got, err)
	}
	hits, err := st.SearchMessages(ctx, store.SearchOptions{Query: "searchable", Source: source.IMessage})
	if err != nil || len(hits) != 1 {
		t.Fatalf("historical search returned %d hits, %v", len(hits), err)
	}
	if err := os.Remove(filepath.Join(root, "Solo.txt")); err != nil {
		t.Fatal(err)
	}
	runArchiveFixture(t, st, root)
	if n, _ := st.CountMessages(ctx); n != 3 {
		t.Fatalf("missing conversation removed messages: %d", n)
	}
}

func TestAppendOnlyEditedGroupAndReorderedMessages(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	first := "May 20, 2020  9:00:00 AM\nMJ\nsame\n"
	other := "May 20, 2020  9:01:00 AM\nMe\noriginal edit\n"
	writeArchiveFixture(t, root, "Group Trip", first+"\n"+first+"\n"+other)
	runArchiveFixture(t, st, root)
	if n, _ := st.CountMessages(context.Background()); n != 3 {
		t.Fatalf("initial duplicate-tuple group count = %d", n)
	}
	edited := "May 20, 2020  9:01:00 AM\nMe\nedited text\n"
	writeArchiveFixture(t, root, "Group Trip", edited+"\n"+first+"\n"+first)
	runArchiveFixture(t, st, root)
	if n, _ := st.CountMessages(context.Background()); n != 4 {
		t.Fatalf("edited group count = %d, want 4 revisions/occurrences", n)
	}
	runArchiveFixture(t, st, root)
	if n, _ := st.CountMessages(context.Background()); n != 4 {
		t.Fatalf("reordered identical import duplicated messages: %d", n)
	}
}

func TestLegacyAttachmentPromotedBeforeSourceRemoval(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	media := filepath.Join(root, "attachments", "AB", "photo.jpg")
	if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(media, []byte("legacy-bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeArchiveFixture(t, root, "Solo", archiveFirst)
	msgs, err := ParseAll("Solo", strings.NewReader(archiveFirst))
	if err != nil {
		t.Fatal(err)
	}
	cid, err := st.UpsertConversation(context.Background(), source.IMessage, "Solo")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.ReplaceConversationMessages(context.Background(), cid, source.IMessage, msgs); err != nil {
		t.Fatal(err)
	}
	runArchiveFixture(t, st, root)
	var rel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments LIMIT 1`).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(rel, archivepath.IMessagePermanentPrefix) {
		t.Fatalf("legacy attachment was not promoted: %q", rel)
	}
	if err := os.RemoveAll(filepath.Join(root, "attachments")); err != nil {
		t.Fatal(err)
	}
	path, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(st.DataDir())}, "", rel)
	if !ok {
		t.Fatal("promoted path did not resolve")
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != "legacy-bytes" {
		t.Fatalf("promoted media missing: %q, %v", got, err)
	}
}

func TestMissingAttachmentRejectsNewMessage(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	writeArchiveFixture(t, root, "Solo", archiveFirst)
	run, err := Run(context.Background(), st, Options{ArchiveRoot: root, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err == nil || run.Errors != 1 {
		t.Fatalf("missing media returned run=%+v, err=%v", run, err)
	}
	if n, _ := st.CountMessages(context.Background()); n != 0 {
		t.Fatalf("message imported without media: %d", n)
	}
}

func TestMissingSourceDoesNotAcceptCorruptRetainedMedia(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	media := filepath.Join(root, "attachments", "AB", "photo.jpg")
	if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(media, []byte("original"), 0o600); err != nil {
		t.Fatal(err)
	}
	writeArchiveFixture(t, root, "Solo", archiveFirst)
	runArchiveFixture(t, st, root)
	var rel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments LIMIT 1`).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	path, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(st.DataDir())}, "", rel)
	if !ok {
		t.Fatal("retained media did not resolve")
	}
	if err := os.WriteFile(path, []byte("damaged"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(media); err != nil {
		t.Fatal(err)
	}
	run, err := Run(context.Background(), st, Options{ArchiveRoot: root, Full: true, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err == nil || run.Errors != 1 {
		t.Fatalf("corrupt retained media returned run=%+v, err=%v", run, err)
	}
}
