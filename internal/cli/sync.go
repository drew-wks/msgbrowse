package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"

	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/embed"
	"github.com/joestump/msgbrowse/internal/facts"
	"github.com/joestump/msgbrowse/internal/imageconv"
	"github.com/joestump/msgbrowse/internal/imessage"
	"github.com/joestump/msgbrowse/internal/ingest"
	"github.com/joestump/msgbrowse/internal/store"
	"github.com/joestump/msgbrowse/internal/whatsapp"
	"github.com/spf13/cobra"
)

// sync is the all-in-one onboarding pipeline: it chains every step that turns a
// fresh install into a populated, browsable archive — export → import → media →
// embed → facts — REUSING the existing per-command logic (it never reimplements a
// stage). The store is opened once and shared by every store-backed stage. See
// ADR-0015 / REQ-0007-003.

// stageFunc runs one pipeline stage and returns a concise one-line summary to
// print on success. Each stage closes over its own inputs (store, config, llm
// client, runner) so runSync stays free of cobra, exec, and the store — the seam
// that makes the pipeline unit-testable with stub stages.
type stageFunc func(ctx context.Context) (summary string, err error)

// syncDeps holds the injectable stage functions. Production wires these to the
// real export/import/media/embed/facts run functions (see syncDepsFromConfig);
// tests inject stubs to assert stage ORDER, the --no-* skips, the error policy,
// and the overall exit code without touching the real tools, store, or LLM.
//
// A nil stage func means "this stage was skipped" (a --no-* flag, or an unset
// source root for the import stages) and runSync skips it silently in order.
type syncDeps struct {
	export         stageFunc // run the upstream exporters (Signal + iMessage + WhatsApp)
	importSignal   stageFunc // ingest.Run for the Signal archive
	importIMsg     stageFunc // imessage.Run for the iMessage archive
	importWhatsApp stageFunc // whatsapp.Run for the WhatsApp archive
	media          stageFunc // imageconv.Run (HEIC/TIFF → JPEG)
	embed          stageFunc // embed.Run (LLM endpoint)
	facts          stageFunc // facts.Run (LLM endpoint)
}

// syncOptions is the resolved, cobra-free input to runSync.
type syncOptions struct {
	skipOnError bool
}

// errSyncFailures marks a run where a hard (export/import/media) stage failed but
// --skip-on-error let the pipeline continue. The run still exits non-zero so
// scripts notice; the per-stage detail was already logged as warnings.
var errSyncFailures = errors.New("sync completed with failures")

// stage describes one entry in the ordered pipeline.
type stage struct {
	name string
	fn   stageFunc
	// llmDependent stages (embed, facts) need the configured LLM endpoint. They
	// MUST warn-and-continue when it is unreachable, REGARDLESS of --skip-on-error,
	// so a fully-local run with no LLM still completes export/import/media and exits
	// success (REQ-0007-003: "degrades without an LLM").
	llmDependent bool
}

// runSync executes the pipeline in order. It is the unit-tested core: no cobra,
// no real exec, no store.
//
// Order is fixed: export → import (Signal, then iMessage, then WhatsApp) →
// media → embed → facts. A nil stage func is a skip (a --no-* flag or an unset
// source root) and is passed over silently, preserving order for the stages
// that do run.
//
// Error policy (REQ-0007-003):
//   - Hard stages (export/import/media): a failure ABORTS the pipeline and is
//     returned immediately — UNLESS --skip-on-error, which logs a warning, marks
//     the run failed, and continues to the next stage. If any hard stage failed
//     under --skip-on-error, runSync returns errSyncFailures so the process exits
//     non-zero.
//   - LLM-dependent stages (embed/facts): a failure is ALWAYS a warning that the
//     pipeline continues past, independent of --skip-on-error, and never affects
//     the exit code. A local run with no reachable LLM still exits success.
func runSync(ctx context.Context, out io.Writer, deps syncDeps, opts syncOptions) error {
	stages := []stage{
		{name: "export", fn: deps.export},
		{name: "import (signal)", fn: deps.importSignal},
		{name: "import (imessage)", fn: deps.importIMsg},
		{name: "import (whatsapp)", fn: deps.importWhatsApp},
		{name: "media", fn: deps.media},
		{name: "embed", fn: deps.embed, llmDependent: true},
		{name: "facts", fn: deps.facts, llmDependent: true},
	}

	var hardFailed int
	for _, s := range stages {
		if s.fn == nil {
			// Skipped: a --no-* flag or an unset source root. Stay in order.
			continue
		}
		summary, err := s.fn(ctx)
		if err == nil {
			fmt.Fprintln(out, summary)
			continue
		}

		if s.llmDependent {
			// embed/facts: always warn-and-continue, never fail the run. These need
			// the LLM endpoint; a local run with none reachable still succeeds.
			slog.Warn("skipping LLM stage after error (no endpoint?); continuing", "stage", s.name, "error", err)
			continue
		}

		// Hard stage (export/import/media).
		if !opts.skipOnError {
			return fmt.Errorf("%s: %w", s.name, err)
		}
		slog.Warn("skipping stage after error (--skip-on-error); continuing", "stage", s.name, "error", err)
		hardFailed++
	}

	if hardFailed > 0 {
		return fmt.Errorf("%w: %d stage(s) failed (see warnings above)", errSyncFailures, hardFailed)
	}
	return nil
}

func newSyncCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "sync [-- extra exporter args]",
		Short: "Run the whole onboarding pipeline: export → import → media → embed → facts",
		Long: "sync is the one-command refresh: it chains every step that turns a fresh\n" +
			"install into a populated, browsable archive, reusing the existing commands\n" +
			"end to end:\n" +
			"\n" +
			"  1. export  run the upstream exporters (Signal + iMessage + WhatsApp)  [--no-export]\n" +
			"  2. import  ingest every configured archive into the database\n" +
			"  3. media   transcode HEIC/TIFF attachments to web JPEGs      [--no-media]\n" +
			"  4. embed   compute embeddings for semantic search            [--no-embed]\n" +
			"  5. facts   extract AI facts about each contact               [--no-facts]\n" +
			"\n" +
			"The database is opened once and shared by every stage. A source whose archive\n" +
			"root is unset is skipped (so a single-source setup just works).\n" +
			"\n" +
			"export/import/media failures abort the run unless --skip-on-error, which logs a\n" +
			"warning and continues (the run still exits non-zero). embed and facts need the\n" +
			"configured LLM endpoint: they ALWAYS warn and continue on failure regardless of\n" +
			"--skip-on-error, so a fully-local run with no reachable LLM still completes\n" +
			"export/import/media and exits success.\n" +
			"\n" +
			"Trailing `-- <args>` are passed through to EVERY upstream exporter (see\n" +
			"`msgbrowse export --help` for the per-tool flags).",
		RunE: func(cmd *cobra.Command, passthrough []string) error {
			cfg, err := resolveConfig()
			if err != nil {
				return err
			}

			noExport, err := cmd.Flags().GetBool("no-export")
			if err != nil {
				return err
			}
			noMedia, err := cmd.Flags().GetBool("no-media")
			if err != nil {
				return err
			}
			noEmbed, err := cmd.Flags().GetBool("no-embed")
			if err != nil {
				return err
			}
			noFacts, err := cmd.Flags().GetBool("no-facts")
			if err != nil {
				return err
			}
			skipOnError, err := cmd.Flags().GetBool("skip-on-error")
			if err != nil {
				return err
			}

			exportOpts, err := exportOptsFromFlags(cmd, passthrough)
			if err != nil {
				return err
			}

			// Open the store ONCE; share it with every store-backed stage.
			st, err := openStore(cfg)
			if err != nil {
				return err
			}
			defer st.Close()

			out := cmd.OutOrStdout()
			deps := syncDepsFromConfig(st, cfg, out, syncWiring{
				noExport:   noExport,
				noMedia:    noMedia,
				noEmbed:    noEmbed,
				noFacts:    noFacts,
				exportOpts: exportOpts,
			})

			return runSync(cmd.Context(), out, deps, syncOptions{skipOnError: skipOnError})
		},
	}
	cmd.Flags().Bool("no-export", false, "skip the export stage (don't re-run the upstream exporters)")
	cmd.Flags().Bool("no-media", false, "skip the media transcode stage")
	cmd.Flags().Bool("no-embed", false, "skip the embed stage")
	cmd.Flags().Bool("no-facts", false, "skip the facts stage")
	cmd.Flags().Bool("skip-on-error", false, "log and continue past a failing export/import/media stage instead of aborting (run still exits non-zero)")
	// Export bin overrides + per-tool extra args, so `sync` can drive export the
	// same way `export` can.
	cmd.Flags().String("signal-export-bin", "", "path to the Signal exporter (default: `sigexport` on PATH; or set signal_export_bin)")
	cmd.Flags().String("imessage-exporter-bin", "", "path to imessage-exporter (default: on PATH; or set imessage_exporter_bin)")
	cmd.Flags().String("whatsapp-exporter-bin", "", "path to the WhatsApp exporter (default: `wtsexporter` on PATH; or set whatsapp_exporter_bin)")
	cmd.Flags().StringArray("signal-export-args", nil, "sigexport-only extra arg, repeatable (for shared flags use trailing `-- <args>`, appended to every tool)")
	cmd.Flags().StringArray("imessage-exporter-args", nil, "imessage-exporter-only extra arg, repeatable (for shared flags use trailing `-- <args>`, appended to every tool)")
	cmd.Flags().StringArray("whatsapp-exporter-args", nil, "wtsexporter-only extra arg, repeatable (e.g. -i, -b <backup>; for shared flags use trailing `-- <args>`, appended to every tool)")
	return cmd
}

// syncWiring carries the resolved per-stage skip flags and export options needed
// to build the production deps.
type syncWiring struct {
	noExport   bool
	noMedia    bool
	noEmbed    bool
	noFacts    bool
	exportOpts exportOptions
}

