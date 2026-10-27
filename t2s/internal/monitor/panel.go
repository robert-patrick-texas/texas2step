package monitor

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"strings"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/show"
)

// What Monitor shows, as its page does: the tiles of each profile, and the chart below.

type tile struct {
	key, label string
}

var tiles = map[string][]tile{
	"path":  {{"latest", "Latest"}, {"median", "Median"}, {"p95", "95th pct"}, {"jitter", "Jitter"}, {"loss", "Loss"}, {"sent", "Probes"}},
	"voice": {{"mos", "Call quality"}, {"worst", "Worst 10s"}, {"below", "Below good"}, {"median", "Round trip"}, {"jitter", "Jitter"}, {"loss", "Loss"}},
	"video": {{"mos", "Call quality"}, {"worst", "Worst 10s"}, {"below", "Below good"}, {"frames", "Frames hit"}, {"median", "Round trip"}, {"loss", "Loss"}},
}

var ratingWord = map[string]string{"excellent": "Excellent", "good": "Good", "fair": "Fair", "poor": "Poor", "bad": "Bad"}

func mos(v *float64) string {
	if v == nil {
		return "—"
	}
	return fmt.Sprintf("%.1f MOS", *v)
}

// value is a figure as its tile shows it, with its small print.
func value(key string, f Figures, now float64) string {
	switch key {
	case "latest":
		return show.Ms(f.Latest)
	case "median":
		return show.Ms(f.Median)
	case "p95":
		return show.Ms(f.P95)
	case "jitter":
		return show.Ms(f.Jitter)
	case "loss":
		s := show.Pct(f.Loss)
		if f.Window > 0 {
			count := fmt.Sprintf(" (%s)", show.Count(f.Lost))
			if f.Lost > 0 {
				count = show.Red(count)
			}
			s += count
		}
		return s
	case "sent":
		return show.Count(f.Sent)
	case "mos":
		if f.Mos == nil {
			return "—"
		}
		return show.Rated(f.Rating, mos(f.Mos)+" "+ratingWord[f.Rating])
	case "worst":
		if f.Worst == nil {
			return "—"
		}
		return mos(f.Worst) + " " + show.Dim(ago(*f.WorstAt, now))
	case "below":
		if f.Below == nil {
			return "—"
		}
		s := show.Pct(f.Below) + fmt.Sprintf(" (%d of %d)", f.BelowCount, f.Slices)
		if f.BelowCount > 0 {
			return show.Red(s)
		}
		return s
	case "frames":
		return show.Pct(f.Frames)
	}
	return "—"
}

// WindowName is the window as the page names it: 1 min, 5 min, 15 min, or 45s.
func WindowName(seconds float64) string {
	if seconds >= 60 && math.Mod(seconds, 60) == 0 {
		return fmt.Sprintf("%.0f min", seconds/60)
	}
	return Span(seconds)
}

// ago is how long before now, as "40s ago" or "3 min ago".
func ago(t, now float64) string {
	s := math.Max(0, math.Round((now-t)/1000))
	if s < 120 {
		return fmt.Sprintf("%.0fs ago", s)
	}
	return fmt.Sprintf("%.0f min ago", math.Round(s/60))
}

func diff(key string, a, b Figures) string {
	signed := func(x, y *float64, format func(*float64) string, unit string) string {
		if x == nil || y == nil {
			return "—"
		}
		d := *y - *x
		sign := ""
		if d > 0 {
			sign = "+"
		} else if d < 0 {
			sign = "−"
		}
		abs := math.Abs(d)
		s := format(&abs)
		if unit != "" {
			s = strings.TrimSuffix(s, " %") + " " + unit
		}
		return sign + s
	}
	switch key {
	case "latest", "median", "p95", "jitter":
		pick := map[string]func(Figures) *float64{
			"latest": func(f Figures) *float64 { return f.Latest }, "median": func(f Figures) *float64 { return f.Median },
			"p95": func(f Figures) *float64 { return f.P95 }, "jitter": func(f Figures) *float64 { return f.Jitter },
		}[key]
		return signed(pick(a), pick(b), show.Ms, "")
	case "loss":
		return signed(a.Loss, b.Loss, show.Pct, "pts")
	case "below":
		return signed(a.Below, b.Below, show.Pct, "pts")
	case "frames":
		return signed(a.Frames, b.Frames, show.Pct, "pts")
	case "mos", "worst":
		x, y := a.Mos, b.Mos
		if key == "worst" {
			x, y = a.Worst, b.Worst
		}
		return signed(x, y, func(v *float64) string { return fmt.Sprintf("%.1f", *v) }, "")
	}
	return ""
}

