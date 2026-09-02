// Package service holds the business logic: creating captures, accepting
// photos, queueing processing and building the viewer manifest.
package service

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"strings"

	"github.com/vishal/orbit/backend/internal/config"
	"github.com/vishal/orbit/backend/internal/domain"
	"github.com/vishal/orbit/backend/internal/queue"
	"github.com/vishal/orbit/backend/internal/realtime"
	"github.com/vishal/orbit/backend/internal/repo"
	"github.com/vishal/orbit/backend/internal/storage"
)

type Capture struct {
	repo  *repo.Repo
	store storage.Store
	q     queue.Queue
	hub   *realtime.Hub
	cfg   config.Config
}

func NewCapture(r *repo.Repo, s storage.Store, q queue.Queue, h *realtime.Hub, cfg config.Config) *Capture {
	return &Capture{repo: r, store: s, q: q, hub: h, cfg: cfg}
}

const slugAlphabet = "abcdefghijkmnpqrstuvwxyz23456789" // no look-alike chars

func newSlug() string {
	b := make([]byte, 10)
	for i := range b {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(slugAlphabet))))
		if err != nil {
			// crypto/rand failing is unrecoverable; better to fail loudly upstream
			panic(fmt.Sprintf("slug generation: %v", err))
		}
		b[i] = slugAlphabet[n.Int64()]
	}
	return string(b)
}

type CreateInput struct {
	Title         string `json:"title"`
	Mode          string `json:"mode"`
	RingCount     int    `json:"ring_count"`
	IncludeUpDown *bool  `json:"include_up_down"`
	RemoveBG      bool   `json:"remove_bg"`
}

// Create opens a draft capture and returns it together with the shot plan the
// client turns into on-screen guidance.
func (s *Capture) Create(ctx context.Context, in CreateInput) (*domain.Capture, domain.Plan, error) {
	mode := in.Mode
	switch mode {
	case domain.ModeSpin, domain.ModeAuto, domain.ModeSphere:
		// keep as-is
	default:
		mode = domain.ModePano
	}
	set := domain.DefaultSettings(mode)
	if in.RingCount > 0 {
		set.RingCount = in.RingCount
	}
	if in.IncludeUpDown != nil {
		set.IncludeUpDown = *in.IncludeUpDown
	}
	set.RemoveBG = in.RemoveBG

	plan := domain.PlanFor(mode, set.RingCount, set.IncludeUpDown)

	title := strings.TrimSpace(in.Title)
	if title == "" {
		title = "Untitled 360"
	}
	c, err := s.repo.CreateCapture(ctx, &domain.Capture{
		Title: title, Slug: newSlug(), Mode: mode,
		Status: domain.StatusDraft, FrameCount: len(plan.Slots),
		Settings: set, IsPublic: true,
	})
	if err != nil {
		return nil, plan, err
	}
	return c, plan, nil
}

func (s *Capture) Get(ctx context.Context, id string) (*domain.Capture, error) {
	return s.repo.GetCapture(ctx, id)
}
func (s *Capture) GetBySlug(ctx context.Context, slug string) (*domain.Capture, error) {
	return s.repo.GetCaptureBySlug(ctx, slug)
}
func (s *Capture) List(ctx context.Context, limit, offset int) ([]domain.Capture, error) {
	return s.repo.ListCaptures(ctx, limit, offset)
}
func (s *Capture) Frames(ctx context.Context, id string) ([]domain.Frame, error) {
	return s.repo.ListFrames(ctx, id)
}
func (s *Capture) Plan(c *domain.Capture) domain.Plan {
	return domain.PlanFor(c.Mode, c.Settings.RingCount, c.Settings.IncludeUpDown)
}

