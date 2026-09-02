package domain

import (
	"fmt"
	"math"
)

// CameraHFOV is the horizontal field of view we assume a phone has, in degrees,
// when held upright. Phones vary between roughly 60 and 70.
const CameraHFOV = 65.0

// OverlapFraction is how much of each photo must also appear in its neighbour.
// A third is the standard rule: enough for a stitcher to find common detail,
// without wasting shots on redundant coverage.
const OverlapFraction = 1.0 / 3.0

// GaplessStep is the largest angle you may turn between shots and still have
// every direction photographed. Turn further and you leave a wedge of the world
// that no photo covers - which appears in the finished 360 as a blurred band,
// because there is genuinely nothing there to show.
//
//	65 degrees of view, a third of it overlapping  ->  turn about 43 degrees
func GaplessStep() float64 {
	return CameraHFOV * (1 - OverlapFraction)
}

// SlotsForFullCircle is how many shots one level ring needs.
func SlotsForFullCircle() int {
	return int(math.Ceil(360.0 / GaplessStep()))
}

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

// FullSpherePlan covers the whole sphere with dots, the way Street View's
// capture does, instead of offering a handful of fixed directions.
//
// Three rings plus the two poles. The rings are spaced so a phone's ~65 degree
// view overlaps its neighbours by roughly a third in both directions, which is
// what a stitcher needs. Only the four cardinals on the horizon are required;
// everything else is there to be filled in if the user wants a fuller sphere.
func FullSpherePlan(step float64) Plan {
	// Never allow a spacing that would leave gaps, whatever is asked for.
	maxStep := GaplessStep()
	if step <= 0 || step > maxStep {
		step = maxStep
	}
	if step < 15 {
		step = 15
	}
	// Rings are spaced by the same rule applied to the vertical field of view.
	// A phone held upright sees far more vertically than horizontally, so two
	// rings either side of the horizon reach the poles with overlap to spare.
	rings := []struct {
		pitch float64
		label string
	}{
		{0, "Around you"},
		{45, "Upper"},
		{-45, "Lower"},
	}

	slots := make([]Slot, 0, 40)
	add := func(sl Slot, group string, required bool) {
		sl.Index = len(slots)
		sl.Group = group
		sl.Required = required
		slots = append(slots, sl)
	}

	for _, ring := range rings {
		ringStep := step
		if ring.pitch != 0 {
			// Circles of latitude are shorter, so the same arc covers more of
			// them. Widening the spacing keeps the shot count sane without
			// opening gaps.
			ringStep = step / math.Cos(ring.pitch*math.Pi/180)
			if ringStep > 90 {
				ringStep = 90
			}
		}
		for yaw := 0.0; yaw < 360; yaw += ringStep {
			id := fmt.Sprintf("r%+.0f_%03.0f", ring.pitch, yaw)
			label, icon, hint := ring.label, "•", "Line the dot up with the ring and hold steady."
			if ring.pitch == 0 {
				label, icon, hint = ringLabelFor(yaw, 0, ringStep)
			} else {
				label = fmt.Sprintf("%s · %.0f°", ring.label, yaw)
			}
			// The horizon ring is what actually makes the 360; the upper and
			// lower rings fill in the sky and floor.
			add(Slot{ID: id, Icon: icon, Yaw: yaw, Pitch: ring.pitch,
				Label: label, Hint: hint},
				map[bool]string{true: GroupCore, false: GroupExtra}[ring.pitch == 0],
				ring.pitch == 0)
		}
	}

	for _, s := range upDown {
		add(s, GroupUpDown, false)
	}

	required := 0
	for _, sl := range slots {
		if sl.Required {
			required++
		}
	}
	return Plan{
		Mode: ModePano, Slots: slots,
		MinRequired: required, YawStep: step,
		AlignTolerance: 10, DuplicateTolerance: step * 0.6,
		Tips: []string{
			"Stand still and turn on the spot. Do not walk in a circle.",
			fmt.Sprintf("Turn about %.0f degrees between shots, every time.", step),
			"Finish the bright ring first - that is the 360 itself.",
			"The dimmer dots above and below fill in the ceiling and floor.",
		},
	}
}

// PanoPlan builds the shot list for a stand-in-one-place 360.
//
// The ring is spaced at GaplessStep, not at the four cardinal directions.
// Four shots 90 degrees apart leaves a 25 degree wedge between each pair that
// no camera ever saw, so a "360" built from them is a quarter empty.
func PanoPlan(includeUpDown, includeExtras bool) Plan {
	n := SlotsForFullCircle()
	step := 360.0 / float64(n)

	slots := make([]Slot, 0, n+2)
	add := func(s Slot, group string, required bool) {
		s.Index = len(slots)
		s.Group = group
		s.Required = required
		slots = append(slots, s)
	}

	for i := 0; i < n; i++ {
		yaw := float64(i) * step
		label, icon, hint := ringLabelFor(yaw, i, step)
		add(Slot{ID: fmt.Sprintf("ring_%02d", i), Label: label, Icon: icon,
			Hint: hint, Yaw: yaw}, GroupCore, true)
	}
	if includeUpDown {
		for _, s := range upDown {
			add(s, GroupUpDown, false)
		}
	}
	return Plan{
		Mode: ModePano, Slots: slots,
		// Every ring shot is required: skipping one puts a hole in the result.
		MinRequired: n, YawStep: step,
		AlignTolerance: 10, DuplicateTolerance: step * 0.6,
		Tips: []string{
			"Stand still and turn on the spot. Do not walk in a circle.",
			fmt.Sprintf("Turn about %.0f degrees between shots - roughly one phone-width of new view.", step),
			"Every dot matters: a skipped one leaves a blurred gap in the result.",
			"Hold the phone upright and keep the horizon on the centre line.",
		},
	}
}

// ringLabelFor gives the cardinal directions their plain names and describes
// the rest by where they sit between them.
func ringLabelFor(yaw float64, i int, step float64) (label, icon, hint string) {
	switch {
	case yaw <= step/2 || yaw >= 360-step/2:
		return "Front", "▲", "Point at whatever is in front of you. This becomes your starting direction."
	case math.Abs(yaw-90) <= step/2:
		return "Right side", "▶", "You are now a quarter turn to your right."
	case math.Abs(yaw-180) <= step/2:
		return "Behind you", "▼", "You are now looking at the opposite of where you started."
	case math.Abs(yaw-270) <= step/2:
		return "Left side", "◀", "One more quarter turn brings you back to the front."
	}
	return fmt.Sprintf("Turn to %.0f°", yaw), "•",
		fmt.Sprintf("Keep turning the same way, about %.0f degrees more.", step)
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
	case ModeSphere:
		return FullSpherePlan(float64(count))
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
