// Package monitor is continuous probing, as the Monitor page does it (web/src/ui/views/
// monitor.ts, docs/methodology.md "Live monitor"): a steady stream of UDP probes to one
// server or to two at once, at the same moments; or a voice or video call's packets,
// scored every 10 seconds. A server that goes quiet is asked over HTTPS until it answers,
// then given a new session; where UDP doesn't get through, a WebSocket carries the probes.
package monitor

import (
	"context"
	"fmt"
	"math"
	"math/rand/v2"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// Getting a server back after an outage. A new UDP session holds a server port for up to
// 30 s if it never opens, so new sessions go one at a time and only once the server answers
// over HTTPS; the HTTPS checks are small.
const (
	silentMs    = 3000   // no echo for this long: the server has gone quiet
	checkMs     = 2000   // between HTTPS checks while it's quiet
	checkSlowMs = 10_000 // between them once it has been quiet a minute
	graceMs     = 1500   // after the server answers, for its session to come back by itself
	waitFirstMs = 5000   // between new sessions that fail: from 5 s, doubling to 30 s
	waitMostMs  = 30_000
	udpRetryMs  = 30_000 // over the TCP fallback, between tries of UDP
	lostAfterMs = 1000   // a probe not back within a second is lost
)

// Config is what Monitor sends.
type Config struct {
	Profile   string  // path, voice or video; "" for the server's choice
	Chosen    bool    // the profile was asked for, not the server's
	Rate      float64 // path probes a second
	Size      int     // bytes in a path probe
	VoiceSize int     // bytes in a voice packet
	Every     float64 // ms between voice packets
	Video     float64 // the video stream's kb/s
	Window    float64 // seconds the figures cover
	Seconds   float64 // stop after this long; 0 for no limit of one's own
}

// Event is something to tell: an outage, a reconnection, a fallback to TCP.
type Event struct {
	At   time.Time `json:"time"`
	Text string    `json:"text"`
}

type point struct {
	t   float64 // wall ms when the probe was sent
	rtt float64 // NaN: lost
}

type probe interface {
	send(seq uint32, size int)
	close()
}

type rtcProbe struct{ r *engine.RtcProbe }

func (p rtcProbe) send(seq uint32, size int) { p.r.Send(seq, size) }
func (p rtcProbe) close()                    { p.r.Close() }

type wsProbe struct {
	w    *engine.WsProber
	echo func(seq uint32, rtt float64)
}

func (p wsProbe) send(seq uint32, _ int) {
	go func() {
		if rtt, ok := p.w.Ping(time.Second); ok {
			p.echo(seq, rtt)
		}
	}()
}
func (p wsProbe) close() { p.w.Close() }

type down struct {
	since    float64 // wall ms of its last echo
	check    float64 // when to ask the server again over HTTPS
	answered float64 // when it answered over HTTPS while its probes still don't; NaN
	wait     float64 // ms between new sessions, growing while they fail
	next     float64 // the earliest time for the next new session
}

// Dest is one server being probed.
type Dest struct {
	Short     string // "A" or "B" with two servers; "" with one
	t         *engine.Target
	info      *engine.Info
	probe     probe
	transport string
	sentAt    map[uint32]float64 // the probes on the chart: path probes or voice packets
	sent      int
	latest    float64
	stream    *CallStream
	lastEcho  float64
	beat      float64 // wall ms of the latest probe sent while it's quiet
	down      *down
	busy      bool
	udpAt     float64
	points    []point
	all       []float64 // every round trip since Start, for the summary
	lost      int
	slices    map[float64]Slice // every slice scored since Start, by its start
	outages   int
}

// Name is the server's name, with A or B when there are two.
func (d *Dest) Name() string {
	name := d.info.Name(d.t)
	if d.Short != "" {
		return d.Short + " (" + name + ")"
	}
	return name
}

// Limit is how long Monitor may run, in ms (0 for no limit), and whose limit it is.
type Limit struct {
	Ms  float64
	Who string // a server's name, or "" for --seconds
}

// Monitor probes its servers until its context ends or a limit is reached.
type Monitor struct {
	mu        sync.Mutex
	ctx       context.Context
	cancel    context.CancelFunc
	cfg       Config
	Dests     []*Dest
	seq       uint32
	frame     int
	running   bool
	stopSend  context.CancelFunc
	run       int // which session a reconnection belongs to
	startedAt float64
	firstSent float64 // wall ms at the first probe: the time limit counts from it, as the call's slices do
	settling  float64 // wall ms at the time limit, while the last probes come back
	stopped   float64
	events    []Event
	ended     string // why it stopped, where it wasn't Ctrl-C
	limit     Limit
	notice    func(Event)
}

func wall() float64 { return float64(time.Now().UnixNano()) / 1e6 }

// New prepares a Monitor of targets; notice hears each event as it happens.
func New(cfg Config, targets []*engine.Target, notice func(Event)) *Monitor {
	m := &Monitor{cfg: cfg, notice: notice}
	for i, t := range targets {
		d := &Dest{t: t, sentAt: map[uint32]float64{}, latest: math.NaN(), slices: map[float64]Slice{}}
		if len(targets) > 1 {
			d.Short = string(rune('A' + i))
		}
		m.Dests = append(m.Dests, d)
	}
	return m
}

// Refusal is a server refusing Monitor: its message, and its link where it gives one.
type Refusal struct {
	Message string
	URL     string
}

func (r *Refusal) Error() string {
	if r.URL != "" {
		return r.Message + " " + r.URL
	}
	return r.Message
}

// Offers is whether info's server offers a profile: Path always, the calls unless it
// withholds them (monitor_voice, monitor_video).
func Offers(info *engine.Info, profile string) bool {
	if profile == "path" || info == nil || info.Monitor == nil {
		return true
	}
	if profile == "voice" && info.Monitor.Voice != nil {
		return *info.Monitor.Voice
	}
	if profile == "video" && info.Monitor.Video != nil {
		return *info.Monitor.Video
	}
	return true
}

var profileNames = map[string]string{"path": "Path", "voice": "Voice call", "video": "Video call"}

// Start reads each server's info, settles the profile, and opens a session to each: over
// UDP, or a WebSocket where UDP can't get through.
func (m *Monitor) Start(ctx context.Context) error {
	m.ctx, m.cancel = context.WithCancel(ctx)
	for _, d := range m.Dests {
		info, err := d.t.GetInfo(ctx)
		if err != nil {
			return fmt.Errorf("%s: %w", d.t.Base, err)
		}
		d.info = info
	}
	if m.cfg.Profile == "" {
		m.cfg.Profile = "path"
		if mon := m.Dests[0].info.Monitor; mon != nil && mon.Profile != "" {
			m.cfg.Profile = mon.Profile
		}
	}
	for _, d := range m.Dests {
		if Offers(d.info, m.cfg.Profile) {
			continue
		}
		if m.cfg.Chosen {
			return &Refusal{Message: fmt.Sprintf("%s doesn't offer Monitor's %s profile.", d.info.Name(d.t), profileNames[m.cfg.Profile])}
		}
		// The first server's own choice, which another withholds: the nearest it offers.
		if m.cfg.Profile == "video" && Offers(d.info, "voice") {
			m.cfg.Profile = "voice"
		} else {
			m.cfg.Profile = "path"
		}
	}
	m.limit = m.strictest()
	call := m.cfg.Profile != "path"
	errs := make([]error, len(m.Dests))
	var wg sync.WaitGroup
	for i, d := range m.Dests {
		if call {
			d.stream = NewCallStream(m.cfg.Every)
		}
		wg.Go(func() { errs[i] = m.connect(d) })
	}
	wg.Wait()
	reached := false
	for i, d := range m.Dests {
		if r, ok := errs[i].(*Refusal); ok {
			m.closeAll()
			return r
		}
		reached = reached || d.probe != nil
	}
	if !reached {
		m.closeAll()
		if len(m.Dests) > 1 {
			return fmt.Errorf("the servers are unreachable")
		}
		return fmt.Errorf("%s is unreachable", m.Dests[0].info.Name(m.Dests[0].t))
	}
	m.mu.Lock()
	m.running = true
	m.startedAt = wall()
	for _, d := range m.Dests {
		d.lastEcho = m.startedAt
	}
	m.every(m.ctx, 500*time.Millisecond, m.watch)
	m.every(m.ctx, 250*time.Millisecond, m.sweep)
	m.schedule()
	m.mu.Unlock()
	return nil
}

// strictest is the strictest limit on Monitor: --seconds, and each server's
// (page_monitor_max_runtime, which its own Monitor page keeps).
func (m *Monitor) strictest() Limit {
	l := Limit{Ms: m.cfg.Seconds * 1000}
	for _, d := range m.Dests {
		if s := d.info.Limits.MaxMonitorSeconds; s > 0 && (l.Ms == 0 || s*1000 < l.Ms) {
			l = Limit{Ms: s * 1000, Who: d.info.Name(d.t)}
		}
	}
	return l
}

// connect opens a session to one server: UDP, or a WebSocket when UDP can't get through.
// A Refusal where the server refused.
func (m *Monitor) connect(d *Dest) error {
	if d.info == nil {
		info, err := d.t.GetInfo(m.ctx)
		if err != nil {
			return err
		}
		m.mu.Lock()
		d.info = info
		m.mu.Unlock()
	}
	if d.info.Capabilities.WebRTC {
		p, transport, err := m.udp(d)
		if r, ok := err.(*Refusal); ok {
			return r
		}
		if err == nil {
			m.mu.Lock()
			d.probe, d.transport = p, transport
			m.mu.Unlock()
			return nil
		}
	}
	w, err := engine.DialWs(m.ctx, d.t, 5*time.Second)
	if err != nil {
		m.mu.Lock()
		d.transport = "unreachable"
		m.mu.Unlock()
		return nil
	}
	w.Closed = func(code int) {
		if code == engine.RuntimeClose {
			m.serverEnded(d, d.info.Limits.MaxSessionSeconds)
		}
	}
	m.mu.Lock()
	// Over TCP the probes are round trips of a fixed small size, and video isn't sent.
	d.probe = wsProbe{w: w, echo: func(seq uint32, rtt float64) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.record(d, seq, rtt)
		if d.stream != nil {
			d.stream.Echo(seq, rtt, engine.Now(), nil)
		}
	}}
	d.transport = "TCP · WebSocket (UDP unavailable)"
	d.udpAt = wall() + udpRetryMs
	m.mu.Unlock()
	return nil
}

