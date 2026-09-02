package service

import (
	"context"
	"fmt"

	"github.com/vishal/orbit/backend/internal/domain"
	"github.com/vishal/orbit/backend/internal/realtime"
	"github.com/vishal/orbit/backend/internal/storage"
)

// FinalizeInput is what the CV worker reports back once it has attempted the
// panorama stitch. Stitching 8 handheld phone photos into a seamless sphere is
// the single most failure-prone step in this product, so the contract is
// explicit: the worker says whether it succeeded and, if not, why.
type FinalizeInput struct {
	Stitched     bool   `json:"stitched"`
	PanoramaKey  string `json:"panorama_key"`
	Width        int    `json:"width"`
	Height       int    `json:"height"`
	FailureCause string `json:"failure_cause"`
	// PhotosUsed is how many photos actually made it into the panorama.
	// cv2.Stitcher succeeds on the largest group of photos that overlap and
	// silently drops the rest, so this can be far lower than PhotosTotal.
	PhotosUsed   int    `json:"photos_used"`
	PhotosTotal  int    `json:"photos_total"`
	CoverageNote string `json:"coverage_note"`
	// SphereCoverage is 0..1: how much of the whole sphere at least one photo
	// actually saw. Anything the camera never pointed at cannot be recovered,
	// so this is the honest ceiling on how complete the 360 can look.
	SphereCoverage float64 `json:"sphere_coverage"`
}

// MinCoverage is the share of the user's photos that must make it into the
// panorama before we are willing to present it as a real 360.
//
// Below this, the sphere would be a narrow strip stretched across a full
// globe - which looks broken when you drag it. In that case the frame viewer
// is both more honest and more useful, because it shows every photo.
const MinCoverage = 0.7

// Finalize builds the viewer manifest and closes out the capture.
//
// If the stitch succeeded we ship a real equirectangular sphere. If it failed
// we do NOT fail the capture: we fall back to a frame-swap viewer built from
// the same photos, mark the manifest degraded and tell the user why. A usable
// 360 with a visible seam beats an error screen.
func (s *Capture) Finalize(ctx context.Context, captureID string, in FinalizeInput) (*domain.Manifest, error) {
	c, err := s.repo.GetCapture(ctx, captureID)
	if err != nil {
		return nil, err
	}
	frames, err := s.repo.ListFrames(ctx, captureID)
	if err != nil {
		return nil, err
	}

	done := make([]domain.Frame, 0, len(frames))
	for _, f := range frames {
		if f.Status == domain.FrameDone {
			done = append(done, f)
		}
	}
	if len(done) == 0 {
		msg := "every photo failed to process"
		_ = s.repo.SetCaptureStatus(ctx, captureID, domain.StatusFailed, &msg)
		s.hub.Publish(ctx, realtime.Event{
			Type: "error", CaptureID: captureID, Status: domain.StatusFailed, Message: msg,
		})
		return nil, fmt.Errorf("%s", msg)
	}

	m := &domain.Manifest{
		Version: 1, CaptureID: c.ID, Slug: c.Slug, Title: c.Title,
		Mode: c.Mode, Direction: c.Settings.Direction, FrameCount: len(done),
	}

	// Both pano (guided) and auto (free upload) produce a sphere when the
	// stitch works; only spin is always a frame sequence by design.
	wantsSphere := c.Mode == domain.ModePano || c.Mode == domain.ModeAuto

	// A stitch that only swallowed a fraction of the photos is not a 360.
	enoughCoverage := true
	if in.Stitched && in.PhotosTotal > 0 {
		enoughCoverage = float64(in.PhotosUsed) >= float64(in.PhotosTotal)*MinCoverage
	}

	status := domain.StatusReady
	switch {
	case wantsSphere && in.Stitched && in.PanoramaKey != "" && enoughCoverage:
		m.Renderer = "sphere"
		m.Panorama = s.PublicURL(captureID, "panorama", 0)
		m.Width, m.Height = in.Width, in.Height
		m.Coverage = in.SphereCoverage
		// Photos cannot cover ground the camera never pointed at. Say how much
		// is missing rather than letting the user wonder what the blur is.
		if in.SphereCoverage > 0 && in.SphereCoverage < 0.9 {
			m.Degraded = true
			m.DegradedWhy = fmt.Sprintf(
				"These photos cover about %.0f%% of the view around you. The soft "+
					"patches are directions the camera never pointed at. Take a photo "+
					"at every dot - including the ceiling and floor - for a complete 360.",
				in.SphereCoverage*100)
			status = domain.StatusPartial
		}
		// Some photos were dropped, but enough remain to be a real 360. Show
		// the sphere and mention the rest rather than hiding it.
		if in.PhotosTotal > 0 && in.PhotosUsed < in.PhotosTotal && in.CoverageNote != "" {
			m.Degraded = true
			m.DegradedWhy = in.CoverageNote
			status = domain.StatusPartial
		}

	default:
		// Frame-swap fallback (and the normal path for spin mode).
		m.Renderer = "frames"
		for _, f := range done {
			m.Frames = append(m.Frames, s.PublicURL(captureID, "processed", f.Index))
			m.Previews = append(m.Previews, s.PublicURL(captureID, "thumb", f.Index))
			m.Yaws = append(m.Yaws, f.Yaw)
		}
		m.Width, m.Height = done[0].Width, done[0].Height
		if wantsSphere {
			m.Degraded = true
			m.DegradedWhy = in.FailureCause
			// Not enough of the photos connected to each other. That is a more
			// specific and more actionable problem than a plain stitch failure.
			if in.Stitched && !enoughCoverage && in.CoverageNote != "" {
				m.DegradedWhy = in.CoverageNote
			}
			if m.DegradedWhy == "" {
				m.DegradedWhy = "The photos could not be stitched into one seamless sphere, " +
					"so they are shown as a swipeable 360 sequence instead. " +
					"Retake with more overlap between shots for a seamless result."
			}
			status = domain.StatusPartial
		}
	}

	if err := s.repo.SetManifest(ctx, captureID, m, status); err != nil {
		return nil, err
	}
	s.hub.Publish(ctx, realtime.Event{
		Type: "ready", CaptureID: captureID, Status: status,
		Processed: len(done), Total: c.FrameCount, Progress: 100, Manifest: m,
	})
	return m, nil
}

// FrameDone is called by the worker after each processed frame; it updates the
// row and pushes a live progress tick to the client.
func (s *Capture) FrameDone(ctx context.Context, captureID, frameID string, idx, w, h int) error {
	if err := s.repo.MarkFrameDone(ctx, frameID,
		storage.ProcessedKey(captureID, idx), storage.ThumbKey(captureID, idx), w, h); err != nil {
		return err
	}
	c, err := s.repo.GetCapture(ctx, captureID)
	if err != nil {
		return err
	}
	if c.Status == domain.StatusQueued {
		_ = s.repo.SetCaptureStatus(ctx, captureID, domain.StatusProcessing, nil)
	}
	s.hub.Publish(ctx, realtime.Event{
		Type: "frame_done", CaptureID: captureID, Index: idx,
		Processed: c.ProcessedCount, Total: c.FrameCount, Progress: c.Progress(),
	})
	return nil
}

func (s *Capture) FrameFailed(ctx context.Context, captureID, frameID string, idx int, reason string) error {
	if err := s.repo.MarkFrameFailed(ctx, frameID, reason); err != nil {
		return err
	}
	s.hub.Publish(ctx, realtime.Event{
		Type: "frame_failed", CaptureID: captureID, Index: idx, Message: reason,
	})
	return nil
}
