package imessage

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/signal"
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
	writeExport("75243(Plaid)", "May 20, 2020  9:10:12 AM\n75243(Plaid)\nAnother code\n\n")
	writeExport("+15551110000(smsfp)", "May 20, 2020  9:10:13 AM\n+15551110000(smsfp)\nPromotion\n\nMay 20, 2020  9:10:14 AM\nMe\nReply\n\n")
	writeExport("+15552220000(smsft)", "May 20, 2020  9:10:15 AM\n+15552220000(smsft)\nTransaction\n\n")
	writeExport("+15553330000(smsft_fi)", "May 20, 2020  9:10:16 AM\n+15553330000(smsft_fi)\nFinancial transaction\n\n")
	writeExport("clinic@example.com", "May 20, 2020  9:12:00 AM\nclinic@example.com\nAppointment reminder tomorrow\n\nMay 20, 2020  9:13:00 AM\nclinic@example.com\nHow are you?\n\n")
	writeExport("+15551234567", "May 20, 2020  9:14:00 AM\n+15551234567\nHello from an unsaved phone number\n\n")
	writeExport("Friend", "May 20, 2020  9:15:00 AM\nFriend\nAppointment reminder tomorrow\n\n")

	policy := ExclusionPolicy{ShortCodes: true, FilteredSMS: true, EmailReminders: true,
		Rules: []config.IMessageExclusion{{Name: "friend-reminder", Conversation: "Friend", BodyContains: "appointment reminder"}}}
	preview, err := PreviewExclusions(root, policy)
	if err != nil {
		t.Fatal(err)
	}
	if preview.Messages != 10 || preview.Excluded != 8 || preview.Eligible != 2 ||
		preview.ByRule[ShortCodeRule] != 2 || preview.ByRule[FilteredSMSRule] != 4 || preview.ByRule[EmailReminderRule] != 1 ||
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
	if _, err := st.DB().Exec(`INSERT INTO imessage_excluded_messages(message_hash, reason, excluded_at)
		SELECT hash, 'owner_exclusion', '2026-11-07' FROM messages WHERE body='Hello from an unsaved phone number'`); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB().Exec(`DELETE FROM messages WHERE body='Hello from an unsaved phone number'`); err != nil {
		t.Fatal(err)
	}
	writeExport("+15551234567", "May 20, 2020  9:14:00 AM\n+15551234567\nHello from an unsaved phone number\n\nMay 20, 2020  9:16:00 AM\n+15551234567\nNew message\n\n")
	fifth, err := Run(ctx, st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || fifth.MessagesAdded != 1 || fifth.MessagesTotal != 3 {
		t.Fatalf("owner exclusion reimport: %+v, %v", fifth, err)
	}
}

func TestInvalidExclusionRule(t *testing.T) {
	if err := ValidateExclusions(ExclusionPolicy{Rules: []config.IMessageExclusion{{Name: "bad"}}}); err == nil {
		t.Fatal("wanted missing conversation error")
	}
}

func TestAppointmentReminderWithoutEmail(t *testing.T) {
	msgs := []signal.Message{
		{Conversation: "Clinic", Sender: "Clinic", Body: "Appointment reminder for tomorrow"},
		{Conversation: "Friend", Sender: "Me", Body: "I received an appointment reminder"},
		{Conversation: "+15551234567", Sender: "+15551234567", Body: "How are you?"},
	}
	kept, counts := FilterExcluded(msgs, ExclusionPolicy{AppointmentReminders: true})
	if len(kept) != 2 || counts[AppointmentReminderRule] != 1 {
		t.Fatalf("unexpected reminder classification: kept=%d counts=%v", len(kept), counts)
	}
}

