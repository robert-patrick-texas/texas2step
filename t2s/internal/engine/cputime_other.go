//go:build !unix && !windows

package engine

// processCPU: this system has no figure for it.
func processCPU() float64 { return -1 }