// syncDepsFromConfig wires the real run functions into syncDeps, closing over the
// shared store, config, output writer, and an LLM client (built per stage, only
// when that stage runs). A disabled-by-flag or unset-root stage is wired as nil
// so runSync skips it in order.
func syncDepsFromConfig(st *store.Store, cfg *config.Config, out io.Writer, w syncWiring) syncDeps {
	var deps syncDeps

	// 1. export — reuse export.go's runExport with the real execRunner. runExport
	//    prints its own per-source line(s) to out, so the stage summary just marks
	//    the stage complete.
	if !w.noExport {
		deps.export = func(ctx context.Context) (string, error) {
			if err := runExport(ctx, out, execRunner, cfg, w.exportOpts); err != nil {
				return "", err
			}
			return "export:   done", nil
		}
	}

	// 2. import — Signal (ingest.Run) and iMessage (imessage.Run), each wired only
	//    when its root is configured (an unset root is a skip, mirroring `import`).
	if cfg.ArchiveRoot != "" {
		deps.importSignal = func(ctx context.Context) (string, error) {
			if err := requireArchive(cfg); err != nil {
				return "", err
			}
			run, err := ingest.Run(ctx, st, ingest.Options{ArchiveRoot: cfg.ArchiveRoot})
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("signal:   %d/%d conversations changed, %d messages total (%d added) in %dms",
				run.ConversationsChanged, run.ConversationsScanned, run.MessagesTotal, run.MessagesAdded, run.DurationMS), nil
		}
	}
	if cfg.IMessageArchiveRoot != "" {
		deps.importIMsg = func(ctx context.Context) (string, error) {
			if err := requireIMessageArchive(cfg); err != nil {
				return "", err
			}
			run, err := imessage.Run(ctx, st, imessage.Options{ArchiveRoot: cfg.IMessageArchiveRoot,
				Exclusions: imessage.ExclusionPolicy{ShortCodes: cfg.IMessageExcludeShortCodes,
					FilteredSMS:          cfg.IMessageExcludeFilteredSMS,
					AppointmentReminders: cfg.IMessageExcludeAppointmentReminders,
					EmailReminders:       cfg.IMessageExcludeEmailReminders, Rules: cfg.IMessageExclusions}})
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("imessage: %d/%d conversations changed, %d messages total (%d added) in %dms",
				run.ConversationsChanged, run.ConversationsScanned, run.MessagesTotal, run.MessagesAdded, run.DurationMS), nil
		}
	}
	if cfg.WhatsAppArchiveRoot != "" {
		deps.importWhatsApp = func(ctx context.Context) (string, error) {
			if err := requireWhatsAppArchive(cfg); err != nil {
				return "", err
			}
			run, err := whatsapp.Run(ctx, st, whatsapp.Options{ArchiveRoot: cfg.WhatsAppArchiveRoot})
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("whatsapp: %d/%d conversations changed, %d messages total (%d added) in %dms",
				run.ConversationsChanged, run.ConversationsScanned, run.MessagesTotal, run.MessagesAdded, run.DurationMS), nil
		}
	}

	// 3. media — imageconv.Run (HEIC/TIFF → JPEG).
	if !w.noMedia {
		deps.media = func(ctx context.Context) (string, error) {
			sum, err := imageconv.Run(ctx, st, imageconv.Options{
				ArchiveRoot:         cfg.ArchiveRoot,
				IMessageArchiveRoot: cfg.IMessageArchiveRoot,
				WhatsAppArchiveRoot: cfg.WhatsAppArchiveRoot,
				DataDir:             cfg.DataDir,
			})
			if err != nil {
				return "", err
			}
			if sum.NoConverter {
				return "media:    no image converter on PATH; nothing transcoded", nil
			}
			return fmt.Sprintf("media:    %d transcoded, %d cached, %d source-missing, %d failed in %dms",
				sum.Converted, sum.Skipped, sum.Missing, sum.Failed, sum.DurationMS), nil
		}
	}

	// 4. embed — embed.Run (LLM endpoint). runSync warns-and-continues on failure.
	if !w.noEmbed {
		deps.embed = func(ctx context.Context) (string, error) {
			sum, err := embed.Run(ctx, st, newLLMClient(cfg), embed.Options{EmbedModel: cfg.LLM.EmbedModel})
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("embed:    %d embedded in %d batches in %dms", sum.Embedded, sum.Batches, sum.DurationMS), nil
		}
	}

	// 5. facts — facts.Run (LLM endpoint). runSync warns-and-continues on failure.
	if !w.noFacts {
		deps.facts = func(ctx context.Context) (string, error) {
			sum, err := facts.Run(ctx, st, newLLMClient(cfg), facts.Options{
				Model:   cfg.LLM.ChatModel,
				Exclude: cfg.Journal.ExcludeConversations,
			})
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("facts:    %d added from %d messages across %d conversations in %dms",
				sum.FactsAdded, sum.MessagesParsed, sum.Conversations, sum.DurationMS), nil
		}
	}

	return deps
}
