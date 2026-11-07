package imessage

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/store"
)

func TestExclusionsBeforeRetentionAndAppend(t *testing.T) {
	root := t.TempDir()
	data := t.TempDir()
	writeExport := func(name, body string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name+".txt"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeExport("75243", "May 20, 2020  9:10:11 AM\n75243\nYour verification code is 123456\nattachments/AB/CD/secret.jpeg\n\n")
	writeExport("clinic@example.com", "May 20, 2020  9:12:00 AM\nclinic@example.com\nAppointment reminder tomorrow\n\nMay 20, 2020  9:13:00 AM\nclinic@example.com\nHow are you?\n\n")
	writeExport("+15551234567", "May 20, 2020  9:14:00 AM\n+15551234567\nHello from an unsaved phone number\n\n")
	writeExport("Friend", "May 20, 2020  9:15:00 AM\nFriend\nAppointment reminder tomorrow\n\n")

	policy := ExclusionPolicy{ShortCodes: true, EmailReminders: true,
		Rules: []config.IMessageExclusion{{Name: "friend-reminder", Conversation: "Friend", BodyContains: "appointment reminder"}}}
	preview, err := PreviewExclusions(root, policy)
	if err != nil {
		t.Fatal(err)
	}
	if preview.Messages != 5 || preview.Excluded != 3 || preview.Eligible != 2 ||
		preview.ByRule[ShortCodeRule] != 1 || preview.ByRule[EmailReminderRule] != 1 ||
		preview.ByRule["friend-reminder"] != 1 {
		t.Fatalf("unexpected preview counts: %+v", preview)
	}
	if _, err := os.Stat(filepath.Join(data, "msgbrowse.sqlite")); !os.IsNotExist(err) {
		t.Fatalf("preview wrote archive DB: %v", err)
	}

	st, err := store.Open(filepath.Join(data, store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	ctx := context.Background()
	first, err := Run(ctx, st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || first.MessagesAdded != 2 {
		t.Fatalf("first import: %+v, %v", first, err)
	}
	if got := scalar(t, st, `SELECT count(*) FROM attachments`); got != 0 {
		t.Fatalf("excluded attachment imported: %d", got)
	}
	if got := scalar(t, st, `SELECT count(*) FROM conversations WHERE source='imessage'`); got != 2 {
		t.Fatalf("wholly excluded conversations appeared in browser: %d", got)
	}
	if _, err := os.Stat(filepath.Join(data, "media", "imessage")); !os.IsNotExist(err) {
		t.Fatalf("excluded attachment copied: %v", err)
	}
	second, err := Run(ctx, st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || second.MessagesAdded != 0 {
		t.Fatalf("repeat import: %+v, %v", second, err)
	}
	// A later export omits a retained message. The archive must keep it.
	writeExport("+15551234567", "")
	third, err := Run(ctx, st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || third.MessagesTotal != 2 {
		t.Fatalf("source deletion import: %+v, %v", third, err)
	}
	// Removing one rule rescans an unchanged TXT and adds newly eligible rows.
	policy.EmailReminders = false
	fourth, err := Run(ctx, st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || fourth.MessagesAdded != 1 || fourth.MessagesTotal != 3 {
		t.Fatalf("policy change import: %+v, %v", fourth, err)
	}
}

func TestInvalidExclusionRule(t *testing.T) {
	if err := ValidateExclusions(ExclusionPolicy{Rules: []config.IMessageExclusion{{Name: "bad"}}}); err == nil {
		t.Fatal("wanted missing conversation error")
	}
}
