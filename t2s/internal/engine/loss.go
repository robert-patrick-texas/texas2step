package engine

import (
	"context"
	"encoding/json"
	"math"
	"sync"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/stats"
)

// ProbeBytes is the loss test's probe size: at least a voice packet, since a path that
// can't carry these can't carry calls.
const ProbeBytes = 200

// Loss is the packet-loss test's result, or only Error where it couldn't run.
type Loss struct {
	Error       string        `json:"-"`
	Transport   string        `json:"transport"`
	Sent        int           `json:"sent"`
	Received    int           `json:"received"`
	Lost        int           `json:"lost"`
	LossPct     float64       `json:"loss_pct"`
	UpLossPct   *float64      `json:"up_loss_pct"`
	DownLossPct *float64      `json:"down_loss_pct"`
	OutOfOrder  int           `json:"out_of_order"`
	Duplicates  int           `json:"duplicates"`
	IntervalMs  float64       `json:"interval_ms"`
	Size        int           `json:"size"`
	Rtt         stats.Summary `json:"rtt"`
	Packets     num.Floats    `json:"packets"`
	*quality.OneWay
}

type lossFields Loss

func (l Loss) MarshalJSON() ([]byte, error) {
	if l.Error != "" {
		return json.Marshal(map[string]string{"error": l.Error})
	}
	return json.Marshal(lossFields(l))
}

// Probes is what call quality needs of it.
func (l *Loss) Probes() quality.Probes {
	p := quality.Probes{Packets: l.Packets, IntervalMs: l.IntervalMs, Ways: l.OneWay}
	if l.Rtt.Median != nil {
		p.MedianMs = *l.Rtt.Median
	}
	return p
}

// LossSummary sums up probes' round trips (NaN for those lost), in the order their echoes
// came, with the server's counters where it gave them.
func LossSummary(rtts []float64, order []int, duplicates int, server *Counters, transport string, intervalMs float64, size int) Loss {
	sent := len(rtts)
	received := 0
	var got []float64
	packets := make(num.Floats, sent)
	for i, r := range rtts {
		packets[i] = num.Null
		if !math.IsNaN(r) {
			received++
			got = append(got, r)
			packets[i] = num.Round(r, 3)
		}
	}
	outOfOrder := 0
	for i := 1; i < len(order); i++ {
		if order[i] < order[i-1] {
			outOfOrder++
		}
	}
	l := Loss{
		Transport: transport, Sent: sent, Received: received, Lost: sent - received,
		OutOfOrder: outOfOrder, Duplicates: duplicates, IntervalMs: intervalMs, Size: size,
		Rtt: stats.Summarize(got, 0, false), Packets: packets,
	}
	if sent > 0 {
		l.LossPct = float64(sent-received) / float64(sent) * 100
	}
	if server != nil && sent > 0 {
		l.UpLossPct = num.Ptr(math.Max(0, float64(sent-server.Rx)/float64(sent)*100))
		if server.Tx != 0 {
			l.DownLossPct = num.Ptr(math.Max(0, float64(server.Tx-received)/float64(server.Tx)*100))
		}
	}
	return l
}

// MeasureLoss sends numbered UDP probes over an unordered data channel without
// retransmits and counts the echoes. A server that stamps its echoes also gives each
// direction's delays and losses.
func MeasureLoss(ctx context.Context, t *Target, info *Info, packets int, intervalMs float64, size int, wait time.Duration, progress Progress) (*Loss, error) {
	size = max(size, probeHeader)
	probe, err := ConnectRtc(ctx, t, info.ICE(), "", 10*time.Second)
	if err != nil {
		return nil, err
	}
	defer probe.Close()
	var mu sync.Mutex
	rtts := make([]float64, packets)
	server := make([]float64, packets)
	back := make([]float64, packets)
	counts := make([]int, packets)
	sentAt := make([]float64, packets)
	for i := range packets {
		rtts[i], server[i], back[i], counts[i], sentAt[i] = num.Null, num.Null, num.Null, -1, num.Null
	}
	var order []int
	duplicates := 0
	allBack := make(chan struct{})
	probe.OnEcho(func(seq uint32, rtt, now float64, stamp *Stamp) {
		mu.Lock()
		defer mu.Unlock()
		i := int(seq)
		if i >= packets || math.IsNaN(sentAt[i]) {
			return
		}
		if !math.IsNaN(rtts[i]) {
			duplicates++
			return
		}
		rtts[i] = now - sentAt[i]
		back[i] = now
		if stamp != nil {
			server[i], counts[i] = stamp.At, stamp.Count
		}
		order = append(order, i)
		if len(order) == packets {
			close(allBack)
		}
	})
	stamps := size >= stamped && info.Capabilities.Stamps && probe.Stamp()

	start := time.Now()
	for seq := range packets {
		// Paced against the schedule rather than a fixed sleep, so a late wake doesn't
		// stretch the test.
		due := start.Add(time.Duration(float64(seq) * intervalMs * float64(time.Millisecond)))
		if err := sleep(ctx, time.Until(due)); err != nil {
			return nil, err
		}
		mu.Lock()
		sentAt[seq] = Now()
		mu.Unlock()
		probe.Send(uint32(seq), size)
		if seq%25 == 0 {
			progress.emit("loss", Tick{Sent: seq + 1})
		}
	}
	select {
	case <-allBack:
	case <-time.After(wait):
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	counters := probe.Counters(3 * time.Second)
	mu.Lock()
	defer mu.Unlock()
	probe.OnEcho(nil)
	out := LossSummary(rtts, order, duplicates, counters, "webrtc", intervalMs, size)
	if stamps {
		total := -1
		if counters != nil {
			total = counters.Rx
		}
		out.OneWay = quality.FromStamps(sentAt, server, back, counts, total)
	}
	return &out, nil
}
