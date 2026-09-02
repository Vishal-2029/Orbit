package service

import (
	"context"
	"log"
	"time"

	"github.com/vishal/orbit/backend/internal/domain"
	"github.com/vishal/orbit/backend/internal/realtime"
)

// StuckAfter is how long a capture may sit in a working state with no progress
// before we declare it dead.
//
// updated_at is bumped every time a frame completes, so a slow but healthy
// capture keeps resetting the clock. Only a capture that has genuinely stopped
// moving trips this.
const StuckAfter = 10 * time.Minute

// ReapStuck fails captures that stopped making progress.
//
// The worker reclaims jobs abandoned by a process that died, which handles the
// normal case. This is the backstop for when that cannot work at all - no
// worker is running, the job was lost, the machine was rebooted - so a user is
// never left watching a progress bar that will never move again.
//
// Returns the number of captures failed.
func (s *Capture) ReapStuck(ctx context.Context, olderThan time.Duration) (int, error) {
	stuck, err := s.repo.FindStuckCaptures(ctx, olderThan)
	if err != nil {
		return 0, err
	}
	for _, c := range stuck {
		msg := "Building this 360 stopped unexpectedly and did not finish. " +
			"Your photos are safe - open the capture and press Build again to retry."
		if err := s.repo.SetCaptureStatus(ctx, c.ID, domain.StatusFailed, &msg); err != nil {
			log.Printf("reaper: could not fail capture %s: %v", c.ID, err)
			continue
		}
		log.Printf("reaper: capture %s was stuck in %q since %s; marked failed",
			c.ID, c.Status, c.UpdatedAt.Format(time.RFC3339))
		// Tell anyone still watching, so an open progress screen updates
		// instead of spinning forever.
		s.hub.Publish(ctx, realtime.Event{
			Type: "error", CaptureID: c.ID, Status: domain.StatusFailed,
			Message: msg, Progress: c.Progress(),
		})
	}
	return len(stuck), nil
}

// StartReaper runs ReapStuck on a ticker until the context is cancelled.
func (s *Capture) StartReaper(ctx context.Context, every, olderThan time.Duration) {
	go func() {
		t := time.NewTicker(every)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				n, err := s.ReapStuck(ctx, olderThan)
				if err != nil {
					log.Printf("reaper: %v", err)
				} else if n > 0 {
					log.Printf("reaper: failed %d stuck capture(s)", n)
				}
			}
		}
	}()
}
