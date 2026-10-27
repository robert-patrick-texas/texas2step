// Package diag is every way t2s can reach a server, tried about once a second, as the
// Diag page does it (web/src/ui/views/diag.ts): HTTP/1.1, HTTP/2 and HTTP/3, WebSocket and
// UDP, over IPv4 and over IPv6. Unlike a browser, t2s picks the address family and the HTTP
// version itself, so every row is tried on the server's own name; the server's
// single-family and single-version names are used where it lists them.
package diag

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/quic-go/quic-go"
	"github.com/quic-go/quic-go/http3"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/iface"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

const (
	keepMs      = 300_000 // five minutes of answers
	recentMs    = 60_000  // the figures beside each row
	udpRetryMs  = 10_000  // a WebRTC session takes seconds to open and holds a server port
	wsRetryMs   = 2_000
	codeRetryMs = 10_000 // an address that asks for the access code, until it's in
)

// Kinds are the rows of each family, in order.
var Kinds = []string{"http1", "http2", "http3", "ws", "udp"}

// KindName is a row's name.
var KindName = map[string]string{"http1": "HTTP/1.1", "http2": "HTTP/2", "http3": "HTTP/3", "ws": "WebSocket", "udp": "UDP"}

// Sample is one attempt's outcome.
type Sample struct {
	T   float64 // wall ms
	OK  bool
	Rtt float64 // ms; NaN without an answer
	Up  bool    // the row had answered before: a miss counts as loss
}

// Method is one way of reaching the server, with what its answers have shown.
type Method struct {
	Fam    string // IPv4 or IPv6
	Kind   string
	t      *engine.Target
	Tone   string // trying, up, loss or off
	Reason string // why it isn't offered
	Detail string // what the latest answer said
	Warn   bool   // the detail tells of a fault in how the server is set up
	Port   string
	Server string // the server's address that answered
	// Restricted: its address asks for the access code, which isn't in.
	Restricted bool
	Samples    []Sample
	lostRun    int
	downSince  float64
	seq        int
	recheck    float64
	next       float64
	busy       bool
	client     *http.Client
	h3         *http3.Transport
	ws         *engine.WsProber
	rtc        *engine.RtcProbe
	waiting    map[uint32]chan float64
	udpSeq     uint32
	named      bool   // the HTTP/3 row: the server names it a name of its own
	altSvc     string // an HTTP/1.1 or HTTP/2 row: the HTTP/3 port its answers' Alt-Svc gives
}

// Label names the row: "IPv4 HTTP/2".
func (m *Method) Label() string { return m.Fam + " " + KindName[m.Kind] }

// Answer is what /api/v1/diag/http says of a connection.
type Answer struct {
	Client struct {
		IP     string `json:"ip"`
		Port   int    `json:"port"`
		Family string `json:"family"`
	} `json:"client"`
	Server struct {
		IP   string `json:"ip"`
		Port int    `json:"port"`
	} `json:"server"`
	HTTPVersion string `json:"http_version"`
	TLS         *struct {
		Protocol string `json:"protocol"`
		Cipher   string `json:"cipher"`
	} `json:"tls"`
	TCPRttMs *float64 `json:"tcp_rtt_ms"`
	TimeMs   float64  `json:"time_ms"`
}

// Seen is a family's view of this computer: its own address, and as the server saw it.
type Seen struct {
	Local    string // this computer's address on the TCP path
	UDPLocal string // and on the UDP path
	TCP      string // ip : port over HTTPS (TCP)
	TCPIP    string
	TCPPort  int
	QUIC     string // over HTTP/3
	UDP      string // over UDP (WebRTC)
	UDPIP    string
	UDPPort  int
	MAC      string
}

// Event is a row starting or stopping to answer, or something about the run.
type Event struct {
	At   time.Time `json:"time"`
	Who  string    `json:"row,omitempty"`
	Text string    `json:"text"`
}

// Diag tries every row of one server until its context ends or a limit is reached.
type Diag struct {
	mu       sync.Mutex
	ctx      context.Context
	cancel   context.CancelFunc
	t        *engine.Target
	code     string
	Info     *engine.Info
	Methods  []*Method
	seen     map[string]*Seen
	offsets  []float64
	spread   []float64
	events   []Event
	notice   func(Event)
	started  float64
	stopped  float64
	limit    float64 // ms; 0 for none
	limitWho string
	ended    string
}

