// Package stats holds the summary statistics texas2step reports, as the server's stats.py
// defines them (and web/src/engine/stats.ts); docs/methodology.md explains them.
package stats

import (
	"math"
	"slices"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
)

// Percentile is the linear-interpolated percentile, q in [0, 100]; NaN of no values.
func Percentile(values []float64, q float64) float64 {
	if len(values) == 0 {
		return math.NaN()
	}
	ordered := slices.Clone(values)
	slices.Sort(ordered)
	pos := float64(len(ordered)-1) * q / 100.0
	lo, hi := math.Floor(pos), math.Ceil(pos)
	if lo == hi {
		return ordered[int(lo)]
	}
	a, b := ordered[int(lo)], ordered[int(hi)]
	return a + (b-a)*(pos-lo)
}

// Jitter is the mean absolute difference between consecutive samples (RFC 3550 style,
// unsmoothed).
func Jitter(values []float64) float64 {
	if len(values) < 2 {
		return 0
	}
	diffs := make([]float64, len(values)-1)
	for i := 1; i < len(values); i++ {
		diffs[i-1] = math.Abs(values[i] - values[i-1])
	}
	return num.Sum(diffs) / float64(len(values)-1)
}

// Summary is a latency-style summary in milliseconds; its figures are null without samples.
type Summary struct {
	Count   int        `json:"count"`
	Lost    int        `json:"lost"`
	Min     *float64   `json:"min"`
	Max     *float64   `json:"max"`
	Mean    *float64   `json:"mean"`
	Median  *float64   `json:"median"`
	P25     *float64   `json:"p25"`
	P75     *float64   `json:"p75"`
	P90     *float64   `json:"p90"`
	Stdev   *float64   `json:"stdev"`
	Jitter  *float64   `json:"jitter"`
	Samples *[]float64 `json:"samples,omitempty"`
}

// Summarize summarizes samples kept in arrival order, for jitter; with keep, the samples
// come too, to the microsecond.
func Summarize(samples []float64, lost int, keep bool) Summary {
	out := Summary{Count: len(samples), Lost: lost}
	if len(samples) > 0 {
		mean := num.Sum(samples) / float64(len(samples))
		squares := make([]float64, len(samples))
		for i, s := range samples {
			squares[i] = (s - mean) * (s - mean)
		}
		out.Min = num.Ptr(slices.Min(samples))
		out.Max = num.Ptr(slices.Max(samples))
		out.Mean = num.Ptr(mean)
		out.Median = num.Ptr(Percentile(samples, 50))
		out.P25 = num.Ptr(Percentile(samples, 25))
		out.P75 = num.Ptr(Percentile(samples, 75))
		out.P90 = num.Ptr(Percentile(samples, 90))
		out.Stdev = num.Ptr(math.Sqrt(num.Sum(squares) / float64(len(samples))))
		out.Jitter = num.Ptr(Jitter(samples))
	}
	if keep {
		kept := make([]float64, len(samples))
		for i, s := range samples {
			kept[i] = num.Round(s, 3)
		}
		out.Samples = &kept
	}
	return out
}

// WarmupSeconds is the ramp-up left out of the stable throughput figure (TCP slow start,
// window growth).
func WarmupSeconds(duration float64) float64 {
	return math.Min(math.Max(0.2*duration, 1.0), 0.4*duration)
}

// Throughput is a transfer's timeline, and its stable and peak rates in Mb/s.
type Throughput struct {
	Mbps      float64    `json:"mbps"`
	PeakMbps  float64    `json:"peak_mbps"`
	Bytes     int64      `json:"bytes"`
	DurationS float64    `json:"duration_s"`
	WarmupS   float64    `json:"warmup_s"`
	BinMs     float64    `json:"bin_ms"`
	Timeline  []float64  `json:"timeline"`
	RateLimit *RateLimit `json:"rate_limit"`
}

