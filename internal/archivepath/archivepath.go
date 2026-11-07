// Package archivepath resolves a stored attachment's relative path to an
// absolute file path inside the correct read-only archive, with path-traversal
// containment. It is shared by the web media handler and the image-transcode
// pipeline so both agree on (and equally protect) the mapping.
package archivepath

import (
	"path/filepath"
	"strings"

	"github.com/joestump/msgbrowse/internal/source"
)

// exportDir mirrors ingest.ExportDir — the signal-export subdirectory holding
// per-conversation folders. Hardcoded here to keep this a dependency-light leaf
// package; it must stay in sync with ingest.ExportDir.
const exportDir = "export"

// Roots bundles the per-source read-only archive roots that Resolve maps
// attachments into. An empty root means the source is not configured, so its
// attachments do not resolve (ok=false) rather than falling back to another
// source's archive.
type Roots struct {
	// Signal is the signal-export archive root (media is per-conversation:
	// <root>/export/<conversation>/<rel>).
	Signal string
	// IMessage is the imessage-exporter output root (flat: <root>/<rel>,
	// rel like "attachments/AB/CD/IMG.HEIC").
	IMessage string
	// IMessagePermanent holds immutable, content-addressed iMessage media.
	IMessagePermanent string
	// WhatsApp is the whatsapp-chat-exporter output root (flat: <root>/<rel>,
	// rel like "Message/Media/<jid>/photo.jpg" — the parser stores RelPaths
	// root-relative, SPEC-0009 REQ-0009-006).
	WhatsApp string
	// NexusPermanent holds retained historical Nexus MMS parts.
	NexusPermanent string
}

// IMessageMediaRoot is the archive-owned media tree beside the SQLite file.
func IMessageMediaRoot(dataDir string) string {
	if dataDir == "" {
		return ""
	}
	return filepath.Join(dataDir, "media", "imessage")
}

// NexusMediaRoot is the archive-owned media tree for historical Nexus MMS.
func NexusMediaRoot(dataDir string) string {
	if dataDir == "" {
		return ""
	}
	return filepath.Join(dataDir, "media", "nexus")
}

// IMessagePermanentPrefix distinguishes retained media from exporter paths.
const IMessagePermanentPrefix = "permanent/"

// Resolve maps an attachment's (source, conversation, rel) to an absolute path
// under the correct archive, returning ok=false on traversal or misconfig:
//
//   - signal:   <roots.Signal>/export/<conversation>/<rel>
//   - imessage: <roots.IMessage>/<rel>  (flat export; rel like "attachments/…")
//   - whatsapp: <roots.WhatsApp>/<rel>  (flat export; rel like "Message/Media/…")
//   - nexus:    <roots.NexusPermanent>/<rel after permanent/>
//   - legacy_mms, legacy_mms_review: <roots.IMessagePermanent>/<rel after permanent/>
func Resolve(src string, roots Roots, convName, rel string) (string, bool) {
	switch src {
	case source.LegacyMMS, source.LegacyMMSReview:
		if !strings.HasPrefix(rel, IMessagePermanentPrefix) {
			return "", false
		}
		return Contain(roots.IMessagePermanent, strings.TrimPrefix(rel, IMessagePermanentPrefix))
	case source.IMessage:
		if strings.HasPrefix(rel, IMessagePermanentPrefix) {
			return Contain(roots.IMessagePermanent, strings.TrimPrefix(rel, IMessagePermanentPrefix))
		}
		return Contain(roots.IMessage, rel)
	case source.WhatsApp:
		return Contain(roots.WhatsApp, rel)
	case source.Nexus, source.NexusRecovery:
		if !strings.HasPrefix(rel, IMessagePermanentPrefix) {
			return "", false
		}
		return Contain(roots.NexusPermanent, strings.TrimPrefix(rel, IMessagePermanentPrefix))
	default: // signal (and legacy rows with empty source)
		if roots.Signal == "" {
			return "", false
		}
		return Contain(filepath.Join(roots.Signal, exportDir, convName), rel)
	}
}

// Contain resolves rel against base, rejecting empty inputs and any path that
// escapes base. The containment check is lexical against base, so a symlinked
// media dir inside base is still served (its target is not checked).
func Contain(base, rel string) (string, bool) {
	if base == "" || rel == "" {
		return "", false
	}
	cleanRel := filepath.Clean("/" + strings.TrimPrefix(rel, "/"))
	full := filepath.Join(base, cleanRel)
	relCheck, err := filepath.Rel(base, full)
	if err != nil || relCheck == ".." || strings.HasPrefix(relCheck, ".."+string(filepath.Separator)) {
		return "", false
	}
	return full, true
}
