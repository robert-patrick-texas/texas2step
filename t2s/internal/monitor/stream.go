package monitor

import (
	"math"
	"sort"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// Monitor's call profiles, as web/src/engine/stream.ts has them: a call's packets sent for
// as long as Monitor runs, and every 10 seconds scored as the packet-loss test is scored.
const (
	VideoBytes = 1100 // a video packet: under what fits one UDP packet
	VideoFPS   = 30
	SliceMs    = 10_000
	SettleMs   = 1500 // a slice is scored once its last packet has had time to come back
	keepMs     = 15 * 60_000
)

// FramePackets is video packets per frame for a rate: a frame is sent as a burst of them.
func FramePackets(kbps float64) int {
	return max(1, int(math.Round(kbps*1000/(8*VideoBytes*VideoFPS))))
}

type packet struct {
	seq    uint32
	frame  int     // the video frame it belongs to; -1 for voice
	wall   float64 // ms since the epoch when sent
	sent   float64 // this process's clock when sent
	rtt    float64 // NaN until it comes back
	server float64 // the server's stamp: its clock on arrival; NaN without
	back   float64 // this process's clock when the echo came back
	lostUp bool    // lost on the way to the server, by the server's counts
}

// FrameCount is video frames hit by a lost or late packet, of those sent.
type FrameCount struct {
	Total int `json:"total"`
	Hit   int `json:"hit"`
}

// Frames is a slice's video frames hit: the worse way's, and each way with the stamps.
type Frames struct {
	FrameCount
	Up   *FrameCount `json:"up,omitempty"`   // what the other end sees
	Down *FrameCount `json:"down,omitempty"` // what you see
}

// Share is the share of frames hit, 0 to 1.
func (c FrameCount) Share() float64 {
	if c.Total == 0 {
		return 0
	}
	return float64(c.Hit) / float64(c.Total)
}

// Slice is one 10-second slice of a call, counted from the first packet sent: its
// quality, or Dead when no packet came back.
type Slice struct {
	Start  float64       `json:"start"` // ms since the epoch
	End    float64       `json:"end"`
	Call   *quality.Call `json:"call"`
	Dead   bool          `json:"dead"`
	Lost   int           `json:"lost"`
	Sent   int           `json:"sent"`
	Frames *Frames       `json:"frames"`
}

// Score is the slice's score: a dead one counts as 1.
func (s Slice) Score() float64 {
	if s.Dead || s.Call == nil {
		return 1
	}
	return s.Call.Mos
}

// Rating is the slice's rating: a dead one is bad.
func (s Slice) Rating() string {
	if s.Dead || s.Call == nil {
		return "bad"
	}
	return s.Call.Rating
}

// CallStream is a call's packets and their 10-second slices.
type CallStream struct {
	voiceMs float64
	packets map[uint32]*packet
	bySlice map[int][]*packet
	last    *struct{ seq, count int } // the latest stamped echo
	origin  float64                   // wall ms at the first packet: slices count from it
	started bool
	scored  map[int]Slice
}

// NewCallStream is a call whose voice packets go every voiceMs.
func NewCallStream(voiceMs float64) *CallStream {
	return &CallStream{voiceMs: voiceMs, packets: map[uint32]*packet{}, bySlice: map[int][]*packet{}, scored: map[int]Slice{}}
}

// NewSession is a new session to the server, after an outage: its counts start again.
func (c *CallStream) NewSession() { c.last = nil }

// Sent is a packet sent: voice (frame -1) or one of a video frame's.
func (c *CallStream) Sent(seq uint32, frame int, sent, wall float64) {
	if c.last == nil { // the server counts from its session's first probe, this stream's
		c.last = &struct{ seq, count int }{int(seq) - 1, 0}
	}
	if !c.started {
		c.origin, c.started = wall, true
	}
	p := &packet{seq: seq, frame: frame, wall: wall, sent: sent, rtt: math.NaN(), server: math.NaN(), back: math.NaN()}
	c.packets[seq] = p
	k := int(math.Floor((wall - c.origin) / SliceMs))
	c.bySlice[k] = append(c.bySlice[k], p)
}

// Echo is an echo. With the server's stamps, the gap since the last stamped echo shows how
// many of the packets in between reached the server; the rest were lost on the way there,
// and are taken to be the first, as quality.FromStamps takes them.
func (c *CallStream) Echo(seq uint32, rtt, now float64, stamp *engine.Stamp) {
	p, ok := c.packets[seq]
	if !ok || !math.IsNaN(p.rtt) {
		return
	}
	p.rtt, p.back = rtt, now
	if stamp == nil || c.last == nil {
		return
	}
	p.server = stamp.At
	last := c.last
	if int(seq) <= last.seq {
		return // came back out of order: the gap was placed already
	}
	missing := int(seq) - last.seq - 1
	reached := min(max(stamp.Count-last.count-1, 0), missing)
	for s := last.seq + 1; s < last.seq+1+missing-reached; s++ {
		if gone, ok := c.packets[uint32(s)]; ok {
			gone.lostUp = true
		}
	}
	c.last = &struct{ seq, count int }{int(seq), max(last.count, stamp.Count)}
}

// Slices is every slice scored in Monitor's longest window, oldest first, scoring those
// whose packets have settled by wall; only whole slices are scored.
func (c *CallStream) Slices(wall float64) []Slice {
	origin := c.origin
	if !c.started {
		origin = wall
	}
	for k, list := range c.bySlice {
		if origin+float64(k+1)*SliceMs+SettleMs > wall {
			continue
		}
		c.scored[k] = c.score(k, list)
		delete(c.bySlice, k)
		// A packet still missing with no stamped echo after it, as in an outage, counts as
		// lost coming back.
		for _, p := range list {
			delete(c.packets, p.seq)
		}
	}
	out := make([]Slice, 0, len(c.scored))
	for k, s := range c.scored {
		if origin+float64(k+1)*SliceMs < wall-keepMs {
			delete(c.scored, k)
			continue
		}
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Start < out[j].Start })
	return out
}

