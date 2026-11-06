package cli

import (
	"fmt"
	"log/slog"

	"github.com/joestump/msgbrowse/internal/imageconv"
	"github.com/joestump/msgbrowse/internal/imessage"
	"github.com/spf13/cobra"
)

func newIMessageImportCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "imessage-import",
		Short: "Import (or refresh) an imessage-exporter archive into the local store",
		Long: "imessage-import scans a read-only imessage-exporter archive (a flat\n" +
			"directory of <ChatName>.txt files produced by `imessage-exporter -f txt`),\n" +
			"parses each changed conversation into the unified SQLite store, and tags every\n" +
			"row source=\"imessage\". Incremental and idempotent, like signal-import.\n" +
			"HEIC/HEIF/TIFF attachments are converted to cached JPEG previews when a\n" +
			"local image converter is available.\n" +
			"\n" +
			"Targets the imessage-exporter 4.2.0 txt format; the path comes from\n" +
			"imessage_archive_root / MSGBROWSE_IMESSAGE_ARCHIVE_ROOT / --imessage-archive-root.",
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, err := resolveConfig()
			if err != nil {
				return err
			}
			if err := requireIMessageArchive(cfg); err != nil {
				return err
			}
			full, err := cmd.Flags().GetBool("full")
			if err != nil {
				return err
			}

			st, err := openStore(cfg)
			if err != nil {
				return err
			}
			defer st.Close()

			run, err := imessage.Run(cmd.Context(), st, imessage.Options{
				ArchiveRoot: cfg.IMessageArchiveRoot,
				Full:        full,
			})
			if err != nil {
				return err
			}
			if _, err = fmt.Fprintf(cmd.OutOrStdout(),
				"imessage-import: %d/%d conversations changed, %d messages total (%d added), %d skipped lines in %dms\n",
				run.ConversationsChanged, run.ConversationsScanned, run.MessagesTotal, run.MessagesAdded,
				run.SkippedLines, run.DurationMS); err != nil {
				return err
			}
			roots := archiveRoots(cfg)
			media, err := imageconv.Run(cmd.Context(), st, imageconv.Options{
				ArchiveRoot: roots.Signal, IMessageArchiveRoot: roots.IMessage,
				WhatsAppArchiveRoot: roots.WhatsApp, DataDir: cfg.DataDir,
			})
			if err != nil {
				slog.Warn("image preview conversion failed", "error", err)
				return nil
			}
			if media.NoConverter {
				return nil
			}
			_, err = fmt.Fprintf(cmd.OutOrStdout(),
				"media: %d converted, %d cached, %d source-missing, %d failed\n",
				media.Converted, media.Skipped, media.Missing, media.Failed)
			return err
		},
	}
	cmd.Flags().Bool("full", false, "ignore incremental state and re-scan every conversation")
	return cmd
}