func wall() float64 { return float64(time.Now().UnixNano()) / 1e6 }

// New is a Diag of t; code is the access code to enter where an address asks for it.
func New(t *engine.Target, code string, seconds float64, notice func(Event)) *Diag {
	return &Diag{t: t, code: code, seen: map[string]*Seen{"IPv4": {}, "IPv6": {}}, notice: notice, limit: seconds * 1000}
}

// location is this server's entry in /api/v1/locations: its other names.
type location struct {
	URLv4      string `json:"url_v4"`
	URLv6      string `json:"url_v6"`
	URLv4HTTP1 string `json:"url_v4_http1"`
	URLv4HTTP3 string `json:"url_v4_http3"`
	URLv6HTTP1 string `json:"url_v6_http1"`
	URLv6HTTP3 string `json:"url_v6_http3"`
}

// Start reads the server's info and its names, and starts every row.
func (d *Diag) Start(ctx context.Context, families []string) error {
	d.ctx, d.cancel = context.WithCancel(ctx)
	info, err := d.t.GetInfo(ctx)
	if err != nil {
		return err
	}
	d.Info = info
	if s := info.Limits.MaxDiagSeconds; s > 0 && (d.limit == 0 || s*1000 < d.limit) {
		d.limit, d.limitWho = s*1000, info.Name(d.t)
	}
	var locs struct {
		Locations []location `json:"locations"`
	}
	_, _ = d.t.GetJSON(ctx, d.t.Client(1), "/locations", nil, &locs)
	var loc location
	if len(locs.Locations) > 0 {
		loc = locs.Locations[0] // this server, first
	}
	names := map[string][3]string{ // http1, http2, http3 names of each family
		"IPv4": {loc.URLv4HTTP1, loc.URLv4, loc.URLv4HTTP3},
		"IPv6": {loc.URLv6HTTP1, loc.URLv6, loc.URLv6HTTP3},
	}
	d.event(nil, "checking "+info.Name(d.t)+" at "+d.t.Base)
	for _, fam := range families {
		n := names[fam]
		network := map[string]string{"IPv4": "tcp4", "IPv6": "tcp6"}[fam]
		pick := func(names ...string) string {
			for _, s := range names {
				if s != "" {
					return s
				}
			}
			return d.t.Base
		}
		for _, kind := range Kinds {
			base := map[string]string{"http1": pick(n[0], n[1]), "http2": pick(n[1]), "http3": pick(n[2], n[1]), "ws": pick(n[1]), "udp": d.t.Base}[kind]
			m := &Method{Fam: fam, Kind: kind, t: d.t.With(base, network), Tone: "trying"}
			m.Port = defaultPort(m.t, kind)
			switch {
			case kind != "udp" && !engine.HasAddress(ctx, m.t.Base, fam):
				m.off(fmt.Sprintf("%s has no %s address", host(m.t.Base), fam))
			case kind == "http2" && !m.t.HTTPS():
				m.off("HTTP/2 needs https://")
			case kind == "http3" && !m.t.HTTPS():
				m.off("HTTP/3 needs https://")
			case kind == "ws" && !info.Capabilities.WebSocket:
				m.off("the server's WebSocket is off")
			case kind == "udp" && !info.Capabilities.WebRTC:
				m.off("the server's WebRTC is off")
			case kind == "http3":
				m.named = n[2] != ""
				if !m.named {
					m.Detail = "waiting for an answer over TCP to say the server offers HTTP/3 (Alt-Svc)"
				}
			}
			d.Methods = append(d.Methods, m)
		}
	}
	d.started = wall()
	for i, m := range d.Methods {
		phase := time.Duration(float64(i) * float64(time.Second) / float64(len(d.Methods)))
		go d.loop(m, phase)
	}
	return nil
}

func host(base string) string {
	u, err := url.Parse(base)
	if err != nil {
		return base
	}
	return u.Hostname()
}

