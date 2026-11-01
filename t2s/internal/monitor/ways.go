package monitor

import "github.com/robert-patrick-texas/texas2step/t2s/internal/quality"

// Which way Monitor's lost probes went, as web/src/engine/ways.ts works it out: each one
// the server never got was lost on the way there, and each it got on the way back. Monitor
// learns it from the server's record of the probes that arrived ({"op": "seen"}), asked
// about now and then while any wait.

// The ways a lost probe went: on the way to the server, on the way back, held back by the
// server at a traffic limit (lost neither way), or not known: sent over an earlier session,
// over TCP, to a server that keeps no record, or not sent at all, as to a server gone quiet.
const (
	wayUp      = "up"
	wayDown    = "down"
	wayHeld    = "held"
	wayUnknown = "unknown"
)

// inAnyRun is whether seq is in one of runs, [first, last] in order.
func inAnyRun(runs [][2]int, seq int) bool {
	lo, hi := 0, len(runs)-1
	for lo <= hi {
		mid := (lo + hi) / 2
		switch r := runs[mid]; {
		case seq < r[0]:
			hi = mid - 1
		case seq > r[1]:
			lo = mid + 1
		default:
			return true
		}
	}
	return false
}

type waiting struct {
	seq     uint32
	session int
	at      float64 // wall ms when it was found lost
	done    func(way string)
}

// lostWays is one server's lost probes, waiting for the server's record to place them.
// Called with m.mu held.
type lostWays struct {
	session int // the UDP session probes go out on now: a new one starts over
	waiting []waiting
	asking  bool
}

// lost is a probe found lost, sent over session (-1: not sent); can: the session's server
// keeps a record. done hears which way it went, once known.
func (w *lostWays) lost(seq uint32, session int, can bool, done func(way string)) {
	if session < 0 || session != w.session || !can {
		done(wayUnknown)
		return
	}
	w.waiting = append(w.waiting, waiting{seq, session, wall(), done})
}

// newSession starts a new session: the probes of the last can't be placed any more.
func (w *lostWays) newSession() {
	w.session++
	for _, x := range w.waiting {
		x.done(wayUnknown)
	}
	w.waiting = nil
}

// begin starts a question, from the oldest waiting: false while one is under way, or
// none waits.
func (w *lostWays) begin() (from int, session int, at float64, ok bool) {
	if w.asking || len(w.waiting) == 0 {
		return 0, 0, 0, false
	}
	from = int(w.waiting[0].seq)
	for _, x := range w.waiting {
		from = min(from, int(x.seq))
	}
	w.asking = true
	return from, w.session, wall(), true
}

// take is the server's answer (nil: none) to the question begun at at over session. A
// probe after the last that arrived never did, if it was already lost when asked: it went
// on the way there.
func (w *lostWays) take(a *quality.Seen, at float64, session int) {
	w.asking = false
	if a == nil || session != w.session {
		return
	}
	kept := w.waiting[:0]
	for _, x := range w.waiting {
		way := ""
		seq := int(x.seq)
		switch {
		case x.session != w.session || seq < a.From:
			way = wayUnknown
		case seq <= a.To && inAnyRun(a.Held, seq):
			way = wayHeld
		case seq <= a.To && inAnyRun(a.Missing, seq):
			way = wayUp
		case seq <= a.To:
			way = wayDown
		case !a.More && x.at <= at:
			way = wayUp
		}
		if way == "" {
			kept = append(kept, x)
		} else {
			x.done(way)
		}
	}
	w.waiting = kept
}
