package service

import (
	"errors"
	"strings"
	"testing"
)

// The message matters as much as the rejection here. Somebody uploading a
// holiday snap and hoping for a 360 has a wrong idea about what is possible,
// and "invalid aspect ratio" leaves them with it.
func TestAcceptsARealPanorama(t *testing.T) {
	for _, size := range [][2]int{
		{4096, 2048}, // the usual output of this app
		{8192, 4096}, // a 360 camera
		{5376, 2688}, // Google Photo Sphere
		{2048, 1024}, // small but usable
	} {
		if err := checkEquirect(size[0], size[1]); err != nil {
			t.Errorf("%dx%d is a valid panorama but was refused: %v",
				size[0], size[1], err)
		}
	}
}

func TestAllowsTheSlackAStitcherLeaves(t *testing.T) {
	// Closing the wrap seam trims columns, and padding to a whole number of
	// rows adds a few. Neither should cost someone their upload.
	for _, size := range [][2]int{
		{4000, 2048}, // 1.95
		{4200, 2048}, // 2.05
	} {
		if err := checkEquirect(size[0], size[1]); err != nil {
			t.Errorf("%dx%d is within tolerance but was refused: %v",
				size[0], size[1], err)
		}
	}
}

func TestRefusesAnOrdinaryPhoto(t *testing.T) {
	// The case this whole check exists for.
	for _, size := range [][2]int{
		{4032, 3024}, // 4:3 phone photo
		{1920, 1080}, // 16:9
		{3024, 4032}, // portrait
		{2000, 2000}, // square
	} {
		err := checkEquirect(size[0], size[1])
		if err == nil {
			t.Fatalf("%dx%d is an ordinary photo and must be refused", size[0], size[1])
		}
		var ve *ValidationError
		if !errors.As(err, &ve) {
			t.Fatalf("should be a validation error, got %T", err)
		}
		msg := err.Error()
		// It has to say a single photo cannot become a 360, and say what to do
		// instead. Without that the user simply tries another photo.
		if !strings.Contains(msg, "cannot be") {
			t.Errorf("%dx%d: message does not explain it is impossible: %q",
				size[0], size[1], msg)
		}
		if !strings.Contains(strings.ToLower(msg), "photo sphere") {
			t.Errorf("%dx%d: message does not say what to use instead: %q",
				size[0], size[1], msg)
		}
	}
}

func TestRefusesAFlatPanoramaStrip(t *testing.T) {
	// A phone's sweep panorama. It IS a panorama, which is exactly why the
	// message has to distinguish it from a sphere rather than call it a photo.
	for _, size := range [][2]int{
		{10000, 2000}, // 5:1
		{8000, 1500},  // 5.3:1
	} {
		err := checkEquirect(size[0], size[1])
		if err == nil {
			t.Fatalf("%dx%d is a flat strip, not a sphere", size[0], size[1])
		}
		if !strings.Contains(err.Error(), "above or below") {
			t.Errorf("%dx%d: message should explain what a strip is missing: %q",
				size[0], size[1], err.Error())
		}
	}
}

func TestRefusesSomethingTooSmallToLookSharp(t *testing.T) {
	// Correct shape, not enough pixels. Stretched around a full turn a 1000px
	// image is under 3 pixels per degree.
	err := checkEquirect(1000, 500)
	if err == nil {
		t.Fatal("1000x500 is the right shape but far too small")
	}
	if !strings.Contains(err.Error(), "1600") {
		t.Errorf("message should name the minimum width: %q", err.Error())
	}
}

func TestRefusesNonsense(t *testing.T) {
	for _, size := range [][2]int{{0, 0}, {-1, 100}, {100, 0}} {
		if err := checkEquirect(size[0], size[1]); err == nil {
			t.Errorf("%dx%d should be refused", size[0], size[1])
		}
	}
}

// Every refusal reaches the user, so none of them may read like a stack trace.
func TestEveryRefusalIsPlainEnglish(t *testing.T) {
	for _, size := range [][2]int{
		{4032, 3024}, {10000, 2000}, {1000, 500}, {2000, 1500},
	} {
		err := checkEquirect(size[0], size[1])
		if err == nil {
			continue
		}
		msg := err.Error()
		if len(msg) < 40 {
			t.Errorf("%dx%d: message is too terse to help: %q", size[0], size[1], msg)
		}
		if msg != strings.ToUpper(msg[:1])+msg[1:] {
			t.Errorf("%dx%d: message should start with a capital: %q", size[0], size[1], msg)
		}
		for _, jargon := range []string{"equirect", "aspect ratio of", "nil", "invalid input"} {
			if strings.Contains(strings.ToLower(msg), jargon) {
				t.Errorf("%dx%d: message uses jargon %q: %s", size[0], size[1], jargon, msg)
			}
		}
	}
}
