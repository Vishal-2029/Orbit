package repo

import (
	"context"
	"strings"
	"testing"

	"github.com/vishal/orbit/backend/migrations"
)

// The migrations have to be IN the binary, not next to it. A container ships
// the binary alone, so a file read from disk at startup would work in
// development and fail in production - which is the shape of failure this whole
// mechanism exists to prevent.
func TestEveryMigrationIsEmbedded(t *testing.T) {
	entries, err := migrations.FS.ReadDir(".")
	if err != nil {
		t.Fatalf("migrations are not embedded: %v", err)
	}
	var sql []string
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".sql") {
			sql = append(sql, e.Name())
		}
	}
	if len(sql) < 3 {
		t.Fatalf("expected at least 001, 002 and 003; embedded: %v", sql)
	}
	// Named explicitly: 003 is the one that was missing in production, and a
	// new migration that does not get embedded fails exactly as silently.
	for _, want := range []string{"001_init.sql", "002_quaternion.sql", "003_hotspots.sql"} {
		found := false
		for _, got := range sql {
			if got == want {
				found = true
			}
		}
		if !found {
			t.Errorf("%s is not embedded; embedded: %v", want, sql)
		}
	}
}

// Applying them all on every boot only works because each one is safe to
// re-run. If a later migration is written without that, the server stops
// starting - and it will be this test that says why rather than a deploy.
func TestMigrationsAreSafeToReRun(t *testing.T) {
	entries, _ := migrations.FS.ReadDir(".")
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".sql") {
			continue
		}
		body, err := migrations.FS.ReadFile(e.Name())
		if err != nil {
			t.Fatalf("reading %s: %v", e.Name(), err)
		}
		text := strings.ToLower(string(body))
		for _, stmt := range []struct{ verb, guard string }{
			{"create table", "if not exists"},
			{"create index", "if not exists"},
			{"add column", "if not exists"},
			{"create extension", "if not exists"},
		} {
			idx := 0
			for {
				at := strings.Index(text[idx:], stmt.verb)
				if at < 0 {
					break
				}
				at += idx
				// The guard follows the verb within the same statement.
				end := strings.Index(text[at:], ";")
				if end < 0 {
					end = len(text) - at
				}
				if !strings.Contains(text[at:at+end], stmt.guard) {
					t.Errorf("%s: a %q without %q - re-running it on boot would fail",
						e.Name(), stmt.verb, stmt.guard)
				}
				idx = at + len(stmt.verb)
			}
		}
	}
}

// The real thing, against a live database: drop the table the way production
// had it, run Migrate, and check it comes back.
func TestMigrateCreatesTheSchema(t *testing.T) {
	r := liveRepo(t)
	ctx := context.Background()

	if _, err := r.pool.Exec(ctx, `DROP TABLE IF EXISTS hotspots CASCADE`); err != nil {
		t.Fatalf("dropping hotspots: %v", err)
	}
	var exists *string
	if err := r.pool.QueryRow(ctx,
		`SELECT to_regclass('public.hotspots')::text`).Scan(&exists); err != nil {
		t.Fatalf("checking: %v", err)
	}
	if exists != nil {
		t.Fatalf("the table should be gone before the test runs")
	}

	if err := r.Migrate(ctx); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	if err := r.pool.QueryRow(ctx,
		`SELECT to_regclass('public.hotspots')::text`).Scan(&exists); err != nil {
		t.Fatalf("checking after migrate: %v", err)
	}
	if exists == nil {
		t.Fatal("hotspots was not created - this is the production bug")
	}

	// And again, because it runs on every boot.
	if err := r.Migrate(ctx); err != nil {
		t.Fatalf("Migrate is not safe to re-run: %v", err)
	}
}
