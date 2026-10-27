// Package engine measures from this computer to a texas2step server, by the methodology of
// the server's engine.py and the browser's web/src/engine, over the same wire endpoints,
// so results are comparable (docs/methodology.md, docs/api.md).
package engine

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const API = "/api/v1"

// Version is t2s's, set at build time from the server's src/texas2step/__init__.py.
var Version = "dev"

// UserAgent is what every request says it is.
func UserAgent() string { return "t2s/" + Version }

var epoch = time.Now()

// Now is this process's monotonic clock in ms, as probes carry it.
func Now() float64 { return float64(time.Since(epoch).Nanoseconds()) / 1e6 }

// idLetters are the server's result-id letters (store.py): no 0/o, 1/l/i.
const idLetters = "23456789abcdefghjkmnpqrstwxyz"

// NewSession is an id for one run, sent as ?session= (docs/clients.md).
func NewSession() string {
	b := make([]byte, 8)
	for i := range b {
		n, _ := rand.Int(rand.Reader, big.NewInt(int64(len(idLetters))))
		b[i] = idLetters[n.Int64()]
	}
	return string(b)
}

// Target is a texas2step server and how to reach it: the pass, the session and page every
// request carries, and the address family and HTTP version to hold to.
type Target struct {
	Base    string // scheme://host[:port][/prefix], no trailing slash
	Pass    string // an access pass (docs/access.md), sent as ?pass=
	Session string
	Page    string
	Test    string // the test download and upload requests belong to, sent as ?test=
	HTTP2   bool   // let transfers and pings share one HTTP/2 connection
	Family  string // "tcp4" or "tcp6" to hold to one address family; "" for either
}

// Base is a server's URL as given, tidied: an http:// scheme if none, no trailing slash.
func Base(raw string) string {
	u := strings.TrimRight(strings.TrimSpace(raw), "/")
	if !strings.Contains(u, "://") {
		u = "http://" + u
	}
	return u
}

// NewTarget is a target for one run of the command line.
func NewTarget(raw string) *Target {
	return &Target{Base: Base(raw), Session: NewSession(), Page: "cli"}
}

// With is a copy of t at another URL: another name for the same server.
func (t *Target) With(base, family string) *Target {
	c := *t
	c.Base, c.Family = Base(base), family
	return &c
}

// Params is what every request carries: the session and page, and the pass if any.
func (t *Target) Params() url.Values {
	q := url.Values{"session": {t.Session}, "page": {t.Page}}
	if t.Pass != "" {
		q.Set("pass", t.Pass)
	}
	return q
}

// URL is an API path on the server, with the common parameters and extra ones.
func (t *Target) URL(path string, extra url.Values) string {
	q := t.Params()
	for k, v := range extra {
		q[k] = v
	}
	return t.Base + API + path + "?" + q.Encode()
}

// WsURL is the echo socket's URL.
func (t *Target) WsURL() string {
	return "ws" + strings.TrimPrefix(t.URL("/ws", nil), "http")
}

// HTTPS is whether the server is reached over TLS.
func (t *Target) HTTPS() bool { return strings.HasPrefix(t.Base, "https://") }

// Dialer opens connections in the target's address family.
func (t *Target) Dialer(timeout time.Duration) func(ctx context.Context, network, addr string) (net.Conn, error) {
	d := &net.Dialer{Timeout: timeout, KeepAlive: 30 * time.Second}
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		if t.Family != "" {
			network = t.Family
		}
		return d.DialContext(ctx, network, addr)
	}
}

