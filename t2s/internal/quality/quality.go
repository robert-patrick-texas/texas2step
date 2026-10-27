// Package quality works out what a test's figures mean for calls and for responsiveness,
// as the server's quality.py defines it (and web/src/engine/quality.ts): the packet-loss
// test's round trips give a call-quality score, and the latency-under-load probes give
// responsiveness in round trips per minute. docs/methodology.md explains them.
package quality

import (
	"math"
	"slices"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
)

const (
	CallMs      = 20 // a call's packet interval
	BufferMs    = 60 // a call's jitter buffer: a packet further behind the fastest is too late
	CallDelayMs = CallMs + BufferMs
	// The E-model (ITU-T G.107) for G.711 with packet-loss concealment.
	bpl        = 25.1
	rDefault   = 93.2
	concealed  = 60  // a longer gap is heard: at best "good"
	syllableMs = 200 // a gap this long takes a syllable: at best "fair"
	trim       = 0.05
)

// Ratings, worst first, and the lowest MOS of each, read to one decimal as it is shown.
var (
	Order   = []string{"bad", "poor", "fair", "good", "excellent"}
	ratings = []struct {
		floor float64
		name  string
	}{{4.3, "excellent"}, {4.0, "good"}, {3.6, "fair"}, {3.1, "poor"}}
)

// Mos is the mean opinion score, 1 to 4.5, from the E-model's rating R (G.107 Annex B).
func Mos(r float64) float64 {
	if r <= 0 {
		return 1
	}
	if r >= 100 {
		return 4.5
	}
	return 1 + 0.035*r + 7e-6*r*(r-60)*(100-r)
}

// Rating names a score as it is shown.
func Rating(score float64) string {
	shown := num.HalfUp(score, 1)
	for _, r := range ratings {
		if shown >= r.floor {
			return r.name
		}
	}
	return "bad"
}

// Rank is a rating's place in Order, worst first.
func Rank(rating string) int { return slices.Index(Order, rating) }

// Heard is the rating as a caller would hear it: at most what the longest gap allows.
func Heard(rating string, longestGapMs float64) string {
	var best string
	switch {
	case longestGapMs >= syllableMs:
		best = "fair"
	case longestGapMs > concealed:
		best = "good"
	default:
		return rating
	}
	if Rank(best) < Rank(rating) {
		return best
	}
	return rating
}

// RFactor is the E-model's R for a call with this mouth-to-ear delay, with this share of
// its packets lost or too late, in bursts this much longer than random loss would give.
func RFactor(delayMs, impairedPct, burstRatio float64) float64 {
	delay := 0.024*delayMs + 0.11*math.Max(0, delayMs-177.3)
	loss := 95 * impairedPct / (impairedPct/burstRatio + bpl)
	return rDefault - delay - loss
}

// BurstRatio is how much longer the runs of impaired packets are than random loss would
// make them (the ITU's BurstR); nil without any.
func BurstRatio(impaired []bool) *float64 {
	count, runs := 0, 0
	for i, bad := range impaired {
		if bad {
			count++
			if i == 0 || !impaired[i-1] {
				runs++
			}
		}
	}
	if count == 0 {
		return nil
	}
	c := float64(count)
	return num.Ptr(c / float64(runs) * (1 - c/float64(len(impaired))))
}

func longestRun(impaired []bool) int {
	longest, run := 0, 0
	for _, bad := range impaired {
		if bad {
			run++
		} else {
			run = 0
		}
		longest = max(longest, run)
	}
	return longest
}

// OneWay is each direction's delays, above its fastest, and the probes lost on the way to
// the server.
type OneWay struct {
	UpMs         num.Floats `json:"up_ms"`
	DownMs       num.Floats `json:"down_ms"`
	LostUpstream []int      `json:"lost_upstream"`
}

