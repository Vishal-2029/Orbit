package service

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strings"

	"github.com/vishal/orbit/backend/internal/domain"
)

// How far a link hotspot may be followed when assembling a manifest.
//
// A tour is a graph, not a list, and it can contain cycles - the corridor links
// to the kitchen and the kitchen links back. The recursive walk terminates on
// its own because it collects a set, but the depth cap is what stops one
// capture at the centre of a large tour dragging every panorama in the account
// into a single manifest.
const MaxTourDepth = 6

// Enough of a body to be worth showing, and short enough that the panel does
// not become a page.
const MaxHotspotBodyLen = 2000
const MaxHotspotTitleLen = 120

// HotspotInput is what a client may set. The ID and capture come from the URL,
// never the body, so a request cannot move a hotspot onto someone else's
// capture by claiming a different parent.
type HotspotInput struct {
	Kind            string   `json:"kind"`
	Yaw             *float64 `json:"yaw"`
	Pitch           *float64 `json:"pitch"`
	Title           string   `json:"title"`
	Body            string   `json:"body"`
	TargetCaptureID string   `json:"target_capture_id"`
	Rotation        *float64 `json:"rotation"`
}

// ValidationError is a problem with what the client sent, as opposed to a
// problem with the request. The handler maps it to 400.
type ValidationError struct{ Msg string }

func (e *ValidationError) Error() string { return e.Msg }

func invalid(format string, args ...any) error {
	return &ValidationError{Msg: fmt.Sprintf(format, args...)}
}

// normaliseAngles brings yaw into [-pi, pi) and clamps pitch to the poles.
//
// Yaw wraps, so 3pi and pi are the same direction and both are legitimate
// things for a client to send; storing the wrapped value means two hotspots in
// the same place compare equal. The half-open end is [-pi rather than pi]
// simply because that is what the modulo gives, and pi and -pi are the same
// direction anyway - special-casing the seam would buy nothing.
//
// Pitch does NOT wrap. Past the pole you are looking back the way you came, so
// wrapping it would move a hotspot to the opposite side of the sphere from
// where it was placed. It clamps instead.
func normaliseAngles(yaw, pitch float64) (float64, float64, error) {
	if math.IsNaN(yaw) || math.IsInf(yaw, 0) || math.IsNaN(pitch) || math.IsInf(pitch, 0) {
		return 0, 0, invalid("yaw and pitch must be real numbers, in radians")
	}
	yaw = math.Mod(yaw+math.Pi, 2*math.Pi)
	if yaw < 0 {
		yaw += 2 * math.Pi
	}
	yaw -= math.Pi
	pitch = math.Max(-math.Pi/2, math.Min(math.Pi/2, pitch))
	return yaw, pitch, nil
}

func (s *Capture) validateHotspot(ctx context.Context, captureID string, in HotspotInput,
	existing *domain.Hotspot) (*domain.Hotspot, error) {

	h := &domain.Hotspot{CaptureID: captureID}
	if existing != nil {
		*h = *existing
	}

	kind := in.Kind
	if kind == "" && existing != nil {
		kind = existing.Kind
	}
	if kind != domain.HotspotInfo && kind != domain.HotspotLink {
		return nil, invalid(`kind must be "info" or "link"`)
	}
	h.Kind = kind

	if in.Yaw != nil {
		h.Yaw = *in.Yaw
	}
	if in.Pitch != nil {
		h.Pitch = *in.Pitch
	}
	yaw, pitch, err := normaliseAngles(h.Yaw, h.Pitch)
	if err != nil {
		return nil, err
	}
	h.Yaw, h.Pitch = yaw, pitch

	if in.Rotation != nil {
		if math.IsNaN(*in.Rotation) || math.IsInf(*in.Rotation, 0) {
			return nil, invalid("rotation must be a real number, in radians")
		}
		h.Rotation = *in.Rotation
	}

	title := strings.TrimSpace(in.Title)
	if in.Title != "" || existing == nil {
		h.Title = title
	}
	if in.Body != "" || existing == nil {
		h.Body = strings.TrimSpace(in.Body)
	}
	if len(h.Title) > MaxHotspotTitleLen {
		return nil, invalid("the title is too long (max %d characters)", MaxHotspotTitleLen)
	}
	if len(h.Body) > MaxHotspotBodyLen {
		return nil, invalid("the text is too long (max %d characters)", MaxHotspotBodyLen)
	}

	switch kind {
	case domain.HotspotInfo:
		if h.Title == "" {
			return nil, invalid("an info hotspot needs a title")
		}
		// An info hotspot that carried a target would render an arrow AND a
		// panel. Clear it rather than half-honouring it.
		h.TargetCaptureID = ""

	case domain.HotspotLink:
		if in.TargetCaptureID != "" {
			h.TargetCaptureID = in.TargetCaptureID
		}
		if h.TargetCaptureID == "" {
			return nil, invalid("a direction hotspot needs somewhere to go")
		}
		if h.TargetCaptureID == captureID {
			return nil, invalid("a direction hotspot cannot point at its own capture")
		}
		// The target must exist and be viewable. Checking now turns a broken
		// arrow - which looks like a bug in the viewer - into a message at the
		// moment the mistake is made.
		target, err := s.repo.GetCapture(ctx, h.TargetCaptureID)
		if err != nil {
			return nil, invalid("that capture could not be found")
		}
		if target.Status != domain.StatusReady && target.Status != domain.StatusPartial {
			return nil, invalid("%q is still %s - it cannot be linked to until it is finished",
				target.Title, target.Status)
		}
	}

	return h, nil
}

