package store

import (
	"context"
	"fmt"
	"strings"

	"github.com/joestump/msgbrowse/internal/contacts"
	"github.com/joestump/msgbrowse/internal/source"
)

type imessageConversationName struct {
	raw         string
	contactName string
	group       bool
}

// conversationHandleKey returns a comparable phone or email identity.
func conversationHandleKey(raw string) string {
	id := contacts.Normalize(raw)
	if id.Kind != contacts.KindPhone && id.Kind != contacts.KindEmail {
		return ""
	}
	return string(id.Kind) + "\x00" + id.Value
}

// usableConversationPersonName rejects handles and group lists as person labels.
func usableConversationPersonName(name string) bool {
	name = strings.TrimSpace(name)
	return name != "" && !strings.EqualFold(name, "Me") &&
		conversationHandleKey(name) == "" && !contacts.IsMultiRecipientName(name)
}

// compatibleConversationPersonNames allows exporter labels that retain the
// same first and last name, including capitalization and shared-household
// variants. Different people still leave the conversation address visible.
func compatibleConversationPersonNames(a, b string) bool {
	left, right := strings.Fields(a), strings.Fields(b)
	return len(left) >= 2 && len(right) >= 2 &&
		strings.EqualFold(left[0], right[0]) &&
		strings.EqualFold(left[len(left)-1], right[len(right)-1])
}

// decorateIMessageConversationNames resolves titles from the archive's own
// contact and sender evidence. Conflicting labels leave the source handle in
// place; it never changes stored conversation or sender identities.
func (s *Store) decorateIMessageConversationNames(ctx context.Context, summaries []ConversationSummary) error {
	needed := false
	for i := range summaries {
		summaries[i].RawName = summaries[i].Name
		if summaries[i].Source == source.IMessage {
			needed = true
		}
	}
	if !needed {
		return nil
	}

	rows, err := s.db.QueryContext(ctx, `SELECT c.id, c.name, c.is_group, COALESCE(ct.display_name, '')
		FROM conversations c LEFT JOIN contacts ct ON ct.id=c.contact_id
		WHERE c.source=?`, source.IMessage)
	if err != nil {
		return fmt.Errorf("load iMessage conversation names: %w", err)
	}
	metadata := make(map[int64]imessageConversationName)
	for rows.Next() {
		var id int64
		var name imessageConversationName
		if err := rows.Scan(&id, &name.raw, &name.group, &name.contactName); err != nil {
			rows.Close()
			return err
		}
		metadata[id] = name
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()

	// Sender labels come from the export itself. Use the most recent label
	// only when variants retain the same person's first and last name; a
	// group sender cannot identify a particular phone in its participant list.
	senderRows, err := s.db.QueryContext(ctx, `SELECT m.conversation_id, m.sender, MAX(m.ts_unix)
		FROM messages m JOIN conversations c ON c.id=m.conversation_id
		WHERE c.source=? AND m.sender<>'Me' AND m.is_system=0
		GROUP BY m.conversation_id, m.sender`, source.IMessage)
	if err != nil {
		return fmt.Errorf("load iMessage sender labels: %w", err)
	}
	senderName := make(map[int64]string)
	senderLatest := make(map[int64]int64)
	ambiguousSender := make(map[int64]bool)
	for senderRows.Next() {
		var id int64
		var sender string
		var latest int64
		if err := senderRows.Scan(&id, &sender, &latest); err != nil {
			senderRows.Close()
			return err
		}
		meta := metadata[id]
		if meta.group || conversationHandleKey(meta.raw) == "" || !usableConversationPersonName(sender) {
			continue
		}
		sender = strings.TrimSpace(sender)
		if previous := senderName[id]; previous != "" && !compatibleConversationPersonNames(previous, sender) {
			ambiguousSender[id] = true
		}
		if latest > senderLatest[id] || senderName[id] == "" ||
			(latest == senderLatest[id] && sender < senderName[id]) {
			senderName[id] = sender
			senderLatest[id] = latest
		}
	}
	if err := senderRows.Err(); err != nil {
		senderRows.Close()
		return err
	}
	senderRows.Close()

	personByConversation := make(map[int64]string)
	for id, meta := range metadata {
		if meta.group || conversationHandleKey(meta.raw) == "" {
			continue
		}
		if usableConversationPersonName(meta.contactName) {
			personByConversation[id] = strings.TrimSpace(meta.contactName)
		} else if !ambiguousSender[id] {
			personByConversation[id] = senderName[id]
		}
	}
	personByHandle := make(map[string]string)
	ambiguousHandle := make(map[string]bool)
	for id, person := range personByConversation {
		if person == "" {
			continue
		}
		key := conversationHandleKey(metadata[id].raw)
		if previous := personByHandle[key]; previous != "" && previous != person {
			ambiguousHandle[key] = true
		}
		personByHandle[key] = person
	}

	for i := range summaries {
		cs := &summaries[i]
		if cs.Source != source.IMessage {
			continue
		}
		meta, ok := metadata[cs.ID]
		if !ok {
			continue
		}
		cs.RawName = meta.raw
		if cs.Name != meta.raw {
			continue // a linked contact already supplied a deliberate title
		}
		if name := personByConversation[cs.ID]; name != "" {
			cs.Name = name
			continue
		}
		if !contacts.IsMultiRecipientName(meta.raw) {
			continue
		}
		parts := strings.Split(strings.ReplaceAll(meta.raw, "; ", ", "), ", ")
		changed := false
		for j, part := range parts {
			key := conversationHandleKey(strings.TrimSpace(part))
			if key == "" || ambiguousHandle[key] || personByHandle[key] == "" {
				continue
			}
			parts[j] = personByHandle[key]
			changed = true
		}
		if changed {
			cs.Name = strings.Join(parts, ", ")
		}
	}
	for i := range summaries {
		cs := &summaries[i]
		if cs.Source == source.IMessage && cs.Name != cs.RawName &&
			(conversationHandleKey(cs.RawName) != "" || contacts.IsMultiRecipientName(cs.RawName)) {
			cs.AddressLine = cs.RawName
		}
	}
	return nil
}
