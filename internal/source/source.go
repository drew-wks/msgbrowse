// Package source defines the names of the message archive sources msgbrowse
// imports from. Every conversation, message, contact identifier, and ingest run
// is tagged with one of these strings so the unified store can hold data from
// every upstream exporter at once.
//
// New sources must add their constant here and to the migrations layer's
// allowed-source check; the value is persisted to SQLite and never renamed.
package source

// Known sources. Keep the literal values stable — they are written to the
// database `source` columns and to `contact_identifiers.source`.
const (
	// Signal identifies conversations and messages imported from a
	// signal-export Markdown archive.
	Signal = "signal"

	// IMessage identifies conversations and messages imported from an
	// imessage-exporter Markdown archive. Wired up in Slice 2.5.
	IMessage = "imessage"

	// WhatsApp identifies conversations and messages imported from a
	// WhatsApp-Chat-Exporter JSON archive (SPEC-0009).
	WhatsApp = "whatsapp"

	// Nexus identifies historical SMS/MMS copied from Drew's Android Nexus
	// backups. It is an import-only reconstruction source, not a live provider.
	Nexus = "nexus"

	// LegacyMMS identifies MMS reconstructed from Drew's older iPhone sms.db
	// backups. It is import-only and does not represent a live Apple provider.
	LegacyMMS = "legacy_mms"

	// LegacyMMSReview holds recovered MMS with unresolved contact or direction.
	// It is import-only; its attachments use the permanent iMessage media root.
	LegacyMMSReview = "legacy_mms_review"

	// LegacySMSReview holds older SMS texts whose source identity is certain but
	// whose overlap with nearby Apple messages remains for human review.
	LegacySMSReview = "legacy_sms_review"

	// NexusRecovery identifies historical Nexus observations whose conversation
	// identity or cross-source duplicate status still needs owner review.
	NexusRecovery = "nexus_recovery"
)

// All is the canonical list of live provider sources. Import-only historical
// sources do not get exporter, refresh, or disable controls.
var All = []string{Signal, IMessage, WhatsApp}

// IsKnown reports whether s is a recognized live provider source string.
func IsKnown(s string) bool {
	for _, k := range All {
		if k == s {
			return true
		}
	}
	return false
}

// Label returns the human-facing display name for a source string (e.g.
// "iMessage" for the stored "imessage"). Unknown values are returned unchanged
// so callers never render an empty label.
func Label(s string) string {
	switch s {
	case Signal:
		return "Signal"
	case IMessage:
		return "iMessage"
	case WhatsApp:
		return "WhatsApp"
	case Nexus:
		return "Nexus SMS/MMS"
	case LegacyMMS:
		return "Legacy iPhone MMS"
	case LegacyMMSReview:
		return "Legacy iPhone MMS (review)"
	case LegacySMSReview:
		return "Legacy iPhone SMS (review)"
	case NexusRecovery:
		return "Nexus recovery (review)"
	default:
		return s
	}
}
