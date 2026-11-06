package web

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"image"
	"image/png"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

// TestLinkPreviewCard verifies that the transcript uses locally retained image
// bytes, keeps the copy control, and hides a long URL from visible body text.
func TestLinkPreviewCard(t *testing.T) {
	ctx := context.Background()
	st, cfg, _ := newTestStoreAndConfig(t)
	cfg.DataDir = st.DataDir()
	convID, err := st.UpsertConversation(ctx, source.IMessage, "Preview Example")
	if err != nil {
		t.Fatal(err)
	}
	when := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	u := "https://www.tiktok.com/@example/video/123?tracking=verylong"
	m := signal.Message{
		Conversation: "Preview Example", Timestamp: when, TimestampRaw: when.Format(signal.TimestampLayout),
		Sender: "Me", Body: u + "\nTikTok · Example", Links: []signal.Link{{URL: u}},
	}
	if n, err := st.AppendIMessageMessages(ctx, convID, []signal.Message{m}); err != nil || n != 1 {
		t.Fatalf("append message: %d, %v", n, err)
	}
	var imageData bytes.Buffer
	if err := png.Encode(&imageData, image.NewRGBA(image.Rect(0, 0, 2, 2))); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(imageData.Bytes())
	hash := hex.EncodeToString(sum[:])
	rel := "permanent/" + hash[:2] + "/" + hash + ".png"
	path := filepath.Join(archivepath.IMessageMediaRoot(st.DataDir()), hash[:2], hash+".png")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, imageData.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	page, err := st.GetMessages(ctx, convID, 0, 0, 10, false)
	if err != nil || len(page.Messages) != 1 {
		t.Fatalf("get messages: %+v, %v", page, err)
	}
	if _, err := st.AppendIMessagePreviewAssets(ctx, page.Messages[0].ID,
		[]store.IMessagePreviewAsset{{Kind: "link_preview", RelPath: rel, OriginalName: "link-preview.png"}}); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(st, cfg, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	resp := get(t, srv, "/c/"+itoa(convID))
	if resp.Code != http.StatusOK {
		t.Fatalf("transcript status: %d", resp.Code)
	}
	body := resp.Body.String()
	start := strings.Index(body, `<div class="msg-text`)
	if start < 0 {
		t.Fatal("message text not rendered")
	}
	textBody := body[start:]
	if end := strings.Index(textBody, `</div>`); end >= 0 {
		textBody = textBody[:end]
	}
	if strings.Contains(textBody, "tracking=verylong") || !strings.Contains(textBody, "TikTok · Example") {
		t.Fatalf("visible message text shows raw URL or lost title: %q", textBody)
	}
	if !strings.Contains(body, `class="link-preview-image"`) ||
		!strings.Contains(body, `src="`+mediaURL(convID, rel)+`"`) ||
		!strings.Contains(body, `data-copy-value="`+u+`"`) {
		t.Fatal("preview image or full-URL copy control missing")
	}
	media := get(t, srv, mediaURL(convID, rel))
	if media.Code != http.StatusOK || !bytes.Equal(media.Body.Bytes(), imageData.Bytes()) {
		t.Fatalf("offline preview media: status=%d", media.Code)
	}
}
