package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func release(t *testing.T, body []byte, digest string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	var srv *httptest.Server
	mux.HandleFunc("/releases/latest", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") == "" {
			http.Error(w, "GitHub asks for a User-Agent", http.StatusForbidden)
			return
		}
		fmt.Fprintf(w, `{"tag_name": "v2.6.0", "published_at": "2026-11-01T12:00:00Z", "html_url": "%[1]s/notes",
			"assets": [{"name": "texas2step-2.6.0-1-amd64.deb", "browser_download_url": "%[1]s/deb", "digest": "sha256:%[2]s"},
			           {"name": %[3]q, "browser_download_url": "%[1]s/t2s", "digest": %[4]q, "size": %[5]d}]}`,
			srv.URL, strings.Repeat("0", 64), AssetName(), digest, len(body))
	})
	mux.HandleFunc("/t2s", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(body) })
	srv = httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func sum(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func TestFetchFindsThisPlatformsExecutableAndItsDigest(t *testing.T) {
	body := []byte("new t2s")
	srv := release(t, body, "sha256:"+sum(body))
	rel, err := Fetch(context.Background(), srv.Client(), srv.URL, "", File, "t2s/test")
	if err != nil {
		t.Fatal(err)
	}
	if rel.Version != "2.6.0" || rel.Date != "2026-11-01" || rel.Asset.Name != AssetName() || rel.Asset.SHA256 != sum(body) {
		t.Errorf("got %+v", rel)
	}
}

func TestAnAssetWithoutADigestIsRefused(t *testing.T) {
	srv := release(t, []byte("x"), "")
	if _, err := Fetch(context.Background(), srv.Client(), srv.URL, "", File, "t2s/test"); err == nil || !strings.Contains(err.Error(), "no SHA-256") {
		t.Errorf("got %v", err)
	}
}

func TestADownloadThatDoesntMatchItsDigestIsNotKept(t *testing.T) {
	srv := release(t, []byte("tampered"), "sha256:"+sum([]byte("genuine")))
	rel, err := Fetch(context.Background(), srv.Client(), srv.URL, "", File, "t2s/test")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if _, err := Download(context.Background(), srv.Client(), rel, dir, "t2s/test"); err == nil || !strings.Contains(err.Error(), "doesn't match") {
		t.Fatalf("got %v", err)
	}
	if left, _ := os.ReadDir(dir); len(left) != 0 {
		t.Errorf("left behind: %v", left)
	}
}

func TestReplacePutsTheNewFileInPlace(t *testing.T) {
	dir := t.TempDir()
	exe, fresh := filepath.Join(dir, "t2s"), filepath.Join(dir, ".t2s-1.new")
	_ = os.WriteFile(exe, []byte("old"), 0o755)
	_ = os.WriteFile(fresh, []byte("new"), 0o755)
	if err := Replace(fresh, exe); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(exe); string(got) != "new" {
		t.Errorf("t2s is %q", got)
	}
}

func TestFetchFindsTheT2sPackageForThisComputer(t *testing.T) {
	deb := "t2s_2.6.0-1_" + runtime.GOARCH + ".deb"
	mux := http.NewServeMux()
	var srv *httptest.Server
	mux.HandleFunc("/releases/latest", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `{"tag_name": "v2.6.0", "assets": [
			{"name": "texas2step_2.6.0-1_all.deb", "browser_download_url": "%[1]s/server", "digest": "sha256:%[2]s"},
			{"name": "t2s_2.6.0-1_other.deb", "browser_download_url": "%[1]s/other", "digest": "sha256:%[2]s"},
			{"name": %[3]q, "browser_download_url": "%[1]s/deb", "digest": "sha256:%[2]s"},
			{"name": %[4]q, "browser_download_url": "%[1]s/t2s", "digest": "sha256:%[2]s"}]}`,
			srv.URL, strings.Repeat("a", 64), deb, AssetName())
	})
	srv = httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	rel, err := Fetch(context.Background(), srv.Client(), srv.URL, "", Package, "t2s/test")
	if err != nil {
		t.Fatal(err)
	}
	if rel.Asset.Name != deb || rel.Asset.URL != srv.URL+"/deb" {
		t.Errorf("got %+v", rel.Asset)
	}
	if rel, err := Fetch(context.Background(), srv.Client(), srv.URL, "", File, "t2s/test"); err != nil || rel.Asset.Name != AssetName() {
		t.Errorf("the file: got %+v, %v", rel, err)
	}
}