// Client is an HTTP client for conns connections at once: HTTP/1.1, a connection per
// request in flight, as the Python engine's; over HTTP/2 with t.HTTP2. Certificates are
// checked against this computer's own trust store, proxies taken from the environment
// (https_proxy and the like), and bodies read as sent, not decompressed.
func (t *Target) Client(conns int) *http.Client {
	tr := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           t.Dialer(10 * time.Second),
		TLSHandshakeTimeout:   10 * time.Second,
		ResponseHeaderTimeout: 15 * time.Second,
		MaxIdleConnsPerHost:   conns + 2,
		MaxConnsPerHost:       conns + 2,
		DisableCompression:    true,
		ForceAttemptHTTP2:     t.HTTP2,
		TLSClientConfig:       &tls.Config{},
	}
	if !t.HTTP2 {
		tr.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	}
	return &http.Client{Transport: tr}
}

// StatusError is a server's answer that isn't a success, with what it said about it.
type StatusError struct {
	Status int
	Path   string
	Detail string
	URL    string // a traffic limit's link, where it gives one
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("%d from %s: %s", e.Status, e.Path, e.Detail)
}

func statusError(r *http.Response) error {
	body, _ := io.ReadAll(io.LimitReader(r.Body, 64<<10))
	var said struct {
		Detail  any    `json:"detail"`
		Message string `json:"message"`
		URL     string `json:"url"`
	}
	detail := strings.TrimSpace(strings.TrimPrefix(r.Status, strconv.Itoa(r.StatusCode)))
	if json.Unmarshal(body, &said) == nil {
		if s, ok := said.Detail.(string); ok && s != "" {
			detail = s
		}
	}
	return &StatusError{Status: r.StatusCode, Path: r.Request.URL.Path, Detail: detail, URL: said.URL}
}

func (t *Target) request(ctx context.Context, method, path string, extra url.Values, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, t.URL(path, extra), body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", UserAgent())
	return req, nil
}

// GetJSON asks for an API path and reads its JSON answer into out; a StatusError if the
// server answers with an error.
func (t *Target) GetJSON(ctx context.Context, c *http.Client, path string, extra url.Values, out any) (*http.Response, error) {
	req, err := t.request(ctx, http.MethodGet, path, extra, nil)
	if err != nil {
		return nil, err
	}
	return do(c, req, out)
}

