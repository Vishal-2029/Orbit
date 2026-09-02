package repo

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/vishal/orbit/backend/internal/domain"
)

const testDSN = "postgres://orbit:orbit@localhost:5433/orbit?sslmode=disable"

func liveRepo(t *testing.T) *Repo {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	r, err := New(ctx, testDSN)
	if err != nil {
		t.Skipf("postgres unreachable, skipping: %v", err)
	}
	t.Cleanup(r.Close)
	return r
}

func newTestCapture(t *testing.T, r *Repo) *domain.Capture {
	t.Helper()
	c, err := r.CreateCapture(context.Background(), &domain.Capture{
		Title:      "Test Capture",
		Slug:       "test-" + uuid.NewString(),
		Mode:       domain.ModePano,
		Status:     domain.StatusDraft,
		FrameCount: 0,
		Settings:   domain.DefaultSettings(domain.ModePano),
		IsPublic:   true,
	})
	if err != nil {
		t.Fatalf("CreateCapture: %v", err)
	}
	t.Cleanup(func() {
		_ = r.DeleteCapture(context.Background(), c.ID)
	})
	return c
}

func TestCaptureCreateGetList(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()

	c := newTestCapture(t, r)
	if c.ID == "" {
		t.Fatalf("expected generated ID")
	}

	got, err := r.GetCapture(ctx, c.ID)
	if err != nil {
		t.Fatalf("GetCapture: %v", err)
	}
	if got.Slug != c.Slug || got.Title != c.Title {
		t.Fatalf("GetCapture mismatch: %+v vs %+v", got, c)
	}

	bySlug, err := r.GetCaptureBySlug(ctx, c.Slug)
	if err != nil {
		t.Fatalf("GetCaptureBySlug: %v", err)
	}
	if bySlug.ID != c.ID {
		t.Fatalf("GetCaptureBySlug id mismatch")
	}

	list, err := r.ListCaptures(ctx, 50, 0)
	if err != nil {
		t.Fatalf("ListCaptures: %v", err)
	}
	found := false
	for _, lc := range list {
		if lc.ID == c.ID {
			found = true
		}
	}
	if !found {
		t.Fatalf("created capture not present in ListCaptures")
	}
}

func TestCaptureGetNotFound(t *testing.T) {
	r := liveRepo(t)
	_, err := r.GetCapture(context.Background(), uuid.NewString())
	if err != ErrNotFound {
		t.Fatalf("expected ErrNotFound, got %v", err)
	}
}

func TestFrameUpsertIdempotent(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	c := newTestCapture(t, r)

	f1, err := r.UpsertFrame(ctx, &domain.Frame{
		CaptureID: c.ID, Index: 0, SlotID: "n", Yaw: 0, Pitch: 0,
		OriginalKey: "captures/x/original/000.jpg", Status: domain.FramePending,
	})
	if err != nil {
		t.Fatalf("UpsertFrame first: %v", err)
	}

	f2, err := r.UpsertFrame(ctx, &domain.Frame{
		CaptureID: c.ID, Index: 0, SlotID: "n", Yaw: 1.5, Pitch: 2.5,
		OriginalKey: "captures/x/original/000.jpg", Status: domain.FramePending,
	})
	if err != nil {
		t.Fatalf("UpsertFrame second: %v", err)
	}
	if f1.ID != f2.ID {
		t.Fatalf("upsert on same (capture_id, idx) created a new row: %s vs %s", f1.ID, f2.ID)
	}

	n, err := r.CountFrames(ctx, c.ID)
	if err != nil {
		t.Fatalf("CountFrames: %v", err)
	}
	if n != 1 {
		t.Fatalf("expected 1 frame row after double upsert, got %d", n)
	}

	frames, err := r.ListFrames(ctx, c.ID)
	if err != nil {
		t.Fatalf("ListFrames: %v", err)
	}
	if len(frames) != 1 || frames[0].Yaw != 1.5 {
		t.Fatalf("upsert did not update fields: %+v", frames)
	}
}

func TestMarkFrameDoneRecomputesProcessedCount(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	c := newTestCapture(t, r)

	var frameIDs []string
	for i := 0; i < 3; i++ {
		f, err := r.UpsertFrame(ctx, &domain.Frame{
			CaptureID: c.ID, Index: i, SlotID: "n", Yaw: 0, Pitch: 0,
			OriginalKey: "captures/x/original/000.jpg", Status: domain.FramePending,
		})
		if err != nil {
			t.Fatalf("UpsertFrame %d: %v", i, err)
		}
		frameIDs = append(frameIDs, f.ID)
	}

	for _, fid := range frameIDs[:2] {
		if err := r.MarkFrameDone(ctx, fid, "processed/key.jpg", "thumb/key.jpg", 100, 200); err != nil {
			t.Fatalf("MarkFrameDone: %v", err)
		}
	}

	got, err := r.GetCapture(ctx, c.ID)
	if err != nil {
		t.Fatalf("GetCapture: %v", err)
	}
	if got.ProcessedCount != 2 {
		t.Fatalf("expected processed_count 2, got %d", got.ProcessedCount)
	}

	// Retrying MarkFrameDone on an already-done frame must not double count.
	if err := r.MarkFrameDone(ctx, frameIDs[0], "processed/key.jpg", "thumb/key.jpg", 100, 200); err != nil {
		t.Fatalf("MarkFrameDone retry: %v", err)
	}
	got, err = r.GetCapture(ctx, c.ID)
	if err != nil {
		t.Fatalf("GetCapture after retry: %v", err)
	}
	if got.ProcessedCount != 2 {
		t.Fatalf("expected processed_count still 2 after retry, got %d", got.ProcessedCount)
	}
}