// About is what the profile sends, for the panel's second line.
func About(c Config, two bool) string {
	window := WindowName(c.Window)
	switch c.Profile {
	case "voice":
		return fmt.Sprintf("Voice call: %d B every %s ms, scored each 10s · window %s", c.VoiceSize, trim(c.Every), window)
	case "video":
		return fmt.Sprintf("Video call: voice, and %s kb/s of video at %d frames a second, scored each 10s · window %s", trim(c.Video), VideoFPS, window)
	}
	to := ""
	if two {
		to = ", to two servers at the same moments"
	}
	return fmt.Sprintf("Path: %s probes/s, %d B%s · window %s", trim(c.Rate), c.Size, to, window)
}

func trim(v float64) string { return strings.TrimSuffix(fmt.Sprintf("%.1f", v), ".0") }

// ran is how long Monitor has run, against its limit where there is one.
func ran(s Snapshot) string {
	if s.Started == 0 {
		return ""
	}
	text := "ran " + Span(math.Floor((s.Now-s.Started)/1000))
	if s.Limit.Ms > 0 {
		text += " of " + Span(s.Limit.Ms/1000)
	}
	return text
}

const blocks = "▁▂▃▄▅▆▇█"

// strips are a server's last window in columns: the slowest round trip of each, scaled
// from zero, a mark for each column with a lost probe, and each 10s slice's rating.
func strips(v View, s Snapshot, cols int) (rtt, loss, calls, top string) {
	windowMs := s.Config.Window * 1000
	cols = max(10, min(cols, int(s.Config.Window)))
	span := windowMs / float64(cols)
	from := s.Now - windowMs
	worst := make([]float64, cols)
	lost := make([]bool, cols)
	seen := make([]bool, cols)
	for i := range worst {
		worst[i] = math.NaN()
	}
	peak := 0.0
	for _, p := range v.points {
		i := int((p.t - from) / span)
		if i < 0 || i >= cols {
			continue
		}
		seen[i] = true
		if math.IsNaN(p.rtt) {
			lost[i] = true
			continue
		}
		if math.IsNaN(worst[i]) || p.rtt > worst[i] {
			worst[i] = p.rtt
		}
		peak = math.Max(peak, p.rtt)
	}
	var r, l, c strings.Builder
	for i := range cols {
		switch {
		case !math.IsNaN(worst[i]) && peak > 0:
			level := min(len([]rune(blocks))-1, int(worst[i]/peak*float64(len([]rune(blocks))-1)+0.5))
			r.WriteRune([]rune(blocks)[level])
		case seen[i]:
			r.WriteString(" ")
		default:
			r.WriteString(show.Dim("·"))
		}
		switch {
		case lost[i]:
			l.WriteString(show.Red("x"))
		case seen[i]:
			l.WriteString(show.Dim("·"))
		default:
			l.WriteString(" ")
		}
		// The worst slice under the column: E excellent, G good, F fair, P poor, B bad.
		mark := ""
		rank := 5
		for _, sl := range v.Slices {
			a, b := (sl.Start-from)/span, (sl.End-from)/span
			if float64(i+1) <= a || float64(i) >= b {
				continue
			}
			if r := rankOf(sl.Rating()); r < rank {
				rank, mark = r, sl.Rating()
			}
		}
		if mark == "" {
			c.WriteString(" ")
		} else {
			c.WriteString(show.Rated(mark, strings.ToUpper(mark[:1])))
		}
	}
	if peak > 0 {
		top = "max " + show.Ms(&peak)
	}
	return r.String(), l.String(), c.String(), top
}

func rankOf(rating string) int {
	return map[string]int{"bad": 0, "poor": 1, "fair": 2, "good": 3, "excellent": 4}[rating]
}

