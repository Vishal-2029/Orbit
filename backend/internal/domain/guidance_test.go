package domain

import (
	"math"
	"testing"
)

// The ring must start wherever the user is facing and step evenly all the way
// round. Cardinal names are still used where a shot lands near one, because
// "Right side" means more to a person than "Turn to 90 degrees".
func TestPanoRingStartsAtFrontAndStepsEvenly(t *testing.T) {
	p := PanoPlan(true, false)

	if p.Slots[0].Yaw != 0 {
		t.Errorf("first shot yaw = %v, want 0", p.Slots[0].Yaw)
	}
	if p.Slots[0].Label != "Front" {
		t.Errorf("first shot label = %q, want Front", p.Slots[0].Label)
	}

	var ring []Slot
	for _, s := range p.Slots {
		if s.Pitch == 0 {
			ring = append(ring, s)
		}
	}
	for i := 1; i < len(ring); i++ {
		if step := ring[i].Yaw - ring[i-1].Yaw; math.Abs(step-p.YawStep) > 0.01 {
			t.Errorf("shot %d is %.1f deg from the last, want %.1f", i, step, p.YawStep)
		}
	}
	names := map[string]bool{}
	for _, s := range ring {
		names[s.Label] = true
	}
	for _, want := range []string{"Front", "Right side", "Behind you", "Left side"} {
		if !names[want] {
			t.Errorf("no shot is labelled %q", want)
		}
	}
}

// Every ring shot is compulsory: skipping one leaves a wedge of the world
// unphotographed, which the finished 360 can only show as a blurred gap.
func TestEveryRingShotIsRequired(t *testing.T) {
	p := PanoPlan(true, false)
	if p.MinRequired != SlotsForFullCircle() {
		t.Errorf("MinRequired = %d, want %d (a full circle)",
			p.MinRequired, SlotsForFullCircle())
	}
	for _, s := range p.Slots {
		switch s.Group {
		case GroupCore:
			if !s.Required {
				t.Errorf("ring shot %q must be required", s.ID)
			}
		case GroupUpDown:
			if s.Required {
				t.Errorf("%q is ceiling or floor and must stay optional", s.ID)
			}
		}
	}
}

func TestPanoPlanCanOmitUpAndDown(t *testing.T) {
	n := SlotsForFullCircle()
	if got := len(PanoPlan(false, false).Slots); got != n {
		t.Errorf("bare plan has %d slots, want %d", got, n)
	}
	if got := len(PanoPlan(true, false).Slots); got != n+2 {
		t.Errorf("plan with up/down has %d slots, want %d", got, n+2)
	}
}

func TestEverySlotHasUserFacingText(t *testing.T) {
	seen := map[string]bool{}
	for _, s := range PanoPlan(true, true).Slots {
		if s.Label == "" || s.Hint == "" || s.Icon == "" {
			t.Errorf("slot %q is missing label/hint/icon", s.ID)
		}
		if seen[s.ID] {
			t.Errorf("duplicate slot id %q", s.ID)
		}
		seen[s.ID] = true
	}
}

func TestSpinPlanFullCircleNoRepeat(t *testing.T) {
	p := SpinPlan(24)
	if len(p.Slots) != 24 || p.Mode != ModeSpin {
		t.Fatalf("got %d slots mode %q", len(p.Slots), p.Mode)
	}
	if last := p.Slots[23].Yaw; math.Abs(last-345) > 1e-9 {
		t.Errorf("last yaw = %v, want 345 (must not repeat 0)", last)
	}
}

func TestSpinPlanClampsToFour(t *testing.T) {
	for _, n := range []int{-3, 0, 1, 3} {
		if got := len(SpinPlan(n).Slots); got != 4 {
			t.Errorf("SpinPlan(%d) gave %d slots, want 4", n, got)
		}
	}
}

func TestPlanForDispatchesOnMode(t *testing.T) {
	if PlanFor(ModeSpin, 12, true).Mode != ModeSpin {
		t.Error("PlanFor(spin) did not return a spin plan")
	}
	if PlanFor("nonsense", 6, true).Mode != ModePano {
		t.Error("unknown mode should fall back to pano")
	}
	if got := len(PlanFor(ModePano, 10, true).Slots); got < SlotsForFullCircle() {
		t.Errorf("a pano plan must cover a full circle, got %d slots", got)
	}
}

// AngularDistance is what stops four photos of the same wall being accepted as
// a 360, so its edge cases matter.
func TestAngularDistance(t *testing.T) {
	cases := []struct {
		name                 string
		y1, p1, y2, p2, want float64
	}{
		{"identical", 0, 0, 0, 0, 0},
		{"quarter turn", 0, 0, 90, 0, 90},
		{"opposite", 0, 0, 180, 0, 180},
		{"wraps around zero", 350, 0, 10, 0, 20},
		{"wraps the other way", 10, 0, 350, 0, 20},
		{"pitch only", 0, 0, 0, 45, 45},
		{"straight up vs straight up, different yaw", 0, 90, 180, 90, 0},
		{"horizon vs straight up", 0, 0, 0, 90, 90},
	}
	for _, c := range cases {
		got := AngularDistance(c.y1, c.p1, c.y2, c.p2)
		if math.Abs(got-c.want) > 0.5 {
			t.Errorf("%s: AngularDistance(%v,%v,%v,%v) = %.2f, want %.2f",
				c.name, c.y1, c.p1, c.y2, c.p2, got, c.want)
		}
	}
}

