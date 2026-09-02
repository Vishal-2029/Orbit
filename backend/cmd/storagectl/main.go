// Command storagectl is an operator CLI for inspecting and maintaining the
// Orbit object store and its database bookkeeping. Subcommands:
//
//	storagectl stats             count objects+bytes per bucket, captures by status
//	storagectl sweep [--apply]   run the orphan sweeper (dry-run unless --apply)
//	storagectl verify <capture>  confirm every frame's original object exists
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/minio/minio-go/v7"

	"github.com/vishal/orbit/backend/internal/config"
	"github.com/vishal/orbit/backend/internal/repo"
	"github.com/vishal/orbit/backend/internal/storage"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]

	cfg := config.Load()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	switch cmd {
	case "stats":
		runStats(ctx, cfg)
	case "sweep":
		fs := flag.NewFlagSet("sweep", flag.ExitOnError)
		apply := fs.Bool("apply", false, "actually delete orphans (default is dry-run)")
		_ = fs.Parse(os.Args[2:])
		runSweep(ctx, cfg, *apply)
	case "verify":
		if len(os.Args) < 3 {
			fmt.Fprintln(os.Stderr, "usage: storagectl verify <capture-id>")
			os.Exit(2)
		}
		runVerify(ctx, cfg, os.Args[2])
	default:
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  storagectl stats
  storagectl sweep [--apply]
  storagectl verify <capture-id>`)
}

func mustMinIO(cfg config.Config) *storage.MinIO {
	s, err := storage.NewMinIO(cfg.MinIOEndpoint, cfg.MinIOAccess, cfg.MinIOSecret,
		cfg.MinIOUseSSL, cfg.BucketPrivate, cfg.BucketPublic)
	if err != nil {
		fmt.Fprintf(os.Stderr, "minio: %v\n", err)
		os.Exit(1)
	}
	return s
}

func mustRepo(ctx context.Context, cfg config.Config) *repo.Repo {
	r, err := repo.New(ctx, cfg.DatabaseURL)
	if err != nil {
		fmt.Fprintf(os.Stderr, "postgres: %v\n", err)
		os.Exit(1)
	}
	return r
}

func runStats(ctx context.Context, cfg config.Config) {
	store := mustMinIO(cfg)
	r := mustRepo(ctx, cfg)
	defer r.Close()

	buckets := []string{cfg.BucketPrivate, cfg.BucketPublic}
	fmt.Println("=== object storage ===")
	for _, b := range buckets {
		count, size := 0, int64(0)
		for obj := range store.RawClient().ListObjects(ctx, b, minio.ListObjectsOptions{Recursive: true}) {
			if obj.Err != nil {
				fmt.Fprintf(os.Stderr, "list %s: %v\n", b, obj.Err)
				os.Exit(1)
			}
			count++
			size += obj.Size
		}
		fmt.Printf("%-16s %8d objects  %12d bytes (%.2f MB)\n", b, count, size, float64(size)/1e6)
	}

	fmt.Println("\n=== captures by status ===")
	rows, err := r.CountByStatus(ctx)
	if err != nil {
		fmt.Fprintf(os.Stderr, "count by status: %v\n", err)
		os.Exit(1)
	}
	for status, n := range rows {
		fmt.Printf("%-12s %6d\n", status, n)
	}
}

func runSweep(ctx context.Context, cfg config.Config, apply bool) {
	store := mustMinIO(cfg)
	r := mustRepo(ctx, cfg)
	defer r.Close()

	var (
		rep storage.Report
		err error
	)
	if apply {
		rep, err = storage.SweepOrphansApply(ctx, store, r.Pool(), cfg.BucketPrivate, cfg.BucketPublic)
	} else {
		rep, err = storage.SweepOrphans(ctx, store, r.Pool(), cfg.BucketPrivate, cfg.BucketPublic)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "sweep: %v\n", err)
		os.Exit(1)
	}

	mode := "DRY RUN"
	if apply {
		mode = "APPLIED"
	}
	fmt.Printf("=== orphan sweep (%s) ===\n", mode)
	fmt.Printf("orphan object prefixes: %d\n", len(rep.OrphanObjectPrefixes))
	for _, p := range rep.OrphanObjectPrefixes {
		fmt.Printf("  %s\n", p)
	}
	fmt.Printf("orphan (stale) capture rows: %d\n", len(rep.OrphanCaptureIDs))
	for _, id := range rep.OrphanCaptureIDs {
		fmt.Printf("  %s\n", id)
	}
	if !apply && (len(rep.OrphanObjectPrefixes) > 0 || len(rep.OrphanCaptureIDs) > 0) {
		fmt.Println("\nre-run with --apply to delete the above")
	}
}

func runVerify(ctx context.Context, cfg config.Config, captureID string) {
	store := mustMinIO(cfg)
	r := mustRepo(ctx, cfg)
	defer r.Close()

	c, err := r.GetCapture(ctx, captureID)
	if err != nil {
		fmt.Fprintf(os.Stderr, "get capture: %v\n", err)
		os.Exit(1)
	}
	frames, err := r.ListFrames(ctx, captureID)
	if err != nil {
		fmt.Fprintf(os.Stderr, "list frames: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("capture %s (%s) status=%s frames=%d\n", c.ID, c.Slug, c.Status, len(frames))

	missing := 0
	for _, f := range frames {
		if f.OriginalKey == "" {
			fmt.Printf("  frame idx=%d id=%s has no original_key recorded\n", f.Index, f.ID)
			missing++
			continue
		}
		ok, err := store.Exists(ctx, cfg.BucketPrivate, f.OriginalKey)
		if err != nil {
			fmt.Fprintf(os.Stderr, "  frame idx=%d: exists check failed: %v\n", f.Index, err)
			missing++
			continue
		}
		if !ok {
			fmt.Printf("  MISSING: frame idx=%d id=%s key=%s\n", f.Index, f.ID, f.OriginalKey)
			missing++
		}
	}
	if missing == 0 {
		fmt.Println("all frame originals present")
	} else {
		fmt.Printf("%d frame(s) missing their original object\n", missing)
		os.Exit(1)
	}
}
