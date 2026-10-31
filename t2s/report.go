package main

import (
	"fmt"
	"math"
	"os"
	"runtime"
	"strconv"
	"strings"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/term"
)

// The report's lines are texas2step run's (cli.py), so the two read alike.

func f(v *float64, unit string, digits int) string {
	if v == nil {
		return "—"
	}
	return num.Grouped(*v, digits) + unit
}

func fv(v float64, unit string, digits int) string { return f(&v, unit, digits) }

func latencyLine(label string, s *stats.Summary) string {
	if s == nil || s.Median == nil {
		return fmt.Sprintf("  %-14s —", label)
	}
	lost := ""
	if s.Lost > 0 {
		lost = fmt.Sprintf(", %d lost", s.Lost)
	}
	return fmt.Sprintf("  %-14s %14s   jitter %s   (min %s · p90 %s · %d samples%s)",
		label, f(s.Median, " ms", 2), f(s.Jitter, " ms", 2), f(s.Min, "", 2), f(s.P90, "", 2), s.Count, lost)
}

func throughputLine(label string, t *engine.Throughput, idle *float64) string {
	if t == nil {
		return fmt.Sprintf("  %-14s —", label)
	}
	added := ""
	if t.LoadedLatency != nil && t.LoadedLatency.Median != nil && idle != nil {
		added = fmt.Sprintf(" · loaded %+.1f ms", *t.LoadedLatency.Median-*idle)
	}
	return fmt.Sprintf("  %-14s %14s   peak %s   (%d streams%s)",
		label, fv(t.Mbps, " Mbps", 1), fv(t.PeakMbps, "", 1), t.Streams, added)
}

// limitLine is a transfer's rate limit, under its line; nothing when it had none.
func limitLine(t *engine.Throughput) []string {
	if t == nil || t.RateLimit == nil {
		return nil
	}
	l := t.RateLimit
	moved := float64(l.BeforeBytes) / 1e6
	before := fmt.Sprintf("%s MB", fv(moved, "", 0))
	if moved >= 1000 {
		before = fmt.Sprintf("%s GB", fv(moved/1000, "", 1))
	}
	return []string{fmt.Sprintf("  %-14s rate limit: %s until %ss, %s in, then %s to the end",
		"", fv(l.FromMbps, " Mbps", 1), strconv.FormatFloat(l.AtS, 'g', -1, 64), before, fv(l.ToMbps, " Mbps", 1))}
}

// boundLine says when the server's CPU or this computer, not the network, set a
// transfer's rate; nothing otherwise. The server's first: when it set the rate, this
// computer didn't.
func boundLine(t *engine.Throughput) []string {
	if t == nil {
		return nil
	}
	if t.ServerCPU != nil && *t.ServerCPU >= engine.Bound {
		return []string{fmt.Sprintf("  %-14s server CPU-bound: a worker ran at %.0f%% of a core, so the server, not the network, set this rate", "", *t.ServerCPU*100)}
	}
	var lines []string
	if t.DeviceBound == "window" && t.DeviceWindow != nil {
		lines = append(lines, fmt.Sprintf("  %-14s device-bound: this computer held the server back %.0f%% of the time, so it, not the network, set this rate", "", *t.DeviceWindow*100))
	}
	if t.DeviceBound != "" && t.DeviceCPU != nil && *t.DeviceCPU >= engine.Bound*float64(min(max(t.Streams, 1), runtime.GOMAXPROCS(0))) {
		lines = append(lines, fmt.Sprintf("  %-14s client CPU-bound: t2s ran at %.0f%% of a core, so this computer, not the network, set this rate", "", *t.DeviceCPU*100))
	}
	return lines
}

func oneStreamLine(r *engine.Result) []string {
	one, multi := r.OneStream, r.Download
	if one == nil {
		return nil
	}
	share := ""
	if multi != nil && multi.Mbps != 0 {
		share = fmt.Sprintf(" · %.0f %% of %d streams", one.Mbps/multi.Mbps*100, multi.Streams)
	}
	return append([]string{fmt.Sprintf("  %-14s %14s   peak %s   (a single file copy%s)",
		"One stream", fv(one.Mbps, " Mbps", 1), fv(one.PeakMbps, "", 1), share)}, append(limitLine(one), boundLine(one)...)...)
}

func lossLine(l *engine.Loss) string {
	if l == nil {
		return fmt.Sprintf("  %-14s —", "Loss")
	}
	if l.Error != "" {
		return fmt.Sprintf("  %-14s unavailable (%s)", "Loss", l.Error)
	}
	parts := []string{fmt.Sprintf("%d/%d lost", l.Lost, l.Sent)}
	if l.UpLossPct != nil {
		parts = append(parts, fmt.Sprintf("↑ %s ↓ %s", f(l.UpLossPct, " %", 2), f(l.DownLossPct, " %", 2)))
	}
	if l.Rtt.Median != nil {
		parts = append(parts, fmt.Sprintf("UDP RTT %s · jitter %s", f(l.Rtt.Median, " ms", 2), f(l.Rtt.Jitter, " ms", 2)))
	}
	return fmt.Sprintf("  %-14s %14s   (%s)", "Loss", fv(l.LossPct, " %", 2), strings.Join(parts, " · "))
}

