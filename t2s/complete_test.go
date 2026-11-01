package main

import (
	"flag"
	"runtime"
	"slices"
	"strings"
	"testing"
)

// bash's COMP_WORDBREAKS, as it is by default.
const breaks = " \t\n\"'><=;|&(:"

func TestCompletionOffersCommandsThenOptions(t *testing.T) {
	commandNames := []string{"run", "latency", "download", "upload", "loss", "monitor", "diag", "update"}
	if runtime.GOOS == "linux" {
		commandNames = append(commandNames, "install")
	}
	commandNames = append(commandNames, "version", "help")
	runOptions := []string{"--code", "--http2", "--interval", "--json", "--no-local", "--no-loss",
		"--no-one-stream", "--one-stream", "--packets", "--save", "--seconds", "--streams"}
	for _, c := range []struct {
		line string
		want []string
	}{
		{"t2s ", commandNames},
		{"t2s mo", []string{"monitor"}},
		{"t2s -", []string{"--help", "--version"}},
		{"t2s bogus ", nil},
		{"t2s run ", nil}, // a server's address goes here: nothing to offer
		{"t2s run https://dallas.example.net/texas2step ", runOptions},
		{"t2s run --se", []string{"--seconds"}},
		{"t2s run --seconds 30 ", nil}, // 30 is --seconds' value, so the address is still to come
		{"t2s run --seconds 30 https://dallas.example.net/texas2step --j", []string{"--json"}},
		{"t2s run https://dallas.example.net/texas2step --json --j", nil}, // given already
		{"t2s run https://dallas.example.net/texas2step --code ", nil},    // its value: anything
		{"t2s monitor https://dallas.example.net/texas2step -", []string{"--code", "--every", "--json", "--lines",
			"--profile", "--rate", "--seconds", "--size", "--video", "--voice-size", "--window", "-4", "-6"}},
		{"t2s monitor --profile ", []string{"path", "voice", "video"}},
		{"t2s monitor --profile v", []string{"voice", "video"}},
		{"t2s monitor --profile=v", []string{"voice", "video"}}, // bash puts them after the =
		{"t2s monitor -profile=pa", []string{"path"}},
		{"t2s update ", []string{"--check", "--insecure", "--proxy", "--url", "--yes", "-k"}},
		{"t2s update --pr", []string{"--proxy"}},
		{"t2s run 'https://dallas", nil}, // quoted: left to the shell
		{"t2s", nil},
	} {
		got := complete(breaks, c.line)
		w := slices.Clone(c.want)
		slices.Sort(w)
		g := slices.Clone(got)
		slices.Sort(g)
		if !slices.Equal(g, w) {
			t.Errorf("%q: got %q, want %q", c.line, got, c.want)
		}
	}
}

func TestCompletionWithoutEqualsAsAWordBreak(t *testing.T) {
	// With = taken out of COMP_WORDBREAKS, bash replaces the whole word.
	if got := complete(" \t\n", "t2s monitor --profile=vi"); !slices.Equal(got, []string{"--profile=video"}) {
		t.Errorf("got %q", got)
	}
}

func TestWordsSplitsAsTheShellDoes(t *testing.T) {
	done, cur, plain := words(`t2s run "a b" c\ d --js`)
	if !slices.Equal(done, []string{"t2s", "run", "a b", "c d"}) || cur != "--js" || !plain {
		t.Errorf("got %q %q %v", done, cur, plain)
	}
	if _, cur, plain := words(`t2s run "dal`); cur != "dal" || plain {
		t.Errorf("an open quote: got %q %v", cur, plain)
	}
	if done, cur, _ := words("t2s run  "); len(done) != 2 || cur != "" {
		t.Errorf("after blanks: got %q %q", done, cur)
	}
}

func TestTheBashCompletionAsksT2sItself(t *testing.T) {
	for _, want := range []string{`"${COMP_WORDS[0]}" __complete "$COMP_WORDBREAKS" "${COMP_LINE:0:COMP_POINT}"`, "complete -F _t2s t2s"} {
		if !strings.Contains(bashCompletion, want) {
			t.Errorf("the completion lacks %s", want)
		}
	}
}

func TestTheManPageHasEveryCommandAndOption(t *testing.T) {
	page := manPage()
	for _, c := range commands {
		if !c.here() {
			continue
		}
		if !strings.Contains(page, `.SS "t2s `+c.name+" ") {
			t.Errorf("no section for %s", c.name)
		}
		o := newOptions(c.name, c.args, c.about)
		c.options(o)
		o.VisitAll(func(fl *flag.Flag) {
			if left, _, _ := flagWords(fl); !strings.Contains(page, `\fB`+roff(left)+`\fR`) {
				t.Errorf("%s: no %s", c.name, left)
			}
		})
	}
	for _, want := range []string{`\fB\-\-seconds\fR \fIFLOAT\fR`, `\fB\-4\fR`, "path, voice or video:",
		".SH EXIT STATUS", ".B TEXAS2STEP_CODE", `t2s \- texas2step's command\-line client`} {
		if !strings.Contains(page, want) {
			t.Errorf("the man page lacks %s", want)
		}
	}
	requests := []string{".TH", ".nh", ".ad", ".SH", ".SS", ".TP", ".PP", ".B", ".I"}
	for i, l := range strings.Split(page, "\n") {
		request, _, _ := strings.Cut(l, " ")
		if strings.HasPrefix(l, "'") || (strings.HasPrefix(l, ".") && !slices.Contains(requests, request)) {
			t.Errorf("line %d would be read as a request: %q", i+1, l)
		}
	}
}

func TestOwnerOfReadsDpkgQuery(t *testing.T) {
	for _, c := range [][3]string{
		{"t2s: /usr/bin/t2s\n", "/usr/bin/t2s", "t2s"},
		{"texas2step: /opt/texas2step/t2s/t2s-linux-amd64\n", "/opt/texas2step/t2s/t2s-linux-amd64", "texas2step"},
		{"t2s:amd64: /usr/bin/t2s\n", "/usr/bin/t2s", "t2s"},
		{"diversion by local from: /usr/bin/t2s\ndiversion by local to: /usr/bin/t2s.real\nt2s: /usr/bin/t2s\n", "/usr/bin/t2s", "t2s"},
		{"texas2step, t2s: /usr/bin/t2s\n", "/usr/bin/t2s", "texas2step"},
		{"t2s: /usr/bin/t2s-other\n", "/usr/bin/t2s", ""},
		{"", "/usr/bin/t2s", ""},
	} {
		if got := ownerOf(c[0], c[1]); got != c[2] {
			t.Errorf("%q: got %q, want %q", c[0], got, c[2])
		}
	}
}

func TestInstallIsForTheFoldersMadeForIt(t *testing.T) {
	for exe, want := range map[string]bool{"/usr/local/bin/t2s": true, "/usr/bin/t2s": true,
		"/home/austin/Downloads/t2s": false, "/usr/local/bin/sub/t2s": false, "/opt/t2s/t2s": false} {
		if got := inByHand(exe); got != want {
			t.Errorf("%s: got %v", exe, got)
		}
	}
}

func TestTheUsageNamesInstallOnLinuxOnly(t *testing.T) {
	has := strings.Contains(usage(), "t2s install ")
	if has != (runtime.GOOS == "linux") {
		t.Errorf("install in the usage: %v, on %s", has, runtime.GOOS)
	}
}