func defaultPort(t *engine.Target, kind string) string {
	u, _ := url.Parse(t.Base)
	port := u.Port()
	if port == "" {
		port = map[bool]string{true: "443", false: "80"}[t.HTTPS()]
	}
	switch kind {
	case "http3":
		return "UDP " + port
	case "udp":
		return "UDP"
	}
	return "TCP " + port
}

func (m *Method) off(reason string) {
	m.Tone, m.Reason = "off", reason
}

// loop attempts about once a second, starting phase from now. Each wait is 850 to 1150 ms,
// so the rows drift apart: their packets leave at different moments.
func (d *Diag) loop(m *Method, phase time.Duration) {
	timer := time.NewTimer(phase)
	defer timer.Stop()
	for {
		select {
		case <-d.ctx.Done():
			d.mu.Lock()
			m.close()
			d.mu.Unlock()
			return
		case <-timer.C:
		}
		d.mu.Lock()
		if m.Tone != "off" && !m.busy {
			d.tick(m)
		}
		d.mu.Unlock()
		timer.Reset(time.Duration(850+rand.Float64()*300) * time.Millisecond)
	}
}

func (m *Method) close() {
	if m.ws != nil {
		go m.ws.Close()
		m.ws = nil
	}
	if m.rtc != nil {
		go m.rtc.Close()
		m.rtc = nil
	}
	if m.h3 != nil {
		go m.h3.Close()
	}
}

func (d *Diag) event(m *Method, text string) {
	e := Event{At: time.Now(), Text: text}
	if m != nil {
		e.Who = m.Label()
	}
	d.events = append(d.events, e)
	if len(d.events) > 300 {
		d.events = d.events[1:]
	}
	if d.notice != nil {
		d.notice(e)
	}
}

// record is an attempt's outcome. Once a row has answered, a miss counts as loss; why is
// what the log says when answers stop. Called with d.mu held.
func (d *Diag) record(m *Method, ok bool, rtt float64, why string) {
	if m.Tone == "off" || d.ctx.Err() != nil {
		return
	}
	now := wall()
	up := m.Tone == "up" || m.Tone == "loss"
	m.Samples = append(m.Samples, Sample{T: now, OK: ok, Rtt: rtt, Up: up})
	for len(m.Samples) > 0 && m.Samples[0].T < now-keepMs {
		m.Samples = m.Samples[1:]
	}
	switch {
	case ok:
		m.Restricted = false
		if m.Tone == "trying" {
			d.event(m, "answering, "+ms(rtt))
		}
		if m.Tone == "loss" {
			d.event(m, fmt.Sprintf("answering again after %s, %d lost", seconds(now-m.downSince), m.lostRun))
		}
		m.Tone, m.lostRun = "up", 0
	case m.Tone == "up":
		m.Tone, m.downSince, m.lostRun = "loss", now, 1
		d.event(m, why)
	case m.Tone == "loss":
		m.lostRun++
	}
}

// needsCode is a row's address asking for the access code: attempts wait, and the seconds
// don't count as loss. Called with d.mu held.
func (d *Diag) needsCode(m *Method) {
	if !m.Restricted {
		d.event(m, "access code required")
	}
	m.Restricted = true
	m.Tone, m.lostRun = "trying", 0
}

func ms(v float64) string {
	if math.IsNaN(v) {
		return "—"
	}
	digits := 2
	switch {
	case v >= 100:
		digits = 0
	case v >= 10:
		digits = 1
	}
	return strconv.FormatFloat(v, 'f', digits, 64) + " ms"
}

func seconds(msecs float64) string { return fmt.Sprintf("%.0fs", math.Max(1, math.Round(msecs/1000))) }

