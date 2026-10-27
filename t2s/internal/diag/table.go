package diag

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/show"
)

func figures(r Row) string {
	if r.State == "Not offered" || r.State == "Restricted" {
		return ""
	}
	if r.Tone == "trying" {
		if r.Method == "UDP" {
			return "retrying every " + seconds(udpRetryMs)
		}
		return "retrying every second"
	}
	parts := []string{}
	if r.Tone == "up" {
		parts = append(parts, show.Ms(r.Latest))
	}
	parts = append(parts, "median "+show.Ms(r.Median), fmt.Sprintf("%d of %d lost in 1 min", r.Lost, r.Tried))
	return strings.Join(parts, " · ")
}

func state(r Row) string {
	switch r.State {
	case "Answering":
		return show.Green(r.State)
	case "Restricted":
		return show.Yellow(r.State)
	case "Not offered", "No answer yet":
		return show.Dim(r.State)
	}
	return show.Red(r.State)
}

// lane is the row's last attempts, oldest first: · answered, x missed, space not counted.
func lane(r Row) string {
	var b strings.Builder
	b.WriteString(strings.Repeat(" ", 20-len(r.Recent)))
	for _, v := range r.Recent {
		switch v {
		case 1:
			b.WriteString(show.Green("·"))
		case 0:
			b.WriteString(show.Red("x"))
		default:
			b.WriteString(show.Dim("-"))
		}
	}
	return b.String()
}

func ran(s Snapshot) string {
	text := "ran " + span(math.Floor(s.Ran))
	if s.Limit > 0 {
		text += " of " + span(s.Limit)
	}
	return text
}

// Table is Diag as a table, width columns wide: the rows of each family, the addresses,
// NAT and egress, and the latest events.
func Table(s Snapshot, width, events int, live bool) []string {
	right := ran(s)
	if live && s.Running {
		right += " · Ctrl-C stops"
	}
	head := show.Bold("t2s diag") + " → " + s.Name + "  " + show.Dim(s.URL)
	lines := []string{head + strings.Repeat(" ", max(2, width-show.Width(head)-show.Width(right))) + show.Dim(right)}
	var you []string
	for _, fam := range s.Families {
		you = append(you, "Your "+fam+" "+show.Bold(s.You[fam]))
	}
	lines = append(lines, strings.Join(you, " · "))
	if s.Clock != "" {
		lines = append(lines, show.Dim(s.Clock))
	}
	for _, fam := range s.Families {
		servers := []string{}
		seen := map[string]bool{}
		var rows [][]string
		for _, r := range s.Rows {
			if r.Fam != fam {
				continue
			}
			if r.Server != "" && !seen[r.Server] {
				seen[r.Server] = true
				servers = append(servers, r.Server)
			}
			detail := r.Detail
			if r.Warn {
				detail = show.Yellow(detail)
			} else {
				detail = show.Dim(detail)
			}
			rows = append(rows, []string{r.Method, r.Port, lane(r), state(r), figures(r), detail})
		}
		title := fam
		if len(servers) > 0 {
			title += " · server " + strings.Join(servers, ", ")
		}
		lines = append(lines, "", show.Bold(title))
		lines = append(lines, show.Table(rows, "  ")...)
	}
	lines = append(lines, "", show.Bold("NAT and egress"))
	order := []string{"This computer", "Over HTTPS (TCP)", "Over HTTP/3 (UDP)", "Over UDP (WebRTC)", "Compared", "NAT", "MAC"}
	for _, fam := range s.Families {
		var parts []string
		for _, k := range order {
			if v, ok := s.Egress[fam][k]; ok {
				parts = append(parts, show.Dim(k)+" "+v)
			}
		}
		lines = append(lines, "  "+fam+"  "+strings.Join(parts, " · "))
	}
	if events > 0 && len(s.Events) > 0 {
		lines = append(lines, "", show.Bold("Events"))
		list := append([]Event(nil), s.Events...)
		sort.SliceStable(list, func(i, j int) bool { return list[i].At.After(list[j].At) }) // the latest first, as the page lists them
		for _, e := range list[:min(events, len(list))] {
			lines = append(lines, "  "+show.Dim(e.At.Format("15:04:05"))+"  "+Said(e))
		}
	}
	for i, l := range lines {
		lines[i] = show.Fit(l, width)
	}
	return lines
}

// Said is an event as the log says it.
func Said(e Event) string {
	if e.Who != "" {
		return e.Who + ": " + e.Text
	}
	return e.Text
}

// Log writes events as lines as they happen.
func Log(w io.Writer, e Event) {
	fmt.Fprintf(w, "%s  %s\n", e.At.Format("15:04:05"), Said(e))
}

// JSONEvent writes an event as a JSON line.
func JSONEvent(w io.Writer, e Event) {
	data, _ := json.Marshal(map[string]any{"type": "event", "time": e.At.Format(time.RFC3339Nano), "row": e.Who, "text": e.Text})
	fmt.Fprintln(w, string(data))
}

// JSONSnapshot writes the rows as a JSON line.
func JSONSnapshot(w io.Writer, kind string, s Snapshot) {
	data, _ := json.Marshal(map[string]any{"type": kind, "time": time.UnixMilli(int64(s.Now)).Format(time.RFC3339Nano), "diag": s})
	fmt.Fprintln(w, string(data))
}
