package engine

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"io"
	"math"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// Tick is progress to show while a measurement runs.
type Tick struct {
	Sample  float64 // a latency sample, ms
	Index   int
	Elapsed float64 // a transfer's seconds so far
	Mbps    float64
	Sent    int // probes sent
}

// Progress hears of a phase's ticks: "latency", "download", "upload", "1 stream", "loss".
type Progress func(phase string, t Tick)

func (p Progress) emit(phase string, t Tick) {
	if p != nil {
		p(phase, t)
	}
}

// Latency is idle latency's summary, with what a new connection cost.
type Latency struct {
	stats.Summary
	SetupMs *float64 `json:"setup_ms,omitempty"`
}

// Ping is one GET of /ping on c: its round trip less the server's own time, in ms.
// A transport error (no answer) is err with a nil StatusError; an error answer is a
// StatusError.
func (t *Target) Ping(ctx context.Context, c *http.Client, n int, timeout time.Duration) (float64, *http.Response, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var q url.Values
	if n >= 0 {
		q = url.Values{"r": {strconv.Itoa(n)}}
	}
	req, err := t.request(ctx, http.MethodGet, "/ping", q, nil)
	if err != nil {
		return 0, nil, err
	}
	t0 := time.Now()
	r, err := c.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer r.Body.Close()
	if r.StatusCode >= 400 {
		return 0, r, statusError(r)
	}
	if _, err := io.Copy(io.Discard, r.Body); err != nil {
		return 0, r, err
	}
	ms := float64(time.Since(t0).Nanoseconds())/1e6 - ServerTimingMs(r.Header.Get("Server-Timing"), "app")
	return ms, r, nil
}

// MeasureLatency is idle round-trip time over HTTP, the server's processing time taken out
// by Server-Timing. A ping without an answer is lost; one the server answers with an error,
// such as 401 for want of an access code, ends the measurement with it: a refusal isn't a
// lost ping.
func MeasureLatency(ctx context.Context, t *Target, count int, progress Progress) (*Latency, error) {
	c := t.Client(1)
	defer c.CloseIdleConnections()
	if _, _, err := t.Ping(ctx, c, -1, 10*time.Second); err != nil { // open + warm the connection
		return nil, err
	}
	var samples []float64
	lost := 0
	for i := range count {
		rtt, _, err := t.Ping(ctx, c, i, 2*time.Second)
		var se *StatusError
		if errors.As(err, &se) {
			return nil, err
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if err != nil {
			lost++
			continue
		}
		samples = append(samples, math.Max(rtt, 0))
		progress.emit("latency", Tick{Sample: rtt, Index: i})
	}
	return &Latency{Summary: stats.Summarize(samples, lost, true)}, nil
}

// Steps are a new connection's steps, in ms: the TCP connect, the TLS handshake (https
// only) and a first request.
type Steps struct {
	TCP, TLS, HTTP float64
	Secure         bool
}

// Total is the connection's setup in all.
func (s Steps) Total() float64 { return s.TCP + s.TLS + s.HTTP }

// NewConnection times a new connection's steps, as the IETF's responsiveness draft does;
// nil if one failed.
func NewConnection(ctx context.Context, t *Target, limit time.Duration) *Steps {
	u, err := url.Parse(t.Base)
	if err != nil {
		return nil
	}
	secure := u.Scheme == "https"
	port := u.Port()
	if port == "" {
		port = map[bool]string{true: "443", false: "80"}[secure]
	}
	path := strings.TrimRight(u.Path, "/") + API + "/ping?" + t.Params().Encode()
	request := "GET " + path + " HTTP/1.1\r\nHost: " + u.Host + "\r\nUser-Agent: " + UserAgent() +
		"\r\nConnection: close\r\n\r\n"
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	t0 := time.Now()
	conn, err := t.Dialer(limit)(ctx, "tcp", net.JoinHostPort(u.Hostname(), port))
	if err != nil {
		return nil
	}
	defer conn.Close()
	if dl, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(dl)
	}
	t1 := time.Now()
	if secure {
		tc := tls.Client(conn, &tls.Config{ServerName: u.Hostname()})
		if err := tc.HandshakeContext(ctx); err != nil {
			return nil
		}
		conn = tc
	}
	t2 := time.Now()
	if _, err := io.WriteString(conn, request); err != nil {
		return nil
	}
	status, err := bufio.NewReader(conn).ReadString('\n')
	t3 := time.Now()
	if err != nil || !strings.HasPrefix(status, "HTTP/") {
		return nil
	}
	ms := func(a, b time.Time) float64 { return float64(b.Sub(a).Nanoseconds()) / 1e6 }
	s := &Steps{TCP: ms(t0, t1), HTTP: ms(t2, t3), Secure: secure}
	if secure {
		s.TLS = ms(t1, t2)
	}
	return s
}

// newConnections opens a new connection a second while a transfer runs: the
// responsiveness test's probes away from the load.
type newConnections struct {
	t     *Target
	found []Steps
	lost  int
}

func (n *newConnections) run(ctx context.Context) {
	if sleep(ctx, 500*time.Millisecond) != nil {
		return
	}
	for ctx.Err() == nil {
		started := time.Now()
		steps := NewConnection(ctx, n.t, 5*time.Second)
		if ctx.Err() != nil {
			return
		}
		if steps != nil {
			n.found = append(n.found, *steps)
		} else {
			n.lost++
		}
		if sleep(ctx, time.Second-time.Since(started)) != nil {
			return
		}
	}
}