// FromStamps works out each direction from the server's stamps: for each probe, when it
// was sent, when the server got it (by its own clock, NaN if not echoed), how many it had
// got by then (-1 if unknown), and when the echo came back (NaN if it didn't). total is how
// many the server got in all, -1 if unknown. Between two echoes, the server's counts tell
// how many of the probes in between reached it; those lost on the way there are taken to
// be the first. Nil without any echo.
func FromStamps(sent, server, back []float64, received []int, total int) *OneWay {
	n := len(sent)
	var echoed []int
	for i := range n {
		if !math.IsNaN(server[i]) && !math.IsNaN(back[i]) {
			echoed = append(echoed, i)
		}
	}
	if len(echoed) == 0 {
		return nil
	}
	upMin, downMin := math.Inf(1), math.Inf(1)
	for _, i := range echoed {
		upMin = math.Min(upMin, server[i]-sent[i])
		downMin = math.Min(downMin, back[i]-server[i])
	}
	lost := []int{}
	prev, had := -1, 0
	for _, i := range append(slices.Clone(echoed), n) { // n: past the last probe
		var count int
		if i < n {
			count = max(received[i], 0)
		} else if total < 0 {
			count = had + 1
		} else {
			count = total + 1
		}
		missing := i - prev - 1
		reached := min(max(count-had-1, 0), missing)
		for s := prev + 1; s < prev+1+missing-reached; s++ {
			lost = append(lost, s)
		}
		prev, had = i, max(had, count)
	}
	out := &OneWay{UpMs: make(num.Floats, n), DownMs: make(num.Floats, n), LostUpstream: lost}
	for i := range n {
		out.UpMs[i], out.DownMs[i] = num.Null, num.Null
	}
	for _, i := range echoed {
		out.UpMs[i] = num.HalfUp(server[i]-sent[i]-upMin, 3)
		out.DownMs[i] = num.HalfUp(back[i]-server[i]-downMin, 3)
	}
	return out
}

// Side is a call over one path: each way, or the round trip.
type Side struct {
	Packets      int      `json:"packets"`
	Late         int      `json:"late"`
	LatePct      float64  `json:"late_pct"`
	ImpairedPct  float64  `json:"impaired_pct"`
	BurstRatio   *float64 `json:"burst_ratio"`
	LongestGapMs float64  `json:"longest_gap_ms"`
	R            float64  `json:"r"`
	Mos          float64  `json:"mos"`
	MosRating    string   `json:"mos_rating"`
	Rating       string   `json:"rating"`
}

// Call is a call's quality over the path the packet-loss test measured. Each way, Down is
// what you hear and Up what the other end hears, and the figures at the top are the worse
// one's.
type Call struct {
	Basis      string  `json:"basis"`
	IntervalMs float64 `json:"interval_ms"`
	DelayMs    float64 `json:"delay_ms"`
	Side
	Worse string `json:"worse,omitempty"`
	Down  *Side  `json:"down,omitempty"`
	Up    *Side  `json:"up,omitempty"`
}

// Probes is what call quality needs of a packet-loss test: each probe's round trip (NaN
// for a lost one), the interval, the median round trip and, with the server's stamps,
// each way's delays (nil without them).
type Probes struct {
	Packets    num.Floats
	IntervalMs float64
	MedianMs   float64
	Ways       *OneWay
}

func judge(seqs []int, lost, late []bool, step int, intervalMs, delayMs float64) Side {
	impaired := make([]bool, len(lost))
	var view []bool
	bad, lateCount := 0, 0
	for i := range lost {
		impaired[i] = lost[i] || late[i]
		if impaired[i] {
			bad++
		}
		if late[i] {
			lateCount++
		}
		if seqs[i]%step == 0 {
			view = append(view, impaired[i])
		}
	}
	pct := float64(bad) / float64(len(impaired)) * 100
	ratio := BurstRatio(view)
	burst := 1.0
	if ratio != nil && *ratio != 0 { // as Python's `ratio or 1.0`
		burst = *ratio
	}
	r := RFactor(delayMs, pct, burst)
	score := Mos(r)
	gap := float64(longestRun(view)) * intervalMs * float64(step)
	out := Side{
		Packets:      len(view),
		Late:         lateCount,
		LatePct:      float64(lateCount) / float64(len(late)) * 100,
		ImpairedPct:  pct,
		LongestGapMs: gap,
		R:            num.HalfUp(r, 1),
		Mos:          num.HalfUp(score, 2),
		MosRating:    Rating(score),
		Rating:       Heard(Rating(score), gap),
	}
	if ratio != nil {
		out.BurstRatio = num.Ptr(num.HalfUp(*ratio, 2))
	}
	return out
}

