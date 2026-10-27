package stats

import (
	"encoding/json"
	"math"
	"testing"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/cases"
)

func TestPercentileAndJitterAsThePythonEngine(t *testing.T) {
	var ps []struct {
		Values []float64
		Q      float64
		Want   float64
	}
	if err := cases.Load("percentile", &ps); err != nil {
		t.Fatal(err)
	}
	for _, c := range ps {
		if got := Percentile(c.Values, c.Q); math.Abs(got-c.Want) > 1e-9 {
			t.Errorf("Percentile(%d values, %v) = %v, want %v", len(c.Values), c.Q, got, c.Want)
		}
	}
	var js []struct {
		Values []float64
		Want   float64
	}
	if err := cases.Load("jitter", &js); err != nil {
		t.Fatal(err)
	}
	for _, c := range js {
		if got := Jitter(c.Values); math.Abs(got-c.Want) > 1e-9 {
			t.Errorf("Jitter(%d values) = %v, want %v", len(c.Values), got, c.Want)
		}
	}
}

func TestSummarizeAsThePythonEngine(t *testing.T) {
	var found []struct {
		Samples []float64
		Lost    int
		Keep    bool `json:"keep_samples"`
		Want    json.RawMessage
	}
	if err := cases.Load("summarize", &found); err != nil {
		t.Fatal(err)
	}
	for _, c := range found {
		if d := cases.Diff(Summarize(c.Samples, c.Lost, c.Keep), c.Want); d != "" {
			t.Errorf("Summarize(%d samples):\n%s", len(c.Samples), d)
		}
	}
}

func TestSummarizeThroughputAsThePythonEngine(t *testing.T) {
	var found []struct {
		Bins    []int64
		BinMs   float64 `json:"bin_ms"`
		Seconds float64
		Want    json.RawMessage
	}
	if err := cases.Load("summarize_throughput", &found); err != nil {
		t.Fatal(err)
	}
	for _, c := range found {
		if d := cases.Diff(SummarizeThroughput(c.Bins, c.BinMs, c.Seconds), c.Want); d != "" {
			t.Errorf("SummarizeThroughput(%d bins, %vs):\n%s", len(c.Bins), c.Seconds, d)
		}
	}
}
