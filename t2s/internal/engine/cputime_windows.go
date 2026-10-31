//go:build windows

package engine

import "syscall"

// processCPU is the seconds of CPU this process has used, user and kernel; -1 if Windows
// won't say.
func processCPU() float64 {
	h, err := syscall.GetCurrentProcess()
	if err != nil {
		return -1
	}
	var created, exited, kernel, user syscall.Filetime
	if syscall.GetProcessTimes(h, &created, &exited, &kernel, &user) != nil {
		return -1
	}
	return ticks(kernel) + ticks(user)
}

// ticks reads a FILETIME span, in 100 ns ticks, as seconds.
func ticks(f syscall.Filetime) float64 {
	return float64(uint64(f.HighDateTime)<<32|uint64(f.LowDateTime)) / 1e7
}