// udp is a UDP session to one server, set up for the profile.
func (m *Monitor) udp(d *Dest) (probe, string, error) {
	rtc, err := engine.ConnectRtc(m.ctx, d.t, d.info.ICE(), "", 8*time.Second)
	if err != nil {
		return nil, "", err
	}
	if p := m.cfg.Profile; p != "path" {
		if answer := rtc.Profile(p); answer != nil && !answer.OK {
			rtc.Close()
			if answer.Reason == "traffic limit" {
				return nil, "", &Refusal{Message: or(answer.Message, "Testing temporarily disabled."), URL: answer.URL}
			}
			return nil, "", &Refusal{Message: fmt.Sprintf("%s doesn't offer Monitor's %s profile.", d.info.Name(d.t), profileNames[p])}
		}
		if d.info.Capabilities.Stamps {
			rtc.Stamp()
		}
	}
	// At a traffic limit the server holds a call to path probes: follow it. At its
	// session_max_runtime it ends the session: stop, rather than open another.
	rtc.OnNotice(func(msg map[string]any) {
		switch msg["op"] {
		case "limit":
			text, _ := msg["message"].(string)
			link, _ := msg["url"].(string)
			m.limited(or(text, "Testing temporarily disabled."), link)
		case "ended":
			seconds, _ := msg["seconds"].(float64)
			m.serverEnded(d, seconds)
		}
	})
	rtc.OnEcho(func(seq uint32, rtt, now float64, stamp *engine.Stamp) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.record(d, seq, rtt)
		if d.stream != nil {
			d.stream.Echo(seq, rtt, now, stamp)
		}
	})
	transport := "UDP · WebRTC"
	if path := rtc.Path(); path != nil {
		if strings.Contains(path.Address, ":") {
			transport += " · IPv6"
		} else {
			transport += " · IPv4"
		}
	}
	return rtcProbe{rtc}, transport, nil
}

