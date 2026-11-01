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
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/quality"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/update"
)

// version is set at build time (make t2s) from the server's src/texas2step/__init__.py.
var version = "dev"

// command is one of t2s's commands. Its options are declared apart from what runs it, so
// --help, tab completion (t2s __complete) and the man page all read the same ones.
type command struct {
	name    string
	words   string // after the name in t2s's usage: "URL"
	summary string // its line in t2s's usage
	args    string // after the name in its --help: "URL [options]"
	about   string // what its --help says first
	want    int    // arguments it needs
	most    int    // arguments it takes
	linux   bool   // only on Linux
	// options declares the command's options on o, and gives what runs it with its arguments.
	options func(o *options) func(ctx context.Context, found []string) error
}

// commands are t2s's commands, set in init: install's man page reads them.
var commands []command

func init() {
	commands = []command{
		{name: "run", words: "URL", summary: "full test: latency, download, upload, packet loss",
			args: "URL [options]", about: "A full test: latency, download, upload and packet loss, as the Test page runs it.",
			want: 1, most: 1, options: cmdRun},
		{name: "latency", words: "URL", summary: "idle HTTP round-trip time and jitter",
			args: "URL [options]", about: "Idle HTTP round-trip time and jitter.",
			want: 1, most: 1, options: cmdLatency},
		{name: "download", words: "URL", summary: "download throughput (multi-stream)",
			args: "URL [options]", about: "Download throughput over parallel streams, with latency under load.",
			want: 1, most: 1, options: cmdTransfer("download")},
		{name: "upload", words: "URL", summary: "upload throughput (multi-stream)",
			args: "URL [options]", about: "Upload throughput over parallel streams, with latency under load.",
			want: 1, most: 1, options: cmdTransfer("upload")},
		{name: "loss", words: "URL", summary: "UDP packet loss, RTT and jitter over WebRTC",
			args: "URL [options]", about: "UDP packet loss, round-trip time and jitter, over a WebRTC data channel.",
			want: 1, most: 1, options: cmdLoss},
		{name: "monitor", words: "URL [URL2]", summary: "continuous UDP probing, to one server or two at once",
			args: "URL [URL2] [options]", about: "Continuous UDP probing, as the Monitor page does it: to one server, or to two at once at the same moments.\n" +
				"On a terminal it shows a panel that redraws in place; piped, or with --lines, a line a second. Ctrl-C stops it.",
			want: 1, most: 2, options: cmdMonitor},
		{name: "diag", words: "URL", summary: "every way of reaching the server, tried once a second",
			args: "URL [options]", about: "Every way of reaching the server, tried once a second, as the Diag page does it: HTTP/1.1, HTTP/2,\n" +
				"HTTP/3, WebSocket and UDP, over IPv4 and over IPv6. On a terminal the table redraws in place;\n" +
				"piped, or with --lines, the events come as lines and the table at the end. Ctrl-C stops it.",
			want: 1, most: 1, options: cmdDiag},
		{name: "update", summary: "install the newest t2s from its GitHub releases",
			args: "[VERSION] [options]", about: "Install the newest t2s, or VERSION, from texas2step's GitHub releases, over this one.\n" +
				"It is checked against the SHA-256 the release gives, and run once before it takes this one's place.\n" +
				"t2s looks for updates only when asked: run this by hand, or with --yes from cron or Task Scheduler.",
			want: 0, most: 1, options: cmdUpdate},
		{name: "install", summary: "write t2s's bash completion and man page",
			args: "[options]", about: "Write t2s's bash completion and man page under " + shareDir + ", where bash and man look, for a t2s\n" +
				"installed by hand in /usr/local/bin or /usr/bin. Run it with sudo. A t2s from a package has them already.",
			want: 0, most: 0, linux: true, options: cmdInstall},
	}
}

// here is whether c is a command on this computer.
func (c *command) here() bool { return !c.linux || runtime.GOOS == "linux" }

// find is the command named name, if it is one here.
func find(name string) *command {
	for i := range commands {
		if commands[i].name == name && commands[i].here() {
			return &commands[i]
		}
	}
	return nil
}

