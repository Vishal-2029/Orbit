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
		s.RingCount = 24
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

// Manifest is what the viewer downloads. It is deliberately self-contained:
// the viewer needs no other API call to render.
type Manifest struct {
	Version     int       `json:"version"`
	CaptureID   string    `json:"capture_id"`
	Slug        string    `json:"slug"`
	Title       string    `json:"title"`
	Mode        string    `json:"mode"`
	Renderer    string    `json:"renderer"` // "sphere" | "frames"
	FrameCount  int       `json:"frame_count"`
	Direction   string    `json:"direction"`
	Width       int       `json:"width"`
	Height      int       `json:"height"`
	Panorama    string    `json:"panorama,omitempty"` // equirect URL when renderer=sphere
	Frames      []string  `json:"frames,omitempty"`
	Previews    []string  `json:"previews,omitempty"`
	Yaws        []float64 `json:"yaws,omitempty"`
	Degraded    bool      `json:"degraded"` // true when stitch failed and we fell back
	DegradedWhy string    `json:"degraded_why,omitempty"`
}
