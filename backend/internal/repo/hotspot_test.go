package repo

import (
	"context"
	"errors"
	"math"
	"testing"

	"github.com/google/uuid"

	"github.com/vishal/orbit/backend/internal/domain"
)

// readyCapture makes a capture the link graph will actually return. Only
// captures a viewer could open take part in a tour, so a draft one is invisible
// to LinkedCaptureIDs by design.
func readyCapture(t *testing.T, r *Repo, title string) *domain.Capture {
	t.Helper()
	ctx := context.Background()
	c, err := r.CreateCapture(ctx, &domain.Capture{
		Title:    title,
		Slug:     "test-" + uuid.NewString(),
		Mode:     domain.ModePano,
		Status:   domain.StatusReady,
		Settings: domain.DefaultSettings(domain.ModePano),
		IsPublic: true,
	})
	if err != nil {
		t.Fatalf("CreateCapture: %v", err)
	}
	t.Cleanup(func() { _ = r.DeleteCapture(ctx, c.ID) })
	return c
}

func TestHotspotCreateListUpdateDelete(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	c := readyCapture(t, r, "Living room")

	made, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: c.ID,
		Kind:      domain.HotspotInfo,
		Yaw:       1.2,
		Pitch:     -0.3,
		Title:     "The fireplace",
		Body:      "Original 1890s tiling.",
	})
	if err != nil {
		t.Fatalf("CreateHotspot: %v", err)
	}
	if made.ID == "" {
		t.Fatalf("expected a generated id")
	}
	if made.Title != "The fireplace" || made.Body != "Original 1890s tiling." {
		t.Fatalf("text did not round-trip: %+v", made)
	}

	list, err := r.ListHotspots(ctx, c.ID)
	if err != nil {
		t.Fatalf("ListHotspots: %v", err)
	}
	if len(list) != 1 || list[0].ID != made.ID {
		t.Fatalf("expected the one hotspot back, got %d", len(list))
	}

	made.Title = "The hearth"
	made.Yaw = -2.0
	updated, err := r.UpdateHotspot(ctx, made)
	if err != nil {
		t.Fatalf("UpdateHotspot: %v", err)
	}
	if updated.Title != "The hearth" || math.Abs(updated.Yaw+2.0) > 1e-9 {
		t.Fatalf("update did not stick: %+v", updated)
	}

	if err := r.DeleteHotspot(ctx, made.ID); err != nil {
		t.Fatalf("DeleteHotspot: %v", err)
	}
	if _, err := r.GetHotspot(ctx, made.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound after delete, got %v", err)
	}
	if err := r.DeleteHotspot(ctx, made.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleting twice should report not found, got %v", err)
	}
}

// An empty title is genuinely absent rather than the empty string, and the
// migration's CHECK constraints are written against NULL. If nullable() ever
// stopped doing its job, a link hotspot with no target would slip past them.
func TestHotspotEmptyStringsBecomeNull(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	c := readyCapture(t, r, "Hall")

	h, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: c.ID, Kind: domain.HotspotInfo, Title: "Just a title",
	})
	if err != nil {
		t.Fatalf("CreateHotspot: %v", err)
	}
	if h.Body != "" || h.TargetCaptureID != "" {
		t.Fatalf("absent fields should read back empty: %+v", h)
	}
}

func TestHotspotConstraintsAreEnforcedByTheDatabase(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	c := readyCapture(t, r, "Study")

	// The service validates all of this too. These check the database refuses
	// as well, because that is the guarantee that survives a new caller.
	if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: c.ID, Kind: domain.HotspotLink,
	}); err == nil {
		t.Fatalf("a link hotspot with no target should be refused")
	}

	if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: c.ID, Kind: domain.HotspotLink, TargetCaptureID: c.ID,
	}); err == nil {
		t.Fatalf("a link hotspot pointing at its own capture should be refused")
	}

	if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: c.ID, Kind: "arrow",
	}); err == nil {
		t.Fatalf("an unknown kind should be refused")
	}
}

