package onboardsvc

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/onboard"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

func TestIMessageProvidersRefreshRetainsOldMedia(t *testing.T) {
	dataDir := t.TempDir()
	st, err := store.Open(filepath.Join(dataDir, store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	runs := 0
	export := func(_ context.Context, _ string, _ []string, args ...string) (string, error) {
		runs++
		root := args[len(args)-1]
		if err := os.MkdirAll(root, 0o700); err != nil {
			return "", err
		}
		body := "May 20, 2020  9:02:00 AM\nMe\nnew after refresh\n"
		if runs == 1 {
			body = "May 20, 2020  9:00:00 AM\nMe\nold message\nattachments/AB/photo.jpg\n"
			media := filepath.Join(root, "attachments", "AB", "photo.jpg")
			if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
				return "", err
			}
			if err := os.WriteFile(media, []byte("provider-photo"), 0o600); err != nil {
				return "", err
			}
		}
		return "", os.WriteFile(filepath.Join(root, "Solo.txt"), []byte(body), 0o600)
	}
	runner, err := onboard.NewRunner(onboard.Config{
		Resolver: staticResolver{}, Exec: export,
		Importer: &storeImporter{st: st, cfg: &config.Config{DataDir: dataDir}, log: discardLogger()},
		DataDir:  dataDir, Logger: discardLogger(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(runner.Shutdown)
	if _, err := runner.Enable(source.IMessage); err != nil {
		t.Fatal(err)
	}
	if p := waitTerminal(t, runner, source.IMessage); p.Phase != onboard.PhaseDone {
		t.Fatalf("enable failed: %v", p.Err)
	}
	// Simulate a pre-retention SQLite row whose only surviving bytes still
	// reside in the managed export that Refresh is about to replace.
	var initialRel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments LIMIT 1`).Scan(&initialRel); err != nil {
		t.Fatal(err)
	}
	initialPath, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(dataDir)}, "", initialRel)
	if !ok {
		t.Fatal("initial retained media did not resolve")
	}
	if err := os.Remove(initialPath); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB().Exec(`UPDATE attachments SET rel_path = ?`, "attachments/AB/photo.jpg"); err != nil {
		t.Fatal(err)
	}
	if _, err := runner.Refresh(source.IMessage); err != nil {
		t.Fatal(err)
	}
	if p := waitTerminal(t, runner, source.IMessage); p.Phase != onboard.PhaseDone {
		t.Fatalf("refresh failed: %v", p.Err)
	}
	if n, _ := st.CountMessages(context.Background()); n != 2 {
		t.Fatalf("refresh deleted old message: count=%d", n)
	}
	var rel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments LIMIT 1`).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(rel, archivepath.IMessagePermanentPrefix) {
		t.Fatalf("media still uses export root: %q", rel)
	}
	path, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(dataDir)}, "", rel)
	if !ok {
		t.Fatal("retained media did not resolve")
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != "provider-photo" {
		t.Fatalf("media lost on refresh: %q, %v", got, err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, "archives", "imessage", "attachments", "AB", "photo.jpg")); !os.IsNotExist(err) {
		t.Fatalf("old managed export was not replaced: %v", err)
	}
}
