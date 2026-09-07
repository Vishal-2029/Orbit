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

// --------------------------------------------------------------------------
// Hotspots
// --------------------------------------------------------------------------

const hotspotCols = `id, capture_id, kind, yaw, pitch, title, body,
	target_capture_id, rotation, created_at, updated_at`

func scanHotspot(row pgx.Row) (*domain.Hotspot, error) {
	var h domain.Hotspot
	var title, body, target *string
	err := row.Scan(&h.ID, &h.CaptureID, &h.Kind, &h.Yaw, &h.Pitch,
		&title, &body, &target, &h.Rotation, &h.CreatedAt, &h.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) || isBadUUID(err) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	if title != nil {
		h.Title = *title
	}
	if body != nil {
		h.Body = *body
	}
	if target != nil {
		h.TargetCaptureID = *target
	}
	return &h, nil
}

// nullable turns an empty string into a SQL NULL. The columns are nullable
// because "an info hotspot has no target" is genuinely absent rather than
// empty, and the CHECK constraints in the migration are written against NULL.
func nullable(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func (r *Repo) CreateHotspot(ctx context.Context, h *domain.Hotspot) (*domain.Hotspot, error) {
	row := r.pool.QueryRow(ctx, `
		INSERT INTO hotspots (capture_id, kind, yaw, pitch, title, body, target_capture_id, rotation)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING `+hotspotCols,
		h.CaptureID, h.Kind, h.Yaw, h.Pitch, nullable(h.Title), nullable(h.Body),
		nullable(h.TargetCaptureID), h.Rotation)
	return scanHotspot(row)
}

func (r *Repo) GetHotspot(ctx context.Context, id string) (*domain.Hotspot, error) {
	return scanHotspot(r.pool.QueryRow(ctx,
		`SELECT `+hotspotCols+` FROM hotspots WHERE id=$1`, id))
}

// ListHotspots returns one capture's hotspots, oldest first so the order a user
// placed them in is the order they come back in.
func (r *Repo) ListHotspots(ctx context.Context, captureID string) ([]domain.Hotspot, error) {
	rows, err := r.pool.Query(ctx, `SELECT `+hotspotCols+`
		FROM hotspots WHERE capture_id=$1 ORDER BY created_at`, captureID)
	if err != nil {
		if isBadUUID(err) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	defer rows.Close()
	out := []domain.Hotspot{}
	for rows.Next() {
		h, err := scanHotspot(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *h)
	}
	return out, rows.Err()
}

// UpdateHotspot replaces the editable fields. Kind and capture_id are not among
// them: changing either turns the hotspot into a different thing, and deleting
// and re-creating is both clearer to read and easier to undo.
func (r *Repo) UpdateHotspot(ctx context.Context, h *domain.Hotspot) (*domain.Hotspot, error) {
	row := r.pool.QueryRow(ctx, `
		UPDATE hotspots
		SET yaw=$2, pitch=$3, title=$4, body=$5, target_capture_id=$6,
		    rotation=$7, updated_at=now()
		WHERE id=$1 RETURNING `+hotspotCols,
		h.ID, h.Yaw, h.Pitch, nullable(h.Title), nullable(h.Body),
		nullable(h.TargetCaptureID), h.Rotation)
	return scanHotspot(row)
}

func (r *Repo) DeleteHotspot(ctx context.Context, id string) error {
	tag, err := r.pool.Exec(ctx, `DELETE FROM hotspots WHERE id=$1`, id)
	if err != nil {
		if isBadUUID(err) {
			return ErrNotFound
		}
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// LinkedCaptureIDs walks the link graph outward and returns every capture
// reachable from this one, including the starting point.
//
// The viewer needs them all up front to build its scenes. A recursive CTE
// rather than a loop of queries because a tour of a house is a dozen rooms
// linking to each other, and doing that one round trip at a time against a
// hosted database is the difference between a viewer that opens and one that
// stutters.
//
// Depth is capped so a cycle in the data cannot walk forever, and because a
// viewer that silently prepares eighty panoramas is its own kind of bug.
func (r *Repo) LinkedCaptureIDs(ctx context.Context, captureID string, maxDepth int) ([]string, error) {
	if maxDepth < 1 {
		maxDepth = 1
	}
	rows, err := r.pool.Query(ctx, `
		WITH RECURSIVE reachable(id, depth) AS (
			SELECT $1::uuid, 0
			UNION
			SELECT h.target_capture_id, r.depth + 1
			FROM reachable r
			JOIN hotspots h ON h.capture_id = r.id
			WHERE h.target_capture_id IS NOT NULL AND r.depth < $2
		)
		SELECT DISTINCT reachable.id
		FROM reachable
		JOIN captures c ON c.id = reachable.id
		-- Only scenes the viewer could actually render. A capture still
		-- processing has no panorama, and an arrow leading to one would open a
		-- black screen.
		WHERE c.status IN ('ready', 'partial')`, captureID, maxDepth)
	if err != nil {
		if isBadUUID(err) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}