func TestCascadeDeleteRemovesFrames(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()
	c := newTestCapture(t, r)

	for i := 0; i < 3; i++ {
		if _, err := r.UpsertFrame(ctx, &domain.Frame{
			CaptureID: c.ID, Index: i, SlotID: "n", Yaw: 0, Pitch: 0,
			OriginalKey: "captures/x/original/000.jpg", Status: domain.FramePending,
		}); err != nil {
			t.Fatalf("UpsertFrame %d: %v", i, err)
		}
	}
	n, err := r.CountFrames(ctx, c.ID)
	if err != nil || n != 3 {
		t.Fatalf("expected 3 frames before delete, got %d err=%v", n, err)
	}

	if err := r.DeleteCapture(ctx, c.ID); err != nil {
		t.Fatalf("DeleteCapture: %v", err)
	}

	// Query frames directly via a raw pool since the capture row (and thus
	// CountFrames' normal caller context) is gone.
	pool, err := pgxpool.New(ctx, testDSN)
	if err != nil {
		t.Fatalf("pool: %v", err)
	}
	defer pool.Close()
	var count int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM frames WHERE capture_id=$1`, c.ID).Scan(&count); err != nil {
		t.Fatalf("count frames after cascade: %v", err)
	}
	if count != 0 {
		t.Fatalf("expected 0 frames after cascade delete, got %d", count)
	}
}

// A client asking for a malformed id must get a clean not-found, never a raw
// Postgres error. This was a real bug: the web client sent the literal string
// "undefined" and the user saw "SQLSTATE 22P02" on screen.
func TestMalformedUUIDIsNotFound(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()

	for _, bad := range []string{"undefined", "null", "", "not-a-uuid", "123"} {
		_, err := r.GetCapture(ctx, bad)
		if !errors.Is(err, ErrNotFound) {
			t.Errorf("GetCapture(%q) = %v, want ErrNotFound", bad, err)
		}
		if err != nil && strings.Contains(err.Error(), "SQLSTATE") {
			t.Errorf("GetCapture(%q) leaked a database error: %v", bad, err)
		}

		if _, err := r.ListFrames(ctx, bad); !errors.Is(err, ErrNotFound) {
			t.Errorf("ListFrames(%q) = %v, want ErrNotFound", bad, err)
		}
	}
}

// The reaper's query: a capture stuck mid-process with no progress must be
// found, and a healthy or finished one must not be.
func TestFindStuckCaptures(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()

	stale := func(id string, d time.Duration) {
		t.Helper()
		if _, err := r.pool.Exec(ctx,
			`UPDATE captures SET updated_at = now() - $2::interval WHERE id=$1`,
			id, d.String()); err != nil {
			t.Fatalf("aging capture: %v", err)
		}
	}
	has := func(list []domain.Capture, id string) bool {
		for _, c := range list {
			if c.ID == id {
				return true
			}
		}
		return false
	}

	// Stuck: processing, untouched for an hour.
	stuck := newTestCapture(t, r)
	if err := r.SetCaptureStatus(ctx, stuck.ID, domain.StatusProcessing, nil); err != nil {
		t.Fatal(err)
	}
	stale(stuck.ID, time.Hour)

	// Also stuck: queued and old.
	queued := newTestCapture(t, r)
	if err := r.SetCaptureStatus(ctx, queued.ID, domain.StatusQueued, nil); err != nil {
		t.Fatal(err)
	}
	stale(queued.ID, time.Hour)

	// Healthy: processing but updated seconds ago.
	fresh := newTestCapture(t, r)
	if err := r.SetCaptureStatus(ctx, fresh.ID, domain.StatusProcessing, nil); err != nil {
		t.Fatal(err)
	}

	// Finished: old, but nothing is waiting on it.
	done := newTestCapture(t, r)
	if err := r.SetCaptureStatus(ctx, done.ID, domain.StatusReady, nil); err != nil {
		t.Fatal(err)
	}
	stale(done.ID, time.Hour)

	// A draft the user simply never finished must not be touched either.
	draft := newTestCapture(t, r)
	stale(draft.ID, time.Hour)

	got, err := r.FindStuckCaptures(ctx, 10*time.Minute)
	if err != nil {
		t.Fatalf("FindStuckCaptures: %v", err)
	}

	if !has(got, stuck.ID) {
		t.Error("a capture stuck in 'processing' for an hour was not found")
	}
	if !has(got, queued.ID) {
		t.Error("a capture stuck in 'queued' for an hour was not found")
	}
	if has(got, fresh.ID) {
		t.Error("a capture that is still making progress must not be reaped")
	}
	if has(got, done.ID) {
		t.Error("a finished capture must not be reaped")
	}
	if has(got, draft.ID) {
		t.Error("an unfinished draft must not be reaped")
	}
}
