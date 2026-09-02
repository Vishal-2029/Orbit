// Package storage wraps object storage. Everything the rest of the app needs
// is on the Store interface, so MinIO can be swapped for S3 or local disk.
package storage

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type Store interface {
	Put(ctx context.Context, bucket, key string, r io.Reader, size int64, contentType string) error
	Get(ctx context.Context, bucket, key string) (io.ReadCloser, error)
	GetBytes(ctx context.Context, bucket, key string) ([]byte, error)
	Exists(ctx context.Context, bucket, key string) (bool, error)
	Delete(ctx context.Context, bucket, prefix string) error
	PresignPut(ctx context.Context, bucket, key string, ttl time.Duration) (string, error)
}

type MinIO struct {
	c *minio.Client
}

func NewMinIO(endpoint, access, secret string, useSSL bool, buckets ...string) (*MinIO, error) {
	c, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(access, secret, ""),
		Secure: useSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("minio client: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	for _, b := range buckets {
		ok, err := c.BucketExists(ctx, b)
		if err != nil {
			return nil, fmt.Errorf("check bucket %s: %w", b, err)
		}
		if !ok {
			if err := c.MakeBucket(ctx, b, minio.MakeBucketOptions{}); err != nil {
				return nil, fmt.Errorf("create bucket %s: %w", b, err)
			}
		}
	}
	return &MinIO{c: c}, nil
}

// RawClient exposes the underlying minio-go client for callers (e.g.
// storagectl, cleanup) that need listing operations beyond the Store
// interface. Prefer the Store interface wherever it suffices.
func (m *MinIO) RawClient() *minio.Client { return m.c }

func (m *MinIO) Put(ctx context.Context, bucket, key string, r io.Reader, size int64, ct string) error {
	_, err := m.c.PutObject(ctx, bucket, key, r, size, minio.PutObjectOptions{ContentType: ct})
	return err
}

func (m *MinIO) Get(ctx context.Context, bucket, key string) (io.ReadCloser, error) {
	o, err := m.c.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	// GetObject is lazy; force an error now rather than at first Read.
	if _, err := o.Stat(); err != nil {
		o.Close()
		return nil, err
	}
	return o, nil
}

func (m *MinIO) GetBytes(ctx context.Context, bucket, key string) ([]byte, error) {
	rc, err := m.Get(ctx, bucket, key)
	if err != nil {
		return nil, err
	}
	defer rc.Close()
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, rc); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func (m *MinIO) Exists(ctx context.Context, bucket, key string) (bool, error) {
	_, err := m.c.StatObject(ctx, bucket, key, minio.StatObjectOptions{})
	if err != nil {
		if minio.ToErrorResponse(err).Code == "NoSuchKey" {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// Delete removes every object under a prefix. Used when a capture is deleted.
func (m *MinIO) Delete(ctx context.Context, bucket, prefix string) error {
	objs := m.c.ListObjects(ctx, bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true})
	for err := range m.c.RemoveObjects(ctx, bucket, objs, minio.RemoveObjectsOptions{}) {
		if err.Err != nil {
			return err.Err
		}
	}
	return nil
}

func (m *MinIO) PresignPut(ctx context.Context, bucket, key string, ttl time.Duration) (string, error) {
	u, err := m.c.PresignedPutObject(ctx, bucket, key, ttl)
	if err != nil {
		return "", err
	}
	return u.String(), nil
}

// Key helpers keep the storage layout in exactly one place.
func OriginalKey(captureID string, idx int) string {
	return fmt.Sprintf("captures/%s/original/%03d.jpg", captureID, idx)
}
func ProcessedKey(captureID string, idx int) string {
	return fmt.Sprintf("captures/%s/processed/%03d.jpg", captureID, idx)
}
func ThumbKey(captureID string, idx int) string {
	return fmt.Sprintf("captures/%s/thumb/%03d.jpg", captureID, idx)
}
func PanoramaKey(captureID string) string {
	return fmt.Sprintf("captures/%s/panorama.jpg", captureID)
}
func CapturePrefix(captureID string) string {
	return fmt.Sprintf("captures/%s/", captureID)
}
