package domain

import (
	"math"
	"testing"
)

// The order the user shoots in is the product decision this test protects:
// front, right, behind, left — then the optional extras.
func TestPanoPlanCardinalOrder(t *testing.T) {
	p := PanoPlan(true, false)
	want := []struct {
		id  string
		yaw float64
	}{
		{"front", 0}, {"right", 90}, {"back", 180}, {"left", 270},
	}
	for i, w := range want {
		got := p.Slots[i]
		if got.ID != w.id {
			t.Errorf("slot %d is %q, want %q", i, got.ID, w.id)
		}
		if got.Yaw != w.yaw {
			t.Errorf("slot %s yaw = %v, want %v", got.ID, got.Yaw, w.yaw)
		}
		if !got.Required {
			t.Errorf("slot %s must be required", got.ID)
		}
		if got.Index != i {
			t.Errorf("slot %s has Index %d, want %d", got.ID, got.Index, i)
		}
	}
}

func TestPanoMinimumIsFourNotEight(t *testing.T) {
	p := PanoPlan(true, true)
	if p.MinRequired != 4 {
		t.Errorf("MinRequired = %d, want 4", p.MinRequired)
	}
	if got := len(p.RequiredSlots()); got != 4 {
		t.Errorf("required slots = %d, want 4", got)
	}
}

func TestUpDownAndExtrasAreOptionalAndComeAfterTheRing(t *testing.T) {
	p := PanoPlan(true, true)
	if len(p.Slots) != 10 {
		t.Fatalf("slot count = %d, want 10 (4 cardinal + up/down + 4 extra)", len(p.Slots))
	}
	for i, s := range p.Slots {
		switch {
		case i < 4:
			if s.Group != GroupCore {
				t.Errorf("slot %d group = %q, want core", i, s.Group)
			}
		case i < 6:
			if s.Group != GroupUpDown || s.Required {
				t.Errorf("slot %s should be an optional up/down slot", s.ID)
			}
		default:
			if s.Group != GroupExtra || s.Required {
				t.Errorf("slot %s should be an optional extra", s.ID)
			}
		}
	}
	if p.Slots[4].Pitch != 90 {
		t.Errorf("up pitch = %v, want 90", p.Slots[4].Pitch)
	}
	if p.Slots[5].Pitch != -90 {
		t.Errorf("down pitch = %v, want -90", p.Slots[5].Pitch)
	}
}

func TestPanoPlanCanOmitOptionalGroups(t *testing.T) {
	if got := len(PanoPlan(false, false).Slots); got != 4 {
		t.Errorf("bare plan has %d slots, want 4", got)
	}
	if got := len(PanoPlan(true, false).Slots); got != 6 {
		t.Errorf("plan with up/down has %d slots, want 6", got)
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
	if got := len(PlanFor(ModePano, 10, true).Slots); got != 10 {
		t.Errorf("asking for 10 should include the extras, got %d slots", got)
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

	if p.MinRequired != 4 {
		t.Errorf("MinRequired = %d, want 4 - only the cardinals are compulsory", p.MinRequired)
	}
	// 4 cardinals + 8 remaining horizon + 12 upper + 12 lower + up + down
	if len(p.Slots) != 38 {
		t.Fatalf("slot count = %d, want 38", len(p.Slots))
	}

	// The four cardinals must lead, so stopping early still gives an even ring.
	for i, want := range []string{"front", "right", "back", "left"} {
		if p.Slots[i].ID != want {
			t.Errorf("slot %d is %q, want %q", i, p.Slots[i].ID, want)
		}
		if !p.Slots[i].Required {
			t.Errorf("cardinal %q must be required", want)
		}
	}
	for _, s := range p.Slots[4:] {
		if s.Required {
			t.Errorf("slot %q beyond the cardinals must be optional", s.ID)
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
	for _, want := range []float64{0, 40, -40, 90, -90} {
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
	if got := FullSpherePlan(400).YawStep; got != 90 {
		t.Errorf("huge spacing = %v, want clamped to 90", got)
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
