package imessage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/joestump/msgbrowse/internal/archivepath"
	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

// PreserveExistingAttachments copies legacy iMessage media before an export root
// can be replaced. Missing originals are reported instead of silently accepted.
func PreserveExistingAttachments(ctx context.Context, st *store.Store, archiveRoot string) error {
	legacy, err := st.LegacyIMessageAttachments(ctx)
	if err != nil {
		return err
	}
	for _, a := range legacy {
		if err := ctx.Err(); err != nil {
			return err
		}
		retained, err := retainAttachment(archiveRoot, st.DataDir(), a.RelPath)
		if err != nil {
			return fmt.Errorf("preserve existing iMessage attachment %d (%s): %w", a.ID, a.RelPath, err)
		}
		if err := st.PromoteIMessageAttachment(ctx, a.ID, a.RelPath, retained); err != nil {
			return err
		}
	}
	return nil
}

// retainMessageAttachments makes independent copies before any SQLite insert.
// An already-retained attachment remains usable when its export file disappears.
func retainMessageAttachments(ctx context.Context, st *store.Store, archiveRoot string, msgs []signal.Message) error {
	mediaRoot := archivepath.IMessageMediaRoot(st.DataDir())
	for i := range msgs {
		m := &msgs[i]
		existing, err := st.IMessageAttachmentPaths(ctx, m.HashWithSource(source.IMessage), m.SourceGUID)
		if err != nil {
			return err
		}
		kept := m.Attachments[:0]
		for _, a := range m.Attachments {
			if err := ctx.Err(); err != nil {
				return err
			}
			retained, err := retainAttachment(archiveRoot, st.DataDir(), a.RelPath)
			if err != nil {
				// A source deletion is acceptable only when the very same message
				// already has a retained file with this displayed name.
				found := false
				for _, old := range existing[a.OriginalName] {
					if !strings.HasPrefix(old, archivepath.IMessagePermanentPrefix) {
						continue
					}
					path, ok := archivepath.Resolve(source.IMessage, archivepath.Roots{IMessagePermanent: mediaRoot}, "", old)
					if ok {
						if retainedFileValid(path) {
							found = true
							break
						}
					}
				}
				if found {
					continue
				}
				return fmt.Errorf("retain attachment %q for message %s: %w", a.RelPath, m.HashWithSource(source.IMessage), err)
			}
			a.RelPath = retained
			kept = append(kept, a)
		}
		m.Attachments = kept
	}
	return nil
}

// retainedFileValid checks that a content-addressed copy still matches its name.
func retainedFileValid(path string) bool {
	name := filepath.Base(path)
	if len(name) < sha256.Size*2 {
		return false
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() {
		return false
	}
	got, err := hashFile(path)
	return err == nil && got == name[:sha256.Size*2]
}

// retainAttachment copies one export file to a content-addressed, immutable path.
func retainAttachment(archiveRoot, dataDir, rel string) (string, error) {
	clean := filepath.Clean(rel)
	if filepath.IsAbs(rel) || clean != rel || !strings.HasPrefix(filepath.ToSlash(clean), "attachments/") {
		return "", fmt.Errorf("attachment is not a relative export path: %q", rel)
	}
	root, err := filepath.EvalSymlinks(archiveRoot)
	if err != nil {
		return "", err
	}
	src, err := filepath.EvalSymlinks(filepath.Join(archiveRoot, rel))
	if err != nil {
		return "", err
	}
	within, err := filepath.Rel(root, src)
	if err != nil || within == ".." || strings.HasPrefix(within, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("attachment escapes export root: %q", rel)
	}
	return retainSourceFile(src, dataDir, strings.ToLower(filepath.Ext(rel)))
}

// retainSourceFile copies validated source media into immutable archive storage.
// The caller must validate the source path and supply a safe, detected extension.
func retainSourceFile(src, dataDir, ext string) (string, error) {
	in, err := os.Open(src)
	if err != nil {
		return "", err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return "", fmt.Errorf("attachment is not a regular file: %q", src)
	}
	mediaRoot := archivepath.IMessageMediaRoot(dataDir)
	if mediaRoot == "" {
		return "", fmt.Errorf("iMessage data directory is empty")
	}
	if err := os.MkdirAll(mediaRoot, 0o700); err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp(mediaRoot, ".copy-*")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	h := sha256.New()
	_, copyErr := io.Copy(io.MultiWriter(tmp, h), in)
	if copyErr == nil {
		copyErr = tmp.Sync()
	}
	closeErr := tmp.Close()
	if copyErr != nil {
		return "", copyErr
	}
	if closeErr != nil {
		return "", closeErr
	}
	digest := hex.EncodeToString(h.Sum(nil))
	name := digest + ext
	path := filepath.Join(mediaRoot, digest[:2], name)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	if err := os.Link(tmp.Name(), path); err != nil {
		if !os.IsExist(err) {
			return "", err
		}
		current, hashErr := hashFile(path)
		if hashErr != nil || current != digest {
			return "", fmt.Errorf("retained attachment differs from its content hash: %q", path)
		}
	}
	return archivepath.IMessagePermanentPrefix + digest[:2] + "/" + name, nil
}