func or(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

// record is an echo: on the chart if its probe is still waiting, and proof the server
// answers. Called with m.mu held.
func (m *Monitor) record(d *Dest, seq uint32, rtt float64) {
	d.lastEcho = wall()
	t, ok := d.sentAt[seq]
	if !ok {
		return
	}
	delete(d.sentAt, seq)
	d.latest = rtt
	d.all = append(d.all, rtt)
	d.push(point{t, rtt})
}

// push adds a point in time order, and lets go of those older than the longest window.
func (d *Dest) push(p point) {
	i := len(d.points)
	for i > 0 && d.points[i-1].t > p.t {
		i--
	}
	d.points = append(d.points, point{})
	copy(d.points[i+1:], d.points[i:])
	d.points[i] = p
	cut := 0
	for cut < len(d.points) && d.points[cut].t < p.t-keepMs {
		cut++
	}
	if cut > 0 {
		d.points = d.points[cut:]
	}
}

func (m *Monitor) event(text string) {
	e := Event{At: time.Now(), Text: text}
	m.events = append(m.events, e)
	if len(m.events) > 300 {
		m.events = m.events[1:]
	}
	if m.notice != nil {
		m.notice(e)
	}
}

func clock(ms float64) string { return time.UnixMilli(int64(ms)).Format("15:04:05") }

// schedule starts the probes, as the profile sends them. Called with m.mu held.
func (m *Monitor) schedule() {
	if m.stopSend != nil {
		m.stopSend()
	}
	if m.settling > 0 {
		return // past the time limit: no more probes
	}
	var ctx context.Context
	ctx, m.stopSend = context.WithCancel(m.ctx)
	c := m.cfg
	call := c.Profile != "path"
	every := 1000 / c.Rate
	size := c.Size
	if call {
		every, size = c.Every, c.VoiceSize
	}
	// One clock for every server, so their packets leave together.
	send := func(size, frame int, charted bool) {
		now, at := wall(), engine.Now()
		if m.firstSent == 0 {
			m.firstSent = now
		}
		// Half a probe early, so the one due at the limit, a slice past it, isn't sent.
		if m.limit.Ms > 0 && now-m.firstSent >= m.limit.Ms-every/2 {
			m.atLimit(now)
			return
		}
		seq := m.seq
		m.seq++
		for _, d := range m.Dests {
			if frame >= 0 && strings.HasPrefix(d.transport, "TCP") {
				continue
			}
			// Every probe keeps its place, so an outage counts as loss.
			if charted {
				d.sentAt[seq] = now
				d.sent++
			}
			if d.stream != nil {
				d.stream.Sent(seq, frame, at, now)
			}
			// A quiet server gets a probe a second, enough to see it come back by itself.
			if d.probe == nil || (d.down != nil && now-d.beat < 1000) {
				continue
			}
			d.beat = now
			d.probe.send(seq, size)
		}
	}
	m.every(ctx, time.Duration(every*float64(time.Millisecond)), func() { send(size, -1, true) })
	if c.Profile == "video" {
		// A frame at a time: its packets leave together, as a video call sends them.
		burst := FramePackets(c.Video)
		m.every(ctx, time.Second/VideoFPS, func() {
			frame := m.frame
			m.frame++
			for range burst {
				send(VideoBytes, frame, false)
			}
		})
	}
}

// sweep counts the probes not back within a second as lost. Called with m.mu held.
func (m *Monitor) sweep() {
	cutoff := wall() - lostAfterMs
	for _, d := range m.Dests {
		for seq, t := range d.sentAt {
			if t < cutoff {
				delete(d.sentAt, seq)
				d.lost++
				d.push(point{t, math.NaN()})
			}
		}
	}
}

// every runs f with m.mu held every d, until ctx ends or Monitor stops.
func (m *Monitor) every(ctx context.Context, d time.Duration, f func()) {
	go func() {
		ticker := time.NewTicker(d)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				m.mu.Lock()
				if m.running {
					f()
				}
				m.mu.Unlock()
			}
		}
	}()
}