// CallQuality is a call's quality over these probes, nil without an echo. A probe is
// impaired when lost, or when it arrived more than the jitter buffer behind the fastest;
// the bursts count the probes a call's 20 ms packets would be.
func CallQuality(p Probes) *Call {
	n := len(p.Packets)
	fastest := math.Inf(1)
	for _, t := range p.Packets {
		if !math.IsNaN(t) {
			fastest = math.Min(fastest, t)
		}
	}
	if math.IsInf(fastest, 1) {
		return nil
	}
	step := max(1, int(num.HalfUp(CallMs/p.IntervalMs, 0)))
	delay := p.MedianMs/2 + CallDelayMs
	call := &Call{IntervalMs: p.IntervalMs * float64(step), DelayMs: delay}
	all := make([]int, n)
	for i := range n {
		all[i] = i
	}
	if w := p.Ways; w != nil {
		upstream := make(map[int]bool, len(w.LostUpstream))
		for _, i := range w.LostUpstream {
			upstream[i] = true
		}
		upLost, upLate := make([]bool, n), make([]bool, n)
		var reached []int
		var downLost, downLate []bool
		for i := range n {
			upLost[i] = upstream[i]
			upLate[i] = !math.IsNaN(w.UpMs[i]) && w.UpMs[i] > BufferMs
			if !upstream[i] {
				reached = append(reached, i)
				downLost = append(downLost, math.IsNaN(p.Packets[i]))
				downLate = append(downLate, !math.IsNaN(w.DownMs[i]) && w.DownMs[i] > BufferMs)
			}
		}
		up := judge(all, upLost, upLate, step, p.IntervalMs, delay)
		down := judge(reached, downLost, downLate, step, p.IntervalMs, delay)
		// The worse direction: the lower rating, then the lower score; what you hear first.
		call.Basis, call.Worse, call.Side = "each way", "down", down
		if Rank(up.Rating) < Rank(down.Rating) || (Rank(up.Rating) == Rank(down.Rating) && up.Mos < down.Mos) {
			call.Worse, call.Side = "up", up
		}
		call.Down, call.Up = &down, &up
		return call
	}
	lost, late := make([]bool, n), make([]bool, n)
	for i, t := range p.Packets {
		lost[i] = math.IsNaN(t)
		late[i] = !lost[i] && t-fastest > BufferMs
	}
	call.Basis, call.Side = "round trip", judge(all, lost, late, step, p.IntervalMs, delay)
	return call
}

// TrimmedMean is the mean of the fastest 95 %: the slowest 5 % of round trips are left out.
func TrimmedMean(values []float64) float64 {
	ordered := slices.Clone(values)
	slices.Sort(ordered)
	kept := ordered[:len(ordered)-int(math.Floor(float64(len(ordered))*trim))]
	return num.Total(kept) / float64(len(kept))
}

// RPM is round trips per minute from round-trip times in ms; nil without any.
func RPM(samples []float64) *int {
	if len(samples) == 0 {
		return nil
	}
	if mean := TrimmedMean(samples); mean > 0 {
		v := int(num.HalfUp(60000/mean, 0))
		return &v
	}
	return nil
}

// Connections are the new connections a transfer's responsiveness probes opened, a second
// apart: each one's setup in all, and its steps where they were timed one by one.
type Connections struct {
	SetupMs []float64  `json:"setup_ms"`
	TCPMs   []float64  `json:"tcp_ms,omitempty"`
	HTTPMs  []float64  `json:"http_ms,omitempty"`
	Lost    int        `json:"lost"`
	TLS     bool       `json:"tls"`
	TLSMs   *[]float64 `json:"tls_ms,omitempty"`
}

// Transfer is what responsiveness needs of a download or upload.
type Transfer struct {
	Connections   *Connections
	LoadedProbes  []float64 // GETs on the loaded connection itself, on HTTP/2 or 3
	LoadedSamples []float64 // the latency-under-load probes on their own connection
}

// Direction is one direction's responsiveness by the IETF draft's method.
type Direction struct {
	RPM        int      `json:"rpm"`
	ForeignRPM int      `json:"foreign_rpm"`
	LoadedRPM  *int     `json:"loaded_rpm"`
	SetupMs    float64  `json:"setup_ms"`
	LoadedMs   *float64 `json:"loaded_ms"`
	LoadedOn   string   `json:"loaded_on"`
}

