package imessage

import (
	"bytes"
	"context"
	"database/sql"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
	_ "modernc.org/sqlite"
)

// PreviewOptions scopes the optional, read-only Apple Messages preview scan.
// Start and End are inclusive and exclusive bounds, respectively.
type PreviewOptions struct {
	SourceDB       string
	AttachmentRoot string
	Participant    string
	Start          time.Time
	End            time.Time
	Location       *time.Location
	DryRun         bool
}

// PreviewReport contains counts only; it never includes personal message data.
type PreviewReport struct {
	SourceMessages int
	Matched        int
	WeakMatches    int
	Unmatched      int
	Ambiguous      int
	MissingFiles   int
	Unsupported    int
	AssetsObserved int
	AssetsAdded    int
}

type previewSourceMessage struct {
	id      int64
	date    int64
	text    string
	payload []byte
	fromMe  bool
	files   []string
}

type previewCandidate struct {
	id      int64
	fromMe  bool
	domains map[string]bool
}

type previewFile struct {
	path string
	ext  string
	area int64
	size int64
}

var appleEpoch = time.Date(2001, time.January, 1, 0, 0, 0, 0, time.UTC)

var payloadURLHostRe = regexp.MustCompile(`(?i)https?://[a-z0-9.-]+`)

// ImportLinkPreviews reads only URL balloon attachment rows from Apple's local
// database, associates unambiguous rows with existing archived link messages,
// and appends immutable image copies. It never writes to Apple's database.
func ImportLinkPreviews(ctx context.Context, st *store.Store, opts PreviewOptions) (PreviewReport, error) {
	var report PreviewReport
	if opts.Location == nil {
		opts.Location = time.Local
	}
	if opts.SourceDB == "" || opts.AttachmentRoot == "" {
		return report, fmt.Errorf("source database and attachment root are required")
	}
	root, err := filepath.EvalSymlinks(opts.AttachmentRoot)
	if err != nil {
		return report, fmt.Errorf("resolve attachment root: %w", err)
	}
	dbPath, err := filepath.Abs(opts.SourceDB)
	if err != nil {
		return report, err
	}
	u := url.URL{Scheme: "file", Path: dbPath}
	sourceDB, err := sql.Open("sqlite", u.String()+"?mode=ro&_pragma=query_only(ON)")
	if err != nil {
		return report, fmt.Errorf("open Apple Messages read-only: %w", err)
	}
	defer sourceDB.Close()
	if err := sourceDB.PingContext(ctx); err != nil {
		return report, fmt.Errorf("read Apple Messages database (Full Disk Access may be required): %w", err)
	}
	ids, err := previewHandleIDs(ctx, sourceDB, opts.Participant)
	if err != nil {
		return report, err
	}
	if opts.Participant != "" && len(ids) == 0 {
		return report, fmt.Errorf("no matching Apple Messages participant handle")
	}
	messages, err := previewSourceRows(ctx, sourceDB, opts, ids)
	if err != nil {
		return report, err
	}
	for _, m := range messages {
		if err := ctx.Err(); err != nil {
			return report, err
		}
		report.SourceMessages++
		if m.date < 1_000_000_000_000_000 {
			return report, fmt.Errorf("unsupported Apple message timestamp unit; expected nanoseconds")
		}
		wall := appleEpoch.Add(time.Duration(m.date)).In(opts.Location).Format(signal.TimestampLayout)
		candidates, err := findPreviewCandidates(ctx, st, wall, m.fromMe, m.text, m.payload)
		if err != nil {
			return report, err
		}
		if len(candidates) == 0 {
			report.Unmatched++
			continue
		}
		if len(candidates) != 1 {
			report.Ambiguous++
			continue
		}
		if len(signal.ExtractLinks(m.text)) == 0 {
			report.WeakMatches++
		}
		report.Matched++
		var files []previewFile
		failed := false
		for _, name := range m.files {
			path, err := previewSourcePath(root, name)
			if err != nil {
				report.MissingFiles++
				failed = true
				continue
			}
			asset, err := classifyPreviewFile(path)
			if err != nil {
				report.Unsupported++
				failed = true
				continue
			}
			files = append(files, asset)
		}
		if failed || len(files) == 0 {
			continue
		}
		// The largest decoded image is the card thumbnail. Retain every other
		// image and icon too, but keep them out of the ordinary photo gallery.
		sort.Slice(files, func(i, j int) bool {
			if files[i].area != files[j].area {
				return files[i].area > files[j].area
			}
			if files[i].size != files[j].size {
				return files[i].size > files[j].size
			}
			return files[i].path < files[j].path
		})
		report.AssetsObserved += len(files)
		if opts.DryRun {
			continue
		}
		assets := make([]store.IMessagePreviewAsset, 0, len(files))
		primaryUsed := false
		for _, file := range files {
			kind := "link_icon"
			if file.area > 0 {
				kind = "link_preview_extra"
				if !primaryUsed {
					kind = "link_preview"
					primaryUsed = true
				}
			}
			retained, err := retainSourceFile(file.path, st.DataDir(), file.ext)
			if err != nil {
				return report, fmt.Errorf("retain URL preview image: %w", err)
			}
			assets = append(assets, store.IMessagePreviewAsset{
				Kind: kind, RelPath: retained, OriginalName: "link-preview" + file.ext,
			})
		}
		added, err := st.AppendIMessagePreviewAssets(ctx, candidates[0], assets)
		if err != nil {
			return report, err
		}
		report.AssetsAdded += added
	}
	return report, nil
}

