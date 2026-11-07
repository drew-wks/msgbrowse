package imessage

import (
	"errors"
	"fmt"
	"html"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/joestump/msgbrowse/internal/signal"
)

// imessage-exporter 5.0.1's HTML output places Apple's message GUID in the
// Reveal in Messages anchor. The TXT output has the same timestamp and sender
// for each top-level message. Keep this version-sensitive pairing fail-closed.
var htmlGUIDAnchor = regexp.MustCompile(`<a title="Reveal in Messages app" href="sms://open\?message-guid=([A-Za-z0-9-]{36})">([^<]+)</a>`)
var htmlSender = regexp.MustCompile(`(?s)<span class="sender">(.*?)</span>`)
var htmlTag = regexp.MustCompile(`<[^>]+>`)
var errNoGUIDAnchors = errors.New("no iMessage GUID anchors in HTML export")

type guidEntry struct {
	guid string
	key  string
}

func guidKey(ts, sender string) string {
	return strings.Join(strings.Fields(ts), " ") + "\x00" + strings.TrimSpace(sender)
}

// parseHTMLGUIDs reads only the exporter-supplied GUID, date, and sender. A
// repeated anchor for a quoted reply is counted once. Extra quoted anchors
// with no TXT message are ignored by assignGUIDs; conflicting keys fail.
func parseHTMLGUIDs(path string) ([]guidEntry, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	s := string(data)
	anchors := htmlGUIDAnchor.FindAllStringSubmatchIndex(s, -1)
	if len(anchors) == 0 {
		return nil, errNoGUIDAnchors
	}
	seen := make(map[string]bool, len(anchors))
	entries := make([]guidEntry, 0, len(anchors))
	for _, m := range anchors {
		guid := s[m[2]:m[3]]
		if seen[guid] {
			continue
		}
		seen[guid] = true
		end := strings.Index(s[m[1]:], "</p>")
		if end < 0 {
			return nil, fmt.Errorf("GUID anchor without a closing message header")
		}
		header := s[m[1] : m[1]+end]
		sender := htmlSender.FindStringSubmatch(header)
		if sender == nil {
			return nil, fmt.Errorf("GUID anchor without a sender")
		}
		date := html.UnescapeString(s[m[4]:m[5]])
		canonical, _, ok := parseTimestamp(date)
		if !ok {
			return nil, fmt.Errorf("unsupported HTML GUID timestamp format")
		}
		name := html.UnescapeString(htmlTag.ReplaceAllString(sender[1], ""))
		entries = append(entries, guidEntry{guid: guid, key: guidKey(canonical, name)})
	}
	return entries, nil
}

// assignGUIDs pairs the TXT records with HTML GUIDs by date and sender. Where
// several messages share the same second and sender, both exporter formats
// preserve source order within that group. A missing or extra occurrence for a
// key is an error, so an exporter format change cannot silently misidentify it.
func assignGUIDs(msgs []signal.Message, entries []guidEntry) error {
	byKey := make(map[string][]string)
	for _, e := range entries {
		byKey[e.key] = append(byKey[e.key], e.guid)
	}
	need := make(map[string]int)
	for _, m := range msgs {
		need[guidKey(m.TimestampRaw, m.Sender)]++
	}
	for key, n := range need {
		if len(byKey[key]) != n {
			return fmt.Errorf("TXT/HTML GUID pairing mismatch: %d TXT messages, %d HTML GUIDs for a timestamp/sender group", n, len(byKey[key]))
		}
	}
	used := make(map[string]int)
	for i := range msgs {
		key := guidKey(msgs[i].TimestampRaw, msgs[i].Sender)
		msgs[i].SourceGUID = byKey[key][used[key]]
		used[key]++
	}
	return nil
}

// findGUIDHTML finds the matching HTML transcript. Exporter 5.0.1 can truncate
// long TXT and HTML filenames differently, so a unique key-multiset match is
// accepted when no same-stem file exists.
func findGUIDHTML(root, name string, msgs []signal.Message) (string, []guidEntry, error) {
	direct := filepath.Join(root, name+".html")
	if _, err := os.Stat(direct); err == nil {
		entries, err := parseHTMLGUIDs(direct)
		return direct, entries, err
	} else if !os.IsNotExist(err) {
		return "", nil, err
	}
	files, err := os.ReadDir(root)
	if err != nil {
		return "", nil, err
	}
	want := make(map[string]int)
	for _, m := range msgs {
		want[guidKey(m.TimestampRaw, m.Sender)]++
	}
	var match string
	var matched []guidEntry
	for _, f := range files {
		if f.IsDir() || !strings.HasSuffix(f.Name(), ".html") {
			continue
		}
		path := filepath.Join(root, f.Name())
		entries, err := parseHTMLGUIDs(path)
		if err != nil {
			if errors.Is(err, errNoGUIDAnchors) {
				continue
			}
			return "", nil, err
		}
		if len(entries) != len(msgs) {
			continue
		}
		got := make(map[string]int)
		for _, e := range entries {
			got[e.key]++
		}
		if len(got) != len(want) {
			continue
		}
		ok := true
		for key, n := range want {
			if got[key] != n {
				ok = false
				break
			}
		}
		if ok {
			if match != "" {
				return "", nil, fmt.Errorf("multiple HTML transcripts match a TXT conversation")
			}
			match, matched = path, entries
		}
	}
	if match == "" {
		return "", nil, fmt.Errorf("no HTML transcript matches a TXT conversation")
	}
	return match, matched, nil
}
