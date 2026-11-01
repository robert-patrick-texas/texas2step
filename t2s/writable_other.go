//go:build !windows

package main

import "golang.org/x/sys/unix"

// canWrite is whether this user can make files in dir, as t2s update does next to t2s.
func canWrite(dir string) bool { return unix.Access(dir, unix.W_OK) == nil }
