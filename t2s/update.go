package main

import (
	"bufio"
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/term"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/update"
)

func cmdUpdate(o *options) func(context.Context, []string) error {
	check := o.Bool("check", false, "only say whether a newer release is out; exit status 100 if one is")
	yes := o.Bool("yes", false, "install without asking")
	proxy := o.String("proxy", "", "the HTTP proxy to go through, such as http://proxy.example.net:3128 (default: https_proxy)")
	insecure := o.Bool("insecure", false, "check no certificate (for a firewall that inspects TLS with a CA this computer lacks)")
	o.BoolVar(insecure, "k", false, "the same as --insecure")
	base := o.String("url", update.Releases, "the releases API to ask, or a mirror that answers as it does")
	return func(ctx context.Context, found []string) error {
		want := ""
		if len(found) == 1 {
			want = strings.TrimPrefix(found[0], "v")
		}
		// How this t2s was installed decides how it is updated: by hand, it updates itself;
		// from the t2s package, it installs the newer package. The server's package doesn't
		// install t2s; one that some other package installed is left to that package.
		exe, exeErr := update.Executable()
		pkg, how := "", "t2s update"
		if exeErr == nil {
			pkg = packaged(ctx, exe)
		}
		switch pkg {
		case "":
			if exeErr == nil && !canWrite(filepath.Dir(exe)) {
				how = "sudo " + exe + " update" // sudo's PATH may leave out /usr/local/bin
			}
		case "t2s":
			how = "sudo t2s update"
		default:
			how = "the way the " + pkg + " package is updated"
		}
		if pkg != "" && pkg != "t2s" && !*check {
			return fmt.Errorf("%s came with the %s package, so it is updated with it: %s", exe, pkg, how)
		}
		if *insecure {
			fmt.Fprintln(os.Stderr, "warning: --insecure: no certificate is checked, so anything on the way can change what the release says, "+
				"its SHA-256 included. Nothing shows the new t2s is genuine.")
		}
		c, err := update.Client(strings.TrimSpace(*proxy), *insecure)
		if err != nil {
			return err
		}
		kind := update.File
		if pkg == "t2s" {
			kind = update.Package
		}
		rel, err := update.Fetch(ctx, c, strings.TrimSpace(*base), want, kind, engine.UserAgent()+" (update)")
		if err != nil {
			return err
		}
		shown := func(v string) string {
			if v == "dev" {
				return "a development build"
			}
			return v
		}
		newer := version == "dev" || update.Compare(rel.Version, version) > 0
		same := version != "dev" && update.Compare(rel.Version, version) == 0
		date := ""
		if rel.Date != "" {
			date = " (" + rel.Date + ")"
		}
		if *check {
			if newer {
				fmt.Printf("t2s %s%s is available; this is %s. Install it with: %s\n", rel.Version, date, shown(version), how)
				return failure{update.Available}
			}
			fmt.Printf("t2s %s is the newest release%s.\n", shown(version), date)
			return nil
		}
		if same {
			fmt.Printf("t2s %s is installed already%s.\n", version, date)
			return nil
		}
		if !newer && want == "" {
			fmt.Printf("This t2s (%s) is newer than the newest release, %s; nothing to do.\n", version, rel.Version)
			return nil
		}
		if exeErr != nil {
			return exeErr
		}
		if pkg == "t2s" && os.Geteuid() != 0 {
			return fmt.Errorf("this t2s came with the t2s package, which is updated as root: sudo t2s update")
		}
		if !*yes {
			if !term.Is(os.Stdin) {
				return fmt.Errorf("add --yes to install t2s %s without asking", rel.Version)
			}
			if pkg == "t2s" {
				fmt.Printf("Install t2s %s%s over %s, from its package %s? [y/N] ", rel.Version, date, shown(version), rel.Asset.Name)
			} else {
				fmt.Printf("Install t2s %s%s over %s at %s? [y/N] ", rel.Version, date, shown(version), exe)
			}
			answer, _ := bufio.NewReader(os.Stdin).ReadString('\n')
			if a := strings.ToLower(strings.TrimSpace(answer)); a != "y" && a != "yes" {
				fmt.Println("Nothing installed.")
				return nil
			}
		}
		if pkg == "t2s" {
			if err := updatePackage(ctx, c, rel, exe); err != nil {
				return err
			}
		} else if err := updateFile(ctx, c, rel, exe); err != nil {
			return err
		}
		if rel.Page != "" {
			fmt.Println("What's new: " + rel.Page)
		}
		return nil
	}
}

// updateFile puts the release's t2s in place of exe, one installed by hand, and has it
// rewrite the bash completion and man page t2s install wrote.
func updateFile(ctx context.Context, c *http.Client, rel *update.Release, exe string) error {
	path, err := update.Download(ctx, c, rel, filepath.Dir(exe), engine.UserAgent()+" (update)")
	if err != nil {
		return err
	}
	if err := update.Check(ctx, path, rel.Version); err != nil {
		os.Remove(path)
		return err
	}
	if err := update.Replace(path, exe); err != nil {
		return err
	}
	fmt.Printf("Installed t2s %s at %s, checked against its SHA-256 %s.\n", rel.Version, exe, rel.Asset.SHA256)
	if !inByHand(exe) {
		return nil
	}
	if have := installed(); len(have) > 0 {
		out, err := exec.CommandContext(ctx, exe, append([]string{"__share", shareDir}, have...)...).CombinedOutput()
		if err != nil {
			fmt.Printf("t2s %s didn't rewrite its bash completion and man page in %s (%v): %s\n",
				rel.Version, shareDir, err, strings.TrimSpace(string(out)))
		} else {
			fmt.Println("Rewrote its bash completion and man page in " + shareDir + ".")
		}
	}
	return nil
}

// updatePackage installs the release's t2s package over the one dpkg installed exe from:
// downloaded and checked as a file is, the t2s in it run once, then dpkg -i.
func updatePackage(ctx context.Context, c *http.Client, rel *update.Release, exe string) error {
	tmp, err := os.MkdirTemp("", "t2s-update-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(tmp)
	deb, err := update.Download(ctx, c, rel, tmp, engine.UserAgent()+" (update)")
	if err != nil {
		return err
	}
	// dpkg says the name of the file it unpacks: the release's reads better than the
	// download's. Only a plain name, so a mirror's can't place it outside tmp.
	if name := rel.Asset.Name; filepath.Base(name) == name && name != "." && name != ".." {
		if named := filepath.Join(tmp, name); os.Rename(deb, named) == nil {
			deb = named
		}
	}
	bin, err := update.Unpack(ctx, deb, filepath.Dir(exe))
	if err != nil {
		return err
	}
	err = update.Check(ctx, bin, rel.Version)
	os.Remove(bin)
	if err != nil {
		return err
	}
	if err := update.InstallPackage(ctx, deb); err != nil {
		return err
	}
	if err := update.Check(ctx, exe, rel.Version); err != nil {
		return fmt.Errorf("dpkg installed %s, but %s doesn't say it is t2s %s: %v", rel.Asset.Name, exe, rel.Version, err)
	}
	fmt.Printf("Installed t2s %s from %s, checked against its SHA-256 %s.\n", rel.Version, rel.Asset.Name, rel.Asset.SHA256)
	return nil
}
