package main

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/robert-patrick-texas/texas2step/t2s/internal/engine"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/term"
	"github.com/robert-patrick-texas/texas2step/t2s/internal/update"
)

func cmdUpdate(ctx context.Context, args []string) error {
	o := newOptions("update", "[VERSION] [options]", "Install the newest t2s, or VERSION, from texas2step's GitHub releases, over this one.\n"+
		"It is checked against the SHA-256 the release gives, and run once before it takes this one's place.\n"+
		"t2s looks for updates only when asked: run this by hand, or with --yes from cron or Task Scheduler.")
	check := o.Bool("check", false, "only say whether a newer release is out; exit status 100 if one is")
	yes := o.Bool("yes", false, "install without asking")
	proxy := o.String("proxy", "", "the HTTP proxy to go through, such as http://proxy.example.net:3128 (default: https_proxy)")
	insecure := o.Bool("insecure", false, "check no certificate (for a firewall that inspects TLS with a CA this computer lacks)")
	o.BoolVar(insecure, "k", false, "the same as --insecure")
	base := o.String("url", update.Releases, "the releases API to ask, or a mirror that answers as it does")
	found, err := o.parse(args, 0, 1)
	if err != nil {
		return err
	}
	want := ""
	if len(found) == 1 {
		want = strings.TrimPrefix(found[0], "v")
	}
	if *insecure {
		fmt.Fprintln(os.Stderr, "warning: --insecure: no certificate is checked, so anything on the way can change what the release says, "+
			"its SHA-256 included. Nothing shows the new t2s is genuine.")
	}
	c, err := update.Client(strings.TrimSpace(*proxy), *insecure)
	if err != nil {
		return err
	}
	rel, err := update.Fetch(ctx, c, strings.TrimSpace(*base), want, engine.UserAgent()+" (update)")
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
			fmt.Printf("t2s %s%s is available; this is %s. Install it with: t2s update\n", rel.Version, date, shown(version))
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
	exe, err := update.Executable()
	if err != nil {
		return err
	}
	if !*yes {
		if !term.Is(os.Stdin) {
			return fmt.Errorf("add --yes to install t2s %s without asking", rel.Version)
		}
		fmt.Printf("Install t2s %s%s over %s at %s? [y/N] ", rel.Version, date, shown(version), exe)
		answer, _ := bufio.NewReader(os.Stdin).ReadString('\n')
		if a := strings.ToLower(strings.TrimSpace(answer)); a != "y" && a != "yes" {
			fmt.Println("Nothing installed.")
			return nil
		}
	}
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
	if rel.Page != "" {
		fmt.Println("What's new: " + rel.Page)
	}
	return nil
}
