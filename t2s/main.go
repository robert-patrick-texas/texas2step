// t2s is texas2step's command-line client: one executable that tests the network between
// this computer and a texas2step server, as the server's pages do (docs/t2s.md).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/update"
)

// version is set at build time (make t2s) from the server's src/texas2step/__init__.py.
var version = "dev"

const usage = `t2s: texas2step's command-line client.

  t2s run URL          full test: latency, download, upload, packet loss
  t2s latency URL      idle HTTP round-trip time and jitter
  t2s download URL     download throughput (multi-stream)
  t2s upload URL       upload throughput (multi-stream)
  t2s loss URL         UDP packet loss, RTT and jitter over WebRTC
  t2s monitor URL [URL2]  continuous UDP probing, to one server or two at once
  t2s diag URL         every way of reaching the server, tried once a second
  t2s update           install the newest t2s from its GitHub releases
  t2s version          print the version

URL is a texas2step server's address, such as https://dallas.example.net/texas2step.
Add --help after a command for its options.`

// failure is an error already said, with the exit status to end with.
type failure struct{ status int }

func (failure) Error() string { return "" }

func main() {
	engine.Version = version
	update.Tidy()
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, usage)
		return 2
	}
	commands := map[string]func(context.Context, []string) error{
		"run": cmdRun, "latency": cmdLatency, "download": cmdTransfer("download"),
		"upload": cmdTransfer("upload"), "loss": cmdLoss, "monitor": cmdMonitor,
		"diag": cmdDiag, "update": cmdUpdate,
	}
	switch args[0] {
	case "-h", "--help", "help":
		fmt.Println(usage)
		return 0
	case "-V", "--version", "version":
		fmt.Println("t2s " + version)
		return 0
	}
	cmd, ok := commands[args[0]]
	if !ok {
		fmt.Fprintf(os.Stderr, "t2s: no command %q\n\n%s\n", args[0], usage)
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err := cmd(ctx, args[1:])
	var done failure
	switch {
	case err == nil:
		return 0
	case errors.Is(err, flag.ErrHelp):
		return 0
	case errors.As(err, &done):
		return done.status
	case ctx.Err() != nil:
		return 130
	}
	fmt.Fprintln(os.Stderr, "t2s: "+explain(err))
	return 1
}

// explain says what went wrong without the request's URL, which carries the pass.
func explain(err error) string {
	var ue *url.Error
	if errors.As(err, &ue) {
		host := ue.URL
		if u, perr := url.Parse(ue.URL); perr == nil {
			host = u.Scheme + "://" + u.Host
		}
		inner := ue.Err
		if errors.Is(inner, context.DeadlineExceeded) || ue.Timeout() {
			return "no answer from " + host + " in time"
		}
		return "can't reach " + host + ": " + inner.Error()
	}
	return err.Error()
}

// options are a command's flags; parse takes them before and after its arguments.
type options struct {
	*flag.FlagSet
	about  string
	json   bool
	code   string
	http2  bool
	family string // "tcp4" or "tcp6" to hold every connection to one address family
}

func newOptions(name, args, about string) *options {
	o := &options{FlagSet: flag.NewFlagSet(name, flag.ContinueOnError), about: "usage: t2s " + name + " " + args + "\n\n" + about}
	o.SetOutput(io.Discard)
	return o
}

// target adds the options every command toward a server takes.
func (o *options) target(transfers bool) {
	o.BoolVar(&o.json, "json", false, "print the raw result as JSON")
	o.StringVar(&o.code, "code", os.Getenv("TEXAS2STEP_CODE"), "the access code, for a server that asks for one (default: $TEXAS2STEP_CODE)")
	if transfers {
		o.BoolVar(&o.http2, "http2", false, "run the streams inside one HTTP/2 connection, as a browser does (default: HTTP/1.1, a connection per stream)")
	}
}

func (o *options) help() string {
	var b strings.Builder
	b.WriteString(o.about + "\n\noptions:\n")
	o.VisitAll(func(fl *flag.Flag) {
		name, use := flag.UnquoteUsage(fl)
		left := "  --" + fl.Name
		if len(fl.Name) == 1 {
			left = "  -" + fl.Name // -4, -6, -k
		}
		if name != "" {
			left += " " + strings.ToUpper(name)
		}
		if fl.DefValue != "" && fl.DefValue != "false" && fl.DefValue != "0" && !strings.Contains(use, "default") {
			use += " (default: " + fl.DefValue + ")"
		}
		fmt.Fprintf(&b, "%-26s %s\n", left, use)
	})
	return b.String()
}

// parse reads the flags wherever they come, and returns the arguments in between.
func (o *options) parse(args []string, want int, most int) ([]string, error) {
	var found []string
	for {
		if err := o.Parse(args); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				fmt.Print(o.help())
				return nil, err
			}
			fmt.Fprintf(os.Stderr, "t2s %s: %v\n\n%s", o.Name(), err, o.help())
			return nil, failure{2}
		}
		args = o.Args()
		if len(args) == 0 {
			break
		}
		found = append(found, strings.TrimSpace(args[0]))
		args = args[1:]
	}
	if len(found) < want || len(found) > most {
		fmt.Fprintf(os.Stderr, "%s", o.help())
		return nil, failure{2}
	}
	return found, nil
}

