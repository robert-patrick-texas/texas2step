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
	Mbps      float64   `json:"mbps"`
	PeakMbps  float64   `json:"peak_mbps"`
	Bytes     int64     `json:"bytes"`
	DurationS float64   `json:"duration_s"`
	WarmupS   float64   `json:"warmup_s"`
	BinMs     float64   `json:"bin_ms"`
	Timeline  []float64 `json:"timeline"`
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
	return out
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
