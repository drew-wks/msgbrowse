package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
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

// IMessagePreviewAsset is a retained link-card image associated with an
// already-imported iMessage. Kind distinguishes the displayed image, other
// preview images, and an icon without making them ordinary gallery photos.
type IMessagePreviewAsset struct {
	Kind         string
	RelPath      string
	OriginalName string
}

// AppendIMessagePreviewAssets adds preview references to an existing message.
// Repeated runs never duplicate rows or alter previously archived content.
func (s *Store) AppendIMessagePreviewAssets(ctx context.Context, messageID int64, assets []IMessagePreviewAsset) (int, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	var convID, tsUnix int64
	if err := tx.QueryRowContext(ctx, `SELECT conversation_id, ts_unix FROM messages WHERE id=? AND source=?`,
		messageID, source.IMessage).Scan(&convID, &tsUnix); err != nil {
		return 0, fmt.Errorf("find iMessage for preview: %w", err)
	}
	added := 0
	for _, asset := range assets {
		if asset.Kind != "link_preview" && asset.Kind != "link_preview_extra" && asset.Kind != "link_icon" {
			return 0, fmt.Errorf("invalid iMessage preview kind: %q", asset.Kind)
		}
		if !strings.HasPrefix(asset.RelPath, archivepath.IMessagePermanentPrefix) {
			return 0, fmt.Errorf("iMessage preview was not retained: %q", asset.RelPath)
		}
		res, err := tx.ExecContext(ctx, `INSERT INTO attachments(message_id, conversation_id, ts_unix, kind, rel_path, original_name)
			SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS
			(SELECT 1 FROM attachments WHERE message_id=? AND kind=? AND rel_path=?)`,
			messageID, convID, tsUnix, asset.Kind, asset.RelPath, asset.OriginalName,
			messageID, asset.Kind, asset.RelPath)
		if err != nil {
			return 0, fmt.Errorf("append iMessage preview: %w", err)
		}
		n, err := res.RowsAffected()
		if err != nil {
			return 0, err
		}
		added += int(n)
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return added, nil
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

// IMessageAttachmentPaths returns retained paths by original name for a stored
// message, using Apple's GUID when it is known across renamed exports.
func (s *Store) IMessageAttachmentPaths(ctx context.Context, hash, guid string) (map[string][]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT a.original_name, a.rel_path FROM attachments a
		JOIN messages m ON m.id = a.message_id WHERE m.id IN (
			SELECT id FROM messages WHERE source = ? AND source_guid = ?
			UNION SELECT id FROM messages WHERE source = ? AND hash = ?
			AND (source_guid IS NULL OR ? = ''))`,
		source.IMessage, guid, source.IMessage, hash, guid)
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
		var mid, actualConv int64
		var storedHash string
		var storedGUID sql.NullString
		lookupErr := sql.ErrNoRows
		if m.SourceGUID != "" {
			lookupErr = tx.QueryRowContext(ctx, `SELECT id, conversation_id, hash, source_guid FROM messages
				WHERE source = ? AND source_guid = ?`, source.IMessage, m.SourceGUID).Scan(&mid, &actualConv, &storedHash, &storedGUID)
			if lookupErr != nil && !errors.Is(lookupErr, sql.ErrNoRows) {
				return 0, lookupErr
			}
		}
		if errors.Is(lookupErr, sql.ErrNoRows) {
			lookupErr = tx.QueryRowContext(ctx, `SELECT id, conversation_id, hash, source_guid FROM messages
				WHERE source = ? AND hash = ?`, source.IMessage, hash).Scan(&mid, &actualConv, &storedHash, &storedGUID)
			if lookupErr != nil && !errors.Is(lookupErr, sql.ErrNoRows) {
				return 0, lookupErr
			}
			if lookupErr == nil && m.SourceGUID != "" && storedGUID.Valid && storedGUID.String != m.SourceGUID {
				// Distinct Apple messages can have identical visible text in one
				// second. Never attach one GUID's media to another GUID's row.
				lookupErr = sql.ErrNoRows
				sum := sha256.Sum256([]byte("imessage-guid\x00" + m.SourceGUID))
				hash = hex.EncodeToString(sum[:])
			}
		}
		if errors.Is(lookupErr, sql.ErrNoRows) {
			if convID == 0 {
				return 0, fmt.Errorf("new iMessage GUID cannot be appended without a conversation")
			}
			res, err := tx.ExecContext(ctx, `INSERT INTO messages(hash, conversation_id, source, source_guid, ts, ts_unix, sender, body, is_system, seq)
				VALUES (?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?)`,
				hash, convID, source.IMessage, m.SourceGUID, m.TimestampRaw, m.Timestamp.Unix(), m.Sender, m.Body, boolToInt(m.IsSystem), m.Seq)
			if err != nil {
				return 0, fmt.Errorf("append imessage %s: %w", hash, err)
			}
			mid, err = res.LastInsertId()
			if err != nil {
				return 0, err
			}
			actualConv, storedHash = convID, hash
			added++
		} else if m.SourceGUID != "" {
			if _, err := tx.ExecContext(ctx, `UPDATE messages SET source_guid = ?
				WHERE id = ? AND source_guid IS NULL`, m.SourceGUID, mid); err != nil {
				return 0, fmt.Errorf("record iMessage GUID: %w", err)
			}
		}
		if m.SourceGUID == "" && actualConv != convID {
			return 0, fmt.Errorf("iMessage identity collision across conversations: %s", hash)
		}
		for _, a := range m.Attachments {
			if !strings.HasPrefix(a.RelPath, archivepath.IMessagePermanentPrefix) {
				return 0, fmt.Errorf("iMessage attachment was not retained: %q", a.RelPath)
			}
			if _, err := tx.ExecContext(ctx, `INSERT INTO attachments(message_id, conversation_id, ts_unix, kind, rel_path, original_name)
				SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS
				(SELECT 1 FROM attachments WHERE message_id = ? AND rel_path = ? AND kind = ?)`,
				mid, actualConv, m.Timestamp.Unix(), string(a.Kind), a.RelPath, a.OriginalName,
				mid, a.RelPath, string(a.Kind)); err != nil {
				return 0, fmt.Errorf("append iMessage attachment: %w", err)
			}
		}
		for _, l := range m.Links {
			if _, err := tx.ExecContext(ctx, `INSERT INTO links(message_id, conversation_id, ts_unix, url, domain)
				SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM links WHERE message_id = ? AND url = ?)`,
				mid, actualConv, m.Timestamp.Unix(), l.URL, signal.Domain(l.URL), mid, l.URL); err != nil {
				return 0, fmt.Errorf("append iMessage link: %w", err)
			}
		}
		for _, r := range m.Reactions {
			if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO reactions(conversation_id, message_hash, source, emoji, actor)
				VALUES (?, ?, ?, ?, ?)`, actualConv, storedHash, source.IMessage, r.Emoji, r.Actor); err != nil {
				return 0, fmt.Errorf("append iMessage reaction: %w", err)
			}
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return added, nil
}

