package main

import (
	"testing"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// The same line as texas2step run's, in tests/test_cli.py.
func TestServerCPUBoundReadsAsTexas2stepRuns(t *testing.T) {
	cpu := 0.97
	want := "                 server CPU-bound: a worker ran at 97% of a core, so the server, not the network, set this rate"
	if got := boundLine(&engine.Throughput{ServerCPU: &cpu}); len(got) != 1 || got[0] != want {
		t.Errorf("boundLine:\n got %q\nwant %q", got, want)
	}
	cpu = 0.42
	if got := boundLine(&engine.Throughput{ServerCPU: &cpu}); got != nil {
		t.Errorf("boundLine under a full core: %q", got)
	}
	if got := boundLine(&engine.Throughput{}); got != nil {
		t.Errorf("boundLine without a figure: %q", got)
	}
}

// The same lines as texas2step run's, in tests/test_cli.py.
func TestARateLimitReadsAsTexas2stepRuns(t *testing.T) {
	limit := &stats.RateLimit{FromMbps: 938.2, ToMbps: 312.9, AtS: 4.2, BeforeBytes: 427_000_000}
	tr := &engine.Throughput{Throughput: stats.Throughput{RateLimit: limit}}
	want := "                 rate limit: 938.2 Mbps until 4.2s, 427 MB in, then 312.9 Mbps to the end"
	if got := limitLine(tr); len(got) != 1 || got[0] != want {
		t.Errorf("limitLine:\n got %q\nwant %q", got, want)
	}
	limit.AtS, limit.BeforeBytes = 12.0, 1_409_600_000
	want = "                 rate limit: 938.2 Mbps until 12s, 1.4 GB in, then 312.9 Mbps to the end"
	if got := limitLine(tr); len(got) != 1 || got[0] != want {
		t.Errorf("limitLine:\n got %q\nwant %q", got, want)
	}
	if got := limitLine(&engine.Throughput{}); got != nil {
		t.Errorf("limitLine without a limit: %q", got)
	}
}
