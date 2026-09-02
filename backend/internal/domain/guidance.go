package domain

import (
	"fmt"
	"math"
)

// Slot groups, in the order the user shoots them.
const (
	GroupCore   = "core"   // front / right / back / left — the 360 ring itself
	GroupUpDown = "updown" // ceiling and floor
	GroupExtra  = "extra"  // in-between angles, only if the user wants a smoother result
)

// A Slot is one photo the user is told to take: where to point the phone and
// what to call it in plain language. The client renders these as an on-screen
// checklist and uses Yaw/Pitch to drive the live turn arrow.
type Slot struct {
	ID       string  `json:"id"`
	Index    int     `json:"index"`
	Label    string  `json:"label"` // "Right side"
	Hint     string  `json:"hint"`  // "Turn a quarter turn to your right."
	Icon     string  `json:"icon"`
	Yaw      float64 `json:"yaw"`   // degrees clockwise from the Front shot
	Pitch    float64 `json:"pitch"` // degrees above the horizon
	Group    string  `json:"group"`
	Required bool    `json:"required"`
}

// A Plan is the whole shot list for one capture session.
type Plan struct {
	Mode        string  `json:"mode"`
	Slots       []Slot  `json:"slots"`
	MinRequired int     `json:"min_required"`
	YawStep     float64 `json:"yaw_step"`
	// AlignTolerance is how close (degrees) the phone must be to a slot's
	// direction before the client arms the shutter.
	AlignTolerance float64 `json:"align_tolerance"`
	// DuplicateTolerance is how close two photos may be before the second is
	// treated as a re-shoot of the same direction rather than a new one.
	DuplicateTolerance float64  `json:"duplicate_tolerance"`
	Tips               []string `json:"tips"`
}

// RequiredSlots returns just the shots the user must take.
func (p Plan) RequiredSlots() []Slot {
	out := make([]Slot, 0, len(p.Slots))
	for _, s := range p.Slots {
		if s.Required {
			out = append(out, s)
		}
	}
	return out
}

// The four cardinal directions come first, because they are what actually makes
// a 360: front, then a quarter turn right, then behind, then the other side.
// Everything after them is optional refinement.
var coreRing = []Slot{
	{ID: "front", Label: "Front", Icon: "▲", Yaw: 0,
		Hint: "Point at whatever is in front of you and shoot. This becomes your starting direction."},
	{ID: "right", Label: "Right side", Icon: "▶", Yaw: 90,
		Hint: "Turn a quarter turn to your RIGHT, so your right shoulder is now where your nose was."},
	{ID: "back", Label: "Behind you", Icon: "▼", Yaw: 180,
		Hint: "Keep turning the same way. You should now be looking at the exact opposite of the first shot."},
	{ID: "left", Label: "Left side", Icon: "◀", Yaw: 270,
		Hint: "One more quarter turn the same way. One more after this brings you back to the front."},
}

var upDown = []Slot{
	{ID: "up", Label: "Up (ceiling / sky)", Icon: "⬆", Pitch: 90,
		Hint: "Point the phone straight up above your head."},
	{ID: "down", Label: "Down (floor)", Icon: "⬇", Pitch: -90,
		Hint: "Point the phone straight down at your feet."},
}

// Extras sit between the cardinals and are shot last, so a user who stops early
// still has an even ring rather than a lopsided one.
var extraRing = []Slot{
	{ID: "front_right", Label: "Between front and right", Icon: "◥", Yaw: 45},
	{ID: "back_right", Label: "Between right and back", Icon: "◢", Yaw: 135},
	{ID: "back_left", Label: "Between back and left", Icon: "◣", Yaw: 225},
	{ID: "front_left", Label: "Between left and front", Icon: "◤", Yaw: 315},
}