func (o *options) set(name string) bool {
	found := false
	o.Visit(func(fl *flag.Flag) { found = found || fl.Name == name })
	return found
}

// unlock makes a target for url, with the pass --code buys where the server asks for one.
func (o *options) unlock(ctx context.Context, url string) (*engine.Target, error) {
	t := engine.NewTarget(url)
	t.HTTP2, t.Family = o.http2, o.family
	pass, err := t.Unlock(ctx, strings.TrimSpace(o.code))
	if err != nil {
		return nil, fmt.Errorf("%s %w", t.Base, err)
	}
	t.Pass = pass
	return t, nil
}

func printJSON(v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(data))
	return nil
}

func cmdRun(ctx context.Context, args []string) error {
	o := newOptions("run", "URL [options]", "A full test: latency, download, upload and packet loss, as the Test page runs it.")
	o.target(true)
	seconds := o.Float64("seconds", 10, "seconds each transfer runs")
	streams := o.Int("streams", 4, "parallel streams")
	packets := o.Int("packets", 500, "UDP probes in the packet-loss test")
	interval := o.Float64("interval", 10, "ms between UDP probes")
	noLoss := o.Bool("no-loss", false, "skip the UDP packet-loss test")
	one := o.Bool("one-stream", false, "after the download, download over one stream too (default: as the server says)")
	noOne := o.Bool("no-one-stream", false, "don't download over one stream")
	save := o.Bool("save", false, "store the result on the server, for History")
	noLocal := o.Bool("no-local", false, "leave this computer's own address and MAC out of the result")
	found, err := o.parse(args, 1, 1)
	if err != nil {
		return err
	}
	t, err := o.unlock(ctx, found[0])
	if err != nil {
		return err
	}
	opts := engine.Options{Seconds: *seconds, Streams: max(1, *streams), Packets: *packets, IntervalMs: *interval, Loss: !*noLoss, Local: !*noLocal}
	if o.set("one-stream") || o.set("no-one-stream") {
		v := *one && !*noOne
		opts.OneStream = &v
	}
	l := newLive(!o.json)
	r, err := engine.RunAll(ctx, t, opts, l.progress)
	l.clear()
	if err != nil {
		return err
	}
	if *save {
		if r.ID, err = engine.Save(ctx, t, r); err != nil {
			return err
		}
	}
	if o.json {
		return printJSON(r)
	}
	fmt.Println(report(r))
	return nil
}

func cmdLatency(ctx context.Context, args []string) error {
	o := newOptions("latency", "URL [options]", "Idle HTTP round-trip time and jitter.")
	o.target(true)
	count := o.Int("count", 20, "pings")
	found, err := o.parse(args, 1, 1)
	if err != nil {
		return err
	}
	t, err := o.unlock(ctx, found[0])
	if err != nil {
		return err
	}
	l := newLive(!o.json)
	r, err := engine.MeasureLatency(ctx, t, *count, l.progress)
	l.clear()
	if err != nil {
		return err
	}
	if o.json {
		return printJSON(r)
	}
	fmt.Println(latencyLine("Latency", &r.Summary))
	return nil
}

func cmdTransfer(direction string) func(context.Context, []string) error {
	return func(ctx context.Context, args []string) error {
		o := newOptions(direction, "URL [options]", strings.ToUpper(direction[:1])+direction[1:]+" throughput over parallel streams, with latency under load.")
		o.target(true)
		seconds := o.Float64("seconds", 10, "seconds the transfer runs")
		streams := o.Int("streams", 4, "parallel streams")
		found, err := o.parse(args, 1, 1)
		if err != nil {
			return err
		}
		t, err := o.unlock(ctx, found[0])
		if err != nil {
			return err
		}
		l := newLive(!o.json)
		r, err := engine.MeasureThroughput(ctx, t, direction, *seconds, max(1, *streams), true, l.progress, "")
		l.clear()
		if err != nil {
			return err
		}
		if o.json {
			return printJSON(r)
		}
		fmt.Println(throughputLine(strings.ToUpper(direction[:1])+direction[1:], r, nil))
		return nil
	}
}

func cmdLoss(ctx context.Context, args []string) error {
	o := newOptions("loss", "URL [options]", "UDP packet loss, round-trip time and jitter, over a WebRTC data channel.")
	o.target(false)
	packets := o.Int("packets", 500, "probes")
	interval := o.Float64("interval", 10, "ms between probes")
	size := o.Int("size", engine.ProbeBytes, "probe size in bytes")
	found, err := o.parse(args, 1, 1)
	if err != nil {
		return err
	}
	t, err := o.unlock(ctx, found[0])
	if err != nil {
		return err
	}
	info, err := t.GetInfo(ctx)
	if err != nil {
		return err
	}
	l := newLive(!o.json)
	r, err := engine.MeasureLoss(ctx, t, info, *packets, *interval, *size, 2*time.Second, l.progress)
	l.clear()
	if err != nil {
		return err
	}
	if o.json {
		return printJSON(r)
	}
	fmt.Println(lossLine(r) + "\n" + callLine(quality.CallQuality(r.Probes())))
	return nil
}
