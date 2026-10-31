package main

import (
	"strings"
	"testing"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// The same lines as texas2step run's, in tests/test_cli.py (with t2s's name).
func TestDeviceBoundReadsAsTexas2stepRuns(t *testing.T) {
	f := func(v float64) *float64 { return &v }
	held := &engine.Throughput{Streams: 1, DeviceWindow: f(0.72), DeviceBound: "window", DeviceCPU: f(0.31)}
	want := "                 device-bound: this computer held the server back 72% of the time, so it, not the network, set this rate"
	if got := boundLine(held); len(got) != 1 || got[0] != want {
		t.Errorf("boundLine(window):\n got %q\nwant %q", got, want)
	}
	mine := &engine.Throughput{Streams: 1, DeviceWindow: f(0.03), DeviceBound: "cpu", DeviceCPU: f(0.96)}
	want = "                 client CPU-bound: t2s ran at 96% of a core, so this computer, not the network, set this rate"
	if got := boundLine(mine); len(got) != 1 || got[0] != want {
		t.Errorf("boundLine(cpu):\n got %q\nwant %q", got, want)
	}
	server := &engine.Throughput{Streams: 1, ServerCPU: f(0.93), DeviceWindow: f(0.72), DeviceBound: "window"}
	if got := boundLine(server); len(got) != 1 || !strings.Contains(got[0], "server CPU-bound") {
		t.Errorf("boundLine with the server's CPU full: %q", got)
	}
	if got := boundLine(&engine.Throughput{Streams: 1, DeviceWindow: f(0.05), DeviceCPU: f(0.2)}); got != nil {
		t.Errorf("boundLine held back a little: %q", got)
	}
}

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