// Responsiveness is round trips per minute under load: each direction's and the lower.
type Responsiveness struct {
	RPM         int        `json:"rpm"`
	DownloadRPM *int       `json:"download_rpm"`
	UploadRPM   *int       `json:"upload_rpm"`
	Basis       string     `json:"basis"`
	Download    *Direction `json:"download,omitempty"`
	Upload      *Direction `json:"upload,omitempty"`
	IdleSetupMs *float64   `json:"idle_setup_ms,omitempty"`
}

// foreignMs is a new connection's round trip: the mean of its steps', each the mean of its
// fastest 95 %. Timed as one, its open time is split evenly among the steps it took.
func foreignMs(c *Connections) float64 {
	if len(c.TCPMs) > 0 && len(c.HTTPMs) > 0 {
		steps := [][]float64{c.TCPMs}
		if c.TLSMs != nil && len(*c.TLSMs) > 0 {
			steps = append(steps, *c.TLSMs)
		}
		steps = append(steps, c.HTTPMs)
		means := make([]float64, len(steps))
		for i, s := range steps {
			means[i] = TrimmedMean(s)
		}
		return num.Total(means) / float64(len(steps))
	}
	if c.TLS {
		return TrimmedMean(c.SetupMs) / 3
	}
	return TrimmedMean(c.SetupMs) / 2
}

func side(t *Transfer) *Direction {
	if t == nil || t.Connections == nil || len(t.Connections.SetupMs) == 0 || foreignMs(t.Connections) <= 0 {
		return nil
	}
	foreign := int(num.HalfUp(60000/foreignMs(t.Connections), 0))
	onLoad := len(t.LoadedProbes) > 0
	samples := t.LoadedSamples
	if onLoad {
		samples = t.LoadedProbes
	}
	out := &Direction{RPM: foreign, ForeignRPM: foreign, SetupMs: num.HalfUp(TrimmedMean(t.Connections.SetupMs), 3), LoadedOn: "own connection"}
	if onLoad {
		out.LoadedOn = "loaded connection"
	}
	if len(samples) > 0 {
		loadedMs := TrimmedMean(samples)
		out.LoadedMs = num.Ptr(num.HalfUp(loadedMs, 3))
		if loadedMs != 0 {
			loaded := int(num.HalfUp(60000/loadedMs, 0))
			out.LoadedRPM = &loaded
			out.RPM = int(num.HalfUp(float64(foreign+loaded)/2, 0))
		}
	}
	return out
}

// Work out responsiveness from a test's transfers; idleSetupMs is what a new connection
// cost while idle, nil if unknown. With new connections timed during the transfers, by the
// IETF draft's method; otherwise from the latency-under-load probes alone. Nil without
// either.
func Work(download, upload *Transfer, idleSetupMs *float64) *Responsiveness {
	down, up := side(download), side(upload)
	if down != nil || up != nil {
		out := &Responsiveness{Basis: "ippm", Download: down, Upload: up}
		out.RPM = math.MaxInt
		for _, s := range []*Direction{down, up} {
			if s != nil {
				out.RPM = min(out.RPM, s.RPM)
			}
		}
		if down != nil {
			out.DownloadRPM = &down.RPM
		}
		if up != nil {
			out.UploadRPM = &up.RPM
		}
		if idleSetupMs != nil {
			out.IdleSetupMs = num.Ptr(num.HalfUp(*idleSetupMs, 3))
		}
		return out
	}
	samples := func(t *Transfer) []float64 {
		if t == nil {
			return nil
		}
		return t.LoadedSamples
	}
	out := &Responsiveness{Basis: "latency under load", DownloadRPM: RPM(samples(download)), UploadRPM: RPM(samples(upload))}
	if out.DownloadRPM == nil && out.UploadRPM == nil {
		return nil
	}
	out.RPM = math.MaxInt
	for _, v := range []*int{out.DownloadRPM, out.UploadRPM} {
		if v != nil {
			out.RPM = min(out.RPM, *v)
		}
	}
	return out
}
