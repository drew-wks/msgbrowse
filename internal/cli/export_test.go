package cli

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/ingest"
	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
	"github.com/joestump/msgbrowse/internal/whatsapp"
)

// recordedCall captures one runner invocation so tests can assert on the exact
// command + flags export passed to a tool, without running anything real.
type recordedCall struct {
	name string
	args []string
}

// fakeRunner records every call and returns a scripted error for any tool name
// in failFor. It is the injected seam standing in for execRunner in tests.
type fakeRunner struct {
	calls   []recordedCall
	failFor map[string]error
}

func (f *fakeRunner) run(_ context.Context, name string, args ...string) error {
	f.calls = append(f.calls, recordedCall{name: name, args: append([]string(nil), args...)})
	if f.failFor != nil {
		if err, ok := f.failFor[name]; ok {
			return err
		}
	}
	return nil
}

// callFor returns the first recorded call whose name contains sub (so tests can
// find the sigexport / imessage-exporter call regardless of bin override path).
func (f *fakeRunner) callFor(sub string) (recordedCall, bool) {
	for _, c := range f.calls {
		if strings.Contains(c.name, sub) {
			return c, true
		}
	}
	return recordedCall{}, false
}

// argsEqual reports whether args matches want exactly (order-sensitive).
func argsEqual(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

func TestRunExportIMessageUsesCloneCopyMode(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{IMessageArchiveRoot: "/imsg"}
	// Override the bin so resolveTool doesn't need imessage-exporter on PATH.
	opts := exportOptions{imessageBin: "/usr/bin/imessage-exporter"}

	if err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport: %v", err)
	}
	call, ok := fr.callFor("imessage-exporter")
	if !ok {
		t.Fatalf("imessage-exporter was not invoked; calls=%+v", fr.calls)
	}
	want := []string{"-f", "txt", "-c", "clone", "-o", "/imsg"}
	if !argsEqual(call.args, want) {
		t.Errorf("imessage args = %v, want %v", call.args, want)
	}
	// Signal must NOT run when its root is unset.
	if _, ran := fr.callFor("sigexport"); ran {
		t.Errorf("sigexport ran despite unset archive_root")
	}
}

func TestIMessageExportPreservesLegacyAttachmentBeforeReplacement(t *testing.T) {
	ctx := context.Background()
	dataDir := t.TempDir()
	root := filepath.Join(dataDir, "export")
	media := filepath.Join(root, "attachments", "AB", "photo.jpg")
	if err := os.MkdirAll(filepath.Dir(media), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(media, []byte("legacy-photo"), 0o600); err != nil {
		t.Fatal(err)
	}
	st, err := store.Open(filepath.Join(dataDir, store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	cid, err := st.UpsertConversation(ctx, source.IMessage, "Solo")
	if err != nil {
		t.Fatal(err)
	}
	ts, _ := time.Parse(signal.TimestampLayout, "2020-05-20 09:00:00")
	msg := signal.Message{Conversation: "Solo", Timestamp: ts, TimestampRaw: "2020-05-20 09:00:00", Sender: "Me", Body: "old",
		Attachments: []signal.Attachment{{Kind: signal.KindImage, RelPath: "attachments/AB/photo.jpg", OriginalName: "photo.jpg"}}}
	if _, err := st.ReplaceConversationMessages(ctx, cid, source.IMessage, []signal.Message{msg}); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}
	called := false
	run := func(_ context.Context, _ string, _ ...string) error {
		called = true
		return os.Remove(media)
	}
	cfg := &config.Config{DataDir: dataDir, IMessageArchiveRoot: root}
	if err := exportIMessage(ctx, run, cfg, exportOptions{imessageBin: "imessage-exporter"}); err != nil {
		t.Fatal(err)
	}
	if !called {
		t.Fatal("exporter was not invoked")
	}
	st, err = store.Open(filepath.Join(dataDir, store.DBFileName))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	var rel string
	if err := st.DB().QueryRow(`SELECT rel_path FROM attachments LIMIT 1`).Scan(&rel); err != nil {
		t.Fatal(err)
	}
	path, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: archivepath.IMessageMediaRoot(dataDir)}, "", rel)
	if !ok {
		t.Fatalf("legacy media was not retained: %q", rel)
	}
	if got, err := os.ReadFile(path); err != nil || string(got) != "legacy-photo" {
		t.Fatalf("retained media = %q, %v", got, err)
	}
}

