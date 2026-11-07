package store

import (
	"context"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
)

func TestIMessageConversationTitlesResolveWithoutChangingSenders(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	base, _ := time.Parse(signal.TimestampLayout, "2026-11-01 10:00:00")
	add := func(name string, senders ...string) int64 {
		t.Helper()
		id, err := st.UpsertConversation(ctx, source.IMessage, name)
		if err != nil {
			t.Fatal(err)
		}
		msgs := make([]signal.Message, 0, len(senders))
		for i, sender := range senders {
			ts := base.Add(time.Duration(i) * time.Minute)
			msgs = append(msgs, signal.Message{
				Conversation: name, Timestamp: ts, TimestampRaw: ts.Format(signal.TimestampLayout),
				Sender: sender, Body: "synthetic message",
			})
		}
		if _, err := st.AppendIMessageMessages(ctx, id, msgs); err != nil {
			t.Fatal(err)
		}
		return id
	}
	ana := add("+15551234567", "Ana Smith", "Me")
	bob := add("+15557654321", "Bob Jones")
	group := add("+15551234567, +15557654321, +15559990000", "Ana Smith")
	conflict := add("+15550001111", "Avery Lee", "Morgan Reed")
	jon := add("+15550002222", "+15550002222", "Jon Davis", "JON DAVIS")
	jeb := add("+15550003333", "Jeb and Michelle Branham", "Jeb Branham")
	_ = bob

	rows, err := st.ListConversations(ctx)
	if err != nil {
		t.Fatal(err)
	}
	byID := make(map[int64]ConversationSummary)
	for _, row := range rows {
		byID[row.ID] = row
	}
	if got := byID[ana]; got.Name != "Ana Smith" || got.RawName != "+15551234567" ||
		got.AddressLine != "+15551234567" || got.LastSender != "Me" {
		t.Fatalf("one-to-one title or unchanged preview sender: %+v", got)
	}
	if got := byID[group]; got.Name != "Ana Smith, Bob Jones, +15559990000" ||
		got.AddressLine != "+15551234567, +15557654321, +15559990000" || got.LastSender != "Ana Smith" {
		t.Fatalf("group title or unchanged preview sender: %+v", got)
	}
	if got := byID[conflict]; got.Name != "+15550001111" || got.AddressLine != "" {
		t.Fatalf("conflicting sender names should keep number: %+v", got)
	}
	if got := byID[jon]; got.Name != "JON DAVIS" || got.AddressLine != "+15550002222" || got.LastSender != "JON DAVIS" {
		t.Fatalf("case variant and handle-shaped sender: %+v", got)
	}
	if got := byID[jeb]; got.Name != "Jeb Branham" || got.AddressLine != "+15550003333" || got.LastSender != "Jeb Branham" {
		t.Fatalf("shared-household sender variant: %+v", got)
	}
	for _, id := range []int64{ana, group, conflict, jon, jeb} {
		one, err := st.GetConversationByID(ctx, id)
		if err != nil || one == nil {
			t.Fatalf("get conversation %d: %v", id, err)
		}
		if one.Name != byID[id].Name || one.RawName != byID[id].RawName || one.AddressLine != byID[id].AddressLine {
			t.Fatalf("header and sidebar disagree for %d: header=%+v sidebar=%+v", id, one, byID[id])
		}
	}
}