func (s *Capture) CreateHotspot(ctx context.Context, captureID string, in HotspotInput) (*domain.Hotspot, error) {
	if _, err := s.repo.GetCapture(ctx, captureID); err != nil {
		return nil, err
	}
	h, err := s.validateHotspot(ctx, captureID, in, nil)
	if err != nil {
		return nil, err
	}
	return s.repo.CreateHotspot(ctx, h)
}

func (s *Capture) UpdateHotspot(ctx context.Context, hotspotID string, in HotspotInput) (*domain.Hotspot, error) {
	existing, err := s.repo.GetHotspot(ctx, hotspotID)
	if err != nil {
		return nil, err
	}
	h, err := s.validateHotspot(ctx, existing.CaptureID, in, existing)
	if err != nil {
		return nil, err
	}
	h.ID = existing.ID
	return s.repo.UpdateHotspot(ctx, h)
}

func (s *Capture) DeleteHotspot(ctx context.Context, hotspotID string) error {
	return s.repo.DeleteHotspot(ctx, hotspotID)
}

func (s *Capture) ListHotspots(ctx context.Context, captureID string) ([]domain.Hotspot, error) {
	return s.repo.ListHotspots(ctx, captureID)
}

// ManifestWithScenes returns a capture's stored manifest with hotspots and the
// rest of its tour attached.
//
// Hotspots are NOT baked into the stored manifest. That JSON is written once,
// when the stitch finishes, and hotspots are added and moved long afterwards -
// so anything folded into it then would go stale the moment someone placed a
// marker. Attaching at read time costs two queries and means the viewer never
// sees an out-of-date tour.
func (s *Capture) ManifestWithScenes(ctx context.Context, c *domain.Capture) ([]byte, error) {
	if len(c.Manifest) == 0 {
		return nil, nil
	}
	var m domain.Manifest
	if err := json.Unmarshal(c.Manifest, &m); err != nil {
		// A manifest we cannot parse is still a manifest the viewer understood
		// before hotspots existed. Send it through untouched rather than
		// failing the request.
		return c.Manifest, nil
	}

	own, err := s.repo.ListHotspots(ctx, c.ID)
	if err != nil {
		return nil, err
	}

	ids, err := s.repo.LinkedCaptureIDs(ctx, c.ID, MaxTourDepth)
	if err != nil {
		return nil, err
	}

	// This capture always comes first: the viewer opens on scenes[0] unless it
	// is told otherwise, and that must be the one whose link was followed.
	ordered := make([]string, 0, len(ids))
	ordered = append(ordered, c.ID)
	for _, id := range ids {
		if id != c.ID {
			ordered = append(ordered, id)
		}
	}

	titles := map[string]*domain.Capture{}
	scenes := make([]domain.Scene, 0, len(ordered))
	for _, id := range ordered {
		sc, err := s.sceneFor(ctx, id, c, own)
		if err != nil {
			// One unreadable neighbour must not cost the whole tour. Skip it;
			// the arrow pointing at it simply will not switch scenes.
			continue
		}
		titles[id] = sc.capture
		scenes = append(scenes, sc.scene)
	}

	// Label every arrow with where it goes, so the viewer can render a tooltip
	// without fetching each neighbour.
	for i := range scenes {
		for j := range scenes[i].Hotspots {
			h := &scenes[i].Hotspots[j]
			if h.Kind != domain.HotspotLink {
				continue
			}
			if t, ok := titles[h.TargetCaptureID]; ok && t != nil {
				h.TargetTitle = t.Title
				h.TargetSlug = t.Slug
			}
		}
	}

	m.Hotspots = own
	// A tour of one is just a panorama; leave Scenes empty and let the viewer
	// take the simple path.
	if len(scenes) > 1 {
		m.Scenes = scenes
	}
	return json.Marshal(m)
}

type sceneBuild struct {
	scene   domain.Scene
	capture *domain.Capture
}

// sceneFor assembles one scene of a tour.
func (s *Capture) sceneFor(ctx context.Context, id string, self *domain.Capture,
	selfHotspots []domain.Hotspot) (*sceneBuild, error) {

	// The starting capture and its hotspots are already loaded; only a
	// neighbour costs two more queries.
	c, hs := self, selfHotspots
	if id != self.ID {
		var err error
		if c, err = s.repo.GetCapture(ctx, id); err != nil {
			return nil, err
		}
		if hs, err = s.repo.ListHotspots(ctx, id); err != nil {
			return nil, err
		}
	}

	sc := domain.Scene{
		ID:       c.ID,
		Slug:     c.Slug,
		Title:    c.Title,
		Panorama: s.PublicURL(c.ID, "panorama", 0),
		Hotspots: hs,
	}

	// Pull the size and any tile pyramid out of that capture's own manifest.
	if len(c.Manifest) > 0 {
		var nm domain.Manifest
		if err := json.Unmarshal(c.Manifest, &nm); err == nil {
			sc.Width, sc.Height = nm.Width, nm.Height
			sc.Tiles, sc.Preview, sc.Levels = nm.Tiles, nm.Preview, nm.Levels
			// A neighbour that fell back to the frame viewer has no sphere to
			// show. Linking to it would open a black screen, so it is dropped
			// from the tour.
			if nm.Renderer != "sphere" {
				return nil, fmt.Errorf("capture %s has no sphere to show", c.ID)
			}
		}
	}

	return &sceneBuild{scene: sc, capture: c}, nil
}
