package service

import (
	"bytes"
	"context"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"math"
	"strings"

	"github.com/vishal/orbit/backend/internal/domain"
	"github.com/vishal/orbit/backend/internal/storage"
)

// An equirectangular panorama is 360 degrees wide and 180 tall, so it is always
// twice as wide as it is high. That ratio is the one reliable way to tell a
// real panorama from an ordinary photo somebody hoped would work.
//
// The tolerance is generous. Stitchers trim a few rows closing the wrap seam,
// and phones round oddly, so 1.9:1 and 2.1:1 are both fine. A 4:3 snapshot
// (1.33) or a phone panorama strip (5:1 or wider) is not.
const (
	equirectRatio     = 2.0
	equirectTolerance = 0.12
)

// Below this a sphere is visibly soft as soon as anyone zooms, because the
// width is spread across a full turn: 1600px is only 4.4 pixels per degree.
const minPanoramaWidth = 1600

// UploadedPanorama is what the caller learns about an accepted image.
type UploadedPanorama struct {
	Capture  *domain.Capture  `json:"capture"`
	Manifest *domain.Manifest `json:"manifest"`
}

// CreateFromPanorama publishes an image that is ALREADY a 360 panorama.
//
// No capture, no stitching, no worker. The image goes straight to storage and a
// manifest is written pointing at it, so the whole thing costs one upload and
// finishes in a second.
//
// It exists because the best 360s people have are usually not ones this app
// stitched. A phone's own Photo Sphere mode and any 360 camera both produce a
// finished equirectangular image, and those beat a handheld ring of stills for
// the same reason a purpose-built tool usually does. Refusing to accept one and
// insisting they reshoot it here would be pride, not product.
//
// What it will NOT do is turn one ordinary photograph into a 360. Nothing can:
// a single frame holds about 65 degrees of a 360-degree world and the other 295
// are not recoverable, by this or any other tool. Anything claiming otherwise
// is either wrapping one photo around a sphere and calling the smear a
// panorama, or inventing the missing view outright. So the aspect ratio is
// checked, and an ordinary photo is refused with an explanation rather than
// accepted into a result that would look broken.
func (s *Capture) CreateFromPanorama(ctx context.Context, title string, r io.Reader, size int64) (*UploadedPanorama, error) {
	// Read it once. A panorama is a few megabytes and it has to be measured
	// before it is stored, so buffering is simpler than seeking and rereading.
	buf, err := io.ReadAll(io.LimitReader(r, maxPanoramaBytes+1))
	if err != nil {
		return nil, fmt.Errorf("could not read the upload: %w", err)
	}
	if int64(len(buf)) > maxPanoramaBytes {
		return nil, invalid("that image is larger than %d MB", maxPanoramaBytes/(1024*1024))
	}
	if len(buf) == 0 {
		return nil, invalid("that file is empty")
	}

	// DecodeConfig reads the header only, so this costs nothing even on a very
	// large image and cannot be made to allocate a pixel buffer by a hostile
	// file claiming enormous dimensions.
	cfg, format, err := image.DecodeConfig(bytes.NewReader(buf))
	if err != nil {
		return nil, invalid("that file is not a JPEG or PNG image")
	}
	if err := checkEquirect(cfg.Width, cfg.Height); err != nil {
		return nil, err
	}

	title = strings.TrimSpace(title)
	if title == "" {
		title = "Uploaded 360"
	}
	cap, _, err := s.Create(ctx, CreateInput{Title: title, Mode: domain.ModeAuto})
	if err != nil {
		return nil, err
	}

	ct := "image/jpeg"
	if format == "png" {
		ct = "image/png"
	}
	key := storage.PanoramaKey(cap.ID)
	if err := s.store.Put(ctx, s.cfg.BucketPublic, key,
		bytes.NewReader(buf), int64(len(buf)), ct); err != nil {
		return nil, fmt.Errorf("could not store the panorama: %w", err)
	}

	m := &domain.Manifest{
		Version:   1,
		CaptureID: cap.ID,
		Slug:      cap.Slug,
		Title:     title,
		Mode:      cap.Mode,
		Renderer:  "sphere",
		Panorama:  s.PublicURL(cap.ID, "panorama", 0),
		Width:     cfg.Width,
		Height:    cfg.Height,
		// Whoever made this image decided what it covers. Claiming a coverage
		// figure we did not measure would be inventing one.
		FrameCount: 0,
	}
	if err := s.repo.SetManifest(ctx, cap.ID, m, domain.StatusReady); err != nil {
		return nil, err
	}
	cap.Status = domain.StatusReady

	return &UploadedPanorama{Capture: cap, Manifest: m}, nil
}

// Room for a 16384x8192 JPEG, which is larger than any phone or consumer 360
// camera produces.
const maxPanoramaBytes = 60 * 1024 * 1024

// checkEquirect rejects anything that is not shaped like a full sphere, and
// says why in terms of what the person actually did.
func checkEquirect(w, h int) error {
	if w <= 0 || h <= 0 {
		return invalid("That image has no size")
	}
	ratio := float64(w) / float64(h)

	if math.Abs(ratio-equirectRatio) <= equirectTolerance {
		if w < minPanoramaWidth {
			return invalid(
				"That panorama is only %d pixels wide. A 360 is stretched around a "+
					"full turn, so it needs to be at least %d wide to stay sharp.",
				w, minPanoramaWidth)
		}
		return nil
	}

	// Name the shape they uploaded, because "wrong aspect ratio" tells someone
	// nothing about what to do next. These messages go straight to a person, so
	// they are written as sentences rather than as Go error strings.
	switch {
	case ratio < equirectRatio-equirectTolerance:
		// Anything narrower than a sphere is a photograph: 4:3, 16:9, square,
		// portrait. They all land here and they all need the same answer.
		return invalid(
			"That looks like an ordinary photo (%dx%d). A single photo cannot be "+
				"turned into a 360 - it only shows about 65 degrees of the world, and "+
				"the rest was never photographed. Use your phone's Photo Sphere mode, "+
				"or a 360 camera, or capture one here.", w, h)
	case ratio > 2.6:
		return invalid(
			"That looks like a wide panorama (%dx%d), not a full sphere. It covers a "+
				"strip of the horizon but nothing above or below, so it cannot be "+
				"wrapped onto a ball without stretching. A 360 photo is exactly twice "+
				"as wide as it is tall.", w, h)
	default:
		// Wider than a sphere but not by much: probably a real panorama that
		// was cropped, so say what is wrong rather than guessing at intent.
		return invalid(
			"A 360 photo has to be exactly twice as wide as it is tall. That one is "+
				"%dx%d, which is %.2f to 1.", w, h, ratio)
	}
}
