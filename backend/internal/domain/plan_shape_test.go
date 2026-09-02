package domain

import (
	"fmt"
	"math"
	"testing"
)

// The whole point of the spacing rule: every direction must be photographed.
// Four shots 90 degrees apart with a 65 degree camera leaves a 25 degree wedge
// nobody ever saw, which shows up in the finished 360 as a blurred band.
func TestRingSpacingLeavesNoGaps(t *testing.T) {
	fmt.Printf("\n  gapless step: %.1f deg -> %d shots per ring\n",
		GaplessStep(), SlotsForFullCircle())

	for name, p := range map[string]Plan{
		"Quick 360": PanoPlan(true, false),
		"Full 360":  FullSpherePlan(0),
	} {
		fmt.Printf("  %-10s %d slots, %d required, step %.1f deg\n",
			name, len(p.Slots), p.MinRequired, p.YawStep)

		if p.YawStep > GaplessStep()+0.01 {
			t.Errorf("%s: step %.1f exceeds the gapless limit %.1f",
				name, p.YawStep, GaplessStep())
		}
		// Collect the horizon ring and check consecutive shots overlap.
		var yaws []float64
		for _, s := range p.Slots {
			if s.Pitch == 0 {
				yaws = append(yaws, s.Yaw)
			}
		}
		if len(yaws) < 6 {
			t.Errorf("%s: only %d shots on the horizon ring", name, len(yaws))
			continue
		}
		for i := range yaws {
			next := yaws[(i+1)%len(yaws)]
			gap := math.Mod(next-yaws[i]+360, 360)
			if gap > CameraHFOV {
				t.Errorf("%s: %.0f deg between shots at %.0f and %.0f, but the "+
					"camera only sees %.0f - that is a hole in the sphere",
					name, gap, yaws[i], next, CameraHFOV)
			}
		}
	}
}

func TestOverlapIsAtLeastAThird(t *testing.T) {
	overlap := CameraHFOV - GaplessStep()
	if overlap < CameraHFOV*OverlapFraction-0.01 {
		t.Errorf("overlap %.1f deg is under the %.0f%% rule", overlap, OverlapFraction*100)
	}
}
