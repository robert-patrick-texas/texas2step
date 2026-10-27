package engine

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/pion/ice/v4"
	"github.com/pion/logging"
	"github.com/pion/webrtc/v4"
)

const (
	probeHeader = 12               // u32 sequence + f64 send time
	stamped     = probeHeader + 12 // and the server's f64 arrival time + u32 count
	// Probes held in the send queue before more are dropped instead: past this, the network
	// has stopped taking them (an outage), and they would only arrive late.
	maxQueued = 1 << 20
)

// Stamp is what the server stamped into an echo: its clock when the probe arrived (ms),
// and how many probes it had received by then, this one included.
type Stamp struct {
	At    float64
	Count int
}

// NotOffered is a server offering no UDP address in the family asked for.
type NotOffered struct{ Family string }

func (e NotOffered) Error() string {
	return fmt.Sprintf("the server offers no %s address for UDP", e.Family)
}

// RtcProbe is a WebRTC session to the server's UDP echo (docs/api.md, "UDP probe
// protocol"): an unordered channel without retransmits for the probes, and a reliable one
// for control requests.
type RtcProbe struct {
	pc       *webrtc.PeerConnection
	probe    *webrtc.DataChannel
	control  *webrtc.DataChannel
	mu       sync.Mutex
	stamping bool
	waiters  []waiter
	onEcho   func(seq uint32, rtt, now float64, stamp *Stamp)
	onNotice func(msg map[string]any)
}

type waiter struct {
	op   string
	done chan map[string]any
}

func loggers() logging.LoggerFactory {
	f := logging.NewDefaultLoggerFactory()
	if os.Getenv("T2S_DEBUG") == "" {
		f.Writer = io.Discard
	}
	return f
}

// ConnectRtc opens a UDP session to t, within timeout; family "ipv4" or "ipv6" holds both
// ends' candidates to it. Signalling goes over t's own address.
func ConnectRtc(ctx context.Context, t *Target, iceServers []string, family string, timeout time.Duration) (*RtcProbe, error) {
	se := webrtc.SettingEngine{LoggerFactory: loggers()}
	se.SetICEMulticastDNSMode(ice.MulticastDNSModeDisabled) // no .local names, no multicast
	switch family {
	case "ipv4":
		se.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4})
	case "ipv6":
		se.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP6})
	}
	api := webrtc.NewAPI(webrtc.WithSettingEngine(se))
	var servers []webrtc.ICEServer
	for _, u := range iceServers {
		servers = append(servers, webrtc.ICEServer{URLs: []string{u}})
	}
	pc, err := api.NewPeerConnection(webrtc.Configuration{ICEServers: servers})
	if err != nil {
		return nil, err
	}
	ok := false
	defer func() {
		if !ok {
			_ = pc.Close()
		}
	}()
	ordered, retransmits := false, uint16(0)
	probe, err := pc.CreateDataChannel("probe", &webrtc.DataChannelInit{Ordered: &ordered, MaxRetransmits: &retransmits})
	if err != nil {
		return nil, err
	}
	control, err := pc.CreateDataChannel("control", nil)
	if err != nil {
		return nil, err
	}
	opened, controlOpened := make(chan struct{}), make(chan struct{})
	probe.OnOpen(func() { close(opened) })
	control.OnOpen(func() { close(controlOpened) })
	r := &RtcProbe{pc: pc, probe: probe, control: control}
	probe.OnMessage(r.message)
	control.OnMessage(r.controlMessage)

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return nil, err
	}
	gathered := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		return nil, err
	}
	// The server answers without trickle ICE, so it gets a complete candidate list.
	select {
	case <-gathered:
	case <-time.After(2500 * time.Millisecond):
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	sdp := pc.LocalDescription().SDP
	if family != "" {
		sdp = onlyFamily(sdp, family)
	}
	var answer struct {
		SDP string `json:"sdp"`
	}
	signal, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if _, err := t.PostJSON(signal, t.Client(1), "/rtc/offer", map[string]string{"sdp": sdp, "type": "offer"}, &answer); err != nil {
		return nil, err
	}
	remote := answer.SDP
	if family != "" {
		remote = onlyFamily(remote, family)
		if !strings.Contains(remote, "a=candidate:") {
			name := map[string]string{"ipv4": "IPv4", "ipv6": "IPv6"}[family]
			return nil, NotOffered{Family: name}
		}
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: remote}); err != nil {
		return nil, err
	}
	select {
	case <-opened:
	case <-time.After(timeout):
		return nil, errors.New("UDP path did not open (ICE failed or UDP blocked)")
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	select {
	case <-controlOpened:
	case <-time.After(2 * time.Second):
	}
	ok = true
	return r, nil
}

