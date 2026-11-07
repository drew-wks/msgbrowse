package store

import (
	"context"
	"fmt"
)

// IMessageExclusionLedger contains identities explicitly removed by the owner.
// GUIDs are authoritative when an import has one; hashes cover TXT-only input.
type IMessageExclusionLedger struct {
	GUIDs  map[string]struct{}
	Hashes map[string]struct{}
}

// LoadIMessageExclusionLedger reads durable exclusions before an import pass.
func (s *Store) LoadIMessageExclusionLedger(ctx context.Context) (IMessageExclusionLedger, error) {
	ledger := IMessageExclusionLedger{GUIDs: make(map[string]struct{}), Hashes: make(map[string]struct{})}
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
		ledger.Hashes[hash] = struct{}{}
	}
	return ledger, rows.Err()
}
