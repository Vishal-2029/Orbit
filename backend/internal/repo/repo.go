// Package repo is the PostgreSQL persistence layer. Queries are hand-written
// against pgx rather than generated, so the whole data path stays readable.
package repo

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/vishal/orbit/backend/internal/domain"
)

var ErrNotFound = errors.New("not found")

// isBadUUID reports whether an error is Postgres rejecting a malformed uuid.
//
// A client asking for a nonsense id is a 404, not a server fault, and the raw
// "SQLSTATE 22P02" text must never reach a user.
func isBadUUID(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "22P02"
}

type Repo struct{ pool *pgxpool.Pool }

func New(ctx context.Context, dsn string) (*Repo, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping postgres: %w", err)
	}
	return &Repo{pool: pool}, nil
}

func (r *Repo) Close() { r.pool.Close() }

// nullIfEmpty keeps empty strings out of nullable text columns.
func nullIfEmpty(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// Pool exposes the underlying connection pool for callers (e.g. storagectl,
// the orphan sweeper) that need queries beyond the Repo's own methods.
func (r *Repo) Pool() *pgxpool.Pool { return r.pool }

// CountByStatus returns the number of captures per status value.
func (r *Repo) CountByStatus(ctx context.Context) (map[string]int, error) {
	rows, err := r.pool.Query(ctx, `SELECT status, count(*) FROM captures GROUP BY status`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int{}
	for rows.Next() {
		var status string
		var n int
		if err := rows.Scan(&status, &n); err != nil {
			return nil, err
		}
		out[status] = n
	}
	return out, rows.Err()
}

const captureCols = `id, user_id, title, slug, mode, status, frame_count, processed_count,
	settings, manifest, error, is_public, created_at, updated_at`

func scanCapture(row pgx.Row) (*domain.Capture, error) {
	var c domain.Capture
	var settingsRaw []byte
	var manifest []byte
	err := row.Scan(&c.ID, &c.UserID, &c.Title, &c.Slug, &c.Mode, &c.Status,
		&c.FrameCount, &c.ProcessedCount, &settingsRaw, &manifest, &c.Error,
		&c.IsPublic, &c.CreatedAt, &c.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) || isBadUUID(err) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if len(settingsRaw) > 0 {
		_ = json.Unmarshal(settingsRaw, &c.Settings)
	}
	if len(manifest) > 0 {
		c.Manifest = manifest
	}
	return &c, nil
}

func (r *Repo) CreateCapture(ctx context.Context, c *domain.Capture) (*domain.Capture, error) {
	s, err := json.Marshal(c.Settings)
	if err != nil {
		return nil, err
	}
	row := r.pool.QueryRow(ctx, `
		INSERT INTO captures (user_id, title, slug, mode, status, frame_count, settings, is_public)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING `+captureCols,
		c.UserID, c.Title, c.Slug, c.Mode, c.Status, c.FrameCount, s, c.IsPublic)
	return scanCapture(row)
}

func (r *Repo) GetCapture(ctx context.Context, id string) (*domain.Capture, error) {
	return scanCapture(r.pool.QueryRow(ctx, `SELECT `+captureCols+` FROM captures WHERE id=$1`, id))
}

func (r *Repo) GetCaptureBySlug(ctx context.Context, slug string) (*domain.Capture, error) {
	return scanCapture(r.pool.QueryRow(ctx, `SELECT `+captureCols+` FROM captures WHERE slug=$1`, slug))
}

func (r *Repo) ListCaptures(ctx context.Context, limit, offset int) ([]domain.Capture, error) {
	rows, err := r.pool.Query(ctx, `SELECT `+captureCols+`
		FROM captures ORDER BY created_at DESC LIMIT $1 OFFSET $2`, limit, offset)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []domain.Capture{}
	for rows.Next() {
		c, err := scanCapture(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *c)
	}
	return out, rows.Err()
}

// FindStuckCaptures returns captures that have been mid-process, with no
// progress at all, for longer than olderThan.
//
// updated_at moves every time a frame finishes, so this only matches captures
// that have genuinely stopped.
func (r *Repo) FindStuckCaptures(ctx context.Context, olderThan time.Duration) ([]domain.Capture, error) {
	rows, err := r.pool.Query(ctx, `SELECT `+captureCols+`
		FROM captures
		WHERE status IN ('queued','processing')
		  AND updated_at < now() - $1::interval
		ORDER BY updated_at`, olderThan.String())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []domain.Capture{}
	for rows.Next() {
		c, err := scanCapture(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *c)
	}
	return out, rows.Err()
}

func (r *Repo) SetCaptureStatus(ctx context.Context, id, status string, errMsg *string) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE captures SET status=$2, error=$3, updated_at=now() WHERE id=$1`, id, status, errMsg)
	return err
}

func (r *Repo) SetManifest(ctx context.Context, id string, m *domain.Manifest, status string) error {
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	_, err = r.pool.Exec(ctx,
		`UPDATE captures SET manifest=$2, status=$3, updated_at=now() WHERE id=$1`, id, b, status)
	return err
}

func (r *Repo) UpdateCaptureMeta(ctx context.Context, id, title string, isPublic bool) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE captures SET title=$2, is_public=$3, updated_at=now() WHERE id=$1`, id, title, isPublic)
	return err
}

