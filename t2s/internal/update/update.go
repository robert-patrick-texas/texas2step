// Package update installs a newer t2s over the running one, from texas2step's GitHub
// releases, where each release lists t2s's executables beside the server's package:
//
//  1. ask the releases API for the newest release, or for one version;
//  2. download this platform's t2s (t2s-linux-amd64, t2s-windows-amd64.exe, ...) next to
//     the running one, checked against the SHA-256 the API publishes for it;
//  3. run it once to see it is that version and runs here;
//  4. put it in the running one's place: a rename on Linux; on Windows, where a running
//     program can't be replaced, the old one is renamed aside first, and removed by the
//     next run.
//
// A t2s that dpkg installed, from the t2s package, is updated by the package instead, so
// dpkg's records stay right: the release's t2s_<version>-<revision>_<arch>.deb is
// downloaded and checked the same way, the t2s in it run once, and dpkg -i installs it.
//
// Downloads check certificates against this computer's trust store, and go through
// --proxy if given, else https_proxy and the like from the environment.
package update

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Releases is where texas2step's releases are listed.
const Releases = "https://api.github.com/repos/robert-patrick-texas/texas2step"

// Available is --check's exit status when a newer release is out, as dnf check-update.
const Available = 100

// AssetName is this platform's executable in a release.
func AssetName() string { return assetName(runtime.GOOS, runtime.GOARCH) }

// Kind is which of a release's files an update takes.
type Kind int

const (
	File    Kind = iota // this platform's executable, such as t2s-linux-amd64
	Package             // the t2s package for this computer, such as t2s_2.17.0-1_amd64.deb
)

// packageName matches the t2s package for version on arch (Debian's names for amd64 and
// arm64 are Go's), with any revision.
func packageName(version, arch string) *regexp.Regexp {
	return regexp.MustCompile(`^t2s_` + regexp.QuoteMeta(version) + `-[0-9]+_` + regexp.QuoteMeta(arch) + `\.deb$`)
}

// assetName is a platform's executable: t2s-linux-amd64, t2s-macos-arm64,
// t2s-windows-amd64.exe and the like, as make t2s names them.
func assetName(goos, goarch string) string {
	system := goos
	if goos == "darwin" {
		system = "macos" // as people call it; Go calls it darwin
	}
	name := "t2s-" + system + "-" + goarch
	if goos == "windows" {
		name += ".exe"
	}
	return name
}

// Release is the parts of a release an update needs.
type Release struct {
	Tag     string
	Version string
	Date    string
	Page    string
	Asset   Asset
}

// Asset is a release's file, with the SHA-256 it is checked against.
type Asset struct {
	Name   string
	URL    string
	SHA256 string
	Size   int64
}

// Client is an HTTP client for updates: through proxy if set, else the environment's;
// with insecure, no certificate is checked.
func Client(proxy string, insecure bool) (*http.Client, error) {
	tr := &http.Transport{Proxy: http.ProxyFromEnvironment, TLSClientConfig: &tls.Config{InsecureSkipVerify: insecure}} //nolint:gosec // --insecure, by the user's choice
	if proxy != "" {
		u, err := url.Parse(proxy)
		if err != nil || u.Host == "" {
			return nil, fmt.Errorf("--proxy %q isn't a URL such as http://proxy.example.net:3128", proxy)
		}
		tr.Proxy = http.ProxyURL(u)
	}
	return &http.Client{Transport: tr, Timeout: 5 * time.Minute}, nil
}