func TestRunExportSignalUsesExportSubdirAsDest(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{ArchiveRoot: "/arch"}
	opts := exportOptions{signalBin: "/usr/local/bin/sigexport"}

	if err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport: %v", err)
	}
	call, ok := fr.callFor("sigexport")
	if !ok {
		t.Fatalf("sigexport was not invoked; calls=%+v", fr.calls)
	}
	// The single positional arg must be <archive_root>/export so chats land at
	// <archive_root>/export/<conv>/chat.md (the layout ingest scans).
	wantDest := filepath.Join("/arch", ingest.ExportDir)
	if !argsEqual(call.args, []string{wantDest}) {
		t.Errorf("sigexport args = %v, want [%q]", call.args, wantDest)
	}
}

func TestRunExportBothSources(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{ArchiveRoot: "/arch", IMessageArchiveRoot: "/imsg"}
	opts := exportOptions{signalBin: "sig", imessageBin: "imsg"}
	out := &bytes.Buffer{}

	if err := runExport(context.Background(), out, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport: %v", err)
	}
	if len(fr.calls) != 2 {
		t.Fatalf("expected 2 calls (both sources), got %d: %+v", len(fr.calls), fr.calls)
	}
	for _, want := range []string{"signal:", "imessage:"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output %q missing %q", out.String(), want)
		}
	}
}

// TestRunExportWhatsAppInvocation pins the wtsexporter contract (SPEC-0009
// REQ-0009-002): output directory AND JSON destination both point into the
// configured root, so result.json + copied media land where import scans.
func TestRunExportWhatsAppInvocation(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{WhatsAppArchiveRoot: "/wapp"}
	opts := exportOptions{whatsappBin: "/venv/bin/wtsexporter"}
	out := &bytes.Buffer{}

	if err := runExport(context.Background(), out, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport: %v", err)
	}
	call, ok := fr.callFor("wtsexporter")
	if !ok {
		t.Fatalf("wtsexporter was not invoked; calls=%+v", fr.calls)
	}
	want := []string{"-o", "/wapp", "-j", filepath.Join("/wapp", whatsapp.ResultFile)}
	if !argsEqual(call.args, want) {
		t.Errorf("whatsapp args = %v, want %v", call.args, want)
	}
	// The other sources must NOT run when their roots are unset.
	if len(fr.calls) != 1 {
		t.Errorf("expected only the WhatsApp call, got %+v", fr.calls)
	}
	if !strings.Contains(out.String(), "whatsapp:") {
		t.Errorf("output %q missing the whatsapp success line", out.String())
	}
}

func TestRunExportAllThreeSources(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{ArchiveRoot: "/arch", IMessageArchiveRoot: "/imsg", WhatsAppArchiveRoot: "/wapp"}
	opts := exportOptions{signalBin: "sig", imessageBin: "imsg", whatsappBin: "wts"}
	out := &bytes.Buffer{}

	if err := runExport(context.Background(), out, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport: %v", err)
	}
	if len(fr.calls) != 3 {
		t.Fatalf("expected 3 calls (all sources), got %d: %+v", len(fr.calls), fr.calls)
	}
	for _, want := range []string{"signal:", "imessage:", "whatsapp:"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output %q missing %q", out.String(), want)
		}
	}
}

func TestRunExportMissingWhatsAppToolIsClearError(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{WhatsAppArchiveRoot: "/wapp"}
	t.Setenv("PATH", "") // force LookPath("wtsexporter") to fail

	err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, exportOptions{})
	if err == nil {
		t.Fatalf("expected an error for missing wtsexporter, got nil")
	}
	msg := err.Error()
	if !strings.Contains(msg, "wtsexporter") {
		t.Errorf("error %q should name the missing tool", msg)
	}
	if !strings.Contains(msg, "pipx install whatsapp-chat-exporter") {
		t.Errorf("error %q should include the pipx install hint", msg)
	}
	if len(fr.calls) != 0 {
		t.Errorf("runner should not be called when the tool is missing; calls=%+v", fr.calls)
	}
}