// PostJSON sends a JSON body and reads the JSON answer into out.
func (t *Target) PostJSON(ctx context.Context, c *http.Client, path string, in, out any) (*http.Response, error) {
	data, err := json.Marshal(in)
	if err != nil {
		return nil, err
	}
	req, err := t.request(ctx, http.MethodPost, path, nil, bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	return do(c, req, out)
}

func do(c *http.Client, req *http.Request, out any) (*http.Response, error) {
	r, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer r.Body.Close()
	if r.StatusCode >= 400 {
		return r, statusError(r)
	}
	if out != nil {
		if err := json.NewDecoder(r.Body).Decode(out); err != nil {
			return r, fmt.Errorf("%s didn't answer with JSON: %w", req.URL.Path, err)
		}
	}
	_, _ = io.Copy(io.Discard, r.Body)
	return r, nil
}

// HasAddress is whether base's name has an address in fam, "IPv4" or "IPv6": an A or
// AAAA record, or is one, so it can be reached in that family at all.
func HasAddress(ctx context.Context, base, fam string) bool {
	h := base
	if u, err := url.Parse(base); err == nil {
		h = u.Hostname()
	}
	if ip := net.ParseIP(h); ip != nil {
		return (ip.To4() != nil) == (fam == "IPv4")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	ips, err := net.DefaultResolver.LookupIP(ctx, map[string]string{"IPv4": "ip4", "IPv6": "ip6"}[fam], h)
	return err == nil && len(ips) > 0
}

// ErrNeedsCode is a server asking for an access code that wasn't given.
var ErrNeedsCode = errors.New("asks for an access code: add --code, or set TEXAS2STEP_CODE")

// Unlock trades code for a pass at t, which every request then carries; "" if the server
// asks for no code. ErrNeedsCode if it asks for one and code is "", a StatusError if it
// refuses the code.
func (t *Target) Unlock(ctx context.Context, code string) (string, error) {
	c := t.Client(1)
	var state struct {
		Required bool `json:"required"`
	}
	r, err := t.GetJSON(ctx, c, "/access", nil, &state)
	var se *StatusError
	if errors.As(err, &se) && se.Status == 404 {
		return "", nil // a server from before access codes
	}
	if err != nil {
		if r != nil && strings.Contains(err.Error(), "didn't answer with JSON") {
			return "", nil // not JSON: the test itself says what went wrong
		}
		return "", err
	}
	if !state.Required {
		return "", nil
	}
	if code == "" {
		return "", ErrNeedsCode
	}
	var got struct {
		Pass string `json:"pass"`
	}
	if _, err := t.PostJSON(ctx, c, "/access", map[string]string{"code": code}, &got); err != nil {
		if errors.As(err, &se) && se.Status >= 400 && se.Status < 500 && se.Detail != "" {
			return "", errors.New("refused the code: " + se.Detail) // a wrong code, or too many tries
		}
		return "", err
	}
	return got.Pass, nil
}

// Info is the parts of /api/v1/info t2s uses.
type Info struct {
	Version *string `json:"version"`
	Server  struct {
		ID       *string `json:"id"`
		Name     *string `json:"name"`
		Location *string `json:"location"`
	} `json:"server"`
	Capabilities struct {
		WebSocket bool `json:"websocket"`
		WebRTC    bool `json:"webrtc"`
		Stamps    bool `json:"stamps"`
		Results   bool `json:"results"`
	} `json:"capabilities"`
	Limits struct {
		MaxDownloadBytes  *int64  `json:"max_download_bytes"`
		MaxUploadBytes    *int64  `json:"max_upload_bytes"`
		MaxSessionSeconds float64 `json:"max_session_seconds"`
		MaxMonitorSeconds float64 `json:"max_monitor_seconds"`
		MaxDiagSeconds    float64 `json:"max_diag_seconds"`
	} `json:"limits"`
	ICEServers []struct {
		URLs any `json:"urls"`
	} `json:"ice_servers"`
	Defaults struct {
		OneStream *bool `json:"one_stream"`
	} `json:"defaults"`
	Monitor *struct {
		Profile string `json:"profile"`
		Voice   *bool  `json:"voice"`
		Video   *bool  `json:"video"`
	} `json:"monitor"`
}

// Name is the server's name, or its address where it gives none.
func (i *Info) Name(t *Target) string {
	if i != nil && i.Server.Name != nil && *i.Server.Name != "" {
		return *i.Server.Name
	}
	if u, err := url.Parse(t.Base); err == nil {
		return u.Host
	}
	return t.Base
}

// ICE is the STUN/TURN servers the server names for WebRTC: never an implicit public one.
func (i *Info) ICE() []string {
	var out []string
	for _, s := range i.ICEServers {
		switch v := s.URLs.(type) {
		case string:
			out = append(out, v)
		case []any:
			for _, u := range v {
				if s, ok := u.(string); ok {
					out = append(out, s)
				}
			}
		}
	}
	return out
}

// GetInfo reads the server's /api/v1/info.
func (t *Target) GetInfo(ctx context.Context) (*Info, error) {
	var info Info
	if _, err := t.GetJSON(ctx, t.Client(1), "/info", nil, &info); err != nil {
		return nil, err
	}
	return &info, nil
}

// ServerTimingMs is the dur of metric in a Server-Timing header, 0 if not given.
func ServerTimingMs(header, metric string) float64 {
	for _, entry := range strings.Split(header, ",") {
		parts := strings.Split(entry, ";")
		if strings.TrimSpace(parts[0]) != metric {
			continue
		}
		for _, p := range parts[1:] {
			k, v, _ := strings.Cut(strings.TrimSpace(p), "=")
			if k == "dur" {
				if f, err := strconv.ParseFloat(v, 64); err == nil {
					return f
				}
			}
		}
	}
	return 0
}
