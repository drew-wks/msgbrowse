package store

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
)

func TestIChatReviewGUIDPreventsLaterExporterDuplicate(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()
	reviewID, err := st.UpsertConversation(ctx, source.IMessage, "iChat review (unassigned)")
	if err != nil {
		t.Fatal(err)
	}
	realID, err := st.UpsertConversation(ctx, source.IMessage, "+15551234567")
	if err != nil {
		t.Fatal(err)
	}
	guid := "AAAAAAAA-0000-0000-0000-000000000001"
	hash := sha256.Sum256([]byte("imessage-guid\x00" + guid))
	_, err = st.db.ExecContext(ctx, `INSERT INTO messages
		(hash,conversation_id,source,source_guid,ts,ts_unix,sender,body,is_system,seq)
		VALUES(?,?,?,?,?,?,?,?,0,0)`, hex.EncodeToString(hash[:]), reviewID,
		source.IMessage, guid, "2022-01-01 10:00:00", int64(1641031200),
		"+15551234567", "source text")
	if err != nil {
		t.Fatal(err)
	}
	ts, err := time.Parse(signal.TimestampLayout, "2022-01-01 10:00:00")
	if err != nil {
		t.Fatal(err)
	}
	added, err := st.AppendIMessageMessages(ctx, realID, []signal.Message{{
		Conversation: "+15551234567", Timestamp: ts,
		TimestampRaw: ts.Format(signal.TimestampLayout),
		Sender:       "+15551234567", Body: "different exporter representation",
		SourceGUID: guid,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if added != 0 {
		t.Fatalf("later exporter added %d messages for an existing GUID", added)
	}
	var count int
	var conversationID int64
	var body string
	if err := st.db.QueryRowContext(ctx, `SELECT count(*),min(conversation_id),min(body)
		FROM messages WHERE source='imessage' AND source_guid=?`, guid).
		Scan(&count, &conversationID, &body); err != nil {
		t.Fatal(err)
	}
	if count != 1 || conversationID != reviewID || body != "source text" {
		t.Fatalf("GUID import changed review row: count=%d conversation=%d body=%q",
			count, conversationID, body)
	}
}
