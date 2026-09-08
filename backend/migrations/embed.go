// Package migrations carries the database schema as files compiled into the
// binary.
//
// The .sql files stay here, at the path the Makefile, docker-compose and the
// deploy docs already name, rather than moving inside internal/repo - a
// go:embed cannot reach a parent directory, so the embed comes to the files.
package migrations

import "embed"

// FS holds every migration, applied in filename order by repo.Migrate.
//
//go:embed *.sql
var FS embed.FS