// watch looks for a server whose echoes stopped silentMs ago: Monitor asks it over HTTPS,
// and once it answers there while its probes still don't, opens a new session to it. Over
// TCP it tries UDP again every udpRetryMs. Called with m.mu held.
func (m *Monitor) watch() {
	now := wall()
	if m.settling > 0 {
		if now-m.settling >= SettleMs {
			m.stopLocked()
			m.cancel()
		}
		return
	}
	for _, d := range m.Dests {
		if d.busy {
			continue
		}
		quiet := now-d.lastEcho > silentMs
		switch {
		case d.down == nil && quiet:
			d.down = &down{since: d.lastEcho, check: now, answered: math.NaN(), next: now}
			m.event(fmt.Sprintf("%s hasn't answered since %s. Monitor reconnects as soon as it answers again.", d.Name(), clock(d.lastEcho)))
		case d.down == nil:
			if strings.HasPrefix(d.transport, "TCP") && d.info != nil && d.info.Capabilities.WebRTC && now >= d.udpAt {
				d.busy = true
				go m.tryUdp(d)
			}
		case !quiet:
			m.restored(d)
		case !math.IsNaN(d.down.answered) && now-d.down.answered >= graceMs && now >= d.down.next:
			d.busy = true
			go m.reconnect(d)
		case now >= d.down.check:
			d.busy = true
			go m.check(d)
		}
	}
}