type UploadInput struct {
	Index  int
	SlotID string
	Yaw    float64
	Pitch  float64
	// HasHeading is false when the phone gave us no usable compass reading.
	// Without it we cannot tell one direction from another, so the duplicate
	// check is skipped rather than guessed at.
	HasHeading bool
	// Quat is the phone's full 3D rotation at the shutter, when available.
	Quat              *domain.Quaternion
	OrientationSource string
	Body              io.Reader
	Size              int64
	CType             string
}

// DuplicateDirectionError means the user shot the same way twice. It is a
// user-fixable mistake, not a server fault, so it carries the wording the
// client shows verbatim.
type DuplicateDirectionError struct {
	ClashIndex int
	ClashLabel string
	Degrees    float64
	Message    string
}

func (e *DuplicateDirectionError) Error() string { return e.Message }

// checkDirection rejects a photo pointing at a direction we already have.
//
// Without this the app happily accepts four photos of the same wall and then
// fails much later, at stitch time, with a confusing message. Catching it at
// the shutter is the only point where the user can still actually fix it.
func (s *Capture) checkDirection(ctx context.Context, c *domain.Capture, in UploadInput) error {
	if !in.HasHeading {
		return nil // no compass on this device; nothing to compare against
	}
	plan := s.Plan(c)
	existing, err := s.repo.ListFrames(ctx, c.ID)
	if err != nil {
		return err
	}
	labels := map[string]string{}
	for _, sl := range plan.Slots {
		labels[sl.ID] = sl.Label
	}
	for _, f := range existing {
		if f.Index == in.Index {
			continue // replacing this exact shot is a retake, always allowed
		}
		d := domain.AngularDistance(in.Yaw, in.Pitch, f.Yaw, f.Pitch)
		if d < plan.DuplicateTolerance {
			label := labels[f.SlotID]
			if label == "" {
				label = fmt.Sprintf("photo %d", f.Index+1)
			}
			want := labels[in.SlotID]
			if want == "" {
				want = "the next direction"
			}
			return &DuplicateDirectionError{
				ClashIndex: f.Index, ClashLabel: label, Degrees: d,
				Message: fmt.Sprintf(
					"This is pointing the same way as %q (only %.0f\u00b0 apart). "+
						"Turn to face %s before taking this photo, or the 360 will have a gap.",
					label, d, want),
			}
		}
	}
	return nil
}

// AddPhoto stores one original photo and records the frame row.
//
// Uploads are proxied through the API rather than sent to MinIO with a
// presigned URL. For 8-36 photos that is the right trade: it avoids
// browser CORS configuration on the object store and lets the API validate
// the bytes. The presigned path exists on the Store interface for when the
// frame counts get large enough to matter.
func (s *Capture) AddPhoto(ctx context.Context, captureID string, in UploadInput) (*domain.Frame, error) {
	c, err := s.repo.GetCapture(ctx, captureID)
	if err != nil {
		return nil, err
	}
	if c.Status != domain.StatusDraft && c.Status != domain.StatusUploading {
		return nil, fmt.Errorf("capture is %s; photos can only be added to a draft", c.Status)
	}
	if in.Index < 0 || in.Index > 512 {
		return nil, fmt.Errorf("frame index %d out of range", in.Index)
	}
	if err := s.checkDirection(ctx, c, in); err != nil {
		return nil, err
	}

	key := storage.OriginalKey(captureID, in.Index)
	ct := in.CType
	if ct == "" {
		ct = "image/jpeg"
	}
	if err := s.store.Put(ctx, s.cfg.BucketPrivate, key, in.Body, in.Size, ct); err != nil {
		return nil, fmt.Errorf("store photo: %w", err)
	}

	f, err := s.repo.UpsertFrame(ctx, &domain.Frame{
		CaptureID: captureID, Index: in.Index, SlotID: in.SlotID,
		Yaw: in.Yaw, Pitch: in.Pitch, Quat: in.Quat,
		OrientationSource: in.OrientationSource,
		OriginalKey:       key, Status: domain.FramePending,
	})
	if err != nil {
		return nil, err
	}
	if c.Status == domain.StatusDraft {
		_ = s.repo.SetCaptureStatus(ctx, captureID, domain.StatusUploading, nil)
	}
	n, err := s.repo.CountFrames(ctx, captureID)
	if err == nil {
		_ = s.repo.SetFrameCount(ctx, captureID, n)
	}
	return f, nil
}

