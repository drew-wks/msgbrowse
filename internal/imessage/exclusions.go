package imessage

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/joestump/msgbrowse/internal/config"
	"github.com/joestump/msgbrowse/internal/signal"
	"github.com/joestump/msgbrowse/internal/source"
	"github.com/joestump/msgbrowse/internal/store"
)

const (
	ShortCodeRule           = "short_code_sender"
	FilteredSMSRule         = "filtered_sms_sender"
	EmailReminderRule       = "email_appointment_reminder"
	AppointmentReminderRule = "appointment_reminder"
)

var shortCodeRe = regexp.MustCompile(`^[0-9]{5,6}(?:\s*\([^)]*\))?$`)
var filteredSMSRe = regexp.MustCompile(`(?i)\(smsf[pt](?:_[a-z0-9_]+)?\)$`)
var emailAddressRe = regexp.MustCompile(`^[^@\s]+@[^@\s]+\.[^@\s]+$`)

// ExclusionPolicy holds opt-in classifiers and exact conversation rules.
type ExclusionPolicy struct {
	ShortCodes           bool
	FilteredSMS          bool
	EmailReminders       bool
	AppointmentReminders bool
	Rules                []config.IMessageExclusion
}

// ExclusionPreview counts matches without opening the archive database or
// copying attachment bytes. Counts are per first matching rule.
type ExclusionPreview struct {
	Conversations int
	Messages      int
	Excluded      int
	Eligible      int
	SkippedLines  int
	ByRule        map[string]int
}

// ValidateExclusions rejects ambiguous rules before any archive writes.
func ValidateExclusions(policy ExclusionPolicy) error {
	seen := map[string]bool{ShortCodeRule: true, FilteredSMSRule: true, EmailReminderRule: true, AppointmentReminderRule: true}
	for i, rule := range policy.Rules {
		if strings.TrimSpace(rule.Name) == "" || strings.TrimSpace(rule.Conversation) == "" {
			return fmt.Errorf("imessage_exclusions[%d] requires name and conversation", i)
		}
		if strings.TrimSpace(rule.Name) != rule.Name || strings.TrimSpace(rule.Conversation) != rule.Conversation {
			return fmt.Errorf("imessage_exclusions[%d] has leading or trailing whitespace", i)
		}
		if seen[rule.Name] {
			return fmt.Errorf("duplicate imessage exclusion name %q", rule.Name)
		}
		seen[rule.Name] = true
	}
	return nil
}

// FilterExcluded retains messages with no matching rule. A rule with no
// body_contains excludes the entire named conversation.
func FilterExcluded(msgs []signal.Message, policy ExclusionPolicy) ([]signal.Message, map[string]int) {
	kept := make([]signal.Message, 0, len(msgs))
	counts := make(map[string]int)
	for _, msg := range msgs {
		matched := false
		if policy.ShortCodes && (shortCodeRe.MatchString(msg.Sender) || shortCodeRe.MatchString(msg.Conversation)) {
			counts[ShortCodeRule]++
			continue
		}
		if policy.FilteredSMS && (filteredSMSRe.MatchString(msg.Sender) || filteredSMSRe.MatchString(msg.Conversation)) {
			counts[FilteredSMSRule]++
			continue
		}
		if policy.EmailReminders && msg.Sender != "Me" &&
			(emailAddressRe.MatchString(msg.Sender) || emailAddressRe.MatchString(msg.Conversation)) &&
			strings.Contains(strings.ToLower(msg.Body), "appointment reminder") {
			counts[EmailReminderRule]++
			continue
		}
		if policy.AppointmentReminders && msg.Sender != "Me" &&
			strings.Contains(strings.ToLower(msg.Body), "appointment reminder") {
			counts[AppointmentReminderRule]++
			continue
		}
		for _, rule := range policy.Rules {
			if msg.Conversation != rule.Conversation {
				continue
			}
			if rule.BodyContains != "" && !strings.Contains(strings.ToLower(msg.Body), strings.ToLower(rule.BodyContains)) {
				continue
			}
			counts[rule.Name]++
			matched = true
			break
		}
		if !matched {
			kept = append(kept, msg)
		}
	}
	return kept, counts
}

// filterLedgerExcluded keeps owner-removed messages out of later imports.
func filterLedgerExcluded(msgs []signal.Message, ledger store.IMessageExclusionLedger) ([]signal.Message, int) {
	kept := make([]signal.Message, 0, len(msgs))
	excluded := 0
	for i := range msgs {
		msg := &msgs[i]
		if msg.SourceGUID != "" {
			if _, found := ledger.GUIDs[msg.SourceGUID]; found {
				excluded++
				continue
			}
		} else if _, found := ledger.Hashes[msg.HashWithSource(source.IMessage)]; found {
			excluded++
			continue
		}
		kept = append(kept, *msg)
	}
	return kept, excluded
}

// PreviewExclusions parses each TXT export without writing to the database,
// export directory, or permanent media tree. It reports counts, never bodies.
func PreviewExclusions(root string, policy ExclusionPolicy) (ExclusionPreview, error) {
	preview := ExclusionPreview{ByRule: make(map[string]int)}
	if err := ValidateExclusions(policy); err != nil {
		return preview, err
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return preview, fmt.Errorf("read imessage archive: %w", err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".txt") {
			continue
		}
		name := strings.TrimSuffix(entry.Name(), ".txt")
		msgs, skipped, err := parseFile(name, filepath.Join(root, entry.Name()), log)
		if err != nil {
			return preview, fmt.Errorf("parse %q: %w", entry.Name(), err)
		}
		kept, counts := FilterExcluded(msgs, policy)
		preview.Conversations++
		preview.Messages += len(msgs)
		preview.Eligible += len(kept)
		preview.Excluded += len(msgs) - len(kept)
		preview.SkippedLines += skipped
		for rule, count := range counts {
			preview.ByRule[rule] += count
		}
	}
	return preview, nil
}

// exclusionStateHash changes incremental state when policy changes, so a
// previously skipped message becomes eligible after its rule is removed.
func exclusionStateHash(fileHash string, policy ExclusionPolicy) (string, error) {
	if len(policy.Rules) == 0 && !policy.ShortCodes && !policy.FilteredSMS && !policy.EmailReminders && !policy.AppointmentReminders {
		return fileHash, nil
	}
	encoded, err := json.Marshal(policy)
	if err != nil {
		return "", fmt.Errorf("encode imessage exclusions: %w", err)
	}
	digest := sha256.Sum256(append(append([]byte(fileHash), 0), encoded...))
	return hex.EncodeToString(digest[:]), nil
}
