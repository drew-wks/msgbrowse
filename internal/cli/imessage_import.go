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
			guidHTMLRoot, err := cmd.Flags().GetString("guid-html-root")
			if err != nil {
				return err
			}
			preview, err := cmd.Flags().GetBool("preview-exclusions")
			if err != nil {
				return err
			}
			if preview {
				policy := imessage.ExclusionPolicy{ShortCodes: cfg.IMessageExcludeShortCodes,
					FilteredSMS:          cfg.IMessageExcludeFilteredSMS,
					AppointmentReminders: cfg.IMessageExcludeAppointmentReminders,
					EmailReminders:       cfg.IMessageExcludeEmailReminders, Rules: cfg.IMessageExclusions}
				result, err := imessage.PreviewExclusions(cfg.IMessageArchiveRoot, policy)
				if err != nil {
					return err
				}
				if _, err := fmt.Fprintf(cmd.OutOrStdout(),
					"exclusion preview: %d conversations, %d messages, %d excluded, %d eligible, %d skipped lines\n",
					result.Conversations, result.Messages, result.Excluded, result.Eligible, result.SkippedLines); err != nil {
					return err
				}
				if policy.ShortCodes {
					if _, err := fmt.Fprintf(cmd.OutOrStdout(), "  %s: %d excluded\n", imessage.ShortCodeRule, result.ByRule[imessage.ShortCodeRule]); err != nil {
						return err
					}
				}
				if policy.FilteredSMS {
					if _, err := fmt.Fprintf(cmd.OutOrStdout(), "  %s: %d excluded\n", imessage.FilteredSMSRule, result.ByRule[imessage.FilteredSMSRule]); err != nil {
						return err
					}
				}
				if policy.EmailReminders {
					if _, err := fmt.Fprintf(cmd.OutOrStdout(), "  %s: %d excluded\n", imessage.EmailReminderRule, result.ByRule[imessage.EmailReminderRule]); err != nil {
						return err
					}
				}
				if policy.AppointmentReminders {
					if _, err := fmt.Fprintf(cmd.OutOrStdout(), "  %s: %d excluded\n", imessage.AppointmentReminderRule, result.ByRule[imessage.AppointmentReminderRule]); err != nil {
						return err
					}
				}
				for _, rule := range policy.Rules {
					if _, err := fmt.Fprintf(cmd.OutOrStdout(), "  %s: %d excluded\n", rule.Name, result.ByRule[rule.Name]); err != nil {
						return err
					}
				}
				return nil
			}

			st, err := openStore(cfg)
			if err != nil {
				return err
			}
			defer st.Close()

			run, err := imessage.Run(cmd.Context(), st, imessage.Options{
				ArchiveRoot:  cfg.IMessageArchiveRoot,
				GUIDHTMLRoot: guidHTMLRoot,
				Full:         full,
				Exclusions: imessage.ExclusionPolicy{ShortCodes: cfg.IMessageExcludeShortCodes,
					FilteredSMS:          cfg.IMessageExcludeFilteredSMS,
					AppointmentReminders: cfg.IMessageExcludeAppointmentReminders,
					EmailReminders:       cfg.IMessageExcludeEmailReminders, Rules: cfg.IMessageExclusions},
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
	cmd.Flags().String("guid-html-root", "", "matching imessage-exporter 5.0.1 HTML export from the same database snapshot")
	cmd.Flags().Bool("preview-exclusions", false, "count exclusion matches without writing to the archive")
	return cmd
}