// check asks a quiet server over HTTPS whether the network, and the server, are back.
func (m *Monitor) check(d *Dest) {
	ctx, cancel := context.WithTimeout(m.ctx, 2*time.Second)
	_, err := d.t.GetJSON(ctx, d.t.Client(1), "/health", nil, nil)
	cancel()
	m.mu.Lock()
	defer m.mu.Unlock()
	d.busy = false
	if d.down == nil {
		return
	}
	now := wall()
	if err == nil && math.IsNaN(d.down.answered) {
		d.down.answered = now
	} else if err != nil {
		d.down.answered = math.NaN()
	}
	gap := float64(checkMs)
	if now-d.down.since >= 60_000 {
		gap = checkSlowMs
	}
	// A little earlier or later each time, so clients don't all ask a restarted server at once.
	d.down.check = now + gap*(0.8+rand.Float64()*0.4)
}

// reconnect opens a new session to a server that answers over HTTPS but not to its probes.
func (m *Monitor) reconnect(d *Dest) {
	m.mu.Lock()
	run := m.run
	m.drop(d)
	d.info = nil // a restarted server may have new settings
	d.transport = "reconnecting…"
	m.mu.Unlock()
	err := m.connect(d)
	m.mu.Lock()
	defer m.mu.Unlock()
	d.busy = false
	if run != m.run || !m.running {
		m.drop(d)
		return
	}
	if d.stream != nil {
		d.stream.NewSession()
	}
	if dn := d.down; dn != nil {
		// Until its first echo, it's still down; if none comes, the next try waits longer.
		now := wall()
		if dn.wait == 0 {
			dn.wait = waitFirstMs
		} else {
			dn.wait = math.Min(dn.wait*2, waitMostMs)
		}
		dn.next, dn.answered, dn.check = now+dn.wait, math.NaN(), now+silentMs
	}
	if r, ok := err.(*Refusal); ok {
		m.event(r.Error())
	} else if err != nil {
		m.event(fmt.Sprintf("%s: %v", d.t.Base, err))
	}
}

// drop closes a server's session. Called with m.mu held: the closing goes on without it,
// as a session's callbacks wait for it.
func (m *Monitor) drop(d *Dest) {
	if p := d.probe; p != nil {
		d.probe = nil
		go p.close()
	}
}

