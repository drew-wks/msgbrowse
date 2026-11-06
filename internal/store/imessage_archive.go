package store

import (
	"context"
	"fmt"
	"strings"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
)

// LegacyIMessageAttachment is a pre-retention attachment reference in SQLite.
type LegacyIMessageAttachment struct {
	ID      int64
	RelPath string
}

// LegacyIMessageAttachments lists iMessage media still pointing at export files.
func (s *Store) LegacyIMessageAttachments(ctx context.Context) ([]LegacyIMessageAttachment, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT a.id, a.rel_path FROM attachments a
		JOIN messages m ON m.id = a.message_id
		WHERE m.source = ? AND a.rel_path NOT LIKE 'permanent/%'`, source.IMessage)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []LegacyIMessageAttachment
	for rows.Next() {
		var a LegacyIMessageAttachment
		if err := rows.Scan(&a.ID, &a.RelPath); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// PromoteIMessageAttachment points one legacy row at a verified retained copy.
func (s *Store) PromoteIMessageAttachment(ctx context.Context, id int64, oldPath, retainedPath string) error {
	if !strings.HasPrefix(retainedPath, archivepath.IMessagePermanentPrefix) {
		return fmt.Errorf("not a retained iMessage path: %q", retainedPath)
	}
	_, err := s.db.ExecContext(ctx, `UPDATE attachments SET rel_path = ? WHERE id = ? AND rel_path = ?`, retainedPath, id, oldPath)
	return err
}

// IMessageAttachmentPaths returns retained paths by original name for a stored message.
func (s *Store) IMessageAttachmentPaths(ctx context.Context, hash string) (map[string][]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT a.original_name, a.rel_path FROM attachments a
		JOIN messages m ON m.id = a.message_id WHERE m.hash = ? AND m.source = ?`, hash, source.IMessage)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string][]string)
	for rows.Next() {
		var name, path string
		if err := rows.Scan(&name, &path); err != nil {
			return nil, err
		}
		out[name] = append(out[name], path)
	}
	return out, rows.Err()
}

// AppendIMessageMessages inserts new iMessages and appends newly observed media
// and reactions without changing existing message content or removing old rows.
func (s *Store) AppendIMessageMessages(ctx context.Context, convID int64, msgs []signal.Message) (int, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	added := 0
	for i := range msgs {
		m := &msgs[i]
		hash := m.HashWithSource(source.IMessage)
		res, err := tx.ExecContext(ctx, `INSERT INTO messages(hash, conversation_id, source, ts, ts_unix, sender, body, is_system, seq)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(hash) DO NOTHING`,
			hash, convID, source.IMessage, m.TimestampRaw, m.Timestamp.Unix(), m.Sender, m.Body, boolToInt(m.IsSystem), m.Seq)
		if err != nil {
			return 0, fmt.Errorf("append imessage %s: %w", hash, err)
		}
		n, err := res.RowsAffected()
		if err != nil {
			return 0, err
		}
		added += int(n)
		var mid, actualConv int64
		if err := tx.QueryRowContext(ctx, `SELECT id, conversation_id FROM messages WHERE hash = ?`, hash).Scan(&mid, &actualConv); err != nil {
			return 0, err
		}
		if actualConv != convID {
			return 0, fmt.Errorf("iMessage identity collision across conversations: %s", hash)
		}
		for _, a := range m.Attachments {
			if !strings.HasPrefix(a.RelPath, archivepath.IMessagePermanentPrefix) {
				return 0, fmt.Errorf("iMessage attachment was not retained: %q", a.RelPath)
			}
			if _, err := tx.ExecContext(ctx, `INSERT INTO attachments(message_id, conversation_id, ts_unix, kind, rel_path, original_name)
				SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS
				(SELECT 1 FROM attachments WHERE message_id = ? AND rel_path = ? AND kind = ?)`,
				mid, convID, m.Timestamp.Unix(), string(a.Kind), a.RelPath, a.OriginalName,
				mid, a.RelPath, string(a.Kind)); err != nil {
				return 0, fmt.Errorf("append iMessage attachment: %w", err)
			}
		}
		for _, l := range m.Links {
			if _, err := tx.ExecContext(ctx, `INSERT INTO links(message_id, conversation_id, ts_unix, url, domain)
				SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM links WHERE message_id = ? AND url = ?)`,
				mid, convID, m.Timestamp.Unix(), l.URL, signal.Domain(l.URL), mid, l.URL); err != nil {
				return 0, fmt.Errorf("append iMessage link: %w", err)
			}
		}
		for _, r := range m.Reactions {
			if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO reactions(conversation_id, message_hash, source, emoji, actor)
				VALUES (?, ?, ?, ?, ?)`, convID, hash, source.IMessage, r.Emoji, r.Actor); err != nil {
				return 0, fmt.Errorf("append iMessage reaction: %w", err)
			}
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return added, nil
}
