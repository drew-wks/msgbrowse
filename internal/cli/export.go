package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/imessage"
	"github.com/joestump/msgbrowse/internal/ingest"
	"github.com/joestump/msgbrowse/internal/store"
	"github.com/joestump/msgbrowse/internal/whatsapp"
	"github.com/spf13/cobra"
)

// export orchestrates the three upstream exporters msgbrowse reads from:
// carderne/signal-export (console command `sigexport`),
// ReagentX/imessage-exporter, and KnugiHK/WhatsApp-Chat-Exporter (console
// command `wtsexporter`). msgbrowse never auto-installs them and never
// touches the sensitive sources itself — it only spawns the user's own,
// already-installed tools, at the user's explicit request, streaming their
// stdout/stderr through. It stores no secrets and reads no Keychain (the
// invoked tools do, with the OS's consent). See ADR-0015 / REQ-0007-002 and
// SPEC-0009 REQ-0009-002.
//
// Layout contract:
//   - Signal:  `sigexport <archive_root>/export` so each chat lands at
//     <archive_root>/export/<conversation>/chat.md (+ its media folder) —
//     exactly what internal/ingest scans (ingest.ExportDir / ingest.ChatFile).
//   - iMessage: `imessage-exporter -f txt -c clone -o <imessage_archive_root>`
//     so attachments are *copied* into the archive (clone), never referenced by
//     absolute ~/Library path. Copy mode is the whole point: it eliminates the
//     non-copy-mode trap doctor diagnoses.
//   - WhatsApp: `wtsexporter -o <whatsapp_archive_root>
//     -j <whatsapp_archive_root>/result.json` so the JSON export and the media
//     the tool copies both land under the root internal/whatsapp scans. The
//     platform/input flags (`-i`/`-a`, `-b`/`-d`/`-k`, `-m`) are the user's to
//     supply via --whatsapp-exporter-args or the trailing `--` passthrough.

// Default binary names looked up on PATH when no override is given. Note the
// Signal binary is `sigexport` (the console script), NOT `signal-export` (the
// pip *package* name) — a common confusion the install hint also clarifies.
// The WhatsApp binary is likewise `wtsexporter`, not the pip package name
// `whatsapp-chat-exporter`.
const (
	defaultSignalExportBin     = "sigexport"
	defaultIMessageExporterBin = "imessage-exporter"
	defaultWhatsAppExporterBin = "wtsexporter"
)

// Install hints surfaced when a required exporter is absent.
const (
	signalExportInstallHint     = "install it with `pipx install signal-export` (the console command is `sigexport`)"
	imessageExporterInstallHint = "install it with `brew install imessage-exporter`"
	whatsappExporterInstallHint = "install it with `pipx install whatsapp-chat-exporter` (the console command is `wtsexporter`)"
)

// runner is the seam that makes export unit-testable without the real tools.
// Production wires it to execRunner (exec.CommandContext with the user's
// terminal). Tests inject a fake that records (name, args) and returns a
// scripted error so the flag/missing-tool/skip paths are exercised offline.
type runner func(ctx context.Context, name string, args ...string) error

