package cli

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/imageconv"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

// TestIMessageImportBuildsHEICPreviews covers the single-source CLI path that
// previously imported HEIC originals without creating browser previews.
func TestIMessageImportBuildsHEICPreviews(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)
	t.Setenv("XDG_CONFIG_HOME", root)
	binDir := filepath.Join(root, "bin")
	if err := os.MkdirAll(binDir, 0o700); err != nil {
		t.Fatal(err)
	}
	// The fake local converter makes this test independent of the host OS and
	// verifies that imessage-import actually invokes the conversion stage.
	sips := filepath.Join(binDir, "sips")
	if err := os.WriteFile(sips, []byte("#!/bin/sh\nprintf '\\377\\330\\377\\331' > \"$6\"\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir)
	exportDir := filepath.Join(root, "export")
	dataDir := filepath.Join(root, "data")
	attachment := filepath.Join(exportDir, "attachments", "AB", "CD", "cabin.heic")
	if err := os.MkdirAll(filepath.Dir(attachment), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(attachment, []byte("synthetic-heic-source"), 0o600); err != nil {
		t.Fatal(err)
	}
	transcript := "May 20, 2020  9:15:00 AM\nMJ\nHere's a photo\nattachments/AB/CD/cabin.heic\n"
	if err := os.WriteFile(filepath.Join(exportDir, "MJ.txt"), []byte(transcript), 0o600); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(root, "config.yaml")
	configText := "data_dir: " + dataDir + "\nimessage_archive_root: " + exportDir + "\n"
	if err := os.WriteFile(configPath, []byte(configText), 0o600); err != nil {
		t.Fatal(err)
	}
	run := func() string {
		t.Helper()
		var out bytes.Buffer
		cmd := NewRootCommand()
		cmd.SetOut(&out)
		cmd.SetErr(&out)
		cmd.SetArgs([]string{"--config", configPath, "imessage-import"})
		if err := cmd.Execute(); err != nil {
			t.Fatal(err)
		}
		return out.String()
	}
	if out := run(); !strings.Contains(out, "media: 1 converted") {
		t.Fatalf("first import did not create HEIC preview: %s", out)
	}
	st, err := store.OpenReadOnly(filepath.Join(dataDir, store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	var rel string
	if err := st.DB().QueryRowContext(context.Background(), "SELECT rel_path FROM attachments WHERE kind='image'").Scan(&rel); err != nil {
		t.Fatal(err)
	}
	abs, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{
		IMessagePermanent: archivepath.IMessageMediaRoot(dataDir),
	}, "", rel)
	if !ok {
		t.Fatal("retained HEIC path did not resolve")
	}
	preview := imageconv.DerivedPath(imageconv.DerivedDir(dataDir), abs)
	data, err := os.ReadFile(preview)
	if err != nil || !bytes.HasPrefix(data, []byte{0xff, 0xd8, 0xff}) {
		t.Fatalf("missing JPEG derivative: %v", err)
	}
	if out := run(); !strings.Contains(out, "media: 0 converted, 1 cached") {
		t.Fatalf("repeat import did not reuse HEIC preview: %s", out)
	}
}
