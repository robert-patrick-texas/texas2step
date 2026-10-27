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
// NAT and egress, and the latest events. Where the window is too narrow for a whole row,
// its detail goes under it, from the lane's column, as the page has it; narrower still,
// its figures too. A height other than 0 keeps it to the window, a row a line where the
// lines under them don't fit. events is how many events it lists, or -1 for as many as
// the height leaves room for, at least 3.
func Table(s Snapshot, width, height, events int, live bool) []string {
	right := ran(s)
	if live && s.Running {
		right += " · Ctrl-C stops"
	}
	var rows []Row
	var titles []string
	var starts []int
	for _, fam := range s.Families {
		servers := []string{}
		seen := map[string]bool{}
		starts = append(starts, len(rows))
		for _, r := range s.Rows {
			if r.Fam != fam {
				continue
			}
			if r.Server != "" && !seen[r.Server] {
				seen[r.Server] = true
				servers = append(servers, r.Server)
			}
			rows = append(rows, r)
		}
		title := fam
		if len(servers) > 0 {
			title += " · server " + strings.Join(servers, ", ")
		}
		titles = append(titles, title)
	}
	tint := func(r Row, text string) string {
		if r.Warn {
			return show.Yellow(text)
		}
		return show.Dim(text)
	}
	cells := make([][]string, len(rows))
	for i, r := range rows {
		cells[i] = []string{r.Method, r.Port, lane(r), state(r), figures(r), tint(r, r.Detail)}
	}
	// layout is every family's rows in one table, so their columns line up: cols of the
	// cells, gap spaces apart; the rest of each row's cells go under it, wrapped.
	layout := func(cols, gap int, spread bool) []string {
		cut := make([][]string, len(cells))
		for i, c := range cells {
			cut[i] = c[:cols]
		}
		table := show.Columns(cut, "  ", gap)
		at := 2
		for col := range 2 {
			w := 0
			for _, c := range cells {
				w = max(w, show.Width(c[col]))
			}
			at += w + gap
		}
		under := func(text string, style func(string) string) []string {
			var out []string
			if spread && text != "" {
				for _, l := range show.Wrap(strings.Split(text, " · "), " · ", width-at) {
					out = append(out, strings.Repeat(" ", at)+style(l))
				}
			}
			return out
		}
		var lines []string
		for f, title := range titles {
			end := len(rows)
			if f+1 < len(starts) {
				end = starts[f+1]
			}
			lines = append(lines, "", show.Bold(title))
			for i := starts[f]; i < end; i++ {
				lines = append(lines, table[i])
				if cols < 5 {
					lines = append(lines, under(cells[i][4], func(s string) string { return s })...)
				}
				if cols < 6 {
					lines = append(lines, under(rows[i].Detail, func(s string) string { return tint(rows[i], s) })...)
				}
			}
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
			lead := "  " + fam + "  "
			wrapped := []string{strings.Join(parts, " · ")}
			if spread {
				wrapped = show.Wrap(parts, " · ", width-show.Width(lead))
			}
			for j, l := range wrapped {
				if j > 0 {
					lead = strings.Repeat(" ", show.Width(lead))
				}
				lines = append(lines, lead+l)
			}
		}
		return lines
	}
	widest := func(lines []string) int {
		w := 0
		for _, l := range lines {
			w = max(w, show.Width(l))
		}
		return w
	}
	wide := widest(layout(6, 3, false))
	var body []string
	for _, l := range [][2]int{{6, 3}, {5, 2}, {4, 2}} {
		body = layout(l[0], l[1], true)
		if widest(body) <= width {
			break
		}
	}

	head := show.Bold("t2s diag") + " → " + s.Name + "  " + show.Dim(s.URL)
	edge := min(max(show.Width(head)+2+show.Width(right), wide), width) // "ran" at the window's edge, or the table's where the window is wider
	lines := []string{head + strings.Repeat(" ", max(2, edge-show.Width(head)-show.Width(right))) + show.Dim(right)}
	var you []string
	for _, fam := range s.Families {
		you = append(you, "Your "+fam+" "+show.Bold(s.You[fam]))
	}
	lines = append(lines, strings.Join(you, " · "))
	if s.Clock != "" {
		lines = append(lines, show.Dim(s.Clock))
	}
	// The window's last line holds the cursor; the events want 2 lines and 3 events.
	if height > 0 && len(lines)+len(body)+6 > height {
		body = layout(5, 2, false) // a row a line, cut at the window's edge
		if wide <= width {
			body = layout(6, 3, false)
		}
	}
	lines = append(lines, body...)
	if events < 0 {
		events = max(3, height-len(lines)-3)
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