func (c *CallStream) score(k int, packets []*packet) Slice {
	sort.Slice(packets, func(i, j int) bool { return packets[i].seq < packets[j].seq })
	var voice, video []*packet
	for _, p := range packets {
		if p.frame < 0 {
			voice = append(voice, p)
		} else {
			video = append(video, p)
		}
	}
	var got []float64
	for _, p := range voice {
		if !math.IsNaN(p.rtt) {
			got = append(got, p.rtt)
		}
	}
	start := c.origin + float64(k)*SliceMs
	s := Slice{Start: start, End: start + SliceMs, Lost: len(voice) - len(got), Sent: len(voice), Frames: frames(video)}
	if len(got) == 0 {
		s.Dead = len(voice) > 0
		return s
	}
	probes := quality.Probes{Packets: make(num.Floats, len(voice)), IntervalMs: c.voiceMs, MedianMs: stats.Percentile(got, 50)}
	stamped := false
	for i, p := range voice {
		probes.Packets[i] = p.rtt
		stamped = stamped || !math.IsNaN(p.server)
	}
	if stamped {
		probes.Ways = delays(voice)
	}
	s.Call = quality.CallQuality(probes)
	return s
}

// delays is each packet's delay above the fastest, each way, from the server's stamps
// (NaN when it didn't come back), and which were lost on the way there, by position.
func delays(packets []*packet) *quality.OneWay {
	upMin, downMin := math.Inf(1), math.Inf(1)
	for _, p := range packets {
		if !math.IsNaN(p.server) && !math.IsNaN(p.rtt) {
			upMin = math.Min(upMin, p.server-p.sent)
			downMin = math.Min(downMin, p.back-p.server)
		}
	}
	out := &quality.OneWay{UpMs: make(num.Floats, len(packets)), DownMs: make(num.Floats, len(packets)), LostUpstream: []int{}}
	for i, p := range packets {
		out.UpMs[i], out.DownMs[i] = num.Null, num.Null
		if !math.IsNaN(p.server) && !math.IsNaN(p.rtt) {
			out.UpMs[i], out.DownMs[i] = p.server-p.sent-upMin, p.back-p.server-downMin
		}
		if math.IsNaN(p.rtt) && p.lostUp {
			out.LostUpstream = append(out.LostUpstream, i)
		}
	}
	return out
}

// frames is video frames hit by a lost or late packet: each way with the stamps, the worse
// way's at the top; on the round trip without them. Nil without video.
func frames(video []*packet) *Frames {
	if len(video) == 0 {
		return nil
	}
	byFrame := map[int][]*packet{}
	var order []int
	stamped := false
	for _, p := range video {
		if _, ok := byFrame[p.frame]; !ok {
			order = append(order, p.frame)
		}
		byFrame[p.frame] = append(byFrame[p.frame], p)
		stamped = stamped || !math.IsNaN(p.server)
	}
	if !stamped {
		fastest := math.Inf(1)
		for _, p := range video {
			if !math.IsNaN(p.rtt) {
				fastest = math.Min(fastest, p.rtt)
			}
		}
		if math.IsInf(fastest, 1) {
			fastest = 0
		}
		hit := 0
		for _, f := range order {
			for _, p := range byFrame[f] {
				if math.IsNaN(p.rtt) || p.rtt-fastest > quality.BufferMs {
					hit++
					break
				}
			}
		}
		return &Frames{FrameCount: FrameCount{Total: len(order), Hit: hit}}
	}
	ways := delays(video)
	at := map[*packet]int{}
	for i, p := range video {
		at[p] = i
	}
	late := func(list num.Floats, p *packet) bool { v := list[at[p]]; return !math.IsNaN(v) && v > quality.BufferMs }
	up := FrameCount{Total: len(order)}
	down := FrameCount{}
	for _, f := range order {
		for _, p := range byFrame[f] {
			if (math.IsNaN(p.rtt) && p.lostUp) || late(ways.UpMs, p) {
				up.Hit++
				break
			}
		}
		var reached []*packet
		for _, p := range byFrame[f] {
			if !(math.IsNaN(p.rtt) && p.lostUp) {
				reached = append(reached, p)
			}
		}
		if len(reached) == 0 {
			continue
		}
		down.Total++
		for _, p := range reached {
			if math.IsNaN(p.rtt) || late(ways.DownMs, p) {
				down.Hit++
				break
			}
		}
	}
	worse := down
	if up.Share() > down.Share() {
		worse = up
	}
	return &Frames{FrameCount: worse, Up: &up, Down: &down}
}