// tick is one attempt. Called with d.mu held; the attempt goes on without it.
func (d *Diag) tick(m *Method) {
	switch m.Kind {
	case "http1", "http2", "http3":
		if m.Restricted && wall() < m.recheck {
			return
		}
		if m.Kind == "http3" && m.h3 == nil && !d.altSvc(m) {
			return
		}
		m.busy = true
		go d.https(m)
	case "ws":
		if m.ws != nil && m.ws.Ready() {
			ws := m.ws
			m.busy = true
			go func() {
				rtt, ok := ws.Ping(1500 * time.Millisecond)
				d.mu.Lock()
				defer d.mu.Unlock()
				m.busy = false
				d.record(m, ok, rttOr(rtt, ok), "no answer")
			}()
			return
		}
		// Not open: the second goes unanswered, and the socket is opened again.
		if !m.Restricted {
			d.record(m, false, math.NaN(), "the socket closed")
		}
		if wall() < m.next {
			return
		}
		m.next = wall() + wsRetryMs
		m.busy = true
		go d.openWs(m)
	case "udp":
		if m.rtc != nil && m.rtc.Open() {
			seq := m.udpSeq
			m.udpSeq++
			ch := make(chan float64, 1)
			m.waiting[seq] = ch
			rtc := m.rtc
			m.busy = true
			go func() {
				rtc.Send(seq, 64)
				var rtt float64
				ok := true
				select {
				case rtt = <-ch:
				case <-time.After(1500 * time.Millisecond):
					ok = false
				}
				d.mu.Lock()
				defer d.mu.Unlock()
				delete(m.waiting, seq)
				m.busy = false
				d.record(m, ok, rttOr(rtt, ok), "no answer")
			}()
			return
		}
		if m.rtc != nil {
			go m.rtc.Close() // the channel closed under us
			m.rtc = nil
		}
		if !m.Restricted {
			d.record(m, false, math.NaN(), "the UDP session closed")
		}
		if wall() < m.next {
			return
		}
		m.next = wall() + udpRetryMs
		m.busy = true
		go d.openUdp(m)
	}
}

func rttOr(rtt float64, ok bool) float64 {
	if !ok {
		return math.NaN()
	}
	return rtt
}

var altSvc = regexp.MustCompile(`h3="([^"]*):(\d+)"`)

// altSvc is whether HTTP/3 can be tried on the row's name: the server names it a name of
// its own for HTTP/3, or an answer over TCP on the same name said Alt-Svc h3. A name that
// answers over TCP without it doesn't offer HTTP/3. Called with d.mu held.
func (d *Diag) altSvc(m *Method) bool {
	if m.named {
		return true
	}
	for _, o := range d.Methods {
		if o.Fam != m.Fam || (o.Kind != "http2" && o.Kind != "http1") || o.t.Base != m.t.Base || o.Tone != "up" {
			continue
		}
		if o.altSvc != "" {
			m.Port, m.Detail = "UDP "+o.altSvc, ""
			return true
		}
		m.off("the server doesn't offer HTTP/3 here: its answers over TCP carry no Alt-Svc h3")
		d.event(m, "not offered: no Alt-Svc h3")
		return false
	}
	return false
}

