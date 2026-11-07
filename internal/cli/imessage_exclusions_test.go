package cli

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joestump/msgbrowse/internal/store"
)

func TestIMessageExclusionPreviewDoesNotCreateArchive(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)
	t.Setenv("XDG_CONFIG_HOME", root)
	exportDir := filepath.Join(root, "export")
	dataDir := filepath.Join(root, "data")
	if err := os.MkdirAll(exportDir, 0o700); err != nil {
		t.Fatal(err)
	}
	transcript := "May 20, 2020  9:10:11 AM\n75243\nYour code is 123456\n\n"
	if err := os.WriteFile(filepath.Join(exportDir, "75243.txt"), []byte(transcript), 0o600); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(root, "config.yaml")
	configText := "data_dir: " + dataDir + "\nimessage_archive_root: " + exportDir +
		"\nimessage_exclude_short_codes: true\n"
	if err := os.WriteFile(configPath, []byte(configText), 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	cmd := NewRootCommand()
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs([]string{"--config", configPath, "imessage-import", "--preview-exclusions"})
	if err := cmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "1 excluded, 0 eligible") ||
		!strings.Contains(out.String(), "short_code_sender: 1 excluded") {
		t.Fatalf("unexpected preview: %s", out.String())
	}
	if _, err := os.Stat(filepath.Join(dataDir, store.DBFileName)); !os.IsNotExist(err) {
		t.Fatalf("preview wrote DB: %v", err)
	}
}
