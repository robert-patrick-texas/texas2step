package show

import (
	"slices"
	"testing"
)

func TestWrapBreaksBetweenPartsAndInsideALongOne(t *testing.T) {
	parts := []string{"TLSv1.3 TLS_AES_256_GCM_SHA384", "to 192.0.2.10", "server's TCP RTT 18.1 ms"}
	got := Wrap(parts, " · ", 50)
	want := []string{"TLSv1.3 TLS_AES_256_GCM_SHA384 · to 192.0.2.10", "server's TCP RTT 18.1 ms"}
	if !slices.Equal(got, want) {
		t.Errorf("got %q", got)
	}
	got = Wrap([]string{"the server doesn't offer HTTP/3 here: its answers over TCP carry no Alt-Svc h3"}, " · ", 40)
	want = []string{"the server doesn't offer HTTP/3 here:", "its answers over TCP carry no Alt-Svc h3"}
	if !slices.Equal(got, want) {
		t.Errorf("got %q", got)
	}
	if got := Wrap([]string{Dim("MAC") + " 3c:22:fb:12:34:56"}, " · ", 21); len(got) != 1 {
		t.Errorf("a styled part counts its letters alone: got %q", got)
	}
}