func TestDeletingACaptureTakesItsHotspotsAndItsInboundLinks(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	from := readyCapture(t, r, "Corridor")
	to := readyCapture(t, r, "Kitchen")

	if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: from.ID, Kind: domain.HotspotLink, TargetCaptureID: to.ID,
	}); err != nil {
		t.Fatalf("CreateHotspot: %v", err)
	}

	// Deleting the TARGET must not leave an arrow pointing at nothing.
	if err := r.DeleteCapture(ctx, to.ID); err != nil {
		t.Fatalf("DeleteCapture: %v", err)
	}
	left, err := r.ListHotspots(ctx, from.ID)
	if err != nil {
		t.Fatalf("ListHotspots: %v", err)
	}
	if len(left) != 0 {
		t.Fatalf("the arrow should have gone with its target, %d left", len(left))
	}
}

func TestLinkedCaptureIDsWalksTheTour(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	hall := readyCapture(t, r, "Hall")
	kitchen := readyCapture(t, r, "Kitchen")
	garden := readyCapture(t, r, "Garden")

	link := func(from, to *domain.Capture) {
		t.Helper()
		if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
			CaptureID: from.ID, Kind: domain.HotspotLink, TargetCaptureID: to.ID,
		}); err != nil {
			t.Fatalf("CreateHotspot: %v", err)
		}
	}
	// hall -> kitchen -> garden, and kitchen back to hall. The cycle is the
	// point: a tour of a house is a graph, and a walk that cannot cope with one
	// runs forever.
	link(hall, kitchen)
	link(kitchen, garden)
	link(kitchen, hall)

	ids, err := r.LinkedCaptureIDs(ctx, hall.ID, 6)
	if err != nil {
		t.Fatalf("LinkedCaptureIDs: %v", err)
	}
	got := map[string]bool{}
	for _, id := range ids {
		got[id] = true
	}
	for _, want := range []*domain.Capture{hall, kitchen, garden} {
		if !got[want.ID] {
			t.Fatalf("%s missing from the tour: %v", want.Title, ids)
		}
	}
	if len(ids) != 3 {
		t.Fatalf("expected exactly 3 captures, got %d", len(ids))
	}
}

func TestLinkedCaptureIDsStopsAtTheDepthCap(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	a := readyCapture(t, r, "A")
	b := readyCapture(t, r, "B")
	cc := readyCapture(t, r, "C")

	for _, pair := range [][2]*domain.Capture{{a, b}, {b, cc}} {
		if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
			CaptureID: pair[0].ID, Kind: domain.HotspotLink, TargetCaptureID: pair[1].ID,
		}); err != nil {
			t.Fatalf("CreateHotspot: %v", err)
		}
	}

	ids, err := r.LinkedCaptureIDs(ctx, a.ID, 1)
	if err != nil {
		t.Fatalf("LinkedCaptureIDs: %v", err)
	}
	if len(ids) != 2 {
		t.Fatalf("depth 1 should reach A and B only, got %d: %v", len(ids), ids)
	}
	for _, id := range ids {
		if id == cc.ID {
			t.Fatalf("C is two hops away and should not be in a depth-1 walk")
		}
	}
}

// A capture still processing has no panorama. An arrow leading to one would
// open a black screen, so it is left out of the tour until it is finished.
func TestLinkedCaptureIDsSkipsUnfinishedCaptures(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	hall := readyCapture(t, r, "Hall")
	pending := readyCapture(t, r, "Still processing")
	if err := r.SetCaptureStatus(ctx, pending.ID, domain.StatusProcessing, nil); err != nil {
		t.Fatalf("SetCaptureStatus: %v", err)
	}
	if _, err := r.CreateHotspot(ctx, &domain.Hotspot{
		CaptureID: hall.ID, Kind: domain.HotspotLink, TargetCaptureID: pending.ID,
	}); err != nil {
		t.Fatalf("CreateHotspot: %v", err)
	}

	ids, err := r.LinkedCaptureIDs(ctx, hall.ID, 6)
	if err != nil {
		t.Fatalf("LinkedCaptureIDs: %v", err)
	}
	for _, id := range ids {
		if id == pending.ID {
			t.Fatalf("an unfinished capture should not join the tour")
		}
	}
}

func TestHotspotNotFoundForGarbageID(t *testing.T) {
	r := liveRepo(t)
	if _, err := r.GetHotspot(context.Background(), "not-a-uuid"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound for a malformed id, got %v", err)
	}
}