// tryUdp tries UDP again over the TCP fallback; it takes over if it opens.
func (m *Monitor) tryUdp(d *Dest) {
	m.mu.Lock()
	run := m.run
	d.udpAt = wall() + udpRetryMs
	m.mu.Unlock()
	p, transport, err := m.udp(d)
	m.mu.Lock()
	defer m.mu.Unlock()
	d.busy = false
	if err != nil {
		return
	}
	if run != m.run || !m.running || !strings.HasPrefix(d.transport, "TCP") {
		p.close()
		return
	}
	m.drop(d)
	d.probe, d.transport = p, transport
	if d.stream != nil {
		d.stream.NewSession()
	}
	m.event(fmt.Sprintf("%s: UDP gets through again, %s", d.Name(), transport))
}

// restored is a quiet server answering its probes again: for how long it was gone.
func (m *Monitor) restored(d *Dest) {
	dn := d.down
	d.down = nil
	d.outages++
	gap := math.Max(0, math.Round((d.lastEcho-dn.since)/1000))
	span := fmt.Sprintf("%.0fs", gap)
	if gap >= 120 {
		span = fmt.Sprintf("%.0f min", math.Round(gap/60))
	}
	m.event(fmt.Sprintf("%s didn't answer from %s to %s, for %s.", d.Name(), clock(dn.since), clock(d.lastEcho), span))
}

// limited is a server's traffic limit reached: Monitor carries on with path probes.
func (m *Monitor) limited(message, link string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cfg.Profile == "path" {
		return
	}
	m.cfg.Profile = "path"
	text := message + " Monitor carries on with small probes."
	if link != "" {
		text += " " + link
	}
	m.event(text)
	for _, d := range m.Dests {
		d.stream = nil
	}
	if m.running {
		m.schedule()
	}
}

// serverEnded is a server ending its session at its session_max_runtime: Monitor stops.
func (m *Monitor) serverEnded(d *Dest, seconds float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if seconds > 0 {
		m.end(fmt.Sprintf("%s ends each session after %s.", d.info.Name(d.t), Span(seconds)))
	} else {
		m.end(fmt.Sprintf("%s ended the session at its time limit.", d.info.Name(d.t)))
	}
}

// end stops Monitor at a limit. Called with m.mu held.
func (m *Monitor) end(why string) {
	if !m.running {
		return
	}
	m.ended = why
	m.event(why)
	m.stopLocked()
	m.cancel()
}

// atLimit stops the probes at the time limit. The last get SettleMs to come back, so the
// call's last slice is scored; then watch stops Monitor, as of the limit. Called with m.mu held.
func (m *Monitor) atLimit(now float64) {
	if m.settling > 0 {
		return
	}
	m.ended = fmt.Sprintf("Stopped after %s, %s's limit.", Span(m.limit.Ms/1000), m.limit.Who)
	if m.limit.Who == "" {
		m.ended = fmt.Sprintf("Stopped after %s, as --seconds asked.", Span(m.limit.Ms/1000))
	}
	m.event(m.ended)
	m.settling = max(now, m.firstSent+m.limit.Ms) // the probes sent cover the limit to its end
	m.stopSend()
}

// Done is closed once Monitor stops.
func (m *Monitor) Done() <-chan struct{} { return m.ctx.Done() }

// Stop stops Monitor: the figures hold where it stopped.
func (m *Monitor) Stop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.stopLocked()
	m.cancel()
}

func (m *Monitor) stopLocked() {
	if m.running {
		m.stopped = wall()
		if m.settling > 0 {
			m.stopped = m.settling
		}
	}
	m.running = false
	m.run++
	m.closeAllLocked()
}

func (m *Monitor) closeAll() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.closeAllLocked()
}

func (m *Monitor) closeAllLocked() {
	for _, d := range m.Dests {
		m.drop(d)
	}
}

// Ended is why Monitor stopped at a limit, "" if it was stopped.
func (m *Monitor) Ended() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.ended
}

// Span is a limit as Monitor says it: "90s", "15 min", "1 h", "1 h 30 min".
func Span(seconds float64) string {
	if seconds < 120 {
		return fmt.Sprintf("%.0fs", math.Round(seconds))
	}
	min := int(math.Round(seconds / 60))
	if min < 60 {
		return fmt.Sprintf("%d min", min)
	}
	hours := fmt.Sprintf("%d h", min/60)
	if min%60 != 0 {
		return fmt.Sprintf("%s %d min", hours, min%60)
	}
	return hours
}

