package archivepath

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/joestump/msgbrowse/internal/source"
)

func TestResolve(t *testing.T) {
	roots := Roots{
		Signal:         "/archive",
		IMessage:       "/imsg",
		WhatsApp:       "/wa",
		NexusPermanent: "/nexus",
	}
	cases := []struct {
		name           string
		src, conv, rel string
		want           string // exact expected path (empty = don't check exact)
		ok             bool
		base           string // when set, result must stay under this base (containment)
	}{
		{name: "signal", src: source.Signal, conv: "Harper", rel: "media/cat.jpg", want: "/archive/export/Harper/media/cat.jpg", ok: true},
		{name: "imessage flat", src: source.IMessage, conv: "+1555", rel: "attachments/AB/IMG.HEIC", want: "/imsg/attachments/AB/IMG.HEIC", ok: true},
		// WhatsApp is flat like iMessage: the parser stores RelPaths relative to
		// the whatsapp-chat-exporter output root (SPEC-0009 REQ-0009-006).
		{name: "whatsapp flat", src: source.WhatsApp, conv: "Ada Fixture", rel: "Message/Media/1555@s.whatsapp.net/photo.jpg", want: "/wa/Message/Media/1555@s.whatsapp.net/photo.jpg", ok: true},
		{name: "nexus permanent", src: source.Nexus, rel: "permanent/a.jpg", want: "/nexus/a.jpg", ok: true},
		{name: "nexus export path rejected", src: source.Nexus, rel: "a.jpg", ok: false},
		// Traversal is NOT rejected — it's neutralized to a path contained under base.
		{name: "signal traversal contained", src: source.Signal, conv: "Harper", rel: "../../etc/passwd", ok: true, base: "/archive/export/Harper"},
		{name: "imessage traversal contained", src: source.IMessage, conv: "x", rel: "../../etc/passwd", ok: true, base: "/imsg"},
		{name: "whatsapp traversal contained", src: source.WhatsApp, conv: "x", rel: "../../etc/passwd", ok: true, base: "/wa"},
		{name: "whatsapp deep traversal contained", src: source.WhatsApp, conv: "x", rel: "Message/../../../etc/shadow", ok: true, base: "/wa"},
		{name: "empty rel", src: source.Signal, conv: "Harper", rel: "", ok: false},
		{name: "whatsapp empty rel", src: source.WhatsApp, conv: "x", rel: "", ok: false},
		{name: "unknown source falls to signal", src: "", conv: "Harper", rel: "media/x.png", want: "/archive/export/Harper/media/x.png", ok: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := Resolve(c.src, roots, c.conv, c.rel)
			if ok != c.ok {
				t.Fatalf("ok = %v, want %v (got %q)", ok, c.ok, got)
			}
			if !ok {
				return
			}
			slash := filepath.ToSlash(got)
			if c.want != "" && slash != c.want {
				t.Errorf("Resolve = %q, want %q", slash, c.want)
			}
			if c.base != "" && !strings.HasPrefix(slash, c.base) {
				t.Errorf("traversal escaped base: %q not under %q", slash, c.base)
			}
		})
	}
}

func TestResolveUnsetRootsFail(t *testing.T) {
	if _, ok := Resolve(source.Signal, Roots{IMessage: "/imsg", WhatsApp: "/wa"}, "Harper", "media/x.jpg"); ok {
		t.Error("signal with empty Signal root should fail")
	}
	if _, ok := Resolve(source.IMessage, Roots{Signal: "/archive", WhatsApp: "/wa"}, "x", "attachments/a.jpg"); ok {
		t.Error("imessage with empty IMessage root should fail")
	}
	// An unset whatsapp root must fail closed, never fall back to another
	// source's archive (SPEC-0009 REQ-0009-006).
	if _, ok := Resolve(source.WhatsApp, Roots{Signal: "/archive", IMessage: "/imsg"}, "x", "Message/Media/a.jpg"); ok {
		t.Error("whatsapp with empty WhatsApp root should fail")
	}
}