// https is one GET of /api/v1/diag/http on the row's open connection.
func (d *Diag) https(m *Method) {
	d.mu.Lock()
	if m.client == nil {
		m.client = d.clientFor(m)
	}
	c := m.client
	m.seq++
	q := url.Values{"n": {strconv.Itoa(m.seq)}}
	d.mu.Unlock()

	var local, remote net.Addr
	ctx, cancel := context.WithTimeout(d.ctx, 2*time.Second)
	defer cancel()
	ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{GotConn: func(c httptrace.GotConnInfo) {
		local, remote = c.Conn.LocalAddr(), c.Conn.RemoteAddr()
	}})
	var a Answer
	sent := wall()
	t0 := time.Now()
	r, err := m.t.GetJSON(ctx, c, "/diag/http", q, &a)
	rtt := float64(time.Since(t0).Nanoseconds()) / 1e6

	d.mu.Lock()
	defer d.mu.Unlock()
	m.busy = false
	var se *engine.StatusError
	if errors.As(err, &se) && se.Status == 401 {
		// Not lost: refused for want of a pass; with --code, entered once in a while.
		m.recheck = wall() + codeRetryMs
		d.needsCode(m)
		if d.code != "" {
			go d.unlock(m)
		}
		return
	}
	if err != nil {
		d.record(m, false, math.NaN(), "no answer")
		return
	}
	http := map[int]string{1: "HTTP/1.1", 2: "HTTP/2", 3: "HTTP/3"}[r.ProtoMajor]
	want := KindName[m.Kind]
	parts := []string{}
	if r.TLS != nil {
		parts = append(parts, tlsName(r.TLS))
	} else if a.TLS != nil {
		parts = append(parts, a.TLS.Protocol+" "+a.TLS.Cipher)
	}
	if a.Server.IP == "" && m.Kind != "http3" && remote != nil && r.Request.URL.Host != "" && !viaProxy(m.t, r) {
		a.Server.IP = addrIP(remote) // the server doesn't say: the connection's own end
	}
	if a.Server.IP != "" {
		m.Server = a.Server.IP
		parts = append(parts, "to "+a.Server.IP)
	}
	if a.TCPRttMs != nil && m.Kind != "http3" {
		parts = append(parts, "server's TCP RTT "+ms(*a.TCPRttMs))
	}
	if a.Server.Port > 0 {
		m.Port = map[bool]string{true: "UDP ", false: "TCP "}[m.Kind == "http3"] + strconv.Itoa(a.Server.Port)
	}
	m.Warn = http != want
	detail := strings.Join(parts, " · ")
	if m.Warn {
		why := "this name doesn't offer " + want
		if m.Kind == "http1" {
			why = "this name offers HTTP/2 too"
		}
		detail = fmt.Sprintf("answered over %s, not %s: %s · %s", http, want, why, detail)
	}
	m.Detail = detail
	if found := altSvc.FindStringSubmatch(r.Header.Get("Alt-Svc")); found != nil && m.Kind != "http3" {
		m.altSvc = found[2]
	}
	// The server's clock against this computer's, from the middle of the round trip.
	d.offsets = append(d.offsets, a.TimeMs-(sent+rtt/2))
	d.spread = append(d.spread, rtt/2)
	if len(d.offsets) > 30 {
		d.offsets, d.spread = d.offsets[1:], d.spread[1:]
	}
	s := d.seen[m.Fam]
	if a.Client.IP != "" {
		if m.Kind == "http3" {
			s.QUIC = hostPort(a.Client.IP, a.Client.Port)
		} else if m.Kind == "http2" || s.TCP == "" {
			s.TCP, s.TCPIP, s.TCPPort = hostPort(a.Client.IP, a.Client.Port), a.Client.IP, a.Client.Port
		}
	}
	if ip := addrIP(local); ip != "" && m.Kind != "http3" {
		s.Local = ip
		if s.MAC == "" {
			s.MAC = iface.MACOf(ip, a.Server.IP)
		}
	}
	d.record(m, true, rtt, "no answer")
}

// viaProxy is whether the request went through an HTTP proxy, whose address the
// connection's far end would be.
func viaProxy(t *engine.Target, r *http.Response) bool {
	p, err := http.ProxyFromEnvironment(r.Request)
	return err == nil && p != nil
}

func tlsName(cs *tls.ConnectionState) string {
	v := map[uint16]string{tls.VersionTLS10: "TLSv1", tls.VersionTLS11: "TLSv1.1", tls.VersionTLS12: "TLSv1.2", tls.VersionTLS13: "TLSv1.3"}[cs.Version]
	return strings.TrimSpace(v + " " + tls.CipherSuiteName(cs.CipherSuite))
}

func hostPort(ip string, port int) string {
	if ip == "" {
		return "—"
	}
	if port > 0 {
		return ip + " : " + strconv.Itoa(port) // spaced, so the port can't be read as part of an IPv6 address
	}
	return ip
}

func addrIP(a net.Addr) string {
	if a == nil {
		return ""
	}
	host, _, err := net.SplitHostPort(a.String())
	if err != nil {
		return ""
	}
	return host
}

