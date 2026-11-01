package main

// canWrite is whether this user can make files in dir. On Windows t2s lives in the user's
// own folder, as its guide installs it, and there is no sudo to name.
func canWrite(string) bool { return true }