// execRunner runs name+args as a child process, streaming its stdout/stderr to
// the user (these exporters print useful progress). msgbrowse passes nothing
// sensitive on the command line.
func execRunner(ctx context.Context, name string, args ...string) error {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func newExportCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "export [-- extra exporter args]",
		Short: "Run the upstream exporters (sigexport, imessage-exporter, wtsexporter) into the configured archive roots",
		Long: "export orchestrates the three upstream tools msgbrowse reads from, so a fresh\n" +
			"install can populate its archives in one step. It runs `sigexport` for Signal\n" +
			"(into <archive_root>/export/<conversation>/chat.md + media),\n" +
			"`imessage-exporter -f txt -c clone -o <imessage_archive_root>` for iMessage,\n" +
			"and `wtsexporter -o <whatsapp_archive_root> -j …/result.json` for WhatsApp.\n" +
			"\n" +
			"iMessage ALWAYS runs in copy mode (-c clone) so attachments are bundled into\n" +
			"the archive instead of left as absolute ~/Library references — the non-copy\n" +
			"trap `msgbrowse doctor` warns about.\n" +
			"\n" +
			"WhatsApp needs its platform/input flags supplied by you (they point at your\n" +
			"backup): e.g. `--whatsapp-exporter-args=-i` plus `-b <backup>` for an iOS\n" +
			"Finder/iTunes backup, or `-a` with `-b <backup> -k <key>` for Android. See\n" +
			"`wtsexporter --help`.\n" +
			"\n" +
			"A source whose root is unset is skipped. The tools must be on PATH (or set\n" +
			"--signal-export-bin / --imessage-exporter-bin / --whatsapp-exporter-bin, or\n" +
			"the matching config keys); msgbrowse never auto-installs them. A\n" +
			"required-but-missing tool is an error naming it and how to install it —\n" +
			"unless --skip-on-error, which logs a warning and continues with the other\n" +
			"sources.\n" +
			"\n" +
			"Extra args reach the underlying tools two ways: --signal-export-args /\n" +
			"--imessage-exporter-args / --whatsapp-exporter-args (repeatable) for flags\n" +
			"meant for ONE tool only, and trailing `-- <args>` for shared flags, which are\n" +
			"appended to EVERY tool's command line. msgbrowse stores no secrets and reads\n" +
			"no Keychain; the invoked tools do, with your consent. Their output streams to\n" +
			"you.",
		RunE: func(cmd *cobra.Command, passthrough []string) error {
			cfg, err := resolveConfig()
			if err != nil {
				return err
			}
			opts, err := exportOptsFromFlags(cmd, passthrough)
			if err != nil {
				return err
			}
			return runExport(cmd.Context(), cmd.OutOrStdout(), execRunner, cfg, opts)
		},
	}
	cmd.Flags().String("signal-export-bin", "", "path to the Signal exporter (default: `sigexport` on PATH; or set signal_export_bin)")
	cmd.Flags().String("imessage-exporter-bin", "", "path to imessage-exporter (default: on PATH; or set imessage_exporter_bin)")
	cmd.Flags().String("whatsapp-exporter-bin", "", "path to the WhatsApp exporter (default: `wtsexporter` on PATH; or set whatsapp_exporter_bin)")
	cmd.Flags().StringArray("signal-export-args", nil, "sigexport-only extra arg, repeatable (for shared flags use trailing `-- <args>`, appended to every tool)")
	cmd.Flags().StringArray("imessage-exporter-args", nil, "imessage-exporter-only extra arg, repeatable (for shared flags use trailing `-- <args>`, appended to every tool)")
	cmd.Flags().StringArray("whatsapp-exporter-args", nil, "wtsexporter-only extra arg, repeatable (e.g. -i, -b <backup>; for shared flags use trailing `-- <args>`, appended to every tool)")
	cmd.Flags().Bool("skip-on-error", false, "log and skip a failing/missing source instead of aborting the run")
	return cmd
}

// exportOptions is the fully-resolved input to runExport. Keeping it separate
// from cobra makes runExport a plain, table-testable function.
type exportOptions struct {
	signalBin    string   // override for the Signal exporter; "" = look up default on PATH
	imessageBin  string   // override for imessage-exporter; "" = look up default on PATH
	whatsappBin  string   // override for wtsexporter; "" = look up default on PATH
	signalArgs   []string // extra args appended to the sigexport invocation
	imessageArgs []string // extra args appended to the imessage-exporter invocation
	whatsappArgs []string // extra args appended to the wtsexporter invocation
	skipOnError  bool
}