// clientFor is the row's own HTTP client: its family, and its HTTP version.
func (d *Diag) clientFor(m *Method) *http.Client {
	switch m.Kind {
	case "http2":
		t := *m.t
		t.HTTP2 = true
		return t.Client(1)
	case "http3":
		network := map[string]string{"IPv4": "ip4", "IPv6": "ip6"}[m.Fam]
		m.h3 = &http3.Transport{
			TLSClientConfig: &tls.Config{},
			Dial: func(ctx context.Context, addr string, cfg *tls.Config, qc *quic.Config) (*quic.Conn, error) {
				host, port, err := net.SplitHostPort(addr)
				if err != nil {
					return nil, err
				}
				if p := strings.TrimPrefix(m.Port, "UDP "); p != "" && p != m.Port {
					port = p // where the server's Alt-Svc says it listens
				}
				ips, err := net.DefaultResolver.LookupIP(ctx, network, host)
				if err != nil || len(ips) == 0 {
					return nil, fmt.Errorf("no %s address for %s", m.Fam, host)
				}
				if cfg.ServerName == "" {
					cfg.ServerName = host
				}
				return quic.DialAddrEarly(ctx, net.JoinHostPort(ips[0].String(), port), cfg, qc)
			},
		}
		return &http.Client{Transport: m.h3}
	}
	return m.t.Client(1)
}

// unlock enters the access code over the row's own address.
func (d *Diag) unlock(m *Method) {
	ctx, cancel := context.WithTimeout(d.ctx, 5*time.Second)
	defer cancel()
	pass, err := m.t.Unlock(ctx, d.code)
	if err != nil || pass == "" {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, o := range d.Methods {
		if o.t.Base == m.t.Base && o.t.Family == m.t.Family {
			o.t.Pass, o.recheck = pass, 0
		}
	}
}

func (d *Diag) openWs(m *Method) {
	w, err := engine.DialWs(d.ctx, m.t, 3*time.Second)
	d.mu.Lock()
	defer d.mu.Unlock()
	m.busy = false
	if err != nil {
		if strings.Contains(err.Error(), "401") {
			d.needsCode(m)
		}
		return
	}
	if d.ctx.Err() != nil {
		go w.Close()
		return
	}
	w.Closed = func(code int) {
		if code == engine.RuntimeClose {
			d.serverEnded(d.Info.Limits.MaxSessionSeconds)
		}
	}
	m.ws, m.Restricted = w, false
	m.Detail = "its own TCP connection, opened by an HTTP/1.1 upgrade"
}

func (d *Diag) openUdp(m *Method) {
	fam := map[string]string{"IPv4": "ipv4", "IPv6": "ipv6"}[m.Fam]
	rtc, err := engine.ConnectRtc(d.ctx, d.t, d.Info.ICE(), fam, 8*time.Second)
	if err != nil {
		d.mu.Lock()
		defer d.mu.Unlock()
		m.busy = false
		var no engine.NotOffered
		var se *engine.StatusError
		switch {
		case errors.As(err, &no) && d.ctx.Err() == nil:
			m.off(no.Error())
			d.event(m, "not offered: "+no.Error())
		case errors.As(err, &se) && se.Status == 401:
			d.needsCode(m)
		}
		return
	}
	m.waiting = map[uint32]chan float64{}
	rtc.OnEcho(func(seq uint32, rtt, _ float64, _ *engine.Stamp) {
		d.mu.Lock()
		ch := m.waiting[seq]
		d.mu.Unlock()
		if ch != nil {
			select {
			case ch <- rtt:
			default:
			}
		}
	})
	rtc.OnNotice(func(msg map[string]any) {
		if msg["op"] == "ended" {
			s, _ := msg["seconds"].(float64)
			d.serverEnded(s)
		}
	})
	path, seen := rtc.Path(), rtc.Address()
	d.mu.Lock()
	defer d.mu.Unlock()
	m.busy = false
	if d.ctx.Err() != nil {
		go rtc.Close()
		return
	}
	m.rtc, m.Restricted = rtc, false
	parts := []string{}
	s := d.seen[m.Fam]
	if path != nil {
		m.Port, m.Server = "UDP "+strconv.Itoa(path.Port), path.Address
		parts = append(parts, path.DTLS, "to "+path.Address)
		s.UDPLocal = path.Local
	}
	if seen != nil && seen.IP != "" {
		s.UDP, s.UDPIP, s.UDPPort = hostPort(seen.IP, seen.Port), seen.IP, seen.Port
		parts = append(parts, "seen from "+s.UDP)
	}
	m.Detail = strings.Join(parts, " · ")
	if m.Detail == "" {
		m.Detail = "WebRTC data channel, no retransmits"
	}
}

func (d *Diag) serverEnded(seconds float64) {
	d.mu.Lock()
	defer d.mu.Unlock()
	name := d.Info.Name(d.t)
	if seconds > 0 {
		d.end(fmt.Sprintf("%s ends each session after %s.", name, span(seconds)))
	} else {
		d.end(name + " ended the session at its time limit.")
	}
}

// end stops Diag at a limit. Called with d.mu held.
func (d *Diag) end(why string) {
	if d.ended != "" || d.ctx.Err() != nil {
		return
	}
	d.ended = why
	d.event(nil, why)
	d.stopped = wall()
	d.cancel()
}

// Watch stops Diag at its time limit; call it about once a second.
func (d *Diag) Watch() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.limit > 0 && wall()-d.started >= d.limit {
		if d.limitWho == "" {
			d.end(fmt.Sprintf("Stopped after %s, as --seconds asked.", span(d.limit/1000)))
		} else {
			d.end(fmt.Sprintf("Stopped after %s, %s's limit.", span(d.limit/1000), d.limitWho))
		}
	}
}