// RateLimit is where a transfer's rate stepped down and stayed down to the end: a shaper, a
// policer or the end of an ISP's speed boost. BeforeBytes is what the transfer moved first.
type RateLimit struct {
	FromMbps    float64 `json:"from_mbps"`
	ToMbps      float64 `json:"to_mbps"`
	AtS         float64 `json:"at_s"`
	BeforeBytes int64   `json:"before_bytes"`
}

// SummarizeThroughput turns per-bin byte counts into a timeline and rates.
func SummarizeThroughput(bins []int64, binMs, duration float64) Throughput {
	scale := 8 / (binMs * 1000) // bytes per bin -> Mb/s
	timeline := make([]float64, len(bins))
	var total int64
	for i, b := range bins {
		timeline[i] = float64(b) * scale
		total += b
	}
	warm := WarmupSeconds(duration)
	first := min(len(timeline), int(math.Ceil(warm*1000/binMs)))
	steady := timeline[first:]
	if len(steady) == 0 {
		steady = timeline
	}
	out := Throughput{Bytes: total, DurationS: duration, WarmupS: warm, BinMs: binMs}
	if len(steady) > 0 {
		out.Mbps = num.Sum(steady) / float64(len(steady))
	}
	if len(timeline) > 0 {
		out.PeakMbps = Percentile(timeline, 95)
	}
	out.Timeline = make([]float64, len(timeline))
	for i, v := range timeline {
		out.Timeline[i] = num.Round(v, 3)
	}
	out.RateLimit = Limit(timeline, bins, binMs, first)
	return out
}

// A rate limit's rules, as stats.py's.
const (
	limitHighS = 1.0  // the higher rate lasts at least this long after ramp-up
	limitLowS  = 2.0  // and the lower rate at least this long, to the end
	limitShare = 0.75 // the lower rate is at most this share of the higher
)

// Limit is where a transfer's rate stepped down and stayed down; nil when it didn't. The bins
// after ramp-up (from first) are split where a higher level then a lower one fit them best,
// and the split must pass stats.py's rate_limit tests.
func Limit(timeline []float64, bins []int64, binMs float64, first int) *RateLimit {
	xs := timeline[first:]
	n := len(xs)
	high := int(math.Ceil(limitHighS * 1000 / binMs))
	low := int(math.Ceil(limitLowS * 1000 / binMs))
	if n < high+low {
		return nil
	}
	// Running sums, added left to right, so this splits where stats.py does.
	heads := make([]float64, n)
	sum := 0.0
	for i, v := range xs {
		sum += v
		heads[i] = sum
	}
	total := heads[n-1]
	best, split := 0.0, 0
	// Sought everywhere, then held to the two lengths.
	for k := 1; k < n; k++ {
		head := heads[k-1]
		rest := total - head
		if head/float64(k) <= rest/float64(n-k) {
			continue // not a step down
		}
		if fit := head*head/float64(k) + rest*rest/float64(n-k); fit > best {
			best, split = fit, k
		}
	}
	if split < high || n-split < low {
		return nil
	}
	from := heads[split-1] / float64(split)
	to := (total - heads[split-1]) / float64(n-split)
	last := (total - heads[n-high-1]) / float64(high)
	if !(0 < to && to <= limitShare*from && Percentile(xs[split:], 90) < Percentile(xs[:split], 50) && last <= limitShare*from) {
		return nil
	}
	at := first + split
	var before int64
	for _, b := range bins[:at] {
		before += b
	}
	return &RateLimit{FromMbps: from, ToMbps: to, AtS: float64(at) * binMs / 1000, BeforeBytes: before}
}

var bloatLimits = []struct {
	grade string
	limit float64
}{{"A+", 5}, {"A", 30}, {"B", 60}, {"C", 200}, {"D", 400}}

// BufferbloatGrade grades the latency a load added, in ms; nil without a figure.
func BufferbloatGrade(added *float64) *string {
	if added == nil {
		return nil
	}
	for _, b := range bloatLimits {
		if *added < b.limit {
			grade := b.grade
			return &grade
		}
	}
	f := "F"
	return &f
}
