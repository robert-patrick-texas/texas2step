package quality

import (
	"encoding/json"
	"testing"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/cases"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
)

func TestOneWayAsThePythonEngine(t *testing.T) {
	var found []struct {
		Sent       []float64
		StampAt    num.Floats `json:"stamp_at"`
		Back       num.Floats
		Counts     []*int
		Server     struct{ Rx int }
		Stamped    bool
		WantOneWay json.RawMessage `json:"want_one_way"`
	}
	if err := cases.Load("loss", &found); err != nil {
		t.Fatal(err)
	}
	for i, c := range found {
		if !c.Stamped {
			continue
		}
		counts := make([]int, len(c.Counts))
		for j, v := range c.Counts {
			counts[j] = -1
			if v != nil {
				counts[j] = *v
			}
		}
		var got any
		if ways := FromStamps(c.Sent, c.StampAt, c.Back, counts, c.Server.Rx); ways != nil {
			got = ways
		}
		if d := cases.Diff(got, c.WantOneWay); d != "" {
			t.Errorf("case %d: FromStamps:\n%s", i, d)
		}
	}
}

type loss struct {
	Error        string
	Packets      num.Floats
	IntervalMs   float64 `json:"interval_ms"`
	Rtt          struct{ Median *float64 }
	UpMs         num.Floats `json:"up_ms"`
	DownMs       num.Floats `json:"down_ms"`
	LostUpstream []int      `json:"lost_upstream"`
}

func TestCallQualityAsThePythonEngine(t *testing.T) {
	var found []struct {
		Loss loss
		Want json.RawMessage
	}
	if err := cases.Load("call_quality", &found); err != nil {
		t.Fatal(err)
	}
	for i, c := range found {
		var got any
		if c.Loss.Error == "" {
			p := Probes{Packets: c.Loss.Packets, IntervalMs: c.Loss.IntervalMs}
			if c.Loss.Rtt.Median != nil {
				p.MedianMs = *c.Loss.Rtt.Median
			}
			if c.Loss.UpMs != nil && c.Loss.DownMs != nil {
				p.Ways = &OneWay{UpMs: c.Loss.UpMs, DownMs: c.Loss.DownMs, LostUpstream: c.Loss.LostUpstream}
			}
			if call := CallQuality(p); call != nil {
				got = call
			}
		}
		if d := cases.Diff(got, c.Want); d != "" {
			t.Errorf("case %d: CallQuality:\n%s", i, d)
		}
	}
}

type transfer struct {
	Connections   *Connections
	LoadedProbes  []float64                    `json:"loaded_probes"`
	LoadedLatency *struct{ Samples []float64 } `json:"loaded_latency"`
}

func (t *transfer) input() *Transfer {
	if t == nil {
		return nil
	}
	in := &Transfer{Connections: t.Connections, LoadedProbes: t.LoadedProbes}
	if t.LoadedLatency != nil {
		in.LoadedSamples = t.LoadedLatency.Samples
	}
	return in
}

func TestResponsivenessAsThePythonEngine(t *testing.T) {
	var found []struct {
		Result struct {
			Latency *struct {
				SetupMs *float64 `json:"setup_ms"`
			}
			Download *transfer
			Upload   *transfer
		}
		Want json.RawMessage
	}
	if err := cases.Load("responsiveness", &found); err != nil {
		t.Fatal(err)
	}
	for i, c := range found {
		var idle *float64
		if c.Result.Latency != nil {
			idle = c.Result.Latency.SetupMs
		}
		var got any
		if r := Work(c.Result.Download.input(), c.Result.Upload.input(), idle); r != nil {
			got = r
		}
		if d := cases.Diff(got, c.Want); d != "" {
			t.Errorf("case %d: Work:\n%s", i, d)
		}
	}
	var rpms []struct {
		Samples []float64
		Want    json.RawMessage
	}
	if err := cases.Load("rpm", &rpms); err != nil {
		t.Fatal(err)
	}
	for i, c := range rpms {
		var got any
		if v := RPM(c.Samples); v != nil {
			got = *v
		}
		if d := cases.Diff(got, c.Want); d != "" {
			t.Errorf("rpm case %d:\n%s", i, d)
		}
	}
}