func TestAReleaseWithoutTheT2sPackageSaysSo(t *testing.T) {
	srv := release(t, []byte("x"), "sha256:"+sum([]byte("x")))
	if _, err := Fetch(context.Background(), srv.Client(), srv.URL, "", Package, "t2s/test"); err == nil ||
		!strings.Contains(err.Error(), "no t2s package for this computer (t2s_2.6.0-*_"+runtime.GOARCH+".deb)") {
		t.Errorf("got %v", err)
	}
}

func TestUnpackTakesT2sOutOfThePackage(t *testing.T) {
	if _, err := exec.LookPath("dpkg-deb"); err != nil {
		t.Skip("no dpkg-deb here")
	}
	root := t.TempDir()
	_ = os.MkdirAll(filepath.Join(root, "DEBIAN"), 0o755)
	_ = os.MkdirAll(filepath.Join(root, "usr", "bin"), 0o755)
	_ = os.WriteFile(filepath.Join(root, "DEBIAN", "control"), []byte("Package: t2s\nVersion: 2.6.0-1\n"+
		"Architecture: "+runtime.GOARCH+"\nMaintainer: Test <test@example.net>\nDescription: test\n"), 0o644)
	_ = os.WriteFile(filepath.Join(root, "usr", "bin", "t2s"), []byte("the packaged t2s"), 0o755)
	deb := filepath.Join(t.TempDir(), "t2s_2.6.0-1_"+runtime.GOARCH+".deb")
	if out, err := exec.Command("dpkg-deb", "--root-owner-group", "--build", root, deb).CombinedOutput(); err != nil {
		t.Fatalf("dpkg-deb: %v: %s", err, out)
	}
	dir := t.TempDir()
	path, err := Unpack(context.Background(), deb, dir)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(path); string(got) != "the packaged t2s" || filepath.Dir(path) != dir {
		t.Errorf("got %q at %s", got, path)
	}
	if st, _ := os.Stat(path); st.Mode().Perm() != 0o755 {
		t.Errorf("mode %v", st.Mode())
	}
	if _, err := Unpack(context.Background(), filepath.Join(root, "DEBIAN", "control"), dir); err == nil {
		t.Error("a file that isn't a package was unpacked")
	}
}

func TestCompareOrdersVersionsByTheirNumbers(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want int
	}{{"2.10.0", "2.9.1", 1}, {"2.5.0", "2.5.0", 0}, {"v2.5", "2.5.0", 0}, {"2.5.0", "2.6.0", -1}} {
		if got := Compare(c.a, c.b); got != c.want {
			t.Errorf("Compare(%s, %s) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

func TestEachPlatformHasTheFileMakeT2sBuilds(t *testing.T) {
	for _, c := range [][3]string{
		{"linux", "amd64", "t2s-linux-amd64"},
		{"linux", "arm64", "t2s-linux-arm64"},
		{"darwin", "amd64", "t2s-macos-amd64"},
		{"darwin", "arm64", "t2s-macos-arm64"},
		{"windows", "amd64", "t2s-windows-amd64.exe"},
		{"windows", "arm64", "t2s-windows-arm64.exe"},
	} {
		if got := assetName(c[0], c[1]); got != c[2] {
			t.Errorf("%s/%s: got %s, want %s", c[0], c[1], got, c[2])
		}
	}
}