// onlyFamily keeps the SDP's candidates of one address family.
func onlyFamily(sdp, family string) string {
	lines := strings.Split(sdp, "\r\n")
	out := lines[:0]
	for _, line := range lines {
		if strings.HasPrefix(line, "a=candidate:") {
			fields := strings.Fields(line)
			if len(fields) < 5 {
				continue
			}
			addr := fields[4]
			if !strings.HasSuffix(addr, ".local") && strings.Contains(addr, ":") != (family == "ipv6") {
				continue
			}
		}
		out = append(out, line)
	}
	return strings.Join(out, "\r\n")
}

func (r *RtcProbe) message(m webrtc.DataChannelMessage) {
	now := Now()
	if m.IsString || len(m.Data) < probeHeader {
		return
	}
	seq := binary.LittleEndian.Uint32(m.Data)
	sent := math.Float64frombits(binary.LittleEndian.Uint64(m.Data[4:]))
	r.mu.Lock()
	stamping, onEcho := r.stamping, r.onEcho
	r.mu.Unlock()
	var stamp *Stamp
	if stamping && len(m.Data) >= stamped {
		stamp = &Stamp{
			At:    math.Float64frombits(binary.LittleEndian.Uint64(m.Data[12:])),
			Count: int(binary.LittleEndian.Uint32(m.Data[20:])),
		}
	}
	if onEcho != nil {
		onEcho(seq, now-sent, now, stamp)
	}
}

func (r *RtcProbe) controlMessage(m webrtc.DataChannelMessage) {
	var msg map[string]any
	if json.Unmarshal(m.Data, &msg) != nil {
		return
	}
	op, _ := msg["op"].(string)
	r.mu.Lock()
	// Each reply names its request ("op"), so a late one can't answer another request.
	for i, w := range r.waiters {
		if w.op == op {
			r.waiters = append(r.waiters[:i], r.waiters[i+1:]...)
			r.mu.Unlock()
			w.done <- msg
			return
		}
	}
	notice := r.onNotice
	r.mu.Unlock()
	if notice != nil {
		notice(msg)
	}
}

// OnEcho hears each echo: its sequence number, round trip and arrival (ms, this process's
// clock), and the server's stamp where stamps are on.
func (r *RtcProbe) OnEcho(f func(seq uint32, rtt, now float64, stamp *Stamp)) {
	r.mu.Lock()
	r.onEcho = f
	r.mu.Unlock()
}

// OnNotice hears what the server sends unasked: {"op": "limit"} at a traffic limit,
// {"op": "ended"} at its session_max_runtime.
func (r *RtcProbe) OnNotice(f func(msg map[string]any)) {
	r.mu.Lock()
	r.onNotice = f
	r.mu.Unlock()
}

// Open is whether the probe channel is still open.
func (r *RtcProbe) Open() bool {
	return r.probe.ReadyState() == webrtc.DataChannelStateOpen
}

// Send sends probe seq, size bytes long, with random padding new for each probe; when it
// was sent. On a channel that has closed, or that the network has stopped draining, the
// probe is dropped: it counts as lost.
func (r *RtcProbe) Send(seq uint32, size int) float64 {
	now := Now()
	if !r.Open() || r.probe.BufferedAmount() > maxQueued {
		return now
	}
	buf := make([]byte, max(size, probeHeader))
	_, _ = rand.Read(buf[probeHeader:])
	binary.LittleEndian.PutUint32(buf, seq)
	binary.LittleEndian.PutUint64(buf[4:], math.Float64bits(now))
	_ = r.probe.Send(buf)
	return now
}

// Ask sends one request on the control channel and waits for its reply; nil if none came
// within timeout (an older server doesn't know every op, and never replies).
func (r *RtcProbe) Ask(op string, extra map[string]any, timeout time.Duration) map[string]any {
	if r.control.ReadyState() != webrtc.DataChannelStateOpen {
		return nil
	}
	w := waiter{op: op, done: make(chan map[string]any, 1)}
	r.mu.Lock()
	r.waiters = append(r.waiters, w)
	r.mu.Unlock()
	msg := map[string]any{"op": op}
	for k, v := range extra {
		msg[k] = v
	}
	data, _ := json.Marshal(msg)
	if err := r.control.SendText(string(data)); err == nil {
		select {
		case reply := <-w.done:
			return reply
		case <-time.After(timeout):
		}
	}
	r.mu.Lock()
	for i, x := range r.waiters {
		if x.done == w.done {
			r.waiters = append(r.waiters[:i], r.waiters[i+1:]...)
			break
		}
	}
	r.mu.Unlock()
	return nil
}

