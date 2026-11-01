package main

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/monitor"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/show"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/term"
)

func cmdMonitor(o *options) func(context.Context, []string) error {
	o.target(false)
	profile := o.String("profile", "", "path, voice or video: small probes, or a voice or video call's packets scored each 10s (default: as the server says)")
	o.oneOf("profile", "path", "voice", "video")
	rate := o.Float64("rate", 10, "path probes a second")
	size := o.Int("size", 64, "bytes in a path probe")
	voice := o.Int("voice-size", 200, "bytes in a voice packet")
	every := o.Float64("every", 20, "ms between voice packets")
	video := o.Float64("video", 1000, "the video call's kb/s")
	window := o.Float64("window", 60, "seconds the figures cover, up to 900")
	seconds := o.Float64("seconds", 0, "stop after this many seconds (default: until Ctrl-C, or a server's limit)")
	asLines := o.Bool("lines", false, "a line a second on the terminal too, as piped, instead of the panel")
	only4 := o.Bool("4", false, "IPv4 only: every server over IPv4, so two compare on one family")
	only6 := o.Bool("6", false, "IPv6 only: every server over IPv6")
	return func(ctx context.Context, found []string) error {
		fam := ""
		switch {
		case *only4 && !*only6:
			o.family, fam = "tcp4", "IPv4"
		case *only6 && !*only4:
			o.family, fam = "tcp6", "IPv6"
		}
		for _, url := range found {
			if fam != "" && !engine.HasAddress(ctx, engine.Base(url), fam) {
				return fmt.Errorf("%s has no %s address", engine.Base(url), fam)
			}
		}
		p := strings.ToLower(strings.TrimSpace(*profile))
		if p != "" && p != "path" && p != "voice" && p != "video" {
			return fmt.Errorf("--profile %q: path, voice or video", *profile)
		}
		if *rate <= 0 || *every <= 0 || *size < 12 || *voice < 12 || *video <= 0 {
			return fmt.Errorf("--rate, --every and --video must be above 0, --size and --voice-size at least 12")
		}
		var targets []*engine.Target
		for _, url := range found {
			t, err := o.unlock(ctx, url)
			if err != nil {
				return err
			}
			targets = append(targets, t)
		}
		cfg := monitor.Config{Profile: p, Chosen: p != "", Rate: *rate, Size: *size, VoiceSize: *voice, Every: *every, Video: *video,
			Window: min(max(*window, 10), 900), Seconds: *seconds}

		// Asked first: on Windows it also readies the console for UTF-8, which lines need too.
		onTerminal := term.Is(os.Stdout)
		panel := !o.json && !*asLines && onTerminal
		show.Colour = show.Colour && panel
		var mu sync.Mutex // one writer at a time
		lines := &monitor.Lines{W: os.Stdout}
		js := monitor.JSON{W: os.Stdout}
		var ends *monitor.Event // why it stopped: printed after the last figures
		notice := func(e monitor.Event) {
			mu.Lock()
			defer mu.Unlock()
			switch {
			case e.Ends && !panel:
				ends = &e
			case o.json:
				js.Event(e)
			case !panel:
				lines.Event(e)
			}
		}
		m := monitor.New(cfg, targets, notice)
		if !o.json && !panel {
			fmt.Fprintln(os.Stderr, "t2s monitor: connecting…")
		}
		if err := m.Start(ctx); err != nil {
			return err
		}
		if panel {
			fmt.Print(term.Alternate)
		} else if !o.json {
			s := m.Snapshot()
			var heads []string
			for _, v := range s.Dests {
				heads = append(heads, strings.TrimSpace(v.Short+" "+v.Name)+" · "+v.Transport)
			}
			fmt.Println("t2s monitor → " + strings.Join(heads, "  ·  "))
			fmt.Println(monitor.About(s.Config, len(s.Dests) > 1) + " · Ctrl-C stops")
			fmt.Println()
		}
		draw := func(s monitor.Snapshot) {
			frame := monitor.Panel(s, term.Width(os.Stdout), max(3, term.Height(os.Stdout)-18))
			fmt.Print(term.Home + strings.Join(frame, term.EndLine+"\n") + term.EndLine + "\n" + term.ClearDown)
		}
		every2 := time.Second
		if panel {
			every2 = 500 * time.Millisecond
		}
		ticker := time.NewTicker(every2)
		defer ticker.Stop()
	loop:
		for {
			select {
			case <-ctx.Done():
				break loop
			case <-m.Done():
				break loop
			case <-ticker.C:
				// Piped, a line a second until the last probe is out; then its figures print
				// once, as Monitor stops, with the call slices scored meanwhile (a snapshot
				// takes its new slices, so none is taken here).
				var s monitor.Snapshot
				if panel {
					s = m.Snapshot()
				} else if live, ok := m.Live(); ok {
					s = live
				} else {
					continue
				}
				mu.Lock()
				switch {
				case panel:
					draw(s)
				case o.json:
					js.Tick(s)
				default:
					lines.Tick(s)
				}
				mu.Unlock()
			}
		}
		m.Stop()
		s := m.Snapshot()
		started, stopped := time.UnixMicro(int64(s.Started*1000)), time.UnixMicro(int64(s.Now*1000))
		totals := m.Totals()
		why := m.Ended()
		mu.Lock()
		held := ends
		mu.Unlock()
		if panel {
			fmt.Print(term.Normal)
			// The last picture stays on the terminal, held where it stopped.
			fmt.Println(strings.Join(monitor.Panel(s, term.Width(os.Stdout), 5), "\n"))
			fmt.Println()
		} else if !o.json {
			lines.Tick(s)
			if held != nil {
				lines.Event(*held)
			}
			fmt.Println()
		}
		if o.json {
			js.Tick(s)
			if held != nil {
				js.Event(*held)
			}
			js.Totals(started, stopped, why, totals)
		} else {
			fmt.Println(strings.Join(monitor.Summary(started, stopped, why, totals), "\n"))
		}
		return nil
	}
}