// Panel is the screen Monitor redraws in place on a terminal, width columns wide.
func Panel(s Snapshot, width int, events int) []string {
	two := len(s.Dests) > 1
	var heads []string
	for _, v := range s.Dests {
		state := v.Transport
		if v.Quiet {
			state = show.Red("no answer")
		}
		heads = append(heads, strings.TrimSpace(v.Short+" "+v.Name)+" · "+state)
	}
	right := ran(s)
	if s.Running {
		right += " · Ctrl-C stops"
	} else {
		right += " · stopped"
	}
	head := show.Bold("t2s monitor") + " → " + strings.Join(heads, "  ·  ")
	gap := max(2, width-show.Width(head)-show.Width(right))
	lines := []string{head + strings.Repeat(" ", gap) + show.Dim(right), show.Dim(About(s.Config, two)), ""}

	keys := tiles[s.Profile]
	window := WindowName(s.Config.Window)
	label := func(t tile) string {
		if t.key == "loss" {
			return "Loss (" + window + ")"
		}
		return t.label
	}
	var rows [][]string
	if !two {
		var names, values []string
		for _, t := range keys {
			names = append(names, show.Dim(label(t)))
			values = append(values, value(t.key, s.Dests[0].Figures, s.Now))
		}
		rows = [][]string{names, values}
	} else {
		names := []string{""}
		for _, t := range keys {
			names = append(names, show.Dim(label(t)))
		}
		rows = append(rows, names)
		for _, v := range s.Dests {
			row := []string{v.Short + " " + v.Name}
			for _, t := range keys {
				row = append(row, value(t.key, v.Figures, s.Now))
			}
			rows = append(rows, row)
		}
		row := []string{show.Dim("B − A")}
		for _, t := range keys {
			row = append(row, show.Dim(diff(t.key, s.Dests[0].Figures, s.Dests[1].Figures)))
		}
		rows = append(rows, row)
	}
	lines = append(lines, show.Table(rows, "  ")...)
	lines = append(lines, "")

	cols := width - 10
	for _, v := range s.Dests {
		rtt, loss, calls, top := strips(v, s, cols)
		who := ""
		if two {
			who = v.Short + " "
		}
		lines = append(lines, fmt.Sprintf("  %-6s %s %s", who+"RTT", rtt, show.Dim(top)), fmt.Sprintf("  %-6s %s", strings.Repeat(" ", len(who))+"Loss", loss))
		if s.Profile != "path" {
			lines = append(lines, fmt.Sprintf("  %-6s %s", strings.Repeat(" ", len(who))+"Calls", calls))
		}
	}
	if len(s.Events) > 0 {
		lines = append(lines, "")
		for _, e := range s.Events[max(0, len(s.Events)-events):] {
			lines = append(lines, "  "+show.Dim(e.At.Format("15:04:05"))+"  "+e.Text)
		}
	}
	for i, l := range lines {
		lines[i] = show.Fit(l, width)
	}
	return lines
}

// Lines writes Monitor as lines: a line a second for each server with Path, a line for
// each 10s slice scored with a call profile, and events as they happen.
type Lines struct {
	W      io.Writer
	header bool
}

func (l *Lines) Event(e Event) {
	fmt.Fprintf(l.W, "%s  %s\n", e.At.Format("15:04:05"), e.Text)
}