func (n *newConnections) summary() *quality.Connections {
	r := func(v float64) float64 { return roundPy(v, 3) }
	out := &quality.Connections{SetupMs: []float64{}, TCPMs: []float64{}, HTTPMs: []float64{}, Lost: n.lost, TLS: n.t.HTTPS()}
	tlsMs := []float64{}
	for _, s := range n.found {
		out.SetupMs = append(out.SetupMs, r(s.Total()))
		out.TCPMs = append(out.TCPMs, r(s.TCP))
		out.HTTPMs = append(out.HTTPMs, r(s.HTTP))
		tlsMs = append(tlsMs, r(s.TLS))
	}
	if out.TLS {
		out.TLSMs = &tlsMs
	}
	return out
}

// sleep waits d, or until ctx ends: its error then.
func sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// RuntimeClose is the echo socket's close code when the server's session_max_runtime is up.
const RuntimeClose = 4001

// WsProber sends echo probes over a WebSocket of its own: u32 sequence and f64 send time,
// little-endian, which the server echoes.
type WsProber struct {
	conn    *websocket.Conn
	mu      sync.Mutex
	seq     uint32
	pending map[uint32]chan float64
	done    chan struct{}
	// Closed hears the socket's close code once it closes, by either end: RuntimeClose,
	// say. Set before the socket can close.
	Closed func(code int)
	code   int
}

// DialWs opens the echo socket, within timeout.
func DialWs(ctx context.Context, t *Target, timeout time.Duration) (*WsProber, error) {
	c := *t
	c.HTTP2 = false // a WebSocket opens with an HTTP/1.1 upgrade, on its own connection
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, c.WsURL(), &websocket.DialOptions{
		HTTPClient: c.Client(1),
		HTTPHeader: http.Header{"User-Agent": {UserAgent()}},
	})
	if err != nil {
		return nil, err
	}
	w := &WsProber{conn: conn, pending: map[uint32]chan float64{}, done: make(chan struct{})}
	go w.read()
	return w, nil
}

func (w *WsProber) read() {
	defer close(w.done)
	for {
		_, data, err := w.conn.Read(context.Background())
		now := Now()
		if err != nil {
			w.mu.Lock()
			w.code = int(websocket.CloseStatus(err))
			for seq, ch := range w.pending {
				close(ch)
				delete(w.pending, seq)
			}
			closed := w.Closed
			w.mu.Unlock()
			if closed != nil {
				closed(w.code)
			}
			return
		}
		if len(data) < 12 {
			continue
		}
		seq := binary.LittleEndian.Uint32(data)
		sent := math.Float64frombits(binary.LittleEndian.Uint64(data[4:]))
		w.mu.Lock()
		if ch, ok := w.pending[seq]; ok {
			ch <- now - sent
			delete(w.pending, seq)
		}
		w.mu.Unlock()
	}
}

// Ready is whether the socket is still open.
func (w *WsProber) Ready() bool {
	select {
	case <-w.done:
		return false
	default:
		return true
	}
}

// Ping sends a probe and waits for its echo: its round trip in ms, or false if none came
// back within timeout. Pings may overlap.
func (w *WsProber) Ping(timeout time.Duration) (float64, bool) {
	ch := make(chan float64, 1)
	w.mu.Lock()
	seq := w.seq
	w.seq++
	w.pending[seq] = ch
	w.mu.Unlock()
	msg := make([]byte, 12)
	binary.LittleEndian.PutUint32(msg, seq)
	binary.LittleEndian.PutUint64(msg[4:], math.Float64bits(Now()))
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	if err := w.conn.Write(ctx, websocket.MessageBinary, msg); err != nil {
		w.forget(seq)
		return 0, false
	}
	select {
	case rtt, ok := <-ch:
		return rtt, ok
	case <-ctx.Done():
		w.forget(seq)
		return 0, false
	}
}

func (w *WsProber) forget(seq uint32) {
	w.mu.Lock()
	delete(w.pending, seq)
	w.mu.Unlock()
}

// Close closes the socket.
func (w *WsProber) Close() {
	_ = w.conn.Close(websocket.StatusNormalClosure, "")
}

// loadedLatency is RTT probes over a dedicated WebSocket while a transfer runs, a quarter
// second apart: latency under load. Best effort: a socket that doesn't open gives none.
type loadedLatency struct {
	t       *Target
	samples []float64
	lost    int
}

func (l *loadedLatency) run(ctx context.Context) {
	w, err := DialWs(ctx, l.t, 5*time.Second)
	if err != nil {
		return
	}
	defer w.Close()
	for ctx.Err() == nil && w.Ready() {
		if rtt, ok := w.Ping(2 * time.Second); ok {
			l.samples = append(l.samples, rtt)
		} else if ctx.Err() == nil {
			l.lost++
		}
		if sleep(ctx, 250*time.Millisecond) != nil {
			return
		}
	}
}

func (l *loadedLatency) summary() *stats.Summary {
	if len(l.samples) == 0 {
		return nil
	}
	s := stats.Summarize(l.samples, l.lost, true)
	return &s
}