func (r *Repo) DeleteCapture(ctx context.Context, id string) error {
	_, err := r.pool.Exec(ctx, `DELETE FROM captures WHERE id=$1`, id)
	return err
}

// --- frames ---

func (r *Repo) UpsertFrame(ctx context.Context, f *domain.Frame) (*domain.Frame, error) {
	var qx, qy, qz, qw *float64
	if f.Quat != nil {
		qx, qy, qz, qw = &f.Quat.X, &f.Quat.Y, &f.Quat.Z, &f.Quat.W
	}
	row := r.pool.QueryRow(ctx, `
		INSERT INTO frames (capture_id, idx, slot_id, yaw, pitch, original_key,
		                    width, height, status, qx, qy, qz, qw, orientation_source)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
		ON CONFLICT (capture_id, idx) DO UPDATE SET
			slot_id=EXCLUDED.slot_id, yaw=EXCLUDED.yaw, pitch=EXCLUDED.pitch,
			original_key=EXCLUDED.original_key, width=EXCLUDED.width,
			height=EXCLUDED.height, status=EXCLUDED.status, error=NULL,
			qx=EXCLUDED.qx, qy=EXCLUDED.qy, qz=EXCLUDED.qz, qw=EXCLUDED.qw,
			orientation_source=EXCLUDED.orientation_source
		RETURNING id`, f.CaptureID, f.Index, f.SlotID, f.Yaw, f.Pitch,
		f.OriginalKey, f.Width, f.Height, f.Status,
		qx, qy, qz, qw, nullIfEmpty(f.OrientationSource))
	if err := row.Scan(&f.ID); err != nil {
		return nil, err
	}
	return f, nil
}

func (r *Repo) ListFrames(ctx context.Context, captureID string) ([]domain.Frame, error) {
	rows, err := r.pool.Query(ctx, `
		SELECT id, capture_id, idx, COALESCE(slot_id,''), COALESCE(yaw,0), COALESCE(pitch,0),
		       original_key, COALESCE(processed_key,''), COALESCE(thumb_key,''),
		       COALESCE(width,0), COALESCE(height,0), status, error,
		       qx, qy, qz, qw, COALESCE(orientation_source,'')
		FROM frames WHERE capture_id=$1 ORDER BY idx`, captureID)
	if err != nil {
		if isBadUUID(err) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	defer rows.Close()
	out := []domain.Frame{}
	for rows.Next() {
		var f domain.Frame
		var qx, qy, qz, qw *float64
		if err := rows.Scan(&f.ID, &f.CaptureID, &f.Index, &f.SlotID, &f.Yaw, &f.Pitch,
			&f.OriginalKey, &f.ProcessedKey, &f.ThumbKey, &f.Width, &f.Height,
			&f.Status, &f.Error, &qx, &qy, &qz, &qw, &f.OrientationSource); err != nil {
			return nil, err
		}
		if qx != nil && qy != nil && qz != nil && qw != nil {
			f.Quat = &domain.Quaternion{X: *qx, Y: *qy, Z: *qz, W: *qw}
		}
		out = append(out, f)
	}
	// pgx surfaces a malformed-uuid parameter here rather than at Query time.
	if err := rows.Err(); err != nil {
		if isBadUUID(err) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return out, nil
}

func (r *Repo) CountFrames(ctx context.Context, captureID string) (int, error) {
	var n int
	err := r.pool.QueryRow(ctx, `SELECT count(*) FROM frames WHERE capture_id=$1`, captureID).Scan(&n)
	return n, err
}

// MarkFrameDone records a processed frame and atomically bumps the capture's
// processed_count, so progress can never drift from reality.
func (r *Repo) MarkFrameDone(ctx context.Context, frameID, processedKey, thumbKey string, w, h int) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	var captureID string
	err = tx.QueryRow(ctx, `
		UPDATE frames SET processed_key=$2, thumb_key=$3, width=$4, height=$5, status='done'
		WHERE id=$1 RETURNING capture_id`,
		frameID, processedKey, thumbKey, w, h).Scan(&captureID)
	if err != nil {
		return err
	}
	// Recount rather than increment: idempotent if a job is retried.
	if _, err := tx.Exec(ctx, `
		UPDATE captures SET processed_count =
			(SELECT count(*) FROM frames WHERE capture_id=$1 AND status='done'),
			updated_at=now() WHERE id=$1`, captureID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *Repo) MarkFrameFailed(ctx context.Context, frameID, reason string) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE frames SET status='failed', error=$2 WHERE id=$1`, frameID, reason)
	return err
}

// ResetForReprocess clears the results of a previous run so the same photos can
// be built again from scratch.
//
// Without this a retry inherits the old state: frames are still marked done, so
// processed_count recounts to full immediately and the progress bar sits at
// 100% from the first second, while the stale manifest keeps being served until
// finalize happens to overwrite it. The originals are untouched - they are what
// is being reprocessed.
func (r *Repo) ResetForReprocess(ctx context.Context, captureID string) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, `
		UPDATE frames SET status='pending', error=NULL, processed_key=NULL, thumb_key=NULL
		WHERE capture_id=$1`, captureID); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `
		UPDATE captures SET manifest=NULL, error=NULL, processed_count=0, updated_at=now()
		WHERE id=$1`, captureID); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *Repo) SetFrameCount(ctx context.Context, captureID string, n int) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE captures SET frame_count=$2, updated_at=now() WHERE id=$1`, captureID, n)
	return err
}