// Figures are one server's, over the window, as Monitor's tiles show them.
type Figures struct {
	Latest *float64 `json:"latest_ms"`
	Median *float64 `json:"median_ms"`
	P95    *float64 `json:"p95_ms"`
	Jitter *float64 `json:"jitter_ms"`
	Loss   *float64 `json:"loss_pct"`
	Lost   int      `json:"lost"`
	Window int      `json:"window"`
	Sent   int      `json:"sent"`
	// With a call profile, over the window's 10-second slices:
	Mos        *float64 `json:"mos,omitempty"`
	Rating     string   `json:"rating,omitempty"` // the median slice's
	Worst      *float64 `json:"worst_mos,omitempty"`
	WorstAt    *float64 `json:"worst_at,omitempty"`
	Below      *float64 `json:"below_good_pct,omitempty"`
	BelowCount int      `json:"below_good,omitempty"`
	Slices     int      `json:"slices,omitempty"`
	Frames     *float64 `json:"frames_hit_pct,omitempty"` // the worse direction's
}

func ptr(v float64) *float64 { return &v }

func figures(d *Dest, now, windowMs float64, slices []Slice) Figures {
	from := now - windowMs
	var got []float64
	n := 0
	for _, p := range d.points {
		if p.t < from || p.t > now {
			continue
		}
		n++
		if !math.IsNaN(p.rtt) {
			got = append(got, p.rtt)
		}
	}
	f := Figures{Lost: n - len(got), Window: n, Sent: d.sent}
	if !math.IsNaN(d.latest) {
		f.Latest = ptr(d.latest)
	}
	if len(got) > 0 {
		f.Median, f.P95 = ptr(stats.Percentile(got, 50)), ptr(stats.Percentile(got, 95))
	}
	if len(got) > 1 {
		f.Jitter = ptr(stats.Jitter(got))
	}
	if n > 0 {
		f.Loss = ptr(float64(f.Lost) / float64(n) * 100)
	}
	var shown []Slice
	for _, s := range slices {
		if s.End > from && (s.Call != nil || s.Dead) {
			shown = append(shown, s)
		}
	}
	if len(shown) == 0 {
		return f
	}
	scores := make([]float64, len(shown))
	worst := shown[0]
	frames := map[string][2]int{}
	eachWay := false
	for i, s := range shown {
		scores[i] = s.Score()
		if s.Score() < worst.Score() {
			worst = s
		}
		if r := s.Rating(); r != "excellent" && r != "good" {
			f.BelowCount++
		}
		if fr := s.Frames; fr != nil {
			add := func(key string, c FrameCount) {
				v := frames[key]
				frames[key] = [2]int{v[0] + c.Total, v[1] + c.Hit}
			}
			add("", fr.FrameCount)
			if fr.Up != nil && fr.Down != nil {
				eachWay = true
				add("up", *fr.Up)
				add("down", *fr.Down)
			}
		}
	}
	f.Slices = len(shown)
	f.Mos = ptr(stats.Percentile(scores, 50))
	byScore := append([]Slice(nil), shown...)
	sort.SliceStable(byScore, func(i, j int) bool { return byScore[i].Score() < byScore[j].Score() })
	// The median slice's rating, so the word goes with the score shown.
	f.Rating = byScore[(len(byScore)-1)/2].Rating()
	f.Worst, f.WorstAt = ptr(worst.Score()), ptr(worst.Start)
	f.Below = ptr(float64(f.BelowCount) / float64(len(shown)) * 100)
	hits := func(key string) float64 {
		v := frames[key]
		if v[0] == 0 {
			return 0
		}
		return float64(v[1]) / float64(v[0]) * 100
	}
	if _, ok := frames[""]; ok {
		if eachWay {
			f.Frames = ptr(math.Max(hits("up"), hits("down")))
		} else {
			f.Frames = ptr(hits(""))
		}
	}
	return f
}

