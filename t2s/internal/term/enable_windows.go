//go:build windows

package term

import (
	"os"

	"golang.org/x/sys/windows"
)

// enable turns on the console's virtual terminal processing (ANSI escapes) and sets its
// output to UTF-8; false where the console can't do escapes (before Windows 10).
func enable(f *os.File) bool {
	h := windows.Handle(f.Fd())
	var mode uint32
	if windows.GetConsoleMode(h, &mode) != nil {
		return false
	}
	if windows.SetConsoleMode(h, mode|windows.ENABLE_VIRTUAL_TERMINAL_PROCESSING) != nil {
		return false
	}
	_ = windows.SetConsoleOutputCP(65001) // UTF-8
	return true
}
