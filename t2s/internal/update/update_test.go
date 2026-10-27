package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
	rel, err := Fetch(context.Background(), srv.Client(), srv.URL, "", "t2s/test")
	if err != nil {
		t.Fatal(err)
	}
	if rel.Version != "2.6.0" || rel.Date != "2026-11-01" || rel.Asset.Name != AssetName() || rel.Asset.SHA256 != sum(body) {
		t.Errorf("got %+v", rel)
	}
}

func TestAnAssetWithoutADigestIsRefused(t *testing.T) {
	srv := release(t, []byte("x"), "")
	if _, err := Fetch(context.Background(), srv.Client(), srv.URL, "", "t2s/test"); err == nil || !strings.Contains(err.Error(), "no SHA-256") {
		t.Errorf("got %v", err)
	}
}

func TestADownloadThatDoesntMatchItsDigestIsNotKept(t *testing.T) {
	srv := release(t, []byte("tampered"), "sha256:"+sum([]byte("genuine")))
	rel, err := Fetch(context.Background(), srv.Client(), srv.URL, "", "t2s/test")
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