// View is one server's state, for the panel and the lines.
type View struct {
	Short     string
	Name      string
	Location  string
	Transport string
	Quiet     bool
	Figures   Figures
	points    []point
	Slices    []Slice
	New       []Slice // slices scored since the last snapshot
}

// Snapshot is what Monitor shows now.
type Snapshot struct {
	Now     float64
	Started float64
	Running bool
	Profile string
	Config  Config
	Limit   Limit
	Dests   []View
	Events  []Event
	Ended   string
}

// Snapshot is Monitor's state now; once stopped, as it stopped. Each call scores the
// slices whose packets have settled.
func (m *Monitor) Snapshot() Snapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := wall()
	scoreAt := now
	if m.settling > 0 {
		now = m.settling // the figures hold at the limit while the last probes come back
	}
	if !m.running && m.stopped > 0 {
		now = m.stopped
		scoreAt = m.stopped + SettleMs // stopped, the slices that ended by then are scored
	}
	s := Snapshot{Now: now, Started: m.startedAt, Running: m.running, Profile: m.cfg.Profile, Config: m.cfg, Limit: m.limit, Events: append([]Event(nil), m.events...), Ended: m.ended}
	for _, d := range m.Dests {
		v := View{Short: d.Short, Name: d.t.Base, Transport: d.transport, Quiet: d.down != nil, points: append([]point(nil), d.points...)}
		if d.info != nil {
			v.Name = d.info.Name(d.t)
			if d.info.Server.Location != nil {
				v.Location = *d.info.Server.Location
			}
		}
		if d.stream != nil {
			v.Slices = d.stream.Slices(scoreAt)
			for _, sl := range v.Slices {
				if _, seen := d.slices[sl.Start]; !seen {
					d.slices[sl.Start] = sl
					v.New = append(v.New, sl)
				}
			}
		}
		v.Figures = figures(d, now, m.cfg.Window*1000, v.Slices)
		s.Dests = append(s.Dests, v)
	}
	return s
}

// Total is one server's figures since Start, for the summary at the end.
type Total struct {
	Name    string   `json:"name"`
	URL     string   `json:"url"`
	Sent    int      `json:"sent"`
	Lost    int      `json:"lost"`
	LossPct *float64 `json:"loss_pct"`
	Median  *float64 `json:"median_ms"`
	P95     *float64 `json:"p95_ms"`
	Jitter  *float64 `json:"jitter_ms"`
	Outages int      `json:"outages"`
	Slices  int      `json:"slices,omitempty"`
	Mos     *float64 `json:"mos,omitempty"`
	Worst   *float64 `json:"worst_mos,omitempty"`
	WorstAt string   `json:"worst_at,omitempty"`
	Below   int      `json:"below_good,omitempty"`
}

// Totals are each server's figures since Start.
func (m *Monitor) Totals() []Total {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []Total
	for _, d := range m.Dests {
		t := Total{Name: d.Name(), URL: d.t.Base, Sent: d.sent, Lost: d.lost, Outages: d.outages}
		if resolved := len(d.all) + d.lost; resolved > 0 {
			t.LossPct = ptr(float64(d.lost) / float64(resolved) * 100)
		}
		if len(d.all) > 0 {
			t.Median, t.P95 = ptr(stats.Percentile(d.all, 50)), ptr(stats.Percentile(d.all, 95))
			t.Jitter = ptr(stats.Jitter(d.all))
		}
		var scores []float64
		var worst *Slice
		for _, s := range d.slices {
			if s.Call == nil && !s.Dead {
				continue
			}
			scores = append(scores, s.Score())
			if worst == nil || s.Score() < worst.Score() {
				w := s
				worst = &w
			}
			if r := s.Rating(); r != "excellent" && r != "good" {
				t.Below++
			}
		}
		if len(scores) > 0 {
			t.Slices = len(scores)
			t.Mos = ptr(stats.Percentile(scores, 50))
			t.Worst, t.WorstAt = ptr(worst.Score()), clock(worst.Start)
		}
		out = append(out, t)
	}
	return out
}
