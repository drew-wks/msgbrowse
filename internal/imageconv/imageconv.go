// Package imageconv transcodes archive images that browsers can't render
// (Apple HEIC/HEIF, TIFF) into cached JPEG derivatives, so the web UI can show
// them. Conversion runs at import time and is incremental + idempotent: each
// derivative is keyed by its source path and skipped if already present.
//
// It shells out to whatever image converter is on PATH (sips on macOS,
// ImageMagick `magick`/`convert`, or libheif's `heif-convert`). This is an
// optional, local, non-network dependency: with no converter present the
// pipeline is a no-op and the UI falls back to a placeholder tile (ADR-0014).
package imageconv

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/store"
)

// webRenderableExts are raster formats every target browser displays inline.
var webRenderableExts = map[string]bool{
	".jpg": true, ".jpeg": true, ".png": true, ".gif": true, ".webp": true, ".bmp": true,
}

// convertibleExts are image formats browsers can't show but converters can turn
// into JPEG (Apple HEIC/HEIF, TIFF).
var convertibleExts = map[string]bool{
	".heic": true, ".heif": true, ".heics": true, ".tif": true, ".tiff": true,
}

func ext(name string) string { return strings.ToLower(filepath.Ext(name)) }

// WebRenderable reports whether a browser can display the file inline as-is.
func WebRenderable(name string) bool { return webRenderableExts[ext(name)] }

// Convertible reports whether the file is an image format we transcode to JPEG.
func Convertible(name string) bool { return convertibleExts[ext(name)] }

// DerivedPath returns the cached-JPEG path for a source file under derivedDir,
// keyed by a digest of the absolute source path. Empty derivedDir disables it.
func DerivedPath(derivedDir, absSrc string) string {
	if derivedDir == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(absSrc))
	return filepath.Join(derivedDir, hex.EncodeToString(sum[:])[:24]+".jpg")
}

// Converter is a detected external image converter.
type Converter struct {
	Name string                         // tool name (for logs)
	args func(src, dst string) []string // command args producing a JPEG at dst
}

// Detect returns the first available converter on PATH, or ok=false if none.
// Order favors the highest-fidelity/most-available tool per platform.
func Detect() (Converter, bool) {
	candidates := []Converter{
		{Name: "sips", args: func(src, dst string) []string { // macOS, always present
			return []string{"-s", "format", "jpeg", src, "--out", dst}
		}},
		{Name: "magick", args: func(src, dst string) []string { // ImageMagick 7
			return []string{src, dst}
		}},
		{Name: "convert", args: func(src, dst string) []string { // ImageMagick 6
			return []string{src, dst}
		}},
		{Name: "heif-convert", args: func(src, dst string) []string { // libheif
			return []string{src, dst}
		}},
	}
	for _, c := range candidates {
		if _, err := exec.LookPath(c.Name); err == nil {
			return c, true
		}
	}
	return Converter{}, false
}

// convert writes a JPEG derivative of src to dst (atomically via a temp file).
// The temp name is unique per call (not dst+".tmp"): the same source path can
// appear in multiple attachment rows — a photo shared to a group or referenced
// by several messages — so two workers may target the same dst concurrently. A
// per-call temp keeps them from clobbering each other's in-flight file; both
// produce identical bytes, so the last rename wins harmlessly.
func (c Converter) convert(ctx context.Context, src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		return err
	}
	tmpf, err := os.CreateTemp(filepath.Dir(dst), "conv-*.jpg")
	if err != nil {
		return err
	}
	tmp := tmpf.Name()
	_ = tmpf.Close()
	cmd := exec.CommandContext(ctx, c.Name, c.args(src, tmp)...)
	if out, err := cmd.CombinedOutput(); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("%s: %w: %s", c.Name, err, strings.TrimSpace(string(out)))
	}
	return os.Rename(tmp, dst)
}