// Done is closed once Diag stops.
func (d *Diag) Done() <-chan struct{} { return d.ctx.Done() }

// Stop stops every row; what they found stays.
func (d *Diag) Stop() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.stopped == 0 {
		d.stopped = wall()
	}
	d.cancel()
}

// Ended is why Diag stopped at a limit, "" if it was stopped.
func (d *Diag) Ended() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.ended
}

func span(seconds float64) string {
	if seconds < 120 {
		return fmt.Sprintf("%.0fs", math.Round(seconds))
	}
	min := int(math.Round(seconds / 60))
	if min < 60 {
		return fmt.Sprintf("%d min", min)
	}
	if min%60 != 0 {
		return fmt.Sprintf("%d h %d min", min/60, min%60)
	}
	return fmt.Sprintf("%d h", min/60)
}

// Row is one row as shown.
type Row struct {
	Fam    string   `json:"family"`
	Method string   `json:"method"`
	Port   string   `json:"port"`
	State  string   `json:"state"` // Answering, No answer for 4s, No answer yet, Not offered, Restricted
	Tone   string   `json:"tone"`
	Latest *float64 `json:"latest_ms"`
	Median *float64 `json:"median_ms"`
	Lost   int      `json:"lost"`
	Tried  int      `json:"tried"`
	Detail string   `json:"detail"`
	Warn   bool     `json:"warn,omitempty"`
	Server string   `json:"server,omitempty"`
	Recent []int    `json:"-"` // the last attempts: 1 answered, 0 not, -1 not counted
}

// Snapshot is what Diag shows now.
type Snapshot struct {
	Name     string                       `json:"server"`
	URL      string                       `json:"url"`
	Now      float64                      `json:"-"`
	Started  float64                      `json:"-"`
	Ran      float64                      `json:"seconds"`
	Limit    float64                      `json:"limit_seconds"`
	Running  bool                         `json:"running"`
	Rows     []Row                        `json:"rows"`
	Seen     map[string]*Seen             `json:"-"`
	You      map[string]string            `json:"you"`
	Egress   map[string]map[string]string `json:"egress"`
	Clock    string                       `json:"clock,omitempty"`
	Events   []Event                      `json:"-"`
	Ended    string                       `json:"ended,omitempty"`
	Families []string                     `json:"families"`
}