type framePayload struct {
	FrameID     string          `json:"frame_id"`
	Index       int             `json:"index"`
	Yaw         float64         `json:"yaw"`
	Pitch       float64         `json:"pitch"`
	OriginalKey string          `json:"original_key"`
	Settings    domain.Settings `json:"settings"`
	Mode        string          `json:"mode"`
}

// Process validates that enough photos landed, then queues one job per frame
// plus a finalize job. Returns an error the user can act on if photos are short.
func (s *Capture) Process(ctx context.Context, captureID string) (*domain.Capture, error) {
	c, err := s.repo.GetCapture(ctx, captureID)
	if err != nil {
		return nil, err
	}
	frames, err := s.repo.ListFrames(ctx, captureID)
	if err != nil {
		return nil, err
	}
	plan := s.Plan(c)
	if len(frames) < plan.MinRequired {
		return nil, fmt.Errorf(
			"need at least %d photos to build a 360 view, got %d - the four directions (front, right, behind, left) are the minimum",
			plan.MinRequired, len(frames))
	}

	if err := s.repo.SetFrameCount(ctx, captureID, len(frames)); err != nil {
		return nil, err
	}
	if err := s.repo.SetCaptureStatus(ctx, captureID, domain.StatusQueued, nil); err != nil {
		return nil, err
	}

	for _, f := range frames {
		p, err := json.Marshal(framePayload{
			FrameID: f.ID, Index: f.Index, Yaw: f.Yaw, Pitch: f.Pitch,
			OriginalKey: f.OriginalKey, Settings: c.Settings, Mode: c.Mode,
		})
		if err != nil {
			return nil, err
		}
		if err := s.q.Publish(ctx, queue.Job{
			Type: queue.TypeProcessFrame, CaptureID: captureID, Payload: p,
		}); err != nil {
			return nil, fmt.Errorf("queue frame %d: %w", f.Index, err)
		}
	}
	if err := s.q.Publish(ctx, queue.Job{
		Type: queue.TypeFinalize, CaptureID: captureID, Payload: json.RawMessage(`{}`),
	}); err != nil {
		return nil, fmt.Errorf("queue finalize: %w", err)
	}

	s.hub.Publish(ctx, realtime.Event{
		Type: "status", CaptureID: captureID, Status: domain.StatusQueued,
		Total: len(frames), Progress: 0,
	})
	return s.repo.GetCapture(ctx, captureID)
}

func (s *Capture) Update(ctx context.Context, id, title string, isPublic bool) error {
	return s.repo.UpdateCaptureMeta(ctx, id, title, isPublic)
}

func (s *Capture) Delete(ctx context.Context, id string) error {
	// Storage first: a DB row with no objects is recoverable, orphaned
	// objects with no row are invisible garbage.
	_ = s.store.Delete(ctx, s.cfg.BucketPrivate, storage.CapturePrefix(id))
	_ = s.store.Delete(ctx, s.cfg.BucketPublic, storage.CapturePrefix(id))
	return s.repo.DeleteCapture(ctx, id)
}

// PublicURL builds the URL the viewer uses to fetch an image. Images are
// served back through the API so the object store never needs to be exposed.
func (s *Capture) PublicURL(captureID, kind string, idx int) string {
	if kind == "panorama" {
		return fmt.Sprintf("%s/api/v1/captures/%s/image/panorama", s.cfg.PublicBaseURL, captureID)
	}
	return fmt.Sprintf("%s/api/v1/captures/%s/image/%s/%d", s.cfg.PublicBaseURL, captureID, kind, idx)
}
