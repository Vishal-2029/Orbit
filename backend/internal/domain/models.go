package domain

import (
	"encoding/json"
	"time"
)

// Capture modes.
const (
	ModePano = "pano" // stand in one place, shoot outward -> photosphere
	ModeSpin = "spin" // orbit an object -> turntable spin
	ModeAuto = "auto" // any photos, any order -> let the stitcher work it out
	// ModeSphere covers the whole sphere with dots rather than a short fixed
	// list, so the user can shoot in any direction they like.
	ModeSphere = "sphere"
)

// Capture statuses.
const (
	StatusDraft      = "draft"
	StatusUploading  = "uploading"
	StatusQueued     = "queued"
	StatusProcessing = "processing"
	StatusReady      = "ready"
	StatusPartial    = "partial" // stitch failed, usable fallback viewer produced
	StatusFailed     = "failed"
)

// Frame statuses.
const (
	FramePending    = "pending"
	FrameProcessing = "processing"
	FrameDone       = "done"
	FrameFailed     = "failed"
)

type Settings struct {
	RingCount     int    `json:"ring_count"`
	IncludeUpDown bool   `json:"include_up_down"`
	RemoveBG      bool   `json:"remove_bg"`
	Align         bool   `json:"align"`
	TargetWidth   int    `json:"target_width"`
	Direction     string `json:"direction"` // cw | ccw
}

func DefaultSettings(mode string) Settings {
	// Pano defaults to the four cardinal directions plus the ceiling and floor.
	s := Settings{RingCount: 6, IncludeUpDown: true, Align: true, TargetWidth: 1600, Direction: "cw"}
	switch mode {
	case ModeSpin:
		// Three rows of five. Spin is shot from above, level and below so the
		// viewer can be dragged in both axes, not just round.
		s.RingCount = SpinMinFrames
		s.IncludeUpDown = false
	case ModeAuto:
		// There is no shot list to count; whatever the user supplies is it.
		s.RingCount = 0
		s.IncludeUpDown = false
	case ModeSphere:
		s.RingCount = 30 // degrees between dots
		s.IncludeUpDown = true
	}
	return s
}

type Capture struct {
	ID             string          `json:"id"`
	UserID         *string         `json:"user_id,omitempty"`
	Title          string          `json:"title"`
	Slug           string          `json:"slug"`
	Mode           string          `json:"mode"`
	Status         string          `json:"status"`
	FrameCount     int             `json:"frame_count"`
	ProcessedCount int             `json:"processed_count"`
	Settings       Settings        `json:"settings"`
	Manifest       json.RawMessage `json:"manifest,omitempty"`
	Error          *string         `json:"error,omitempty"`
	IsPublic       bool            `json:"is_public"`
	CreatedAt      time.Time       `json:"created_at"`
	UpdatedAt      time.Time       `json:"updated_at"`
}

// Progress is the 0..100 percentage the client shows on the processing screen.
func (c Capture) Progress() int {
	switch c.Status {
	case StatusReady, StatusPartial:
		return 100
	case StatusDraft, StatusUploading:
		return 0
	}
	if c.FrameCount == 0 {
		return 0
	}
	// frames are 80% of the work; stitching/finalize is the last 20%.
	return c.ProcessedCount * 80 / c.FrameCount
}

// Quaternion is the phone's full 3D rotation, [x, y, z, w].
// Nil when the device gave us no usable motion sensor.
type Quaternion struct {
	X, Y, Z, W float64
}

type Frame struct {
	ID        string      `json:"id"`
	CaptureID string      `json:"capture_id"`
	Index     int         `json:"index"`
	SlotID    string      `json:"slot_id"`
	Yaw       float64     `json:"yaw"`
	Pitch     float64     `json:"pitch"`
	Quat      *Quaternion `json:"quat,omitempty"`
	// OrientationSource records which sensor produced Quat, so a later
	// pose-aware stitch can weigh a gyroscope reading above a magnetometer one.
	OrientationSource string  `json:"orientation_source,omitempty"`
	OriginalKey       string  `json:"original_key"`
	ProcessedKey      string  `json:"processed_key,omitempty"`
	ThumbKey          string  `json:"thumb_key,omitempty"`
	Width             int     `json:"width"`
	Height            int     `json:"height"`
	Status            string  `json:"status"`
	Error             *string `json:"error,omitempty"`
}


// HotspotKind is which of the two things a hotspot is.
const (
	HotspotInfo = "info"
	HotspotLink = "link"
)

