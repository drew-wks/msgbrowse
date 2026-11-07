package cli

import (
	"fmt"
	"path/filepath"

	"github.com/joestump/msgbrowse/internal/imessage"
	"github.com/joestump/msgbrowse/internal/store"
	"github.com/spf13/cobra"
)

// newIMessageSourceAssetsCommand recovers native attachment files omitted by
// the exporter's TXT output without changing the Apple source.
func newIMessageSourceAssetsCommand() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "imessage-source-assets",
		Short: "Retain available native attachments for already archived Apple GUIDs",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			cfg, err := resolveConfig()
			if err != nil {
				return err
			}
			platform, _ := cmd.Flags().GetString("platform")
			sourcePath, _ := cmd.Flags().GetString("source")
			attachmentRoot, _ := cmd.Flags().GetString("attachment-root")
			dry, _ := cmd.Flags().GetBool("dry-run")
			if (platform != "iOS" && platform != "macOS") || sourcePath == "" ||
				(platform == "macOS" && attachmentRoot == "") {
				return fmt.Errorf("set --platform iOS|macOS, --source, and macOS --attachment-root")
			}
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
			report, err := imessage.ImportSourceAssets(cmd.Context(), st, imessage.SourceAssetOptions{
				Platform: platform, Source: sourcePath, AttachmentRoot: attachmentRoot, DryRun: dry,
			})
			if err != nil {
				return err
			}
			_, err = fmt.Fprintf(cmd.OutOrStdout(),
				"imessage-source-assets: observed=%d archived=%d available=%d hidden=%d visible=%d no_path=%d missing=%d added=%d visible_added=%d catalog_total=%d catalog_available=%d catalog_unavailable=%d visible_missing=%d dry_run=%t\n",
				report.Observed, report.Archived, report.Available, report.Hidden, report.Visible,
				report.NoPath, report.Missing, report.AssetsAdded, report.DisplayAdded,
				report.Coverage.Observed, report.Coverage.Available,
				report.Coverage.Unavailable, report.Coverage.VisibleMissing, dry)
			return err
		},
	}
	cmd.Flags().String("platform", "", "source platform: iOS or macOS")
	cmd.Flags().String("source", "", "read-only iOS MobileSync backup directory or copied macOS chat.db")
	cmd.Flags().String("attachment-root", "", "macOS Messages directory containing Attachments and StickerCache")
	cmd.Flags().Bool("dry-run", false, "count available source assets without copying or changing the archive")
	return cmd
}