func TestOptOutExcludesWholeThreadAndKeepsOrphanedBucket(t *testing.T) {
	for _, word := range []string{"STOP", "quit", " Stop \n"} {
		msgs := []signal.Message{
			{Conversation: "+15551234567", Sender: "+15551234567", Body: "synthetic notice"},
			{Conversation: "+15551234567", Sender: signal.OwnerSender, Body: word},
		}
		kept, counts := FilterExcluded(msgs, ExclusionPolicy{OptOutThreads: true})
		if len(kept) != 0 || counts[OptOutThreadRule] != 2 {
			t.Fatalf("%q should exclude its entire thread: kept=%d counts=%v", word, len(kept), counts)
		}
	}
	for _, msg := range []signal.Message{
		{Conversation: "+15551234567", Sender: signal.OwnerSender, Body: "please stop"},
		{Conversation: "+15551234567", Sender: "+15551234567", Body: "STOP"},
	} {
		kept, _ := FilterExcluded([]signal.Message{msg}, ExclusionPolicy{OptOutThreads: true})
		if len(kept) != 1 {
			t.Fatalf("non-owner or multiword opt-out excluded: %+v", msg)
		}
	}
	orphaned := []signal.Message{{Conversation: "orphaned", Sender: signal.OwnerSender, Body: "stop"},
		{Conversation: "orphaned", Sender: "Someone else", Body: "unrelated"}}
	kept, _ := FilterExcluded(orphaned, ExclusionPolicy{OptOutThreads: true})
	if len(kept) != 2 {
		t.Fatalf("orphaned is not one thread: kept=%d", len(kept))
	}
	// A known blocked thread remains excluded when a later export omits STOP.
	kept, _ = FilterExcluded([]signal.Message{{Conversation: "+15551234567", Sender: "+15551234567", Body: "new notice"}},
		ExclusionPolicy{OptOutThreads: true, Rules: []config.IMessageExclusion{{Name: "known-opt-out", Conversation: "+15551234567"}}})
	if len(kept) != 0 {
		t.Fatal("known opt-out thread reimported after STOP disappeared")
	}
}

func TestOptOutImportRemainsBlockedAfterSourceOmitsReply(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "+15551234567.txt")
	write := func(text string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("May 20, 2020  9:10:11 AM\n+15551234567\nSynthetic promotion\nattachments/missing.jpeg\n\nMay 20, 2020  9:10:12 AM\nMe\nSTOP\n\n")
	st, err := store.Open(filepath.Join(t.TempDir(), store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	policy := ExclusionPolicy{OptOutThreads: true, Rules: []config.IMessageExclusion{{Name: "known-opt-out", Conversation: "+15551234567"}}}
	first, err := Run(context.Background(), st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || first.MessagesAdded != 0 || scalar(t, st, `SELECT COUNT(*) FROM conversations WHERE source='imessage'`) != 0 {
		t.Fatalf("opt-out import retained messages or missing media: %+v, %v", first, err)
	}
	write("May 20, 2020  9:10:11 AM\n+15551234567\nSynthetic promotion\n\nMay 20, 2020  9:10:13 AM\n+15551234567\nNew synthetic promotion\n\n")
	second, err := Run(context.Background(), st, Options{ArchiveRoot: root, Exclusions: policy})
	if err != nil || second.MessagesAdded != 0 || scalar(t, st, `SELECT COUNT(*) FROM messages`) != 0 {
		t.Fatalf("known blocked thread reimported after STOP disappeared: %+v, %v", second, err)
	}
}

func TestLedgerUsesGUIDBeforeHash(t *testing.T) {
	msgs := []signal.Message{
		{Conversation: "Friend", Sender: "Friend", Body: "same text", SourceGUID: "removed"},
		{Conversation: "Friend", Sender: "Friend", Body: "same text", SourceGUID: "different"},
	}
	ledger := store.IMessageExclusionLedger{
		GUIDs:  map[string]struct{}{"removed": {}},
		Hashes: map[string]struct{}{},
	}
	kept, removed := filterLedgerExcluded(msgs, ledger)
	if removed != 1 || len(kept) != 1 || kept[0].SourceGUID != "different" {
		t.Fatalf("ledger suppressed a distinct Apple GUID: removed=%d kept=%+v", removed, kept)
	}
}