// PanoPlan builds the shot list for a stand-in-one-place 360.
//
// The four cardinal shots are required; up, down and the in-between angles are
// offered afterwards and improve the result but are never forced.
func PanoPlan(includeUpDown, includeExtras bool) Plan {
	slots := make([]Slot, 0, 10)
	add := func(s Slot, group string, required bool) {
		s.Index = len(slots)
		s.Group = group
		s.Required = required
		slots = append(slots, s)
	}
	for _, s := range coreRing {
		add(s, GroupCore, true)
	}
	if includeUpDown {
		for _, s := range upDown {
			add(s, GroupUpDown, false)
		}
	}
	if includeExtras {
		for _, s := range extraRing {
			s.Hint = fmt.Sprintf("Optional. Stand between the last two directions (%.0f°) for a smoother result.", s.Yaw)
			add(s, GroupExtra, false)
		}
	}
	return Plan{
		Mode: ModePano, Slots: slots,
		MinRequired: len(coreRing), YawStep: 90,
		AlignTolerance: 15, DuplicateTolerance: 35,
		Tips: []string{
			"Stand still and turn on the spot. Do not walk in a circle.",
			"Always turn the SAME way — keep going right the whole time.",
			"Hold the phone upright and at the same height for every shot.",
			"The more photos you add, the smoother the result.",
		},
	}
}

// SpinPlan builds the shot list for orbiting an object on a turntable.
func SpinPlan(frameCount int) Plan {
	if frameCount < 4 {
		frameCount = 4
	}
	step := 360.0 / float64(frameCount)
	slots := make([]Slot, 0, frameCount)
	for i := 0; i < frameCount; i++ {
		slots = append(slots, Slot{
			ID: fmt.Sprintf("spin_%d", i), Index: i,
			Label: fmt.Sprintf("Shot %d of %d", i+1, frameCount),
			Hint:  fmt.Sprintf("Rotate the object %.0f° and shoot again.", step),
			Icon:  "↻", Yaw: float64(i) * step, Group: GroupCore, Required: true,
		})
	}
	return Plan{
		Mode: ModeSpin, Slots: slots, MinRequired: frameCount,
		YawStep: step, AlignTolerance: 15, DuplicateTolerance: step / 2,
		Tips: []string{
			"Keep the camera still. Rotate the object, not yourself.",
			"Keep the object in the same spot in the frame every time.",
			"Lock exposure and focus so brightness does not jump between frames.",
		},
	}
}

// AutoPlan is the "I already have photos" case: no shot list, no directions to
// hit, no duplicate checking. The stitcher works out how the photos relate to
// each other by matching features, so the ordering the user uploads in does not
// matter. The trade is that nothing can be validated up front — if the photos
// do not overlap, that only becomes apparent at stitch time.
func AutoPlan() Plan {
	return Plan{
		Mode: ModeAuto, Slots: nil, MinRequired: 4,
		YawStep: 0, AlignTolerance: 0, DuplicateTolerance: 0,
		Tips: []string{
			"Photos should overlap each other by about a third.",
			"They should all be taken from the same spot, looking outward.",
			"Order does not matter - the stitcher works it out from the photos.",
			"More photos give a smoother result; 8 or more works best.",
		},
	}
}

// PlanFor returns the shot list for a capture's mode and settings.
func PlanFor(mode string, count int, includeUpDown bool) Plan {
	switch mode {
	case ModeSpin:
		return SpinPlan(count)
	case ModeAuto:
		return AutoPlan()
	}
	// For pano, count is the total the user asked for; extras are offered
	// whenever they want more than the cardinals plus up/down.
	return PanoPlan(includeUpDown, count > 6)
}

// AngularDistance returns the angle in degrees between two look directions.
//
// This is what lets the server tell "you turned to face right" apart from "you
// shot the front again", which is the difference between a real 360 and four
// copies of the same wall.
func AngularDistance(yaw1, pitch1, yaw2, pitch2 float64) float64 {
	// Near the poles yaw stops meaning anything — straight up is straight up
	// whichever way you are facing — so compare pitch alone there.
	const poleCutoff = 60
	if math.Abs(pitch1) >= poleCutoff || math.Abs(pitch2) >= poleCutoff {
		return math.Abs(pitch1 - pitch2)
	}
	toRad := math.Pi / 180
	la1, la2 := pitch1*toRad, pitch2*toRad
	dLon := (yaw1 - yaw2) * toRad
	// Spherical law of cosines, clamped against floating-point drift.
	c := math.Sin(la1)*math.Sin(la2) + math.Cos(la1)*math.Cos(la2)*math.Cos(dLon)
	return math.Acos(math.Max(-1, math.Min(1, c))) / toRad
}