// Stamp asks the server to stamp each echo; whether it will.
func (r *RtcProbe) Stamp() bool {
	on := r.Ask("stamp", nil, 2*time.Second) != nil
	r.mu.Lock()
	r.stamping = on
	r.mu.Unlock()
	return on
}

// Counters are the server's counts of probes received and echoed.
type Counters struct {
	Rx int `json:"rx"`
	Tx int `json:"tx"`
}

// Counters asks for the server's counts; nil if it didn't answer.
func (r *RtcProbe) Counters(timeout time.Duration) *Counters {
	msg := r.Ask("stats", nil, timeout)
	if msg == nil {
		return nil
	}
	n := func(v any) int { f, _ := v.(float64); return int(f) }
	return &Counters{Rx: n(msg["rx"]), Tx: n(msg["tx"])}
}

// ProfileReply is the server's answer to one of Monitor's profiles asked for.
type ProfileReply struct {
	OK      bool
	Reason  string
	Message string
	URL     string
}

// Profile asks to run one of Monitor's profiles; nil from a server from before profiles.
func (r *RtcProbe) Profile(name string) *ProfileReply {
	msg := r.Ask("profile", map[string]any{"name": name}, 2*time.Second)
	if msg == nil {
		return nil
	}
	s := func(k string) string { v, _ := msg[k].(string); return v }
	ok, _ := msg["ok"].(bool)
	return &ProfileReply{OK: ok, Reason: s("reason"), Message: s("message"), URL: s("url")}
}

// UdpAddress is how the server sees the client on the UDP path (after NAT), and its own
// end of it.
type UdpAddress struct {
	IP         string
	Port       int
	ServerIP   string
	ServerPort int
}

// Address asks the server for both ends of the UDP path; nil if it didn't answer.
func (r *RtcProbe) Address() *UdpAddress {
	msg := r.Ask("addr", nil, 2*time.Second)
	if msg == nil {
		return nil
	}
	s := func(k string) string { v, _ := msg[k].(string); return v }
	n := func(k string) int { v, _ := msg[k].(float64); return int(v) }
	return &UdpAddress{IP: s("ip"), Port: n("port"), ServerIP: s("server_ip"), ServerPort: n("server_port")}
}

// UdpPath is the path ICE chose: the server's address and port, this computer's own
// address on it, and the DTLS protecting it.
type UdpPath struct {
	Address string
	Port    int
	Local   string
	DTLS    string // "DTLS 1.2 TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256", say
}

// Path is the UDP path ICE chose; nil before it has one.
func (r *RtcProbe) Path() *UdpPath {
	sctp := r.pc.SCTP()
	if sctp == nil || sctp.Transport() == nil || sctp.Transport().ICETransport() == nil {
		return nil
	}
	pair, err := sctp.Transport().ICETransport().GetSelectedCandidatePair()
	if err != nil || pair == nil || pair.Remote == nil {
		return nil
	}
	out := &UdpPath{Address: pair.Remote.Address, Port: int(pair.Remote.Port)}
	if l := pair.Local; l != nil {
		out.Local = l.Address
		if l.Typ != webrtc.ICECandidateTypeHost && l.RelatedAddress != "" {
			out.Local = l.RelatedAddress
		}
	}
	for _, s := range r.pc.GetStats() {
		if ts, ok := s.(webrtc.TransportStats); ok && (ts.TLSVersion != "" || ts.DTLSCipher != "") {
			out.DTLS = strings.TrimSpace(dtlsName(ts.TLSVersion) + " " + ts.DTLSCipher)
		}
	}
	if out.DTLS == "" {
		out.DTLS = "DTLS 1.2"
	}
	return out
}

func dtlsName(hex string) string {
	switch strings.ToUpper(hex) {
	case "FEFF":
		return "DTLS 1.0"
	case "FEFD":
		return "DTLS 1.2"
	case "FEFC":
		return "DTLS 1.3"
	case "":
		return "DTLS"
	}
	return "DTLS " + hex
}

// Close ends the session.
func (r *RtcProbe) Close() {
	_ = r.probe.Close()
	_ = r.control.Close()
	_ = r.pc.Close()
}
