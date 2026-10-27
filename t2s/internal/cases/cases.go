// Package cases reads tests/methodology-cases.json, the Python engine's answers to a set of
// inputs (scripts/methodology_cases.py), for t2s's tests to check its own against.
package cases

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// Load reads the cases of one kind, such as "summarize", into out (a pointer to a slice).
func Load(kind string, out any) error {
	_, here, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(here), "..", "..", "..", "tests", "methodology-cases.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var all map[string]json.RawMessage
	if err := json.Unmarshal(data, &all); err != nil {
		return err
	}
	found, ok := all[kind]
	if !ok {
		return fmt.Errorf("no %q cases in %s", kind, path)
	}
	return json.Unmarshal(found, out)
}

// Diff says how got, once written as JSON, differs from want: the same keys, nulls and
// lists, figures within a part in a billion. "" when they agree.
func Diff(got any, want json.RawMessage) string {
	data, err := json.Marshal(got)
	if err != nil {
		return err.Error()
	}
	var a, b any
	if err := json.Unmarshal(data, &a); err != nil {
		return err.Error()
	}
	if err := json.Unmarshal(want, &b); err != nil {
		return err.Error()
	}
	var diffs []string
	compare("", a, b, &diffs)
	if len(diffs) > 8 {
		diffs = append(diffs[:8], fmt.Sprintf("... and %d more", len(diffs)-8))
	}
	return strings.Join(diffs, "\n")
}

func compare(at string, got, want any, diffs *[]string) {
	switch w := want.(type) {
	case map[string]any:
		g, ok := got.(map[string]any)
		if !ok {
			*diffs = append(*diffs, fmt.Sprintf("%s: got %v, want an object", at, got))
			return
		}
		keys := map[string]bool{}
		for k := range w {
			keys[k] = true
		}
		for k := range g {
			keys[k] = true
		}
		names := make([]string, 0, len(keys))
		for k := range keys {
			names = append(names, k)
		}
		sort.Strings(names)
		for _, k := range names {
			gv, gok := g[k]
			wv, wok := w[k]
			switch {
			case !gok:
				*diffs = append(*diffs, fmt.Sprintf("%s.%s: missing, want %v", at, k, wv))
			case !wok:
				*diffs = append(*diffs, fmt.Sprintf("%s.%s: got %v, which the Python engine leaves out", at, k, gv))
			default:
				compare(at+"."+k, gv, wv, diffs)
			}
		}
	case []any:
		g, ok := got.([]any)
		if !ok || len(g) != len(w) {
			*diffs = append(*diffs, fmt.Sprintf("%s: got %v, want %d items", at, short(got), len(w)))
			return
		}
		for i := range w {
			compare(fmt.Sprintf("%s[%d]", at, i), g[i], w[i], diffs)
		}
	case float64:
		g, ok := got.(float64)
		if !ok || math.Abs(g-w) > 1e-9*math.Max(1, math.Abs(w)) {
			*diffs = append(*diffs, fmt.Sprintf("%s: got %v, want %v", at, got, w))
		}
	default:
		if got != want {
			*diffs = append(*diffs, fmt.Sprintf("%s: got %v, want %v", at, got, want))
		}
	}
}

func short(v any) string {
	s := fmt.Sprint(v)
	if len(s) > 60 {
		return s[:60] + "…"
	}
	return s
}