// Fetch asks base's releases API for the newest release, or for version, and finds the
// file of the kind wanted in it.
func Fetch(ctx context.Context, c *http.Client, base, version string, kind Kind, userAgent string) (*Release, error) {
	where := strings.TrimRight(base, "/") + "/releases/latest"
	missing := "no release"
	if version != "" {
		version = strings.TrimPrefix(version, "v")
		where = strings.TrimRight(base, "/") + "/releases/tags/v" + version
		missing = "no release v" + version
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, where, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", userAgent)
	r, err := c.Do(req)
	if err != nil {
		return nil, fmt.Errorf("can't reach %s: %w", where, unwrap(err))
	}
	defer r.Body.Close()
	if r.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("%s at %s", missing, where)
	}
	if r.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s answered %s", where, r.Status)
	}
	var data struct {
		Tag       string `json:"tag_name"`
		Published string `json:"published_at"`
		Page      string `json:"html_url"`
		Assets    []struct {
			Name   string `json:"name"`
			URL    string `json:"browser_download_url"`
			Digest string `json:"digest"`
			Size   int64  `json:"size"`
		} `json:"assets"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 8<<20)).Decode(&data); err != nil {
		return nil, fmt.Errorf("%s didn't give a release: %w", where, err)
	}
	rel := &Release{Tag: data.Tag, Version: strings.TrimPrefix(data.Tag, "v"), Page: data.Page}
	if len(data.Published) >= 10 {
		rel.Date = data.Published[:10]
	}
	name := AssetName()
	matches := func(n string) bool { return n == name }
	if kind == Package {
		name = "t2s_" + rel.Version + "-*_" + runtime.GOARCH + ".deb"
		re := packageName(rel.Version, runtime.GOARCH)
		matches = re.MatchString
	}
	for _, a := range data.Assets {
		if !matches(a.Name) {
			continue
		}
		name = a.Name
		algo, sum, _ := strings.Cut(a.Digest, ":")
		if algo != "sha256" || !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(sum) {
			return nil, fmt.Errorf("release %s: %s has no SHA-256 to check it against", rel.Tag, name)
		}
		if !strings.HasPrefix(a.URL, "https://") && !strings.HasPrefix(a.URL, "http://") {
			return nil, fmt.Errorf("release %s: %s has no download URL", rel.Tag, name)
		}
		rel.Asset = Asset{Name: name, URL: a.URL, SHA256: sum, Size: a.Size}
		return rel, nil
	}
	if kind == Package {
		return nil, fmt.Errorf("release %s has no t2s package for this computer (%s)", rel.Tag, name)
	}
	return nil, fmt.Errorf("release %s has no t2s for this computer (%s)", rel.Tag, name)
}

func unwrap(err error) error {
	var ue *url.Error
	if errors.As(err, &ue) {
		return ue.Err
	}
	return err
}

// Compare is -1, 0 or 1: version a against b, by their numbers (2.10.0 after 2.9.1).
func Compare(a, b string) int {
	pa, pb := strings.Split(strings.TrimPrefix(a, "v"), "."), strings.Split(strings.TrimPrefix(b, "v"), ".")
	for i := range max(len(pa), len(pb)) {
		x, y := "0", "0"
		if i < len(pa) {
			x = pa[i]
		}
		if i < len(pb) {
			y = pb[i]
		}
		nx, ex := strconv.Atoi(x)
		ny, ey := strconv.Atoi(y)
		switch {
		case ex == nil && ey == nil && nx != ny:
			if nx < ny {
				return -1
			}
			return 1
		case (ex != nil || ey != nil) && x != y:
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

// Executable is the running t2s, as a file: symbolic links followed.
func Executable() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(exe)
}

// Download fetches the release's t2s into dir, checked against its SHA-256: the new
// file's path, made executable.
func Download(ctx context.Context, c *http.Client, rel *Release, dir, userAgent string) (string, error) {
	tmp, err := os.CreateTemp(dir, ".t2s-*.new")
	if err != nil {
		return "", fmt.Errorf("can't write next to t2s in %s (%w): run this as a user who can, or put t2s somewhere you can write", dir, unwrapPath(err))
	}
	keep := false
	defer func() {
		tmp.Close()
		if !keep {
			os.Remove(tmp.Name())
		}
	}()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rel.Asset.URL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", userAgent)
	r, err := c.Do(req)
	if err != nil {
		return "", fmt.Errorf("can't download %s: %w", rel.Asset.Name, unwrap(err))
	}
	defer r.Body.Close()
	if r.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%s answered %s", rel.Asset.URL, r.Status)
	}
	digest := sha256.New()
	n, err := io.Copy(io.MultiWriter(tmp, digest), io.LimitReader(r.Body, 256<<20))
	if err != nil {
		return "", fmt.Errorf("downloading %s: %w", rel.Asset.Name, err)
	}
	if got := hex.EncodeToString(digest.Sum(nil)); got != rel.Asset.SHA256 {
		return "", fmt.Errorf("%s doesn't match the SHA-256 its release gives (got %s, %d bytes); not installed", rel.Asset.Name, got, n)
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	if err := os.Chmod(tmp.Name(), 0o755); err != nil {
		return "", err
	}
	keep = true
	return tmp.Name(), nil
}

func unwrapPath(err error) error {
	var pe *os.PathError
	if errors.As(err, &pe) {
		return pe.Err
	}
	return err
}

// Check runs the new t2s once: it must say it is the release's version.
func Check(ctx context.Context, path, version string) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, path, "version").Output()
	if err != nil {
		return fmt.Errorf("the new t2s doesn't run here (%v); not installed", err)
	}
	if got := strings.TrimSpace(string(out)); got != "t2s "+version {
		return fmt.Errorf("the new t2s says %q, not t2s %s; not installed", got, version)
	}
	return nil
}

// Replace puts the new file in exe's place.
func Replace(newPath, exe string) error {
	if runtime.GOOS != "windows" {
		if err := os.Rename(newPath, exe); err != nil {
			os.Remove(newPath)
			return fmt.Errorf("can't replace %s: %w", exe, unwrapPath(err))
		}
		return nil
	}
	// A running program's file can be renamed on Windows, not overwritten.
	old := exe + ".old"
	os.Remove(old)
	if err := os.Rename(exe, old); err != nil {
		os.Remove(newPath)
		return fmt.Errorf("can't move %s aside: %w", exe, unwrapPath(err))
	}
	if err := os.Rename(newPath, exe); err != nil {
		_ = os.Rename(old, exe)
		os.Remove(newPath)
		return fmt.Errorf("can't replace %s: %w", exe, unwrapPath(err))
	}
	return nil
}

// Unpack takes the t2s out of the package at deb into dir, as a new executable file, so it
// can run once before the package is installed: its path. dir is the folder t2s is in,
// as for a download, which a system mounting /tmp noexec still lets run.
func Unpack(ctx context.Context, deb, dir string) (string, error) {
	cmd := exec.CommandContext(ctx, "dpkg-deb", "--fsys-tarfile", deb)
	data, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("can't open the package with dpkg-deb: %w", err)
	}
	defer func() {
		_, _ = io.Copy(io.Discard, data) // let dpkg-deb finish, so it ends
		_ = cmd.Wait()
	}()
	tr := tar.NewReader(data)
	for {
		h, err := tr.Next()
		if err != nil {
			return "", fmt.Errorf("the package has no usr/bin/t2s (%v); not installed", err)
		}
		if h.Typeflag != tar.TypeReg || strings.TrimPrefix(h.Name, "./") != "usr/bin/t2s" {
			continue
		}
		tmp, err := os.CreateTemp(dir, ".t2s-*.new")
		if err != nil {
			return "", fmt.Errorf("can't write next to t2s in %s (%w): run this as root", dir, unwrapPath(err))
		}
		_, err = io.Copy(tmp, io.LimitReader(tr, 256<<20))
		if cerr := tmp.Close(); err == nil {
			err = cerr
		}
		if err == nil {
			err = os.Chmod(tmp.Name(), 0o755)
		}
		if err != nil {
			os.Remove(tmp.Name())
			return "", err
		}
		return tmp.Name(), nil
	}
}

// InstallPackage installs the package at deb with dpkg -i, saying what dpkg says.
func InstallPackage(ctx context.Context, deb string) error {
	cmd := exec.CommandContext(ctx, "dpkg", "-i", deb)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("dpkg didn't install the package (%v); see what it said above", err)
	}
	return nil
}

// Tidy removes what an update on Windows left: the old t2s, renamed aside while it ran.
func Tidy() {
	if runtime.GOOS != "windows" {
		return
	}
	if exe, err := Executable(); err == nil {
		os.Remove(exe + ".old")
	}
}
