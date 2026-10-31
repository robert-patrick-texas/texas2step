//go:build unix

package engine

import "syscall"

// processCPU is the seconds of CPU this process has used, user and system; -1 if the
// system won't say.
func processCPU() float64 {
	var ru syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &ru) != nil {
		return -1
	}
	return seconds(ru.Utime) + seconds(ru.Stime)
}

func seconds(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)/1e6 }
