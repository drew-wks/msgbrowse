package imessage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

// These exact private chat.is_filtered values were matched to the owner's
// iPhone screenshots. Other nonzero values are held for review rather than
// being treated as spam; 68 includes the protected Tamar conversation.
var excludedAppleFilterValues = map[int]bool{1: true, 2: true, 3: true, 4: true, 52: true}

// loadAppleFilterValues reads GUID-to-chat flags from the export source only.
// A missing optional column in an older Apple schema leaves no native flags.
func loadAppleFilterValues(ctx context.Context, path string) (map[string]int, bool, error) {
	db, err := openSourceSQLite(path)
	if err != nil {
		return nil, false, fmt.Errorf("read Apple filter source: %w", err)
	}
	defer db.Close()
	columns, err := db.QueryContext(ctx, `PRAGMA table_info(chat)`)
	if err != nil {
		return nil, false, err
	}
	hasColumn := false
	for columns.Next() {
		var id, notNull, primary int
		var name, columnType string
		var defaultValue sql.NullString
		if err := columns.Scan(&id, &name, &columnType, &notNull, &defaultValue, &primary); err != nil {
			columns.Close()
			return nil, false, err
		}
		if name == "is_filtered" {
			hasColumn = true
		}
	}
	if err := columns.Err(); err != nil {
		columns.Close()
		return nil, false, err
	}
	columns.Close()
	query := `SELECT m.guid, 0 FROM message m WHERE m.guid IS NOT NULL`
	if hasColumn {
		query = `SELECT m.guid, COALESCE(c.is_filtered,0) FROM message m
			LEFT JOIN chat_message_join j ON j.message_id=m.ROWID
			LEFT JOIN chat c ON c.ROWID=j.chat_id WHERE m.guid IS NOT NULL`
	}
	rows, err := db.QueryContext(ctx, query)
	if err != nil {
		return nil, false, fmt.Errorf("read Apple message filter values: %w", err)
	}
	defer rows.Close()
	values := make(map[string]int)
	for rows.Next() {
		var guid string
		var value int
		if err := rows.Scan(&guid, &value); err != nil {
			return nil, false, err
		}
		if previous, found := values[guid]; found && previous != value {
			return nil, false, fmt.Errorf("Apple GUID has conflicting chat filter values")
		}
		values[guid] = value
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	return values, hasColumn, nil
}

// filterAppleValues blocks screenshot-verified filter values before media
// retention and records GUID guards. Unrecognized nonzero flags cannot silently
// import a new message; previously archived rows remain available for recovery.
func filterAppleValues(ctx context.Context, st *store.Store, msgs []signal.Message, values map[string]int) ([]signal.Message, int, error) {
	kept := make([]signal.Message, 0, len(msgs))
	blocked := make([]store.IMessageGUIDExclusion, 0)
	for _, msg := range msgs {
		value, found := values[msg.SourceGUID]
		if !found || msg.SourceGUID == "" {
			return nil, 0, fmt.Errorf("paired Apple GUID is absent from filter source; check that TXT, HTML, and database are one snapshot")
		}
		if excludedAppleFilterValues[value] {
			blocked = append(blocked, store.IMessageGUIDExclusion{
				GUID: msg.SourceGUID, Reason: fmt.Sprintf("owner_apple_filter_value_%d", value),
			})
			continue
		}
		if value != 0 {
			var id int64
			err := st.DB().QueryRowContext(ctx, `SELECT id FROM messages WHERE source=? AND source_guid=?`,
				source.IMessage, msg.SourceGUID).Scan(&id)
			if errors.Is(err, sql.ErrNoRows) {
				return nil, 0, fmt.Errorf("unreviewed Apple filter value %d for a new GUID; import held", value)
			}
			if err != nil {
				return nil, 0, err
			}
		}
		kept = append(kept, msg)
	}
	if len(blocked) > 0 {
		if _, err := st.RecordIMessageGUIDExclusions(ctx, blocked); err != nil {
			return nil, 0, err
		}
	}
	return kept, len(blocked), nil
}

// blockedHandleKey normalizes an exact phone, short code, or email sender.
// Configured international phones must include +; bare ten-digit Apple handles
// are interpreted as US numbers only when matching a configured +1 number.
func blockedHandleKey(value string, configured bool) (string, bool) {
	value = strings.TrimSpace(value)
	if emailAddressRe.MatchString(value) {
		return "email:" + strings.ToLower(value), true
	}
	if len(value) >= 4 && len(value) <= 6 && onlyDigits(value) == value {
		return "short:" + value, true
	}
	if !strings.HasPrefix(value, "+") && configured {
		return "", false
	}
	for i, r := range value {
		if r >= '0' && r <= '9' || i == 0 && r == '+' || r == ' ' || r == '(' || r == ')' || r == '-' {
			continue
		}
		return "", false
	}
	digits := onlyDigits(value)
	if !configured && !strings.HasPrefix(value, "+") && len(digits) == 10 {
		digits = "1" + digits
	}
	if len(digits) < 8 || len(digits) > 15 {
		return "", false
	}
	return "phone:" + digits, true
}

// loadBlockedAppleSenders finds exact source-handle GUIDs in the same
// read-only database used for GUID pairing.
func loadBlockedAppleSenders(ctx context.Context, path string, senders []string) (map[string]string, error) {
	db, err := openSourceSQLite(path)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	wanted := make(map[string]bool, len(senders))
	for _, sender := range senders {
		key, valid := blockedHandleKey(sender, true)
		if !valid || wanted[key] {
			return nil, fmt.Errorf("blocked Apple sender must be a distinct exact phone, short code, or email")
		}
		wanted[key] = true
	}
	rows, err := db.QueryContext(ctx, `SELECT ROWID,id FROM handle`)
	if err != nil {
		return nil, fmt.Errorf("Apple source has no sender handles: %w", err)
	}
	defer rows.Close()
	blocked := make(map[string]string)
	for rows.Next() {
		var id int64
		var handle string
		if err := rows.Scan(&id, &handle); err != nil {
			return nil, err
		}
		key, valid := blockedHandleKey(handle, false)
		if !valid || !wanted[key] {
			continue
		}
		guids, err := db.QueryContext(ctx,
			`SELECT guid FROM message WHERE handle_id=? AND guid IS NOT NULL`, id)
		if err != nil {
			return nil, err
		}
		for guids.Next() {
			var guid string
			if err := guids.Scan(&guid); err != nil {
				guids.Close()
				return nil, err
			}
			if previous, found := blocked[guid]; found && previous != key {
				guids.Close()
				return nil, fmt.Errorf("Apple GUID has conflicting blocked sender handles")
			}
			blocked[guid] = key
		}
		if err := guids.Err(); err != nil {
			guids.Close()
			return nil, err
		}
		guids.Close()
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return blocked, nil
}

// filterBlockedAppleSenders records exact GUIDs before later imports can
// recreate messages from an owner-blocked source handle.
func filterBlockedAppleSenders(ctx context.Context, st *store.Store, msgs []signal.Message, blocked map[string]string) ([]signal.Message, int, error) {
	kept := make([]signal.Message, 0, len(msgs))
	exclusions := make([]store.IMessageGUIDExclusion, 0)
	for _, msg := range msgs {
		if senderKey, found := blocked[msg.SourceGUID]; found {
			exclusions = append(exclusions, store.IMessageGUIDExclusion{
				GUID: msg.SourceGUID, Reason: "owner_blocked_sender_" + senderKey,
			})
			continue
		}
		kept = append(kept, msg)
	}
	if len(exclusions) > 0 {
		if _, err := st.RecordIMessageGUIDExclusions(ctx, exclusions); err != nil {
			return nil, 0, err
		}
	}
	return kept, len(exclusions), nil
}
