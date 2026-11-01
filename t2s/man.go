package main

import (
	"flag"
	"fmt"
	"strings"
)

// manPage is t2s's man page, in roff, made from the commands and the options they declare,
// so it says what --help says.
func manPage() string {
	var b strings.Builder
	line := func(format string, a ...any) { fmt.Fprintf(&b, format+"\n", a...) }
	line(`.TH T2S 1 "" "t2s %s" "User Commands"`, roff(version))
	line(".nh")   // no word broken across lines: options and addresses stay whole
	line(".ad l") // ragged right, so a long address doesn't spread a line
	line(".SH NAME")
	line(`t2s \- %s`, roff("texas2step's command-line client"))
	line(".SH SYNOPSIS")
	line(`.B t2s`)
	line(`.I command`)
	line(`[\fIarguments\fR] [\fIoptions\fR]`)
	line(".SH DESCRIPTION")
	line("%s", roff("t2s tests the network between this computer and a texas2step server, as the server's pages do: "+
		"the Test page's full test or any part of it, Monitor's continuous probing, and Diag's every way of reaching the server. "+
		"Its results are the ones the server's own command line gives, with the same JSON."))
	line(".PP")
	line("%s", roff("URL is a texas2step server's address, such as https://dallas.example.net/texas2step. "+
		"A command's options can come before or after its arguments."))
	line(".SH COMMANDS")
	for _, c := range commands {
		if !c.here() {
			continue
		}
		o := newOptions(c.name, c.args, c.about)
		c.options(o)
		line(`.SS "t2s %s %s"`, roff(c.name), roff(c.args))
		line("%s", roff(strings.Join(strings.Fields(c.about), " ")))
		o.VisitAll(func(fl *flag.Flag) {
			left, arg, use := flagWords(fl)
			line(".TP")
			if arg != "" {
				line(`\fB%s\fR \fI%s\fR`, roff(left), roff(arg))
			} else {
				line(`\fB%s\fR`, roff(left))
			}
			if values := o.choices[fl.Name]; len(values) > 0 && !strings.Contains(use, values[0]) {
				use += " (one of " + strings.Join(values, ", ") + ")"
			}
			line("%s", roff(use))
		})
	}
	line(`.SS "t2s version"`)
	line("Print the version.")
	line(".SH ENVIRONMENT")
	for _, e := range [][2]string{
		{"TEXAS2STEP_CODE", "The access code, for a server that asks for one, when --code isn't given."},
		{"https_proxy, http_proxy, no_proxy", "The HTTP proxy that requests and WebSockets go through, and the servers they reach directly. " +
			"UDP, HTTP/3, and the new connections that responsiveness times always go direct."},
		{"SSL_CERT_FILE, SSL_CERT_DIR", "Certificates to trust instead of the system's CA bundle."},
		{"NO_COLOR", "When set and not empty, no colour on the terminal."},
	} {
		line(".TP")
		line(`.B %s`, roff(e[0]))
		line("%s", roff(e[1]))
	}
	line(".SH EXIT STATUS")
	for _, s := range [][2]string{
		{"0", "Done; Monitor and Diag stopped with Ctrl-C or at a limit; no newer t2s (update --check)."},
		{"1", "It failed: the reason is on the error output, after \"t2s:\"."},
		{"2", "The command or its options weren't understood."},
		{"100", "update --check found a newer t2s."},
		{"130", "A test was stopped with Ctrl-C."},
	} {
		line(".TP")
		line(`.B %s`, s[0])
		line("%s", roff(s[1]))
	}
	line(".SH SEE ALSO")
	line("%s", roff("t2s's guide: https://github.com/robert-patrick-texas/texas2step/blob/main/docs/t2s.md"))
	return b.String()
}

// roff is text as man shows it as written: backslashes and hyphens escaped, and a line
// that would start with a dot or quote kept from being read as a request.
func roff(s string) string {
	s = strings.NewReplacer(`\`, `\e`, "-", `\-`).Replace(s)
	if strings.HasPrefix(s, ".") || strings.HasPrefix(s, "'") {
		s = `\&` + s
	}
	return s
}
