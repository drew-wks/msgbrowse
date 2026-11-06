package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/joestump/msgbrowse/internal/imessage"
	"github.com/joestump/msgbrowse/internal/store"
	"github.com/spf13/cobra"
)

// newIMessagePreviewsCommand adds an explicit, read-only Apple source scan for
// URL balloon images omitted by the installed exporter's TXT format.
func newIMessagePreviewsCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "imessage-previews",
		Short: "Retain local URL preview images for already-imported iMessages",
		Long: "Read Apple Messages locally and read-only to copy URL preview images " +
			"into msgbrowse's permanent media archive. The normal TXT exporter omits these files. " +
			"Run after imessage-import, before deleting any Apple Messages. Requires Full Disk Access " +
			"for the terminal or application running this command. Ambiguous message matches are skipped.",
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, err := resolveConfig()
			if err != nil {
				return err
			}
			participant, _ := cmd.Flags().GetString("participant")
			all, _ := cmd.Flags().GetBool("all")
			if participant == "" && !all {
				return fmt.Errorf("set --participant to limit the source scan or --all to scan every conversation")
			}
			startText, _ := cmd.Flags().GetString("start-date")
			endText, _ := cmd.Flags().GetString("end-date")
			if (startText == "") != (endText == "") {
				return fmt.Errorf("set both --start-date and --end-date (end exclusive)")
			}
			zoneName, _ := cmd.Flags().GetString("timezone")
			zone, err := time.LoadLocation(zoneName)
			if err != nil {
				return fmt.Errorf("timezone %q: %w", zoneName, err)
			}
			var start, end time.Time
			if startText != "" {
				start, err = time.ParseInLocation("2006-01-02", startText, zone)
				if err != nil {
					return fmt.Errorf("start date: %w", err)
				}
				end, err = time.ParseInLocation("2006-01-02", endText, zone)
				if err != nil {
					return fmt.Errorf("end date: %w", err)
				}
			}
			dbPath, _ := cmd.Flags().GetString("source-db")
			attachmentRoot, _ := cmd.Flags().GetString("attachment-root")
			dry, _ := cmd.Flags().GetBool("dry-run")
			var st *store.Store
			if dry {
				st, err = store.OpenReadOnly(filepath.Join(cfg.DataDir, store.DBFileName))
			} else {
				st, err = openStore(cfg)
			}
			if err != nil {
				return err
			}
			defer st.Close()
			report, err := imessage.ImportLinkPreviews(cmd.Context(), st, imessage.PreviewOptions{
				SourceDB: dbPath, AttachmentRoot: attachmentRoot, Participant: participant,
				Start: start, End: end, Location: zone, DryRun: dry,
			})
			if err != nil {
				return err
			}
			_, err = fmt.Fprintf(cmd.OutOrStdout(),
				"imessage-previews: source=%d matched=%d weak=%d unmatched=%d ambiguous=%d assets=%d added=%d missing=%d unsupported=%d dry_run=%t\n",
				report.SourceMessages, report.Matched, report.WeakMatches, report.Unmatched,
				report.Ambiguous, report.AssetsObserved, report.AssetsAdded, report.MissingFiles,
				report.Unsupported, dry)
			if err != nil {
				return err
			}
			if report.Unmatched+report.Ambiguous+report.MissingFiles+report.Unsupported > 0 {
				return fmt.Errorf("some URL preview assets were not retained; inspect counts before relying on this archive")
			}
			return nil
		},
	}
	home, _ := os.UserHomeDir()
	defaultRoot := filepath.Join(home, "Library", "Messages")
	cmd.Flags().String("source-db", filepath.Join(defaultRoot, "chat.db"), "read-only Apple Messages database or copied chat.db")
	cmd.Flags().String("attachment-root", defaultRoot, "root containing Apple's Attachments directory")
	cmd.Flags().String("participant", "", "phone or email handle to limit the source scan")
	cmd.Flags().Bool("all", false, "scan all URL balloon messages in the selected date range")
	cmd.Flags().String("start-date", "", "first included local date (YYYY-MM-DD)")
	cmd.Flags().String("end-date", "", "first excluded local date (YYYY-MM-DD)")
	cmd.Flags().String("timezone", "Local", "timezone used by the TXT exporter and archived timestamps")
	cmd.Flags().Bool("dry-run", false, "count matches without modifying the archive")
	return cmd
}