func rated(s quality.Side) string {
	if s.Rating != s.MosRating {
		return s.Rating + ", for its gap (the score alone: " + s.MosRating + ")"
	}
	return s.Rating
}

func g(v float64) string { return strconv.FormatFloat(v, 'g', 6, 64) }

func callLine(c *quality.Call) string {
	if c == nil {
		return fmt.Sprintf("  %-14s —", "Call quality")
	}
	var detail string
	if c.Up != nil && c.Down != nil { // each direction timed with the server's stamps
		detail = fmt.Sprintf("you hear %.1f · they hear %.1f · longest gap ↓ %s ↑ %s ms · late ↓ %s ↑ %s",
			c.Down.Mos, c.Up.Mos, g(c.Down.LongestGapMs), g(c.Up.LongestGapMs),
			fv(c.Down.LatePct, " %", 2), fv(c.Up.LatePct, " %", 2))
	} else {
		detail = fmt.Sprintf("late %s · longest gap %s ms · from round trips", fv(c.LatePct, " %", 2), g(c.LongestGapMs))
	}
	return fmt.Sprintf("  %-14s %14s   %s   (%s)", "Call quality", fv(c.Mos, " MOS", 1), rated(c.Side), detail)
}

func rpmLine(r *quality.Responsiveness) string {
	if r == nil {
		return fmt.Sprintf("  %-14s —", "Responsiveness")
	}
	n := func(v *int) string {
		if v == nil {
			return "—"
		}
		return num.Grouped(float64(*v), 0)
	}
	detail := fmt.Sprintf("↓ %s · ↑ %s", n(r.DownloadRPM), n(r.UploadRPM))
	setup := math.Inf(-1)
	for _, d := range []*quality.Direction{r.Download, r.Upload} {
		if d != nil {
			setup = math.Max(setup, d.SetupMs)
		}
	}
	if !math.IsInf(setup, -1) { // the IETF draft's method, with new connections
		opened := ""
		if r.IdleSetupMs != nil {
			opened = f(r.IdleSetupMs, " ms", 1) + " idle, "
		}
		detail += " · a new connection " + opened + fv(setup, " ms", 1) + " under load"
	} else {
		detail += ", under load"
	}
	return fmt.Sprintf("  %-14s %10s RPM   (%s)", "Responsiveness", num.Grouped(float64(r.RPM), 0), detail)
}

func str(v *string) string {
	if v == nil {
		return "—"
	}
	return *v
}

func report(r *engine.Result) string {
	head := "t2s → " + r.Server.URL + "  (" + str(r.Server.Name)
	if r.Server.Version != nil {
		head += " · v" + *r.Server.Version
	}
	var idle *float64
	if r.Latency != nil {
		idle = r.Latency.Median
	}
	lines := []string{
		head + ")",
		fmt.Sprintf("client %s (%s, %s)", str(r.Client.IP), str(r.Client.Family), str(r.Client.Protocol)),
	}
	if r.Client.LocalIP != "" {
		mac := ""
		if r.Client.MAC != "" {
			mac = " · MAC " + r.Client.MAC
		}
		lines = append(lines, "this computer "+r.Client.LocalIP+mac)
	}
	var latency *stats.Summary
	if r.Latency != nil {
		latency = &r.Latency.Summary
	}
	lines = append(lines, "", latencyLine("Latency", latency), throughputLine("Download", r.Download, idle))
	lines = append(lines, limitLine(r.Download)...)
	lines = append(lines, boundLine(r.Download)...)
	lines = append(lines, oneStreamLine(r)...)
	lines = append(lines, throughputLine("Upload", r.Upload, idle))
	lines = append(lines, limitLine(r.Upload)...)
	lines = append(lines, boundLine(r.Upload)...)
	lines = append(lines, lossLine(r.Loss), callLine(r.Call), rpmLine(r.Responsiveness))
	if g := r.Bufferbloat.Grade; g != nil {
		lines = append(lines, fmt.Sprintf("  %-14s %14s", "Bufferbloat", *g))
	}
	if r.ID != "" {
		lines = append(lines, "", "saved: "+r.Server.URL+"/r/"+r.ID)
	}
	return strings.Join(lines, "\n")
}

// live is one line of progress on stderr, when it is a terminal.
type live struct{ on bool }

func newLive(enabled bool) *live { return &live{on: enabled && term.Is(os.Stderr)} }

func (l *live) progress(phase string, t engine.Tick) {
	if !l.on {
		return
	}
	var text string
	switch phase {
	case "latency":
		text = fmt.Sprintf("%-9s %10.2f ms     #%d", phase, t.Sample, t.Index+1)
	case "loss":
		text = fmt.Sprintf("%-9s %10d packets sent", phase, t.Sent)
	default:
		text = fmt.Sprintf("%-9s %10s Mbps   t=%4.1fs", phase, num.Grouped(t.Mbps, 1), t.Elapsed)
	}
	fmt.Fprint(os.Stderr, term.ClearLine+"  "+text)
}

func (l *live) clear() {
	if l.on {
		fmt.Fprint(os.Stderr, term.ClearLine)
	}
}