// exportOptsFromFlags resolves bin overrides (flag, then config key) and merges
// per-tool extra args with trailing passthrough args (which apply to every tool).
func exportOptsFromFlags(cmd *cobra.Command, passthrough []string) (exportOptions, error) {
	var o exportOptions
	var err error
	if o.signalBin, err = resolveBin(cmd, "signal-export-bin", "signal_export_bin"); err != nil {
		return o, err
	}
	if o.imessageBin, err = resolveBin(cmd, "imessage-exporter-bin", "imessage_exporter_bin"); err != nil {
		return o, err
	}
	if o.whatsappBin, err = resolveBin(cmd, "whatsapp-exporter-bin", "whatsapp_exporter_bin"); err != nil {
		return o, err
	}
	if o.signalArgs, err = cmd.Flags().GetStringArray("signal-export-args"); err != nil {
		return o, err
	}
	if o.imessageArgs, err = cmd.Flags().GetStringArray("imessage-exporter-args"); err != nil {
		return o, err
	}
	if o.whatsappArgs, err = cmd.Flags().GetStringArray("whatsapp-exporter-args"); err != nil {
		return o, err
	}
	if o.skipOnError, err = cmd.Flags().GetBool("skip-on-error"); err != nil {
		return o, err
	}
	// Trailing `-- <args>` go to EVERY tool (a convenience for shared flags like
	// verbosity); per-tool flags above target one tool only.
	o.signalArgs = append(o.signalArgs, passthrough...)
	o.imessageArgs = append(o.imessageArgs, passthrough...)
	o.whatsappArgs = append(o.whatsappArgs, passthrough...)
	return o, nil
}

// resolveBin returns the explicit override for a tool: the --*-bin flag if the
// user set it, else the config key (signal_export_bin / imessage_exporter_bin)
// if present. An empty result means "fall back to LookPath on the default name".
func resolveBin(cmd *cobra.Command, flagName, cfgKey string) (string, error) {
	if cmd.Flags().Changed(flagName) {
		return cmd.Flags().GetString(flagName)
	}
	if v != nil {
		return v.GetString(cfgKey), nil
	}
	return "", nil
}

// runExport drives every source through the injected runner. It is the unit-
// tested core: no cobra, no real exec.
//
// Per source: an unset root is skipped with an info log (never an error). A
// configured source whose tool is missing or whose run fails is, without
// --skip-on-error, an immediate abort (the error is returned, halting before the
// next source); with --skip-on-error it logs a warning and the next source still
// runs. The run exits non-zero iff at least one configured source failed —
// either via the returned per-source error (no skip) or errExportFailures (when
// --skip-on-error swallowed one or more failures).
func runExport(ctx context.Context, out io.Writer, run runner, cfg *config.Config, opts exportOptions) error {
	var failed int

	sources := []struct {
		name string
		root string
		dest string
		fn   func(context.Context, runner, *config.Config, exportOptions) error
		line string
	}{
		// Signal first (no copy-mode trap, so it tends to be the simpler run).
		{
			name: "Signal", root: cfg.ArchiveRoot, dest: filepath.Join(cfg.ArchiveRoot, ingest.ExportDir),
			fn: exportSignal, line: "signal:   exported to %s\n",
		},
		{
			name: "iMessage", root: cfg.IMessageArchiveRoot, dest: cfg.IMessageArchiveRoot,
			fn: exportIMessage, line: "imessage: exported to %s (copy mode: clone)\n",
		},
		{
			name: "WhatsApp", root: cfg.WhatsAppArchiveRoot, dest: cfg.WhatsAppArchiveRoot,
			fn: exportWhatsApp, line: "whatsapp: exported to %s (JSON + media)\n",
		},
	}

	configured := 0
	ran := 0
	for _, s := range sources {
		if s.root == "" {
			slog.Info("skipping export: source root not set", "source", s.name)
			continue
		}
		configured++
		if err := s.fn(ctx, run, cfg, opts); err != nil {
			if !opts.skipOnError {
				// Abort: surface this source's error directly (clear, single line).
				return fmt.Errorf("%s export: %w", s.name, err)
			}
			slog.Warn("skipping source after error (--skip-on-error)", "source", s.name, "error", err)
			failed++
			continue
		}
		ran++
		fmt.Fprintf(out, s.line, s.dest)
	}

	if configured == 0 {
		return fmt.Errorf("nothing to export: set archive_root, imessage_archive_root, and/or whatsapp_archive_root (flags, config, or MSGBROWSE_* env)")
	}
	if failed > 0 {
		// Reached only under --skip-on-error: each failure was already warned
		// about above; exit non-zero without re-reporting them line by line.
		return fmt.Errorf("%w: %d of %d configured source(s) failed (see warnings above)", errExportFailures, failed, configured)
	}
	return nil
}

