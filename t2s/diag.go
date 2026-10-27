package main

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/diag"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/show"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/term"
)

func cmdDiag(ctx context.Context, args []string) error {
	o := newOptions("diag", "URL [options]", "Every way of reaching the server, tried once a second, as the Diag page does it: HTTP/1.1, HTTP/2,\n"+
		"HTTP/3, WebSocket and UDP, over IPv4 and over IPv6. On a terminal the table redraws in place;\n"+
		"piped, the events come as lines and the table at the end. Ctrl-C stops it.")
	o.target(false)
	seconds := o.Float64("seconds", 0, "stop after this many seconds (default: until Ctrl-C, or the server's limit)")
	only4 := o.Bool("4", false, "IPv4 only")
	only6 := o.Bool("6", false, "IPv6 only")
	found, err := o.parse(args, 1, 1)
	if err != nil {
		return err
	}
	families := []string{"IPv4", "IPv6"}
	switch {
	case *only4 && !*only6:
		families = []string{"IPv4"}
	case *only6 && !*only4:
		families = []string{"IPv6"}
	}
	t, err := o.unlock(ctx, found[0])
	if err != nil {
		return err
	}
	live := !o.json && term.Is(os.Stdout)
	show.Colour = show.Colour && live
	var mu sync.Mutex
	notice := func(e diag.Event) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case o.json:
			diag.JSONEvent(os.Stdout, e)
		case !live:
			diag.Log(os.Stdout, e)
		}
	}
	d := diag.New(t, strings.TrimSpace(o.code), *seconds, notice)
	if err := d.Start(ctx, families); err != nil {
		return err
	}
	if live {
		fmt.Print(term.Alternate)
	}
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
loop:
	for {
		select {
		case <-ctx.Done():
			break loop
		case <-d.Done():
			break loop
		case <-ticker.C:
			d.Watch()
			if !live {
				continue
			}
			s := d.Snapshot()
			mu.Lock()
			frame := diag.Table(s, term.Width(os.Stdout), term.Height(os.Stdout), -1, true)
			fmt.Print(term.Home + strings.Join(frame, term.EndLine+"\n") + term.EndLine + "\n" + term.ClearDown)
			mu.Unlock()
		}
	}
	d.Stop()
	s := d.Snapshot()
	mu.Lock()
	defer mu.Unlock()
	switch {
	case o.json:
		diag.JSONSnapshot(os.Stdout, "summary", s)
	case live:
		fmt.Print(term.Normal)
		fmt.Println(strings.Join(diag.Table(s, term.Width(os.Stdout), 0, 10, false), "\n"))
	default:
		fmt.Println()
		fmt.Println(strings.Join(diag.Table(s, 1000, 0, 0, false), "\n")) // a log keeps every detail
	}
	return nil
}
