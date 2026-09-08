package repo

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strings"

	"github.com/vishal/orbit/backend/migrations"
)

// Migrate applies every migration the binary carries, in filename order.
//
// This runs at startup because the alternative did not work. Migrations used to
// be a step in a deploy document - "run psql with this file" - and the hotspots
// table simply never got created in production. The code shipped, the endpoints
// were live, and every request for a hotspot returned
//
//	relation "hotspots" does not exist
//
// with nothing in the deploy to suggest a step had been skipped. A schema the
// server needs is not documentation; it is part of the server.
//
// Every migration is written to be safe to re-run - CREATE TABLE IF NOT EXISTS,
// ADD COLUMN IF NOT EXISTS - so applying them all on every boot is a few
// milliseconds and needs no bookkeeping table. That holds only as long as new
// migrations keep to the same rule, which is why it is stated here.
func (r *Repo) Migrate(ctx context.Context) error {
	entries, err := migrations.FS.ReadDir(".")
	if err != nil {
		return fmt.Errorf("reading migrations: %w", err)
	}

	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	// Filename order is the numbering: 001 before 002 before 003. Later
	// migrations depend on earlier ones having run.
	sort.Strings(names)

	for _, name := range names {
		body, err := migrations.FS.ReadFile(name)
		if err != nil {
			return fmt.Errorf("reading %s: %w", name, err)
		}
		if _, err := r.pool.Exec(ctx, string(body)); err != nil {
			return fmt.Errorf("applying %s: %w", name, err)
		}
	}

	log.Printf("[orbit-api] schema up to date (%d migrations: %s)",
		len(names), strings.Join(names, ", "))
	return nil
}