// IMessageSourceAsset records a native Apple attachment independently of
// exporter TXT. Hidden assets are retained as provenance but not shown in the
// ordinary media gallery until their format and role are established.
type IMessageSourceAsset struct {
	SourceAttachmentGUID string
	RelPath              string
	OriginalName         string
	Kind                 signal.AttachmentKind
	Hidden               bool
}

// ObserveIMessageAttachment catalogs a native attachment GUID even when its
// source file is absent. Each distinct metadata observation is append-only.
func (s *Store) ObserveIMessageAttachment(ctx context.Context, messageID int64, guid string, hidden bool, mimeType string) error {
	if guid == "" {
		return fmt.Errorf("native attachment GUID is empty")
	}
	_, err := s.db.ExecContext(ctx, `INSERT OR IGNORE INTO imessage_attachment_observations
		(message_id, source_attachment_guid, hidden, mime_type) VALUES (?, ?, ?, ?)`,
		messageID, guid, boolToInt(hidden), mimeType)
	return err
}

// IMessageAttachmentCoverage reports the distinct source attachment GUIDs
// observed for archived messages and which still lack retained source bytes.
type IMessageAttachmentCoverage struct {
	Observed       int
	Available      int
	Unavailable    int
	VisibleMissing int
}

func (s *Store) IMessageAttachmentCoverage(ctx context.Context) (IMessageAttachmentCoverage, error) {
	var out IMessageAttachmentCoverage
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(DISTINCT source_attachment_guid)
		FROM imessage_attachment_observations`).Scan(&out.Observed); err != nil {
		return out, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(DISTINCT o.source_attachment_guid)
		FROM imessage_attachment_observations o WHERE EXISTS
		(SELECT 1 FROM imessage_source_assets a
		 WHERE a.source_attachment_guid=o.source_attachment_guid)`).Scan(&out.Available); err != nil {
		return out, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(DISTINCT o.source_attachment_guid)
		FROM imessage_attachment_observations o WHERE o.hidden=0 AND NOT EXISTS
		(SELECT 1 FROM imessage_source_assets a
		 WHERE a.source_attachment_guid=o.source_attachment_guid)`).Scan(&out.VisibleMissing); err != nil {
		return out, err
	}
	out.Unavailable = out.Observed - out.Available
	return out, nil
}

// AppendIMessageSourceAsset appends a verified, archive-owned file to an
// already archived Apple message. It never changes an existing message or file.
func (s *Store) AppendIMessageSourceAsset(ctx context.Context, messageID int64, asset IMessageSourceAsset) (int, int, error) {
	if asset.SourceAttachmentGUID == "" || !strings.HasPrefix(asset.RelPath, archivepath.IMessagePermanentPrefix) {
		return 0, 0, fmt.Errorf("invalid iMessage source asset identity or retained path")
	}
	if asset.Kind != signal.KindImage && asset.Kind != signal.KindVideo && asset.Kind != signal.KindFile {
		return 0, 0, fmt.Errorf("invalid iMessage source asset kind")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, 0, err
	}
	defer func() { _ = tx.Rollback() }()
	var convID, tsUnix int64
	if err := tx.QueryRowContext(ctx, `SELECT conversation_id, ts_unix FROM messages WHERE id=? AND source=?`,
		messageID, source.IMessage).Scan(&convID, &tsUnix); err != nil {
		return 0, 0, err
	}
	res, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO imessage_source_assets
		(message_id, source_attachment_guid, rel_path, hidden, kind, original_name)
		VALUES (?, ?, ?, ?, ?, ?)`, messageID, asset.SourceAttachmentGUID, asset.RelPath,
		boolToInt(asset.Hidden), string(asset.Kind), asset.OriginalName)
	if err != nil {
		return 0, 0, err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, 0, err
	}
	displayAdded := 0
	if !asset.Hidden {
		shown, err := tx.ExecContext(ctx, `INSERT INTO attachments(message_id, conversation_id, ts_unix, kind, rel_path, original_name)
			SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS
			(SELECT 1 FROM attachments WHERE message_id=? AND kind=? AND rel_path=?)`,
			messageID, convID, tsUnix, string(asset.Kind), asset.RelPath, asset.OriginalName,
			messageID, string(asset.Kind), asset.RelPath)
		if err != nil {
			return 0, 0, err
		}
		count, err := shown.RowsAffected()
		if err != nil {
			return 0, 0, err
		}
		displayAdded = int(count)
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, err
	}
	return int(n), displayAdded, nil
}