// Options configures a transcode run.
type Options struct {
	ArchiveRoot         string
	IMessageArchiveRoot string
	WhatsAppArchiveRoot string
	DataDir             string // derivatives are written to <DataDir>/derived
	Concurrency         int
	Force               bool // re-convert even if a derivative already exists
	Logger              *slog.Logger
}

// Summary reports what a run did.
type Summary struct {
	Scanned     int
	Converted   int
	Skipped     int // already had a derivative
	Missing     int // source file not present in the archive
	Failed      int
	NoConverter bool
	DurationMS  int64
}

// DerivedDir is the cache directory for transcoded JPEGs under a data dir.
func DerivedDir(dataDir string) string { return filepath.Join(dataDir, "derived") }

// Run transcodes every convertible image attachment that lacks a derivative.
// With no converter on PATH it logs once and returns NoConverter=true (not an
// error) so import still succeeds.
func Run(ctx context.Context, st *store.Store, opts Options) (Summary, error) {
	log := opts.Logger
	if log == nil {
		log = slog.Default()
	}
	start := time.Now()
	var sum Summary

	conv, ok := Detect()
	if !ok {
		log.Warn("no image converter found on PATH (sips / magick / convert / heif-convert); " +
			"HEIC/TIFF images will show a placeholder. Install one and re-run `msgbrowse media`.")
		sum.NoConverter = true
		return sum, nil
	}

	items, err := st.ListImageAttachments(ctx)
	if err != nil {
		return sum, err
	}
	derivedDir := DerivedDir(opts.DataDir)
	roots := archivepath.Roots{
		Signal:            opts.ArchiveRoot,
		IMessage:          opts.IMessageArchiveRoot,
		IMessagePermanent: archivepath.IMessageMediaRoot(opts.DataDir),
		WhatsApp:          opts.WhatsAppArchiveRoot,
	}
	workers := opts.Concurrency
	if workers <= 0 {
		workers = 6
	}
	log.Info("transcoding non-web images", "converter", conv.Name, "candidates", len(items), "workers", workers)

	var (
		mu   sync.Mutex
		jobs = make(chan store.MediaItem)
		wg   sync.WaitGroup
	)
	worker := func() {
		defer wg.Done()
		for it := range jobs {
			if ctx.Err() != nil {
				return
			}
			abs, ok := archivepath.Resolve(it.Source, roots, it.ConversationName, it.RelPath)
			res := "missing"
			if ok {
				if _, statErr := os.Stat(abs); statErr == nil {
					dst := DerivedPath(derivedDir, abs)
					if !opts.Force {
						if _, derr := os.Stat(dst); derr == nil {
							res = "skip"
						}
					}
					if res != "skip" {
						if cerr := conv.convert(ctx, abs, dst); cerr != nil {
							log.Debug("transcode failed", "src", abs, "error", cerr)
							res = "fail"
						} else {
							res = "ok"
						}
					}
				}
			}
			mu.Lock()
			switch res {
			case "ok":
				sum.Converted++
			case "skip":
				sum.Skipped++
			case "fail":
				sum.Failed++
			default:
				sum.Missing++
			}
			mu.Unlock()
		}
	}
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go worker()
	}
	for _, it := range items {
		if !Convertible(it.RelPath) {
			continue
		}
		sum.Scanned++
		select {
		case <-ctx.Done():
			goto done
		case jobs <- it:
		}
	}
done:
	close(jobs)
	wg.Wait()

	sum.DurationMS = time.Since(start).Milliseconds()
	log.Info("image transcode complete",
		"converted", sum.Converted, "skipped", sum.Skipped, "missing", sum.Missing,
		"failed", sum.Failed, "duration_ms", sum.DurationMS)
	// Cancellation/deadline is a clean stop, not a failure: every derivative was
	// written atomically and a re-run resumes (incremental skip), so report the
	// partial summary without surfacing ctx.Err().
	return sum, nil
}
