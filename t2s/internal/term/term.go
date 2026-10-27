// Package term knows the terminal t2s writes to: whether there is one, how wide it is,
// and how to redraw a panel in place.
package term

import (
	"os"

	"golang.org/x/term"
)

// Is is whether f is a terminal; on Windows it also turns on the console's ANSI escapes
// and UTF-8 output, which a panel needs.
func Is(f *os.File) bool {
	if !term.IsTerminal(int(f.Fd())) {
		return false
	}
	return enable(f)
}

// Width is the terminal's width in columns, 80 if it can't be told.
func Width(f *os.File) int {
	if w, _, err := term.GetSize(int(f.Fd())); err == nil && w > 0 {
		return w
	}
	return 80
}

// Height is the terminal's height in rows, 24 if it can't be told.
func Height(f *os.File) int {
	if _, h, err := term.GetSize(int(f.Fd())); err == nil && h > 0 {
		return h
	}
	return 24
}

const (
	Home      = "\x1b[H"    // the cursor to the top left
	ClearDown = "\x1b[J"    // clear from the cursor to the end of the screen
	ClearLine = "\r\x1b[2K" // clear the line the cursor is on
	EndLine   = "\x1b[K"    // clear the rest of the line
	Alternate = "\x1b[?1049h\x1b[?25l"
	Normal    = "\x1b[?25h\x1b[?1049l"
)