// Snapshot is Diag's state now.
func (d *Diag) Snapshot() Snapshot {
	d.mu.Lock()
	defer d.mu.Unlock()
	now := wall()
	running := d.ctx.Err() == nil
	if !running && d.stopped > 0 {
		now = d.stopped
	}
	s := Snapshot{Name: d.Info.Name(d.t), URL: d.t.Base, Now: now, Started: d.started, Ran: (now - d.started) / 1000, Limit: d.limit / 1000,
		Running: running, Events: append([]Event(nil), d.events...), Ended: d.ended, You: map[string]string{}, Egress: map[string]map[string]string{}}
	fams := map[string]bool{}
	for _, m := range d.Methods {
		if !fams[m.Fam] {
			fams[m.Fam] = true
			s.Families = append(s.Families, m.Fam)
		}
		s.Rows = append(s.Rows, m.row(now))
	}
	for _, fam := range s.Families {
		seen := d.seen[fam]
		ip := seen.TCPIP
		if ip == "" {
			ip = strings.Split(seen.QUIC, " : ")[0]
		}
		if ip == "" {
			ip = seen.UDPIP
		}
		if ip == "" {
			ip = "no answer over " + fam + " yet"
		}
		s.You[fam] = ip
		e := map[string]string{}
		if seen.Local != "" {
			e["This computer"] = seen.Local
		}
		e["Over HTTPS (TCP)"] = or(seen.TCP, "—")
		if seen.QUIC != "" {
			e["Over HTTP/3 (UDP)"] = seen.QUIC
		}
		e["Over UDP (WebRTC)"] = or(seen.UDP, "—")
		verdict := "—"
		if seen.TCPIP != "" && seen.UDPIP != "" {
			switch {
			case seen.TCPIP != seen.UDPIP:
				verdict = "Different addresses"
			case seen.TCPPort != 0 && seen.UDPPort != 0 && seen.TCPPort != seen.UDPPort:
				verdict = "Same address, other port"
			default:
				verdict = "Same address"
			}
		}
		e["Compared"] = verdict
		// NAT: this computer's address against the server's view of it, on one path.
		local, outside := seen.Local, seen.TCPIP
		if local == "" || outside == "" {
			local, outside = seen.UDPLocal, seen.UDPIP
		}
		if e["This computer"] == "" && seen.UDPLocal != "" {
			e["This computer"] = seen.UDPLocal
		}
		if local != "" && outside != "" {
			if local == outside {
				e["NAT"] = "none: same address"
			} else {
				e["NAT"] = local + " → " + outside
			}
		}
		if seen.MAC != "" {
			e["MAC"] = seen.MAC
		}
		s.Egress[fam] = e
	}
	if len(d.offsets) > 0 {
		offset, spread := stats.Percentile(d.offsets, 50), stats.Percentile(d.spread, 50)
		way := "behind"
		if offset < 0 {
			way = "ahead of"
		}
		s.Clock = fmt.Sprintf("Clocks: this computer is %s %s the server (± %s)", ms(math.Abs(offset)), way, ms(spread))
	}
	return s
}

func or(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

func (m *Method) row(now float64) Row {
	r := Row{Fam: m.Fam, Method: KindName[m.Kind], Port: m.Port, Tone: m.Tone, Warn: m.Warn, Server: m.Server}
	r.Detail = m.Detail
	var got []float64
	for _, s := range m.Samples {
		if s.T < now-recentMs {
			continue
		}
		r.Tried++
		if s.OK && !math.IsNaN(s.Rtt) {
			got = append(got, s.Rtt)
		}
		if !s.OK && s.Up {
			r.Lost++
		}
	}
	for _, s := range m.Samples[max(0, len(m.Samples)-20):] {
		switch {
		case s.OK:
			r.Recent = append(r.Recent, 1)
		case s.Up:
			r.Recent = append(r.Recent, 0)
		default:
			r.Recent = append(r.Recent, -1)
		}
	}
	if len(got) > 0 {
		v := stats.Percentile(got, 50)
		r.Median = &v
	}
	if n := len(m.Samples); n > 0 && m.Samples[n-1].OK {
		v := m.Samples[n-1].Rtt
		r.Latest = &v
	}
	switch {
	case m.Restricted:
		r.State, r.Detail = "Restricted", "access code required"
		if m.Kind != "ws" && m.Kind != "udp" {
			r.Detail += ", checking every " + seconds(codeRetryMs)
		}
	case m.Tone == "off":
		r.State, r.Detail = "Not offered", m.Reason
	case m.Tone == "trying":
		r.State = "No answer yet"
	case m.Tone == "up":
		r.State = "Answering"
	default:
		r.State = "No answer for " + seconds(now-m.downSince)
	}
	return r
}
