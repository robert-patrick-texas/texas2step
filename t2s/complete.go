package main

import (
	"flag"
	"strings"
)

// bashCompletion is t2s's bash completion. t2s itself says what can come next (t2s
// __complete), so this stays the same as commands and options come and go.
const bashCompletion = `# bash completion for t2s, texas2step's command-line client.
# t2s itself says what can come next (t2s __complete), so this file never needs changing.
_t2s() {
    COMPREPLY=()
    mapfile -t COMPREPLY < <("${COMP_WORDS[0]}" __complete "$COMP_WORDBREAKS" "${COMP_LINE:0:COMP_POINT}" 2>/dev/null)
} &&
    complete -F _t2s t2s
`

// complete is what t2s __complete prints: the words that can go where the cursor is, for
// the shell's tab completion. line is the command line up to the cursor. breaks are the
// characters, besides blanks, the shell splits words at (bash's COMP_WORDBREAKS, with = and
// :): it puts a completion in place of the part of the word after the last of them, so
// that is the part given.
func complete(breaks, line string) []string {
	done, cur, plain := words(line)
	if len(done) == 0 || !plain {
		return nil // nothing typed yet, or the word is quoted
	}
	var all []string
	if len(done) == 1 { // the command
		if strings.HasPrefix(cur, "-") {
			all = []string{"--help", "--version"}
		} else {
			for _, c := range commands {
				if c.here() {
					all = append(all, c.name)
				}
			}
			all = append(all, "version", "help")
		}
	} else {
		all = completeOptions(done[1:], cur)
	}
	cut := strings.LastIndexAny(cur, strings.Map(func(r rune) rune {
		if r == ' ' || r == '\t' || r == '\n' {
			return -1
		}
		return r
	}, breaks)) + 1
	var out []string
	for _, w := range all {
		if strings.HasPrefix(w, cur) {
			out = append(out, w[cut:])
		}
	}
	return out
}

// completeOptions are the words that can follow a command's words given so far: an
// option, or an option's value where it takes one of a few.
func completeOptions(given []string, cur string) []string {
	c := find(given[0])
	if c == nil {
		return nil
	}
	o := newOptions(c.name, c.args, c.about)
	c.options(o)
	used := map[string]bool{}
	args := 0
	for i := 1; i < len(given); i++ {
		name, value, isOption := optionWord(given[i])
		if !isOption {
			args++
			continue
		}
		fl := o.Lookup(name)
		if fl == nil {
			continue
		}
		used[name] = true
		if value || isBool(fl) {
			continue
		}
		if i == len(given)-1 { // the cursor is on this option's value
			return o.choices[name]
		}
		i++ // its value
	}
	if name, value, ok := strings.Cut(cur, "="); ok && strings.HasPrefix(name, "-") {
		var out []string
		for _, v := range o.choices[strings.TrimLeft(name, "-")] {
			if strings.HasPrefix(v, value) {
				out = append(out, name+"="+v)
			}
		}
		return out
	}
	if !strings.HasPrefix(cur, "-") && args < c.want {
		return nil // a server's address, or the like: nothing to offer
	}
	var out []string
	o.VisitAll(func(fl *flag.Flag) {
		if !used[fl.Name] {
			left, _, _ := flagWords(fl)
			out = append(out, left)
		}
	})
	return out
}

// optionWord reads a word as an option: its name, and whether its value came with it
// (--seconds=30).
func optionWord(w string) (name string, value, ok bool) {
	if len(w) < 2 || w[0] != '-' || w == "--" {
		return "", false, false
	}
	name = strings.TrimPrefix(strings.TrimPrefix(w, "-"), "-")
	name, _, value = strings.Cut(name, "=")
	return name, value, true
}

func isBool(fl *flag.Flag) bool {
	b, ok := fl.Value.(interface{ IsBoolFlag() bool })
	return ok && b.IsBoolFlag()
}

// words splits a command line as the shell does: the words finished, the one the cursor is
// in (empty after a blank), and whether that one is plain, with no quote or backslash.
func words(line string) (done []string, cur string, plain bool) {
	var w strings.Builder
	in, quote, escaped, plain := false, rune(0), false, true
	for _, r := range line {
		switch {
		case escaped:
			w.WriteRune(r)
			escaped = false
		case r == '\\' && quote != '\'':
			escaped, in, plain = true, true, false
		case quote != 0:
			if r == quote {
				quote = 0
			} else {
				w.WriteRune(r)
			}
		case r == '\'' || r == '"':
			quote, in, plain = r, true, false
		case r == ' ' || r == '\t' || r == '\n':
			if in {
				done = append(done, w.String())
				w.Reset()
				in, plain = false, true
			}
		default:
			w.WriteRune(r)
			in = true
		}
	}
	return done, w.String(), plain
}
