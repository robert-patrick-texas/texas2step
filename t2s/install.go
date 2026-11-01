package main

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/update"
)

// shareDir is where t2s install writes, for a t2s installed by hand: bash-completion and
// man look under it wherever the command is, and no package writes there.
const shareDir = "/usr/local/share"

// byHand are the folders t2s install is for.
var byHand = []string{"/usr/local/bin", "/usr/bin"}

// The files t2s install writes under a share folder, such as /usr/local/share.
func completionFile(share string) string {
	return filepath.Join(share, "bash-completion", "completions", "t2s")
}
func manFile(share string) string { return filepath.Join(share, "man", "man1", "t2s.1") }

func cmdInstall(o *options) func(context.Context, []string) error {
	remove := o.Bool("remove", false, "remove them instead")
	return func(ctx context.Context, _ []string) error {
		exe, err := update.Executable()
		if err != nil {
			return err
		}
		if !inByHand(exe) {
			return fmt.Errorf("this t2s is at %s: t2s install is for one in /usr/local/bin or /usr/bin. Put it there first:\n"+
				"  sudo install -m 755 %s /usr/local/bin/t2s", exe, exe)
		}
		if pkg := packaged(ctx, exe); pkg != "" {
			fmt.Printf("%s came with the %s package, which put its bash completion and man page in place: nothing to do.\n", exe, pkg)
			return nil
		}
		if os.Geteuid() != 0 {
			return fmt.Errorf("t2s install writes to %s: run it with sudo:\n  sudo %s install", shareDir, exe)
		}
		files := []string{completionFile(shareDir), manFile(shareDir)}
		if *remove {
			gone := 0
			for _, f := range files {
				switch err := os.Remove(f); {
				case err == nil:
					fmt.Println("Removed " + f)
					gone++
				case !errors.Is(err, fs.ErrNotExist):
					return err
				}
			}
			if gone == 0 {
				fmt.Println("Nothing to remove: t2s's bash completion and man page aren't in " + shareDir + ".")
			}
			return nil
		}
		if err := share([]string{shareDir}); err != nil {
			return err
		}
		for _, f := range files {
			fmt.Println("Wrote " + f)
		}
		if _, err := os.Stat("/usr/share/bash-completion/bash_completion"); err != nil {
			fmt.Println("bash-completion isn't installed, so bash won't load the completion: install the bash-completion package.")
		} else {
			// A shell that tried to complete t2s before keeps bash-completion's minimal
			// stand-in until it is cleared.
			fmt.Println("Tab completion works in new shells (in one open now, run: complete -r t2s), and man t2s now.\n" +
				"t2s update keeps them up to date.")
		}
		return nil
	}
}

// inByHand is whether exe is in a folder t2s install is for.
func inByHand(exe string) bool {
	dir := filepath.Dir(exe)
	for _, d := range byHand {
		if dir == d {
			return true
		}
	}
	return false
}

// share writes t2s's bash completion and man page under a share folder (args[0]), as
// share/bash-completion/completions/t2s and share/man/man1/t2s.1; or only those named
// after it, "bash" or "man". It is t2s __share: t2s install and t2s update run it, and so
// does the package's build.
func share(args []string) error {
	if len(args) == 0 {
		return errors.New("__share DIR [bash] [man]")
	}
	dir, which := args[0], args[1:]
	if len(which) == 0 {
		which = []string{"bash", "man"}
	}
	for _, w := range which {
		var err error
		switch w {
		case "bash":
			err = writeFile(completionFile(dir), bashCompletion)
		case "man":
			err = writeFile(manFile(dir), manPage())
		default:
			err = fmt.Errorf("__share: no file %q (bash or man)", w)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

// writeFile puts text at path, readable by all, in one step: a new file renamed into
// place, so a reader never sees half of one.
func writeFile(path, text string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".t2s-*.new")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.WriteString(text); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// installed are the files t2s install wrote that are there, by __share's names.
func installed() []string {
	var have []string
	if _, err := os.Stat(completionFile(shareDir)); err == nil {
		have = append(have, "bash")
	}
	if _, err := os.Stat(manFile(shareDir)); err == nil {
		have = append(have, "man")
	}
	return have
}

// packaged is the package that installed the file at path, as dpkg knows it: "t2s",
// "texas2step", or "" for one installed by hand (or on a system without dpkg).
func packaged(ctx context.Context, path string) string {
	if runtime.GOOS != "linux" {
		return ""
	}
	query, err := exec.LookPath("dpkg-query")
	if err != nil {
		return ""
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, query, "-S", path).Output()
	if err != nil {
		return "" // no package has it
	}
	return ownerOf(string(out), path)
}

// ownerOf reads dpkg-query -S's answer for path: "t2s: /usr/bin/t2s", or with an
// architecture, "t2s:amd64: /usr/bin/t2s"; lines about diversions aren't about owners.
func ownerOf(out, path string) string {
	for _, l := range strings.Split(out, "\n") {
		pkgs, file, ok := strings.Cut(l, ": ")
		if !ok || strings.HasPrefix(l, "diversion ") {
			continue
		}
		// a package's name never holds ": ", so the first one ends the packages
		if strings.TrimSpace(file) != path {
			continue
		}
		first, _, _ := strings.Cut(pkgs, ", ")
		name, _, _ := strings.Cut(first, ":")
		return strings.TrimSpace(name)
	}
	return ""
}
