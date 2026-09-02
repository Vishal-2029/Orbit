package storage

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/minio/minio-go/v7"
)

// Report summarizes what a sweep found (and, if apply was true, deleted).
type Report struct {
	Apply bool

	// Orphan objects: prefixes under captures/ in a bucket with no matching
	// row in the captures table.
	OrphanObjectPrefixes []string
	OrphanObjectBuckets  map[string]int // bucket -> count of orphan prefixes found/removed

	// Orphan capture rows: stuck in draft/uploading, older than the cutoff,
	// with zero frames.
	OrphanCaptureIDs []string
}

// SweepOrphans finds objects under captures/<id>/ in the given buckets that
// have no corresponding row in captures, and capture rows stuck in
// 'draft'/'uploading' for more than 24h with zero frames. When apply is
// false (the default via the CLI) nothing is deleted - the report only
// describes what would happen. Pass apply=true to actually delete.
func SweepOrphans(ctx context.Context, store Store, pool *pgxpool.Pool, buckets ...string) (Report, error) {
	return sweep(ctx, store, pool, false, buckets...)
}

// SweepOrphansApply is SweepOrphans but actually deletes what it finds.
func SweepOrphansApply(ctx context.Context, store Store, pool *pgxpool.Pool, buckets ...string) (Report, error) {
	return sweep(ctx, store, pool, true, buckets...)
}

func sweep(ctx context.Context, store Store, pool *pgxpool.Pool, apply bool, buckets ...string) (Report, error) {
	rep := Report{Apply: apply, OrphanObjectBuckets: map[string]int{}}

	// mc is only used for listing top-level capture-id prefixes, which the
	// generic Store interface doesn't expose. It's optional: if the store
	// isn't a *MinIO we just skip the object-orphan pass.
	mc, isMinIO := store.(*MinIO)

	// --- pass 1: orphan objects ---
	if isMinIO {
		known, err := knownCaptureIDs(ctx, pool)
		if err != nil {
			return rep, fmt.Errorf("load known capture ids: %w", err)
		}
		for _, bucket := range buckets {
			ids, err := listCaptureIDPrefixes(ctx, mc, bucket)
			if err != nil {
				return rep, fmt.Errorf("list objects in %s: %w", bucket, err)
			}
			for _, id := range ids {
				if known[id] {
					continue
				}
				prefix := CapturePrefix(id)
				rep.OrphanObjectPrefixes = append(rep.OrphanObjectPrefixes, bucket+"/"+prefix)
				rep.OrphanObjectBuckets[bucket]++
				if apply {
					if err := store.Delete(ctx, bucket, prefix); err != nil {
						return rep, fmt.Errorf("delete orphan %s/%s: %w", bucket, prefix, err)
					}
				}
			}
		}
	}

	// --- pass 2: stale draft/uploading captures with zero frames ---
	rows, err := pool.Query(ctx, `
		SELECT c.id FROM captures c
		WHERE c.status IN ('uploading','draft')
		  AND c.created_at < now() - interval '24 hours'
		  AND NOT EXISTS (SELECT 1 FROM frames f WHERE f.capture_id = c.id)`)
	if err != nil {
		return rep, fmt.Errorf("query stale captures: %w", err)
	}
	var staleIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return rep, err
		}
		staleIDs = append(staleIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return rep, err
	}
	rep.OrphanCaptureIDs = staleIDs

	if apply {
		for _, id := range staleIDs {
			if _, err := pool.Exec(ctx, `DELETE FROM captures WHERE id=$1`, id); err != nil {
				return rep, fmt.Errorf("delete stale capture %s: %w", id, err)
			}
			// Best-effort: also remove any objects that might exist for it.
			for _, bucket := range buckets {
				_ = store.Delete(ctx, bucket, CapturePrefix(id))
			}
		}
	}

	return rep, nil
}

func knownCaptureIDs(ctx context.Context, pool *pgxpool.Pool) (map[string]bool, error) {
	rows, err := pool.Query(ctx, `SELECT id FROM captures`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = true
	}
	return out, rows.Err()
}

// listCaptureIDPrefixes returns the distinct capture IDs that appear as the
// first path segment under "captures/" in a bucket, using delimiter listing
// so we only pay for one API call per level rather than enumerating every
// object.
func listCaptureIDPrefixes(ctx context.Context, mc *MinIO, bucket string) ([]string, error) {
	var ids []string
	for obj := range mc.c.ListObjects(ctx, bucket, minio.ListObjectsOptions{
		Prefix: "captures/", Recursive: false,
	}) {
		if obj.Err != nil {
			return nil, obj.Err
		}
		// obj.Key here is a "common prefix" like "captures/<id>/" because
		// Recursive is false.
		key := obj.Key
		if key == "" {
			continue
		}
		trimmed := strings.TrimPrefix(key, "captures/")
		trimmed = strings.TrimSuffix(trimmed, "/")
		if trimmed == "" {
			continue
		}
		ids = append(ids, trimmed)
	}
	return ids, nil
}

// staleCutoff is exposed for tests that want to reason about the 24h window.
var staleCutoff = 24 * time.Hour