func (l *Lines) Tick(s Snapshot) {
	two := len(s.Dests) > 1
	now := time.UnixMilli(int64(s.Now)).Format("15:04:05")
	if s.Profile == "path" {
		if !l.header {
			l.header = true
			who := ""
			if two {
				who = "  server"
			}
			fmt.Fprintf(l.W, "%-8s%s  %-9s  %-9s  %-9s  %-9s  %-15s %s\n", "time", who, "latest", "median", "p95", "jitter", "loss ("+WindowName(s.Config.Window)+")", "probes")
		}
		for _, v := range s.Dests {
			f := v.Figures
			who := ""
			if two {
				who = "  " + show.Pad(v.Short, 6)
			}
			loss := show.Pct(f.Loss)
			if f.Window > 0 {
				loss += fmt.Sprintf(" (%d)", f.Lost)
			}
			fmt.Fprintf(l.W, "%s%s  %-9s  %-9s  %-9s  %-9s  %-15s %s\n", now, who, show.Ms(f.Latest), show.Ms(f.Median), show.Ms(f.P95), show.Ms(f.Jitter), loss, show.Count(f.Sent))
		}
		return
	}
	for _, v := range s.Dests {
		for _, sl := range v.New {
			f := v.Figures
			who := ""
			if two {
				who = "  " + v.Short
			}
			at := time.UnixMilli(int64(sl.End)).Format("15:04:05")
			score := "no packet came back"
			if !sl.Dead && sl.Call != nil {
				score = fmt.Sprintf("%.1f MOS %s", sl.Call.Mos, sl.Call.Rating)
			}
			frames := ""
			if f.Frames != nil {
				frames = " · frames hit " + show.Pct(f.Frames)
			}
			fmt.Fprintf(l.W, "%s%s  %s  (window: %s %s · worst %s · %d of %d below good · round trip %s · loss %s%s)\n",
				at, who, score, mos(f.Mos), f.Rating, mos(f.Worst), f.BelowCount, f.Slices, show.Ms(f.Median), show.Pct(f.Loss), frames)
		}
	}
}

// JSON writes Monitor as JSON lines: figures each second, slices as they are scored,
// events as they happen, and the totals at the end.
type JSON struct{ W io.Writer }

func (j JSON) put(v any) {
	data, _ := json.Marshal(v)
	fmt.Fprintln(j.W, string(data))
}

func (j JSON) Event(e Event) {
	j.put(map[string]any{"type": "event", "time": e.At.Format(time.RFC3339Nano), "text": e.Text})
}

func (j JSON) Tick(s Snapshot) {
	at := time.UnixMilli(int64(s.Now)).Format(time.RFC3339Nano)
	for _, v := range s.Dests {
		j.put(map[string]any{"type": "figures", "time": at, "server": v.Name, "side": v.Short, "transport": v.Transport,
			"quiet": v.Quiet, "profile": s.Profile, "window_s": s.Config.Window, "figures": v.Figures})
		for _, sl := range v.New {
			j.put(map[string]any{"type": "slice", "server": v.Name, "side": v.Short,
				"start": time.UnixMilli(int64(sl.Start)).Format(time.RFC3339Nano), "slice": sl})
		}
	}
}

func (j JSON) Totals(started, stopped time.Time, why string, totals []Total) {
	j.put(map[string]any{"type": "summary", "started": started.Format(time.RFC3339Nano), "stopped": stopped.Format(time.RFC3339Nano),
		"seconds": stopped.Sub(started).Seconds(), "ended": why, "servers": totals})
}

// Summary is the totals since Start, as text.
func Summary(started, stopped time.Time, why string, totals []Total) []string {
	lasted := Span(math.Floor(stopped.Sub(started).Seconds()))
	if why == "" {
		why = "Stopped."
	}
	lines := []string{fmt.Sprintf("Monitored for %s, %s to %s. %s", lasted, started.Format("15:04:05"), stopped.Format("15:04:05"), why)}
	for _, t := range totals {
		outages := ""
		switch t.Outages {
		case 0:
		case 1:
			outages = " · 1 outage"
		default:
			outages = fmt.Sprintf(" · %d outages", t.Outages)
		}
		lines = append(lines, fmt.Sprintf("  %s: %s probes · %s lost (%s) · median %s · p95 %s · jitter %s%s",
			t.Name, show.Count(t.Sent), show.Count(t.Lost), show.Pct(t.LossPct), show.Ms(t.Median), show.Ms(t.P95), show.Ms(t.Jitter), outages))
		if t.Slices > 0 {
			slices := "1 slice"
			if t.Slices != 1 {
				slices = fmt.Sprintf("%d slices", t.Slices)
			}
			lines = append(lines, fmt.Sprintf("    calls: %s of 10s · median %s · worst %s, in the 10s from %s · %d below good",
				slices, mos(t.Mos), mos(t.Worst), t.WorstAt, t.Below))
		}
	}
	return lines
}