// Hotspot is a marker placed inside a finished 360.
//
// Yaw and Pitch are RADIANS, unlike Frame.Yaw which is a compass bearing in
// degrees. They are different quantities - where a marker sits, versus where
// the phone was pointed - and the viewer's projection maths speaks radians, so
// converting at the edges would only move the rounding around.
//
// Pitch is positive DOWNWARDS, which is the viewer's convention and therefore
// the one these numbers arrive in: a click at the top of the screen comes back
// negative. Worth stating, because it is the opposite of Frame.Pitch.
type Hotspot struct {
	ID        string  `json:"id"`
	CaptureID string  `json:"capture_id"`
	Kind      string  `json:"kind"` // "info" | "link"
	Yaw       float64 `json:"yaw"`
	Pitch     float64 `json:"pitch"`

	// info
	Title string `json:"title,omitempty"`
	Body  string `json:"body,omitempty"`

	// link
	TargetCaptureID string  `json:"target_capture_id,omitempty"`
	Rotation        float64 `json:"rotation,omitempty"`

	// TargetTitle and TargetSlug are filled in when a manifest is built, so the
	// viewer can label an arrow and route to it without fetching every
	// neighbouring capture first.
	TargetTitle string `json:"target_title,omitempty"`
	TargetSlug  string `json:"target_slug,omitempty"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Scene is one panorama inside a manifest.
//
// A capture used to be a single panorama and the manifest described it
// directly. Link hotspots make a capture the entry point to a small graph of
// them, and the viewer needs every reachable panorama up front: the panorama
// engine only downloads tiles for the scene on screen, so building them all is
// cheap, and having them built is what lets a link cross-fade instead of
// reloading the page.
type Scene struct {
	ID       string `json:"id"`
	Slug     string `json:"slug,omitempty"`
	Title    string `json:"title"`
	Panorama string `json:"panorama"`
	Width    int    `json:"width,omitempty"`
	Height   int    `json:"height,omitempty"`

	// Tiles is a URL template with {z}/{f}/{y}/{x} placeholders, present only
	// when the worker produced a cube-tile pyramid for this capture. When it is
	// empty the viewer falls back to the single equirectangular image, which
	// works everywhere but cannot stay sharp when zoomed.
	Tiles   string       `json:"tiles,omitempty"`
	Preview string       `json:"preview,omitempty"`
	Levels  []TileLevel  `json:"levels,omitempty"`

	Hotspots []Hotspot `json:"hotspots"`
}

// TileLevel is one step of the cube-tile pyramid, in the shape the panorama
// engine's CubeGeometry expects.
type TileLevel struct {
	TileSize     int  `json:"tileSize"`
	Size         int  `json:"size"`
	FallbackOnly bool `json:"fallbackOnly,omitempty"`
}

// Manifest is what the viewer downloads. It is deliberately self-contained:
// the viewer needs no other API call to render.
type Manifest struct {
	Version    int       `json:"version"`
	CaptureID  string    `json:"capture_id"`
	Slug       string    `json:"slug"`
	Title      string    `json:"title"`
	Mode       string    `json:"mode"`
	Renderer   string    `json:"renderer"` // "sphere" | "frames"
	FrameCount int       `json:"frame_count"`
	Direction  string    `json:"direction"`
	Width      int       `json:"width"`
	Height     int       `json:"height"`
	Panorama   string    `json:"panorama,omitempty"` // equirect URL when renderer=sphere
	Frames     []string  `json:"frames,omitempty"`
	Previews   []string  `json:"previews,omitempty"`
	Yaws       []float64 `json:"yaws,omitempty"`
	// Pitches pairs with Yaws. Spin captures are shot from three heights, so
	// the viewer needs the vertical angle too in order to lay the frames out as
	// a grid and let a drag upward change row rather than doing nothing.
	Pitches []float64 `json:"pitches,omitempty"`
	// Coverage is 0..1: the share of the sphere that was actually photographed.
	Coverage    float64 `json:"coverage,omitempty"`
	Degraded    bool    `json:"degraded"` // true when stitch failed and we fell back
	DegradedWhy string  `json:"degraded_why,omitempty"`

	// Hotspots on THIS capture. Kept alongside Scenes rather than only inside
	// it so a client that predates scenes still finds them.
	Hotspots []Hotspot `json:"hotspots,omitempty"`

	// Scenes is this capture plus every other one reachable from it through a
	// link hotspot, so the viewer can walk a tour without another round trip.
	// The first entry is always this capture.
	Scenes []Scene `json:"scenes,omitempty"`

	// Tiles, Preview and Levels describe the cube-tile pyramid for THIS
	// capture, when one exists. Same fields as Scene, repeated here for the
	// same backwards-compatibility reason as Hotspots.
	Tiles   string      `json:"tiles,omitempty"`
	Preview string      `json:"preview,omitempty"`
	Levels  []TileLevel `json:"levels,omitempty"`
}