func TestRunExportMissingRequiredToolIsClearError(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{IMessageArchiveRoot: "/imsg"}
	// No bin override and the default tool name will not be on PATH in CI under a
	// guaranteed-absent name. Use a name that cannot exist to force LookPath fail.
	opts := exportOptions{imessageBin: ""} // forces LookPath("imessage-exporter")

	// Make PATH empty so LookPath of the real default name fails deterministically.
	t.Setenv("PATH", "")

	err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, opts)
	if err == nil {
		t.Fatalf("expected an error for missing imessage-exporter, got nil")
	}
	msg := err.Error()
	if !strings.Contains(msg, "imessage-exporter") {
		t.Errorf("error %q should name the missing tool", msg)
	}
	if !strings.Contains(msg, "brew install imessage-exporter") {
		t.Errorf("error %q should include the install hint", msg)
	}
	if len(fr.calls) != 0 {
		t.Errorf("runner should not be called when the tool is missing; calls=%+v", fr.calls)
	}
}

func TestRunExportBinOverrideIsHonored(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{ArchiveRoot: "/arch", IMessageArchiveRoot: "/imsg"}
	opts := exportOptions{
		signalBin:   "/custom/sigexport-wrapper",
		imessageBin: "/custom/imessage-exporter-wrapper",
	}
	// Even with an empty PATH, the explicit overrides are used verbatim (no
	// LookPath), proving the override path is taken.
	t.Setenv("PATH", "")

	if err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport with overrides: %v", err)
	}
	if c, ok := fr.callFor("sigexport-wrapper"); !ok || c.name != "/custom/sigexport-wrapper" {
		t.Errorf("signal bin override not honored; calls=%+v", fr.calls)
	}
	if c, ok := fr.callFor("imessage-exporter-wrapper"); !ok || c.name != "/custom/imessage-exporter-wrapper" {
		t.Errorf("imessage bin override not honored; calls=%+v", fr.calls)
	}
}

func TestRunExportSkipOnErrorContinuesPastFailingSource(t *testing.T) {
	// Signal fails; with --skip-on-error the run must still invoke iMessage and
	// then exit non-zero (errExportFailures) because a source failed.
	fr := &fakeRunner{failFor: map[string]error{"sig": errors.New("boom")}}
	cfg := &config.Config{ArchiveRoot: "/arch", IMessageArchiveRoot: "/imsg"}
	opts := exportOptions{signalBin: "sig", imessageBin: "imsg", skipOnError: true}
	out := &bytes.Buffer{}

	err := runExport(context.Background(), out, fr.run, cfg, opts)
	if err == nil {
		t.Fatalf("expected non-zero (errExportFailures) when a source failed under skip-on-error")
	}
	if !errors.Is(err, errExportFailures) {
		t.Errorf("error %v should wrap errExportFailures", err)
	}
	// iMessage must have run despite Signal failing.
	if _, ran := fr.callFor("imsg"); !ran {
		t.Errorf("iMessage did not run after Signal failed under --skip-on-error; calls=%+v", fr.calls)
	}
	// iMessage succeeded, so its success line should still be printed.
	if !strings.Contains(out.String(), "imessage:") {
		t.Errorf("expected iMessage success line, got %q", out.String())
	}
}

func TestRunExportFailingSourceAbortsWithoutSkip(t *testing.T) {
	// Without --skip-on-error, the first failing source aborts before the next.
	fr := &fakeRunner{failFor: map[string]error{"sig": errors.New("boom")}}
	cfg := &config.Config{ArchiveRoot: "/arch", IMessageArchiveRoot: "/imsg"}
	opts := exportOptions{signalBin: "sig", imessageBin: "imsg", skipOnError: false}

	err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, opts)
	if err == nil {
		t.Fatalf("expected error when a source fails without --skip-on-error")
	}
	if !strings.Contains(err.Error(), "Signal export") {
		t.Errorf("error %q should name the failing source", err.Error())
	}
	// iMessage must NOT have run — the run aborted at Signal.
	if _, ran := fr.callFor("imsg"); ran {
		t.Errorf("iMessage ran despite Signal failing without --skip-on-error; calls=%+v", fr.calls)
	}
}

func TestRunExportNothingConfigured(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{} // both roots unset
	err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, exportOptions{})
	if err == nil || !strings.Contains(err.Error(), "nothing to export") {
		t.Fatalf("expected 'nothing to export' error, got %v", err)
	}
	if len(fr.calls) != 0 {
		t.Errorf("no tool should run when nothing is configured; calls=%+v", fr.calls)
	}
}