func TestAngularDistanceIsSymmetric(t *testing.T) {
	a := AngularDistance(37, 12, 200, -30)
	b := AngularDistance(200, -30, 37, 12)
	if math.Abs(a-b) > 1e-9 {
		t.Errorf("not symmetric: %v vs %v", a, b)
	}
}

// The whole point: a second "front" photo must be caught, a genuine "right"
// photo must not be.
func TestDuplicateToleranceSeparatesCardinals(t *testing.T) {
	p := PanoPlan(true, false)
	if d := AngularDistance(0, 0, 90, 0); d < p.DuplicateTolerance {
		t.Errorf("front vs right is %.0f°, must exceed tolerance %.0f°", d, p.DuplicateTolerance)
	}
	if d := AngularDistance(0, 0, 10, 0); d >= p.DuplicateTolerance {
		t.Errorf("front vs front+10° is %.0f°, must be under tolerance %.0f°", d, p.DuplicateTolerance)
	}
}

func TestProgressNeverDividesByZero(t *testing.T) {
	c := Capture{Status: StatusProcessing}
	if got := c.Progress(); got != 0 {
		t.Errorf("Progress() with zero frames = %d, want 0", got)
	}
}

func TestProgressReachesHundredOnlyWhenDone(t *testing.T) {
	mid := Capture{Status: StatusProcessing, FrameCount: 10, ProcessedCount: 10}
	if got := mid.Progress(); got != 80 {
		t.Errorf("frames done but not finalized = %d, want 80", got)
	}
	for _, st := range []string{StatusReady, StatusPartial} {
		if got := (Capture{Status: st, FrameCount: 10, ProcessedCount: 10}).Progress(); got != 100 {
			t.Errorf("Progress(%s) = %d, want 100", st, got)
		}
	}
}

// The full-sphere plan is the "dots everywhere" capture mode.
func TestFullSpherePlanCoversTheWholeSphere(t *testing.T) {
	p := FullSpherePlan(30)

	// The horizon ring is compulsory; the sky and floor rings are not.
	horizon := 0
	for _, s := range p.Slots {
		if s.Pitch == 0 {
			horizon++
		}
	}
	if p.MinRequired != horizon {
		t.Errorf("MinRequired = %d, want %d (the whole horizon ring)",
			p.MinRequired, horizon)
	}
	for _, s := range p.Slots {
		if s.Pitch == 0 && !s.Required {
			t.Errorf("horizon shot %q must be required", s.ID)
		}
		if s.Pitch != 0 && s.Required {
			t.Errorf("off-horizon shot %q must be optional", s.ID)
		}
	}

	// Every direction must be reachable: three pitch rings plus both poles.
	pitches := map[float64]int{}
	ids := map[string]bool{}
	for _, s := range p.Slots {
		pitches[s.Pitch]++
		if ids[s.ID] {
			t.Errorf("duplicate slot id %q", s.ID)
		}
		ids[s.ID] = true
		if s.Yaw < 0 || s.Yaw >= 360 {
			t.Errorf("slot %q has yaw %v outside [0,360)", s.ID, s.Yaw)
		}
		if s.Label == "" || s.Icon == "" {
			t.Errorf("slot %q has no user-facing text", s.ID)
		}
	}
	for _, want := range []float64{0, 45, -45, 90, -90} {
		if pitches[want] == 0 {
			t.Errorf("no slots at pitch %v", want)
		}
	}

	// Dots must be far enough apart that shooting one cannot be mistaken for
	// its neighbour, or the duplicate check would reject valid photos.
	if p.DuplicateTolerance >= p.YawStep {
		t.Errorf("DuplicateTolerance %v must be under the %v spacing",
			p.DuplicateTolerance, p.YawStep)
	}
}

func TestFullSpherePlanClampsSpacing(t *testing.T) {
	if got := FullSpherePlan(1).YawStep; got != 15 {
		t.Errorf("tiny spacing = %v, want clamped to 15", got)
	}
	if got := FullSpherePlan(400).YawStep; got > GaplessStep()+0.01 {
		t.Errorf("spacing %v exceeds the gapless limit %v", got, GaplessStep())
	}
}

func TestPlanForSphereMode(t *testing.T) {
	p := PlanFor(ModeSphere, 30, true)
	if p.Mode != ModePano {
		t.Errorf("sphere plan mode = %q, want pano (it is still a photosphere)", p.Mode)
	}
	if len(p.Slots) < 20 {
		t.Errorf("sphere plan has only %d slots", len(p.Slots))
	}
}