// errExportFailures marks a run where --skip-on-error swallowed one or more
// source failures. The run still exits non-zero so callers/scripts notice, but
// the per-source detail was already logged as warnings.
var errExportFailures = errors.New("export completed with failures")

// exportSignal runs sigexport with <archive_root>/export as its (positional)
// destination, so it writes <archive_root>/export/<conversation>/chat.md — the
// layout ingest scans. Extra args are appended after the destination.
func exportSignal(ctx context.Context, run runner, cfg *config.Config, opts exportOptions) error {
	bin, err := resolveTool(opts.signalBin, defaultSignalExportBin, signalExportInstallHint)
	if err != nil {
		return err
	}
	dest := filepath.Join(cfg.ArchiveRoot, ingest.ExportDir)
	args := append([]string{dest}, opts.signalArgs...)
	slog.Info("running Signal export", "tool", bin, "dest", dest)
	return run(ctx, bin, args...)
}

// exportIMessage runs imessage-exporter into imessage_archive_root in TXT format
// with copy method `clone` (MANDATORY — bundles attachments into the archive).
func exportIMessage(ctx context.Context, run runner, cfg *config.Config, opts exportOptions) error {
	bin, err := resolveTool(opts.imessageBin, defaultIMessageExporterBin, imessageExporterInstallHint)
	if err != nil {
		return err
	}
	// CLI exports write directly into the configured root. Preserve any legacy
	// SQLite references before the exporter can replace files at that root.
	dbPath := filepath.Join(cfg.DataDir, store.DBFileName)
	if _, statErr := os.Stat(dbPath); statErr == nil {
		st, openErr := store.Open(dbPath)
		if openErr != nil {
			return openErr
		}
		preserveErr := imessage.PreserveExistingAttachments(ctx, st, cfg.IMessageArchiveRoot)
		closeErr := st.Close()
		if preserveErr != nil {
			return preserveErr
		}
		if closeErr != nil {
			return closeErr
		}
	} else if !os.IsNotExist(statErr) {
		return statErr
	}
	args := append([]string{"-f", "txt", "-c", "clone", "-o", cfg.IMessageArchiveRoot}, opts.imessageArgs...)
	slog.Info("running iMessage export", "tool", bin, "dest", cfg.IMessageArchiveRoot, "copy-method", "clone")
	return run(ctx, bin, args...)
}

// exportWhatsApp runs wtsexporter with the configured root as both its output
// directory (-o, where the tool copies media) and the destination of its JSON
// export (-j <root>/result.json) — the layout internal/whatsapp scans. The
// device/platform and input flags (-i/-a, -b/-d/-k, -m …) vary per user and
// come from --whatsapp-exporter-args or the trailing `--` passthrough.
func exportWhatsApp(ctx context.Context, run runner, cfg *config.Config, opts exportOptions) error {
	bin, err := resolveTool(opts.whatsappBin, defaultWhatsAppExporterBin, whatsappExporterInstallHint)
	if err != nil {
		return err
	}
	root := cfg.WhatsAppArchiveRoot
	args := append([]string{"-o", root, "-j", filepath.Join(root, whatsapp.ResultFile)}, opts.whatsappArgs...)
	slog.Info("running WhatsApp export", "tool", bin, "dest", root)
	return run(ctx, bin, args...)
}

// resolveTool returns the executable to invoke. An explicit override (flag or
// config) is used verbatim; otherwise the default name is looked up on PATH. A
// required-but-absent tool is a clear error naming it and how to install it.
func resolveTool(override, defaultName, installHint string) (string, error) {
	if override != "" {
		return override, nil
	}
	path, err := exec.LookPath(defaultName)
	if err != nil {
		return "", fmt.Errorf("%s not found on PATH: %s", defaultName, installHint)
	}
	return path, nil
}