func TestRunExportPassesThroughExtraArgs(t *testing.T) {
	fr := &fakeRunner{}
	cfg := &config.Config{ArchiveRoot: "/arch", IMessageArchiveRoot: "/imsg", WhatsAppArchiveRoot: "/wapp"}
	opts := exportOptions{
		signalBin:    "sig",
		imessageBin:  "imsg",
		whatsappBin:  "wts",
		signalArgs:   []string{"--no-html", "--no-json"},
		imessageArgs: []string{"--no-progress"},
		whatsappArgs: []string{"-i", "-b", "/backups/ios"},
	}
	if err := runExport(context.Background(), &bytes.Buffer{}, fr.run, cfg, opts); err != nil {
		t.Fatalf("runExport: %v", err)
	}
	sig, _ := fr.callFor("sig")
	wantSig := []string{filepath.Join("/arch", ingest.ExportDir), "--no-html", "--no-json"}
	if !argsEqual(sig.args, wantSig) {
		t.Errorf("signal args = %v, want %v", sig.args, wantSig)
	}
	imsg, _ := fr.callFor("imsg")
	wantImsg := []string{"-f", "txt", "-c", "clone", "-o", "/imsg", "--no-progress"}
	if !argsEqual(imsg.args, wantImsg) {
		t.Errorf("imessage args = %v, want %v", imsg.args, wantImsg)
	}
	wts, _ := fr.callFor("wts")
	wantWts := []string{"-o", "/wapp", "-j", filepath.Join("/wapp", whatsapp.ResultFile), "-i", "-b", "/backups/ios"}
	if !argsEqual(wts.args, wantWts) {
		t.Errorf("whatsapp args = %v, want %v", wts.args, wantWts)
	}
}

func TestExportOptsFromFlags(t *testing.T) {
	cmd := newExportCommand()
	// Simulate: msgbrowse export --signal-export-bin /x/sig \
	//   --signal-export-args --no-html --imessage-exporter-args --no-progress \
	//   --whatsapp-exporter-bin /x/wts --whatsapp-exporter-args -i \
	//   -- --verbose
	if err := cmd.Flags().Parse([]string{
		"--signal-export-bin", "/x/sig",
		"--signal-export-args", "--no-html",
		"--imessage-exporter-args", "--no-progress",
		"--whatsapp-exporter-bin", "/x/wts",
		"--whatsapp-exporter-args", "-i",
	}); err != nil {
		t.Fatalf("parse flags: %v", err)
	}
	passthrough := []string{"--verbose"}

	opts, err := exportOptsFromFlags(cmd, passthrough)
	if err != nil {
		t.Fatalf("exportOptsFromFlags: %v", err)
	}
	if opts.signalBin != "/x/sig" {
		t.Errorf("signalBin = %q, want /x/sig", opts.signalBin)
	}
	if opts.whatsappBin != "/x/wts" {
		t.Errorf("whatsappBin = %q, want /x/wts", opts.whatsappBin)
	}
	// Per-tool args, then the shared trailing passthrough, in order — the same
	// parity for every tool.
	if !argsEqual(opts.signalArgs, []string{"--no-html", "--verbose"}) {
		t.Errorf("signalArgs = %v", opts.signalArgs)
	}
	if !argsEqual(opts.imessageArgs, []string{"--no-progress", "--verbose"}) {
		t.Errorf("imessageArgs = %v", opts.imessageArgs)
	}
	if !argsEqual(opts.whatsappArgs, []string{"-i", "--verbose"}) {
		t.Errorf("whatsappArgs = %v", opts.whatsappArgs)
	}
}

func TestResolveToolOverrideAndMissing(t *testing.T) {
	// Override is returned verbatim, no PATH lookup.
	got, err := resolveTool("/opt/foo", "foo", "hint")
	if err != nil || got != "/opt/foo" {
		t.Fatalf("resolveTool override = (%q, %v), want (/opt/foo, nil)", got, err)
	}

	// Missing default on an empty PATH is a clear error naming the tool + hint.
	t.Setenv("PATH", "")
	_, err = resolveTool("", "definitely-not-a-real-tool-xyz", "install it somehow")
	if err == nil {
		t.Fatalf("expected error for missing tool")
	}
	if !strings.Contains(err.Error(), "definitely-not-a-real-tool-xyz") || !strings.Contains(err.Error(), "install it somehow") {
		t.Errorf("error %q should name the tool and the hint", err.Error())
	}

	// A real, present tool resolves to an absolute path.
	if _, err := os.Stat("/bin/sh"); err == nil {
		t.Setenv("PATH", "/bin:/usr/bin")
		if _, err := resolveTool("", "sh", "hint"); err != nil {
			t.Errorf("resolveTool for present `sh` errored: %v", err)
		}
	}
}
