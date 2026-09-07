package service

import (
	"errors"
	"math"
	"testing"
)

func asValidation(err error, target **ValidationError) bool {
	return errors.As(err, target)
}

func TestNormaliseAnglesWrapsYaw(t *testing.T) {
	// Yaw wraps, so all of these name the same direction and a client is
	// entitled to send any of them. Storing the wrapped value is what makes two
	// hotspots in the same place compare equal.
	cases := []struct {
		in   float64
		want float64
	}{
		{0, 0},
		{math.Pi / 2, math.Pi / 2},
		{-math.Pi / 2, -math.Pi / 2},
		{2*math.Pi + 1, 1},       // one full turn past
		{-2*math.Pi - 1, -1},     // one full turn back
		{6*math.Pi + 0.25, 0.25}, // three turns past
		{math.Pi, -math.Pi},      // the seam: pi and -pi are one direction
	}
	for _, c := range cases {
		got, _, err := normaliseAngles(c.in, 0)
		if err != nil {
			t.Fatalf("normaliseAngles(%v): %v", c.in, err)
		}
		if math.Abs(got-c.want) > 1e-9 {
			t.Errorf("yaw %v normalised to %v, want %v", c.in, got, c.want)
		}
		if got < -math.Pi-1e-9 || got >= math.Pi {
			t.Errorf("yaw %v left the range [-pi, pi): %v", c.in, got)
		}
	}
}

func TestNormaliseAnglesClampsPitch(t *testing.T) {
	// Pitch does NOT wrap. Past the pole you are looking back the way you came,
	// so wrapping it would put a hotspot on the opposite side of the sphere
	// from where it was placed.
	for _, c := range []struct{ in, want float64 }{
		{0, 0},
		{math.Pi / 4, math.Pi / 4},
		{math.Pi, math.Pi / 2},
		{-math.Pi, -math.Pi / 2},
		{100, math.Pi / 2},
	} {
		_, got, err := normaliseAngles(0, c.in)
		if err != nil {
			t.Fatalf("normaliseAngles(pitch %v): %v", c.in, err)
		}
		if math.Abs(got-c.want) > 1e-9 {
			t.Errorf("pitch %v clamped to %v, want %v", c.in, got, c.want)
		}
	}
}

func TestNormaliseAnglesRejectsNonsense(t *testing.T) {
	// NaN reaches here as soon as a client does arithmetic on an undefined
	// value, and it would otherwise be stored and then break the viewer's
	// projection on every frame rather than at the point of entry.
	for _, bad := range []struct{ yaw, pitch float64 }{
		{math.NaN(), 0},
		{0, math.NaN()},
		{math.Inf(1), 0},
		{0, math.Inf(-1)},
	} {
		if _, _, err := normaliseAngles(bad.yaw, bad.pitch); err == nil {
			t.Errorf("yaw=%v pitch=%v should have been refused", bad.yaw, bad.pitch)
		}
	}
}

func TestValidationErrorIsItsOwnType(t *testing.T) {
	// The HTTP layer tells a bad request from a broken server by matching this
	// type. If it stopped being distinguishable, every validation message would
	// surface to the user as a 500.
	err := invalid("a direction hotspot needs somewhere to go")
	var ve *ValidationError
	if !asValidation(err, &ve) {
		t.Fatalf("invalid() should produce a *ValidationError, got %T", err)
	}
	if ve.Error() != "a direction hotspot needs somewhere to go" {
		t.Fatalf("message did not survive: %q", ve.Error())
	}
}
