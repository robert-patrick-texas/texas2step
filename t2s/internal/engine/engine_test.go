package engine

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/cases"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
)

func TestLossSummaryAsThePythonEngine(t *testing.T) {
	var found []struct {
		Rtts       num.Floats
		Order      []int
		Duplicates int
		Server     *Counters
		IntervalMs float64 `json:"interval_ms"`
		Size       int
		Want       json.RawMessage
	}
	if err := cases.Load("loss", &found); err != nil {
		t.Fatal(err)
	}
	for i, c := range found {
		got := LossSummary(c.Rtts, c.Order, c.Duplicates, c.Server, "webrtc", c.IntervalMs, c.Size)
		if d := cases.Diff(got, c.Want); d != "" {
			t.Errorf("case %d: LossSummary:\n%s", i, d)
		}
	}
}

func TestBufferbloatAsThePythonEngine(t *testing.T) {
	type loaded struct {
		LoadedLatency *struct{ Median *float64 } `json:"loaded_latency"`
	}
	var found []struct {
		Result struct {
			Latency  struct{ Median *float64 }
			Download loaded
			Upload   loaded
		}
		Want json.RawMessage
	}
	if err := cases.Load("bufferbloat", &found); err != nil {
		t.Fatal(err)
	}
	median := func(l loaded) *float64 {
		if l.LoadedLatency == nil {
			return nil
		}
		return l.LoadedLatency.Median
	}
	for i, c := range found {
		got := WorkBufferbloat(c.Result.Latency.Median, median(c.Result.Download), median(c.Result.Upload))
		if d := cases.Diff(got, c.Want); d != "" {
			t.Errorf("case %d: WorkBufferbloat:\n%s", i, d)
		}
	}
}

func TestALossThatFailedIsItsErrorAlone(t *testing.T) {
	data, _ := json.Marshal(Loss{Error: "UDP path did not open"})
	if string(data) != `{"error":"UDP path did not open"}` {
		t.Errorf("got %s", data)
	}
}

func TestOnlyFamilyKeepsOneFamilysCandidates(t *testing.T) {
	sdp := "v=0\r\na=candidate:1 1 udp 1 192.0.2.7 5000 typ host\r\n" +
		"a=candidate:2 1 udp 1 2001:db8::7 5001 typ host\r\na=candidate:3 1 udp 1 abc.local 5002 typ host\r\n"
	got := onlyFamily(sdp, "ipv6")
	want := "v=0\r\na=candidate:2 1 udp 1 2001:db8::7 5001 typ host\r\na=candidate:3 1 udp 1 abc.local 5002 typ host\r\n"
	if got != want {
		t.Errorf("got %q", got)
	}
}

func TestServerCPUFillsEachTransfer(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/tests/abc" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"test":"abc","download":{"cpu":0.97,"bound":true},"upload":{"cpu":0.4,"bound":false}}`))
	}))
	defer srv.Close()
	target := NewTarget(srv.URL)
	target.Test = "abc"
	r := &Result{Download: &Throughput{}, OneStream: &Throughput{}, Upload: &Throughput{}}
	ServerCPU(context.Background(), target, r)
	if r.Download.ServerCPU == nil || *r.Download.ServerCPU != 0.97 || *r.Upload.ServerCPU != 0.4 {
		t.Errorf("got %v, %v", r.Download.ServerCPU, r.Upload.ServerCPU)
	}
	if r.OneStream.ServerCPU != nil {
		t.Errorf("one stream: the server said nothing, got %v", *r.OneStream.ServerCPU)
	}
}

func TestAWrongCodeSaysWhatTheServerSaid(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusForbidden)
			w.Write([]byte(`{"detail":"that isn't the current access code"}`))
			return
		}
		w.Write([]byte(`{"required":true}`))
	}))
	defer srv.Close()
	_, err := NewTarget(srv.URL).Unlock(context.Background(), "austin.123")
	if err == nil || err.Error() != "refused the code: that isn't the current access code" {
		t.Errorf("got %v", err)
	}
	if _, err := NewTarget(srv.URL).Unlock(context.Background(), ""); err != ErrNeedsCode {
		t.Errorf("no code: got %v", err)
	}
}

func TestDeviceVerdictGoesToTheServerFirstThenThisProgram(t *testing.T) {
	f := func(v float64) *float64 { return &v }
	r := &Result{
		Download:  &Throughput{Streams: 1, DeviceCPU: f(0.95)},
		OneStream: &Throughput{Streams: 1, DeviceCPU: f(0.95), ServerCPU: f(0.92), DeviceBound: "window"},
		Upload:    &Throughput{Streams: 1, DeviceCPU: f(0.5)},
	}
	DeviceVerdict(r)
	if r.Download.DeviceBound != "cpu" || r.OneStream.DeviceBound != "" || r.Upload.DeviceBound != "" {
		t.Errorf("DeviceVerdict: %q %q %q", r.Download.DeviceBound, r.OneStream.DeviceBound, r.Upload.DeviceBound)
	}
}

func TestProcessCPUCountsThisProgramsWork(t *testing.T) {
	before := processCPU()
	if before < 0 {
		t.Skip("no CPU figure on this system")
	}
	for x, end := 0, time.Now().Add(100*time.Millisecond); time.Now().Before(end); x++ {
		_ = x * x
	}
	if used := processCPU() - before; used < 0.05 {
		t.Errorf("processCPU rose %.3f s over 100 ms of work", used)
	}
}
