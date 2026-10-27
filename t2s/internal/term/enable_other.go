//go:build !windows

package term

import "os"

func enable(*os.File) bool { return true }
