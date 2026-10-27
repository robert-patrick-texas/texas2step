// Package num holds the arithmetic the methodology needs to agree with the Python engine
// to the last digit: its two kinds of rounding, its sum, and lists with gaps in them.
package num

import (
	"bytes"
	"encoding/json"
	"math"
	"strconv"
)

// Round rounds as Python's round(x, digits) does: the exact binary value, correctly, with
// ties to even.
func Round(x float64, digits int) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	v, _ := strconv.ParseFloat(strconv.FormatFloat(x, 'f', digits, 64), 64)
	return v
}

// HalfUp rounds as JavaScript's Math.round does, and quality.py's _round: half up.
func HalfUp(x float64, digits int) float64 {
	scale := math.Pow(10, float64(digits))
	return math.Floor(x*scale+0.5) / scale
}

// Sum adds as Python's sum() does since 3.12, with Neumaier's compensation, so a mean
// comes out as the Python engine's does.
func Sum(values []float64) float64 {
	var hi, lo float64
	for _, x := range values {
		t := hi + x
		if math.Abs(hi) >= math.Abs(x) {
			lo += (hi - t) + x
		} else {
			lo += (x - t) + hi
		}
		hi = t
	}
	if lo != 0 && !math.IsInf(lo, 0) && !math.IsNaN(lo) {
		return hi + lo
	}
	return hi
}

// Total adds left to right, as JavaScript does, and quality.py's _total.
func Total(values []float64) float64 {
	total := 0.0
	for _, x := range values {
		total += x
	}
	return total
}

// Null is a missing value in a Floats list: JSON's null.
var Null = math.NaN()

// Floats is a list of figures with gaps, as JSON's [12.5, null, 13.1]: NaN is null.
type Floats []float64

func (f Floats) MarshalJSON() ([]byte, error) {
	if f == nil {
		return []byte("null"), nil
	}
	var b bytes.Buffer
	b.WriteByte('[')
	for i, v := range f {
		if i > 0 {
			b.WriteByte(',')
		}
		if math.IsNaN(v) {
			b.WriteString("null")
		} else {
			b.Write(Float(v))
		}
	}
	b.WriteByte(']')
	return b.Bytes(), nil
}

func (f *Floats) UnmarshalJSON(data []byte) error {
	var raw []*float64
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	if raw == nil {
		*f = nil
		return nil
	}
	out := make(Floats, len(raw))
	for i, v := range raw {
		if v == nil {
			out[i] = Null
		} else {
			out[i] = *v
		}
	}
	*f = out
	return nil
}

// Float is one figure as encoding/json writes it.
func Float(v float64) []byte {
	b, _ := json.Marshal(v)
	return b
}

// Ptr is a figure that may be missing, for JSON's null.
func Ptr(v float64) *float64 { return &v }

// Grouped writes v with digits after the point and commas between thousands, as Python's
// f"{v:,.2f}" does.
func Grouped(v float64, digits int) string {
	s := strconv.FormatFloat(v, 'f', digits, 64)
	sign := ""
	if s[0] == '-' {
		sign, s = "-", s[1:]
	}
	whole, frac := s, ""
	if i := bytes.IndexByte([]byte(s), '.'); i >= 0 {
		whole, frac = s[:i], s[i:]
	}
	var b []byte
	for i := range len(whole) {
		if i > 0 && (len(whole)-i)%3 == 0 {
			b = append(b, ',')
		}
		b = append(b, whole[i])
	}
	return sign + string(b) + frac
}
