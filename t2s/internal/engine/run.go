package engine

import (
	"context"
	"net"
	"net/http/httptrace"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/iface"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// Result is a full test, in the result format History and the API keep (version 1), as
// the Python engine's run_all writes it.
type Result struct {
	Version        int                     `json:"version"`
	Tool           string                  `json:"tool"`
	StartedAt      string                  `json:"started_at"`
	Server         ServerRef               `json:"server"`
	Client         ClientRef               `json:"client"`
	Config         Config                  `json:"config"`
	Latency        *Latency                `json:"latency,omitempty"`
	Download       *Throughput             `json:"download,omitempty"`
	OneStream      *Throughput             `json:"one_stream,omitempty"`
	Upload         *Throughput             `json:"upload,omitempty"`
	Loss           *Loss                   `json:"loss,omitempty"`
	Bufferbloat    Bufferbloat             `json:"bufferbloat"`
	Call           *quality.Call           `json:"call,omitempty"`
	Responsiveness *quality.Responsiveness `json:"responsiveness,omitempty"`
	DurationS      float64                 `json:"duration_s"`
	ID             string                  `json:"id,omitempty"`
}

// ServerRef is the server tested.
type ServerRef struct {
	ID       *string `json:"id"`
	Name     *string `json:"name"`
	Location *string `json:"location"`
	URL      string  `json:"url"`
	Version  *string `json:"version"`
}

// ClientRef is this computer as the server saw it, and as it knows itself.
type ClientRef struct {
	IP        *string `json:"ip"`
	Family    *string `json:"family"`
	Protocol  *string `json:"protocol"`
	UserAgent string  `json:"user_agent"`
	LocalIP   string  `json:"local_ip,omitempty"`
	MAC       string  `json:"mac,omitempty"`
	MACSource string  `json:"mac_source,omitempty"`
}

// Config is what the test was asked to do.
type Config struct {
	Streams    int     `json:"streams"`
	Seconds    float64 `json:"seconds"`
	Packets    int     `json:"packets"`
	IntervalMs float64 `json:"interval_ms"`
	OneStream  bool    `json:"one_stream"`
}

// Bufferbloat is the latency each load added, and its grade.
type Bufferbloat struct {
	AddedDownloadMs *float64 `json:"added_download_ms"`
	AddedUploadMs   *float64 `json:"added_upload_ms"`
	Grade           *string  `json:"grade"`
}

// WorkBufferbloat grades the latency under load against the idle latency's median.
func WorkBufferbloat(idle, download, upload *float64) Bufferbloat {
	added := func(loaded *float64) *float64 {
		if idle == nil || loaded == nil {
			return nil
		}
		v := *loaded - *idle
		return &v
	}
	out := Bufferbloat{AddedDownloadMs: added(download), AddedUploadMs: added(upload)}
	var worst *float64
	for _, v := range []*float64{out.AddedDownloadMs, out.AddedUploadMs} {
		if v != nil && (worst == nil || *v > *worst) {
			worst = v
		}
	}
	out.Grade = stats.BufferbloatGrade(worst)
	return out
}

// Whoami is the client as the server sees it.
type Whoami struct {
	IP          *string `json:"ip"`
	Port        *int    `json:"port"`
	Family      *string `json:"family"`
	HTTPVersion *string `json:"http_version"`
}

// Options are what a full test runs.
type Options struct {
	Seconds    float64
	Streams    int
	Packets    int
	IntervalMs float64
	Loss       bool
	Local      bool  // report this computer's own address and MAC
	OneStream  *bool // nil: as the server says
}

// RunAll is a full test toward t: latency, download (and one stream's), upload, packet
// loss, and what they mean for calls and responsiveness.
func RunAll(ctx context.Context, t *Target, o Options, progress Progress) (*Result, error) {
	started := time.Now()
	info, err := t.GetInfo(ctx)
	if err != nil {
		return nil, err
	}
	var who Whoami
	var local, remote net.Addr
	trace := httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{GotConn: func(c httptrace.GotConnInfo) {
		local, remote = c.Conn.LocalAddr(), c.Conn.RemoteAddr()
	}})
	if _, err := t.GetJSON(trace, t.Client(1), "/whoami", nil, &who); err != nil {
		return nil, err
	}
	oneStream := true
	if o.OneStream != nil {
		oneStream = *o.OneStream
	} else if info.Defaults.OneStream != nil {
		oneStream = *info.Defaults.OneStream
	}
	oneStream = oneStream && o.Streams > 1
	packets := 0
	if o.Loss {
		packets = o.Packets
	}
	r := &Result{
		Version:   1,
		Tool:      "t2s/" + Version,
		StartedAt: started.UTC().Format("2006-01-02T15:04:05.000000-07:00"),
		Server: ServerRef{
			ID: info.Server.ID, Name: info.Server.Name, Location: info.Server.Location,
			URL: t.Base, Version: info.Version,
		},
		Client: ClientRef{IP: who.IP, Family: who.Family, Protocol: who.HTTPVersion, UserAgent: UserAgent()},
		Config: Config{Streams: o.Streams, Seconds: o.Seconds, Packets: packets, IntervalMs: o.IntervalMs, OneStream: oneStream},
	}
	if o.Local && local != nil {
		if ip := addrIP(local); ip != "" {
			r.Client.LocalIP = ip
			if mac := iface.MACOf(ip, addrIP(remote)); mac != "" {
				r.Client.MAC, r.Client.MACSource = mac, "nic"
			}
		}
	}
	if r.Latency, err = MeasureLatency(ctx, t, 20, progress); err != nil {
		return nil, err
	}
	if steps := NewConnection(ctx, t, 5*time.Second); steps != nil { // what a new connection costs, idle
		v := roundPy(steps.Total(), 3)
		r.Latency.SetupMs = &v
	}
	test := *t
	test.Test = NewSession() + NewSession() // one test: the upload goes on if the download did
	if r.Download, err = MeasureThroughput(ctx, &test, "download", o.Seconds, o.Streams, true, progress, ""); err != nil {
		return nil, err
	}
	if oneStream { // what a single file copy gets
		if r.OneStream, err = MeasureThroughput(ctx, &test, "download", o.Seconds, 1, false, progress, "1 stream"); err != nil {
			return nil, err
		}
	}
	if r.Upload, err = MeasureThroughput(ctx, &test, "upload", o.Seconds, o.Streams, true, progress, ""); err != nil {
		return nil, err
	}
	if o.Loss && info.Capabilities.WebRTC {
		loss, err := MeasureLoss(ctx, t, info, o.Packets, o.IntervalMs, ProbeBytes, 2*time.Second, progress)
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			loss = &Loss{Error: err.Error()}
		}
		r.Loss = loss
	}
	r.Bufferbloat = WorkBufferbloat(r.Latency.Median, loadedMedian(r.Download), loadedMedian(r.Upload))
	if r.Loss != nil && r.Loss.Error == "" {
		r.Call = quality.CallQuality(r.Loss.Probes())
	}
	r.Responsiveness = quality.Work(r.Download.Transfer(), r.Upload.Transfer(), r.Latency.SetupMs)
	r.DurationS = time.Since(started).Seconds()
	return r, nil
}

func loadedMedian(t *Throughput) *float64 {
	if t == nil || t.LoadedLatency == nil {
		return nil
	}
	return t.LoadedLatency.Median
}

func addrIP(a net.Addr) string {
	if a == nil {
		return ""
	}
	host, _, err := net.SplitHostPort(a.String())
	if err != nil {
		return ""
	}
	if ip := net.ParseIP(host); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			return v4.String()
		}
		return ip.String()
	}
	return host
}

// Save stores a result on the server, for History; its result ID.
func Save(ctx context.Context, t *Target, r *Result) (string, error) {
	var saved struct {
		ID string `json:"id"`
	}
	if _, err := t.PostJSON(ctx, t.Client(1), "/results", r, &saved); err != nil {
		return "", err
	}
	return saved.ID, nil
}
