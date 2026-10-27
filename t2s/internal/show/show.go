// Package show writes figures as texas2step's pages write them (web/src/ui/format.ts), and
// lays out lines for a terminal: colours, widths, and a figure kept with its unit.
package show

import (
	"math"
	"os"
	"strings"
	"unicode/utf8"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/num"
)

// Ms is a time in ms as the pages write it: 0.82 ms, 23.4 ms, 125 ms.
func Ms(v *float64) string {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return "—"
	}
	digits := 2
	switch {
	case *v >= 100:
		digits = 0
	case *v >= 10:
		digits = 1
	}
	return num.Grouped(*v, digits) + " ms"
}

// Pct is a percentage as the pages write it: 0 %, 0.25 %, 1.7 %.
func Pct(v *float64) string {
	if v == nil || math.IsNaN(*v) {
		return "—"
	}
	digits := 1
	switch {
	case *v == 0:
		digits = 0
	case *v < 1:
		digits = 2
	}
	return num.Grouped(*v, digits) + " %"
}

// Count is a whole number with thousands grouped: 1,300.
func Count(n int) string { return num.Grouped(float64(n), 0) }

// Colours, where the terminal shows them and NO_COLOR isn't set.
var Colour = os.Getenv("NO_COLOR") == ""

const (
	reset = "\x1b[0m"
)

func paint(code, s string) string {
	if !Colour || s == "" {
		return s
	}
	return code + s + reset
}

func Bold(s string) string   { return paint("\x1b[1m", s) }
func Dim(s string) string    { return paint("\x1b[2m", s) }
func Red(s string) string    { return paint("\x1b[31m", s) }
func Green(s string) string  { return paint("\x1b[32m", s) }
func Yellow(s string) string { return paint("\x1b[33m", s) }

// Rated is a call rating's word, coloured by how it sounds.
func Rated(rating, s string) string {
	switch rating {
	case "excellent", "good":
		return Green(s)
	case "fair":
		return Yellow(s)
	case "poor", "bad":
		return Red(s)
	}
	return s
}

// Width is how many columns s takes, its colour codes left out.
func Width(s string) int {
	n := 0
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			for i < len(s) && s[i] != 'm' {
				i++
			}
			i++
			continue
		}
		_, size := utf8.DecodeRuneInString(s[i:])
		i += size
		n++
	}
	return n
}

// Fit cuts s to width columns, so a line never wraps on the terminal.
func Fit(s string, width int) string {
	if Width(s) <= width {
		return s
	}
	var b strings.Builder
	n := 0
	for i := 0; i < len(s); {
		if s[i] == 0x1b {
			j := i
			for j < len(s) && s[j] != 'm' {
				j++
			}
			b.WriteString(s[i:min(j+1, len(s))])
			i = j + 1
			continue
		}
		if n >= width-1 {
			b.WriteString("…")
			break
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		b.WriteRune(r)
		i += size
		n++
	}
	if Colour && strings.Contains(s, "\x1b[") {
		b.WriteString(reset)
	}
	return b.String()
}

// Pad is s with spaces after it to width columns.
func Pad(s string, width int) string {
	if w := Width(s); w < width {
		return s + strings.Repeat(" ", width-w)
	}
	return s
}

// Table lays rows out in columns, each as wide as its widest cell and three spaces apart.
func Table(rows [][]string, indent string) []string {
	widths := map[int]int{}
	for _, r := range rows {
		for i, c := range r {
			widths[i] = max(widths[i], Width(c))
		}
	}
	out := make([]string, len(rows))
	for j, r := range rows {
		var b strings.Builder
		b.WriteString(indent)
		for i, c := range r {
			if i == len(r)-1 {
				b.WriteString(c)
			} else {
				b.WriteString(Pad(c, widths[i]+3))
			}
		}
		out[j] = strings.TrimRight(b.String(), " ")
	}
	return out
}
