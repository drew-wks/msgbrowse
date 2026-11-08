package store

import (
	"context"
	"fmt"
	"strings"
)

// IMessageContentExclusionPrefix marks a conversation-independent TXT fallback.
const IMessageContentExclusionPrefix = "content-v1:"

// IMessageExclusionLedger contains identities explicitly removed by the owner.
// GUIDs are authoritative when an import has one; hashes cover TXT-only input.
type IMessageExclusionLedger struct {
	GUIDs               map[string]struct{}
	Hashes              map[string]struct{}
	ContentFingerprints map[string]struct{}
}

// LoadIMessageExclusionLedger reads durable exclusions before an import pass.
func (s *Store) LoadIMessageExclusionLedger(ctx context.Context) (IMessageExclusionLedger, error) {
	ledger := IMessageExclusionLedger{
		GUIDs:               make(map[string]struct{}),
		Hashes:              make(map[string]struct{}),
		ContentFingerprints: make(map[string]struct{}),
	}
	rows, err := s.db.QueryContext(ctx, `SELECT COALESCE(source_guid, ''), message_hash FROM imessage_excluded_messages`)
	if err != nil {
		return ledger, fmt.Errorf("load iMessage exclusion ledger: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var guid, hash string
		if err := rows.Scan(&guid, &hash); err != nil {
			return ledger, err
		}
		if guid != "" {
			ledger.GUIDs[guid] = struct{}{}
		}
		if fingerprint, ok := strings.CutPrefix(hash, IMessageContentExclusionPrefix); ok {
			ledger.ContentFingerprints[fingerprint] = struct{}{}
		} else {
			ledger.Hashes[hash] = struct{}{}
		}
	}
	return ledger, rows.Err()
}

// IMessageGUIDExclusion carries the exact Apple identity and owner decision.
type IMessageGUIDExclusion struct {
	GUID   string
	Reason string
}

// RecordIMessageGUIDExclusions durably blocks new owner-excluded Apple GUIDs.
// Existing archived messages are left intact for separate checked cleanup.
func (s *Store) RecordIMessageGUIDExclusions(ctx context.Context, guids []IMessageGUIDExclusion) (int, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	added := 0
	for _, item := range guids {
		if item.GUID == "" || item.Reason == "" {
			return 0, fmt.Errorf("invalid Apple GUID or reason in exclusion")
		}
		result, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO imessage_excluded_messages
			(source_guid,message_hash,reason,excluded_at)
			SELECT ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')
			WHERE NOT EXISTS (SELECT 1 FROM messages WHERE source='imessage' AND source_guid=?)`,
			item.GUID, "source-guid-only:"+item.GUID, item.Reason, item.GUID)
		if err != nil {
			return 0, err
		}
		n, err := result.RowsAffected()
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