// usage is what t2s says with no command, or with --help.
func usage() string {
	var b strings.Builder
	b.WriteString("t2s: texas2step's command-line client.\n\n")
	line := func(words, summary string) {
		fmt.Fprintf(&b, "  t2s %-*s%s\n", max(17, len(words)+2), words, summary)
	}
	for _, c := range commands {
		if c.here() {
			line(strings.TrimSpace(c.name+" "+c.words), c.summary)
		}
	}
	line("version", "print the version")
	b.WriteString("\nURL is a texas2step server's address, such as https://dallas.example.net/texas2step.\n" +
		"Add --help after a command for its options.")
	return b.String()
}

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
		fmt.Fprintln(os.Stderr, usage())
		return 2
	}
	switch args[0] {
	case "-h", "--help", "help":
		fmt.Println(usage())
		return 0
	case "-V", "--version", "version":
		fmt.Println("t2s " + version)
		return 0
	case "__complete": // for the bash completion: the shell's word breaks, then the line
		if len(args) == 3 {
			for _, w := range complete(args[1], args[2]) {
				fmt.Println(w)
			}
		}
		return 0
	case "__share": // for t2s update and the package's build: write the files t2s install does
		if err := share(args[1:]); err != nil {
			fmt.Fprintln(os.Stderr, "t2s: "+err.Error())
			return 1
		}
		return 0
	}
	c := find(args[0])
	if c == nil {
		fmt.Fprintf(os.Stderr, "t2s: no command %q\n\n%s\n", args[0], usage())
		return 2
	}
	o := newOptions(c.name, c.args, c.about)
	start := c.options(o)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	found, err := o.parse(args[1:], c.want, c.most)
	if err == nil {
		err = start(ctx, found)
	}
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
	about   string
	json    bool
	code    string
	http2   bool
	family  string              // "tcp4" or "tcp6" to hold every connection to one address family
	choices map[string][]string // the values an option takes, where they are few
}

func newOptions(name, args, about string) *options {
	o := &options{FlagSet: flag.NewFlagSet(name, flag.ContinueOnError), about: "usage: t2s " + name + " " + args + "\n\n" + about}
	o.SetOutput(io.Discard)
	return o
}

// oneOf names the values option name takes, for tab completion and the man page.
func (o *options) oneOf(name string, values ...string) {
	if o.choices == nil {
		o.choices = map[string][]string{}
	}
	o.choices[name] = values
}

// target adds the options every command toward a server takes.
func (o *options) target(transfers bool) {
	o.BoolVar(&o.json, "json", false, "print the raw result as JSON")
	o.StringVar(&o.code, "code", os.Getenv("TEXAS2STEP_CODE"), "the access code, for a server that asks for one (default: $TEXAS2STEP_CODE)")
	if transfers {
		o.BoolVar(&o.http2, "http2", false, "run the streams inside one HTTP/2 connection, as a browser does (default: HTTP/1.1, a connection per stream)")
	}
}

// flagWords are an option as --help shows it: "--seconds", "FLOAT" and what it does.
func flagWords(fl *flag.Flag) (left, arg, use string) {
	name, use := flag.UnquoteUsage(fl)
	left = "--" + fl.Name
	if len(fl.Name) == 1 {
		left = "-" + fl.Name // -4, -6, -k
	}
	if fl.DefValue != "" && fl.DefValue != "false" && fl.DefValue != "0" && !strings.Contains(use, "default") {
		use += " (default: " + fl.DefValue + ")"
	}
	return left, strings.ToUpper(name), use
}

func (o *options) help() string {
	var b strings.Builder
	b.WriteString(o.about + "\n\noptions:\n")
	o.VisitAll(func(fl *flag.Flag) {
		left, arg, use := flagWords(fl)
		left = "  " + left
		if arg != "" {
			left += " " + arg
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

func cmdRun(o *options) func(context.Context, []string) error {
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
	return func(ctx context.Context, found []string) error {
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
}

func cmdLatency(o *options) func(context.Context, []string) error {
	o.target(true)
	count := o.Int("count", 20, "pings")
	return func(ctx context.Context, found []string) error {
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
}

func cmdTransfer(direction string) func(*options) func(context.Context, []string) error {
	return func(o *options) func(context.Context, []string) error {
		o.target(true)
		seconds := o.Float64("seconds", 10, "seconds the transfer runs")
		streams := o.Int("streams", 4, "parallel streams")
		return func(ctx context.Context, found []string) error {
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
}

func cmdLoss(o *options) func(context.Context, []string) error {
	o.target(false)
	packets := o.Int("packets", 500, "probes")
	interval := o.Float64("interval", 10, "ms between probes")
	size := o.Int("size", engine.ProbeBytes, "probe size in bytes")
	return func(ctx context.Context, found []string) error {
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
}