// previewHandleIDs resolves a narrowly scoped phone or email filter.
func previewHandleIDs(ctx context.Context, db *sql.DB, participant string) ([]int64, error) {
	if participant == "" {
		return nil, nil
	}
	rows, err := db.QueryContext(ctx, `SELECT ROWID, id FROM handle`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	want := strings.ToLower(strings.TrimSpace(participant))
	wantDigits := onlyDigits(want)
	for rows.Next() {
		var id int64
		var handle string
		if err := rows.Scan(&id, &handle); err != nil {
			return nil, err
		}
		if strings.Contains(want, "@") && strings.EqualFold(want, handle) ||
			!strings.Contains(want, "@") && len(wantDigits) >= 7 && strings.HasSuffix(onlyDigits(handle), wantDigits) {
			ids = append(ids, id)
		}
	}
	return ids, rows.Err()
}

// onlyDigits normalizes phone handles without changing email identifiers.
func onlyDigits(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// previewSourceRows loads only hidden URL balloon files in the requested scope.
func previewSourceRows(ctx context.Context, db *sql.DB, opts PreviewOptions, ids []int64) ([]previewSourceMessage, error) {
	start, end := int64(0), int64(math.MaxInt64)
	if !opts.Start.IsZero() {
		start = opts.Start.Sub(appleEpoch).Nanoseconds()
	}
	if !opts.End.IsZero() {
		end = opts.End.Sub(appleEpoch).Nanoseconds()
	}
	if end <= start {
		return nil, fmt.Errorf("preview end date must be after start date")
	}
	q := `SELECT DISTINCT m.ROWID, m.date, m.text, m.payload_data, m.is_from_me, a.ROWID, a.filename
		FROM message m JOIN message_attachment_join ma ON ma.message_id=m.ROWID
		JOIN attachment a ON a.ROWID=ma.attachment_id`
	args := []any{start, end}
	if opts.Participant != "" {
		q += ` JOIN chat_message_join cm ON cm.message_id=m.ROWID
			JOIN chat_handle_join ch ON ch.chat_id=cm.chat_id`
	}
	q += ` WHERE m.date>=? AND m.date<? AND m.associated_message_guid IS NULL
		AND m.balloon_bundle_id='com.apple.messages.URLBalloonProvider'
		AND a.hide_attachment=1 AND a.filename LIKE '%.pluginPayloadAttachment'`
	if opts.Participant != "" {
		q += ` AND ch.handle_id IN (` + strings.TrimSuffix(strings.Repeat("?,", len(ids)), ",") + `)`
		for _, id := range ids {
			args = append(args, id)
		}
	}
	q += ` ORDER BY m.ROWID, a.ROWID`
	rows, err := db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("scan Apple URL preview rows: %w", err)
	}
	defer rows.Close()
	var messages []previewSourceMessage
	for rows.Next() {
		var id, date, attachmentID, fromMe int64
		var text, filename sql.NullString
		var payload []byte
		if err := rows.Scan(&id, &date, &text, &payload, &fromMe, &attachmentID, &filename); err != nil {
			return nil, err
		}
		if len(messages) == 0 || messages[len(messages)-1].id != id {
			messages = append(messages, previewSourceMessage{id: id, date: date, text: text.String, payload: payload, fromMe: fromMe != 0})
		}
		messages[len(messages)-1].files = append(messages[len(messages)-1].files, filename.String)
	}
	return messages, rows.Err()
}

// findPreviewCandidates fails closed unless one link message matches the source
// wall-clock second, direction, and a source URL host. The binary balloon
// payload is scanned only for URL hosts when Apple's text field is empty.
func findPreviewCandidates(ctx context.Context, st *store.Store, wall string, fromMe bool, sourceText string, payload []byte) ([]int64, error) {
	rows, err := st.DB().QueryContext(ctx, `SELECT DISTINCT m.id, m.sender, l.domain FROM messages m
		JOIN links l ON l.message_id=m.id WHERE m.source=? AND m.ts=?`, source.IMessage, wall)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	byID := map[int64]*previewCandidate{}
	for rows.Next() {
		var id int64
		var sender, domain string
		if err := rows.Scan(&id, &sender, &domain); err != nil {
			return nil, err
		}
		c := byID[id]
		if c == nil {
			c = &previewCandidate{id: id, fromMe: sender == "Me", domains: map[string]bool{}}
			byID[id] = c
		}
		c.domains[domain] = true
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	sourceDomains := map[string]bool{}
	for _, link := range signal.ExtractLinks(sourceText) {
		sourceDomains[signal.Domain(link.URL)] = true
	}
	for _, raw := range payloadURLHostRe.FindAll(payload, -1) {
		if domain := signal.Domain(string(raw)); domain != "" {
			sourceDomains[domain] = true
		}
	}
	if len(sourceDomains) == 0 {
		return nil, nil
	}
	var out []int64
	for _, c := range byID {
		if c.fromMe != fromMe {
			continue
		}
		matched := false
		for domain := range sourceDomains {
			matched = matched || c.domains[domain]
		}
		if !matched {
			continue
		}
		out = append(out, c.id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

// previewSourcePath admits only existing files beneath Apple's attachment root.
func previewSourcePath(root, filename string) (string, error) {
	if strings.HasPrefix(filename, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		filename = filepath.Join(home, strings.TrimPrefix(filename, "~/"))
	}
	if !filepath.IsAbs(filename) {
		filename = filepath.Join(root, filename)
	}
	path, err := filepath.EvalSymlinks(filename)
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("URL preview is outside attachment root")
	}
	return path, nil
}

// classifyPreviewFile accepts only small, decodable local image types. The
// pluginPayloadAttachment suffix is not trusted as a MIME or file extension.
func classifyPreviewFile(path string) (previewFile, error) {
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() {
		return previewFile{}, fmt.Errorf("URL preview is not a regular file")
	}
	if info.Size() > 20<<20 {
		return previewFile{}, fmt.Errorf("URL preview exceeds 20 MiB")
	}
	f, err := os.Open(path)
	if err != nil {
		return previewFile{}, err
	}
	defer f.Close()
	var head [8]byte
	if _, err := f.Read(head[:]); err != nil {
		return previewFile{}, err
	}
	asset := previewFile{path: path, size: info.Size()}
	switch {
	case bytes.HasPrefix(head[:], []byte("\x89PNG\r\n\x1a\n")):
		asset.ext = ".png"
	case bytes.HasPrefix(head[:], []byte("\xff\xd8\xff")):
		asset.ext = ".jpg"
	case bytes.HasPrefix(head[:], []byte{0, 0, 1, 0}):
		asset.ext = ".ico"
		return asset, nil
	default:
		return previewFile{}, fmt.Errorf("unsupported URL preview image type")
	}
	if _, err := f.Seek(0, 0); err != nil {
		return previewFile{}, err
	}
	cfg, _, err := image.DecodeConfig(f)
	if err != nil || cfg.Width <= 0 || cfg.Height <= 0 || cfg.Width > 8192 || cfg.Height > 8192 {
		return previewFile{}, fmt.Errorf("invalid URL preview image dimensions")
	}
	asset.area = int64(cfg.Width) * int64(cfg.Height)
	return asset, nil
}
