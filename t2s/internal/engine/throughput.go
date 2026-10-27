package engine

import (
	"context"
	"crypto/rand"
	"errors"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

const (
	BinMs = 100
	chunk = 1 << 20 // the payload pool's blocks, as payload.py's
)

func roundPy(v float64, digits int) float64 { return num.Round(v, digits) }

// Throughput is one direction's transfer.
type Throughput struct {
	stats.Throughput
	Direction     string               `json:"direction"`
	Streams       int                  `json:"streams"`
	PerStreamMbps []float64            `json:"per_stream_mbps"`
	LoadedLatency *stats.Summary       `json:"loaded_latency"`
	Connections   *quality.Connections `json:"connections,omitempty"`
	// On HTTP/2, GETs of /ping on the transfer's own connection, as the browser's.
	LoadedProbes []float64 `json:"loaded_probes,omitempty"`
}

// Transfer is what responsiveness needs of it.
func (t *Throughput) Transfer() *quality.Transfer {
	if t == nil {
		return nil
	}
	out := &quality.Transfer{Connections: t.Connections, LoadedProbes: t.LoadedProbes}
	if t.LoadedLatency != nil && t.LoadedLatency.Samples != nil {
		out.LoadedSamples = *t.LoadedLatency.Samples
	}
	return out
}

type bins struct {
	start time.Time
	bytes []atomic.Int64
}

func newBins(start time.Time, seconds float64) *bins {
	n := max(1, int(math.RoundToEven(seconds*1000/BinMs)))
	return &bins{start: start, bytes: make([]atomic.Int64, n)}
}

func (b *bins) add(n int, now time.Time) {
	i := int(now.Sub(b.start).Milliseconds() / BinMs)
	if i >= 0 && i < len(b.bytes) {
		b.bytes[i].Add(int64(n))
	}
}

func (b *bins) upTo(n int) []int64 {
	out := make([]int64, min(n, len(b.bytes)))
	for i := range out {
		out[i] = b.bytes[i].Load()
	}
	return out
}

var (
	poolOnce sync.Once
	pool     []byte // 8 MiB of random bytes, made once: what uploads send
)

func payload() []byte {
	poolOnce.Do(func() {
		pool = make([]byte, 8*chunk)
		_, _ = rand.Read(pool)
	})
	return pool
}

// upload is a request body of the pool's bytes, cap in all, ending at the deadline. Each
// chunk counts once the transport has taken it, when it asks for the next.
type upload struct {
	sent, cap int64
	deadline  time.Time
	pending   int
	bins      *bins
	stream    *atomic.Int64
}

func (u *upload) Read(p []byte) (int, error) {
	now := time.Now()
	if u.pending > 0 {
		u.bins.add(u.pending, now)
		u.stream.Add(int64(u.pending))
		u.pending = 0
	}
	if u.sent >= u.cap || !now.Before(u.deadline) {
		return 0, io.EOF
	}
	n := int(min(int64(len(p)), int64(chunk/4), u.cap-u.sent))
	data := payload()
	at := int(u.sent % int64(len(data)))
	copied := copy(p[:n], data[at:])
	if copied < n {
		copy(p[copied:n], data)
	}
	u.sent += int64(n)
	u.pending = n
	return n, nil
}

// MeasureThroughput downloads or uploads over streams parallel requests for seconds, with
// latency under load and new connections timed beside it when loaded is set.
func MeasureThroughput(ctx context.Context, t *Target, direction string, seconds float64, streams int, loaded bool, progress Progress, phase string) (*Throughput, error) {
	if phase == "" {
		phase = direction
	}
	test := t.Test
	if test == "" {
		test = NewSession() + NewSession()
	}
	c := t.Client(streams)
	defer c.CloseIdleConnections()
	if _, _, err := t.Ping(ctx, c, -1, 10*time.Second); err != nil {
		return nil, err
	}
	var info Info
	r, err := t.GetJSON(ctx, c, "/info", nil, &info)
	if err != nil {
		return nil, err
	}
	// On HTTP/2 the probes can share the transfer's own connection, as a browser's do.
	multiplexed := r.ProtoMajor >= 2
	capBytes := int64(1 << 30)
	if l := info.Limits.MaxDownloadBytes; direction == "download" && l != nil {
		capBytes = *l
	} else if l := info.Limits.MaxUploadBytes; direction == "upload" && l != nil {
		capBytes = *l
	}

	start := time.Now()
	deadline := start.Add(time.Duration(seconds * float64(time.Second)))
	b := newBins(start, seconds)
	perStream := make([]atomic.Int64, streams)
	side, stopSide := context.WithCancel(ctx)
	var wg sync.WaitGroup
	var prober *loadedLatency
	var conns *newConnections
	var onLoad []float64
	if loaded {
		prober = &loadedLatency{t: t}
		conns = &newConnections{t: t}
		wg.Go(func() { prober.run(side) })
		wg.Go(func() { conns.run(side) })
		if multiplexed {
			wg.Go(func() {
				if sleep(side, 125*time.Millisecond) != nil {
					return
				}
				for side.Err() == nil {
					started := time.Now()
					if rtt, _, err := t.Ping(side, c, int(started.UnixMilli()%1e9), 3*time.Second); err == nil && side.Err() == nil {
						onLoad = append(onLoad, rtt)
					}
					if sleep(side, 250*time.Millisecond-time.Since(started)) != nil {
						return
					}
				}
			})
		}
	}
	wg.Go(func() {
		ticker := time.NewTicker(250 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-side.Done():
				return
			case now := <-ticker.C:
				elapsed := now.Sub(start).Seconds()
				done := min(len(b.bytes), int(elapsed*1000/BinMs))
				window := b.upTo(done)[max(0, done-5):]
				var sum int64
				for _, v := range window {
					sum += v
				}
				mbps := 0.0
				if len(window) > 0 {
					mbps = float64(sum) * 8 / (float64(len(window)) * BinMs * 1000)
				}
				progress.emit(phase, Tick{Elapsed: elapsed, Mbps: mbps})
			}
		}
	})

	transfer, cancel := context.WithDeadline(ctx, deadline.Add(10*time.Second))
	defer cancel()
	worker := func(i int) error {
		for time.Now().Before(deadline) {
			var req *http.Request
			var err error
			if direction == "download" {
				q := url.Values{"bytes": {strconv.FormatInt(capBytes, 10)}, "test": {test}}
				req, err = t.request(transfer, http.MethodGet, "/down", q, nil)
			} else {
				body := &upload{cap: capBytes, deadline: deadline, bins: b, stream: &perStream[i]}
				req, err = t.request(transfer, http.MethodPost, "/up", url.Values{"test": {test}}, body)
			}
			if err != nil {
				return err
			}
			r, err := c.Do(req)
			if err != nil {
				if !time.Now().Before(deadline) {
					return nil
				}
				return err
			}
			if r.StatusCode >= 400 { // such as 503 from a traffic limit: not test data
				err := statusError(r)
				r.Body.Close()
				return err
			}
			if direction == "upload" {
				_, _ = io.Copy(io.Discard, r.Body)
				r.Body.Close()
				continue
			}
			buf := make([]byte, 64<<10)
			for {
				n, err := r.Body.Read(buf)
				now := time.Now()
				if !now.Before(deadline) {
					r.Body.Close()
					return nil
				}
				if n > 0 {
					b.add(n, now)
					perStream[i].Add(int64(n))
				}
				if err != nil {
					r.Body.Close()
					if errors.Is(err, io.EOF) {
						break
					}
					return err
				}
			}
		}
		return nil
	}
	errs := make([]error, streams)
	var workers sync.WaitGroup
	for i := range streams {
		workers.Go(func() { errs[i] = worker(i) })
	}
	workers.Wait()
	stopSide()
	wg.Wait()
	failed := 0
	for _, err := range errs {
		if err != nil {
			failed++
		}
	}
	if failed == streams {
		return nil, errs[0]
	}

	elapsed := min(time.Since(start).Seconds(), seconds)
	complete := b.upTo(max(1, int(elapsed*1000/BinMs)))
	out := &Throughput{Throughput: stats.SummarizeThroughput(complete, BinMs, seconds), Direction: direction, Streams: streams}
	for i := range perStream {
		out.PerStreamMbps = append(out.PerStreamMbps, float64(perStream[i].Load())*8/(elapsed*1e6))
	}
	if prober != nil {
		out.LoadedLatency = prober.summary()
		out.Connections = conns.summary()
		for _, v := range onLoad {
			out.LoadedProbes = append(out.LoadedProbes, roundPy(v, 3))
		}
	}
	return out, nil
}
