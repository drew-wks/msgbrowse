package imessage

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/joestump/msgbrowse/internal/store"
)

const guidOne = "11111111-1111-1111-1111-111111111111"
const guidTwo = "22222222-2222-2222-2222-222222222222"

func writeGUIDHTML(t *testing.T, root, name, guid, date, sender string) {
	t.Helper()
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	s := `<div class="message"><p><span class="timestamp"><a title="Reveal in Messages app" href="sms://open?message-guid=` + guid + `">` + date + `</a></span><span class="sender">` + sender + `</span></p></div>`
	if err := os.WriteFile(filepath.Join(root, name+".html"), []byte(s), 0o600); err != nil {
		t.Fatal(err)
	}
}

func runGUIDFixture(t *testing.T, st *store.Store, txt, html string) store.IngestRun {
	t.Helper()
	var logs bytes.Buffer
	run, err := Run(context.Background(), st, Options{ArchiveRoot: txt, GUIDHTMLRoot: html,
		Logger: slog.New(slog.NewTextHandler(&logs, nil))})
	if err != nil {
		t.Fatalf("GUID import: %v\n%s", err, logs.String())
	}
	return run
}

func TestGUIDUnionAcrossRenamedExportsAndMediaRecovery(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	txtA, htmlA := filepath.Join(root, "a-txt"), filepath.Join(root, "a-html")
	txtB, htmlB := filepath.Join(root, "b-txt"), filepath.Join(root, "b-html")
	date := "May 20, 2020  9:00:00 AM"
	for _, item := range []struct{ dir, name, media, bytes string }{
		{txtA, "Old Name", "first.jpg", "first-bytes"},
		{txtB, "New Name", "second.jpg", "second-bytes"},
	} {
		media := filepath.Join(item.dir, "attachments", item.media)
		if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(media, []byte(item.bytes), 0o600); err != nil {
			t.Fatal(err)
		}
		writeArchiveFixture(t, item.dir, item.name, date+"\nMe\nshared body\nattachments/"+item.media+"\n")
	}
	writeGUIDHTML(t, htmlA, "Old Name", guidOne, date, "Me")
	writeGUIDHTML(t, htmlB, "New Name", guidOne, date, "Me")
	if got := runGUIDFixture(t, st, txtA, htmlA).MessagesAdded; got != 1 {
		t.Fatalf("first import added %d", got)
	}
	if got := runGUIDFixture(t, st, txtB, htmlB).MessagesAdded; got != 0 {
		t.Fatalf("overlap added %d duplicate messages", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM attachments`); got != 2 {
		t.Fatalf("retained media references = %d, want 2", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM conversations WHERE source='imessage'`); got != 1 {
		t.Fatalf("renamed overlap created %d conversation shells", got)
	}
	if err := os.RemoveAll(txtA); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(txtB); err != nil {
		t.Fatal(err)
	}
	if got := scalar(t, st, `SELECT count(*) FROM messages WHERE source='imessage'`); got != 1 {
		t.Fatalf("source deletion changed archive count: %d", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM attachments WHERE rel_path LIKE 'permanent/%'`); got != 2 {
		t.Fatalf("source deletion changed retained media references: %d", got)
	}
}

func TestGUIDPairingFailsOnMissingHTMLMessage(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	txt, htmlRoot := filepath.Join(root, "txt"), filepath.Join(root, "html")
	writeArchiveFixture(t, txt, "Solo", "May 20, 2020  9:00:00 AM\nMe\nfirst\n\nMay 20, 2020  9:01:00 AM\nMe\nsecond\n")
	writeGUIDHTML(t, htmlRoot, "Solo", guidOne, "May 20, 2020  9:00:00 AM", "Me")
	run, err := Run(context.Background(), st, Options{ArchiveRoot: txt, GUIDHTMLRoot: htmlRoot,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err == nil || run.Errors != 1 {
		t.Fatalf("unmatched HTML was accepted: run=%+v err=%v", run, err)
	}
	if got := scalar(t, st, `SELECT count(*) FROM messages`); got != 0 {
		t.Fatalf("partial pairing inserted %d messages", got)
	}
}

func TestDistinctGUIDsWithSameVisibleContentRemainDistinct(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	txt, htmlRoot := filepath.Join(root, "txt"), filepath.Join(root, "html")
	date := "May 20, 2020  9:00:00 AM"
	writeArchiveFixture(t, txt, "Solo", date+"\nMe\nsame\n\n"+date+"\nMe\nsame\n")
	s := `<p><span class="timestamp"><a title="Reveal in Messages app" href="sms://open?message-guid=` + guidOne + `">` + date + `</a></span><span class="sender">Me</span></p>` +
		`<p><span class="timestamp"><a title="Reveal in Messages app" href="sms://open?message-guid=` + guidTwo + `">` + date + `</a></span><span class="sender">Me</span></p>`
	if err := os.MkdirAll(htmlRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(htmlRoot, "Solo.html"), []byte(s), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := runGUIDFixture(t, st, txt, htmlRoot).MessagesAdded; got != 2 {
		t.Fatalf("distinct GUIDs added %d messages, want 2", got)
	}
	if got := runGUIDFixture(t, st, txt, htmlRoot).MessagesAdded; got != 0 {
		t.Fatalf("repeat import added %d messages", got)
	}
}

func TestGUIDFallbackMatchesRenamedHTMLAndSkipsEmptyCandidates(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	txt, htmlRoot := filepath.Join(root, "txt"), filepath.Join(root, "html")
	date := "May 20, 2020  9:00:00 AM"
	writeArchiveFixture(t, txt, "Long TXT Name", date+"\nMe\nhello\n")
	writeGUIDHTML(t, htmlRoot, "Different HTML Name", guidOne, date, "Me")
	if err := os.WriteFile(filepath.Join(htmlRoot, "Empty.html"), []byte("<html></html>"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := runGUIDFixture(t, st, txt, htmlRoot).MessagesAdded; got != 1 {
		t.Fatalf("fallback imported %d messages", got)
	}
}

func TestDifferentGUIDsDoNotMergeOnIdenticalContentAcrossSnapshots(t *testing.T) {
	st := newStore(t)
	root := t.TempDir()
	date := "May 20, 2020  9:00:00 AM"
	for _, item := range []struct {
		label string
		guid  string
	}{
		{"first", guidOne}, {"second", guidTwo},
	} {
		txt := filepath.Join(root, item.label+"-txt")
		htmlRoot := filepath.Join(root, item.label+"-html")
		writeArchiveFixture(t, txt, "Solo", date+"\nMe\nidentical content\n")
		writeGUIDHTML(t, htmlRoot, "Solo", item.guid, date, "Me")
		if got := runGUIDFixture(t, st, txt, htmlRoot).MessagesAdded; got != 1 {
			t.Fatalf("%s snapshot added %d, want 1 distinct GUID", item.label, got)
		}
	}
	if got := scalar(t, st, `SELECT count(distinct source_guid) FROM messages WHERE source='imessage'`); got != 2 {
		t.Fatalf("distinct source GUIDs = %d, want 2", got)
	}
}
