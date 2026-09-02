package storage

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"
)

const (
	testEndpoint = "localhost:9010"
	testAccess   = "orbitadmin"
	testSecret   = "orbitadmin123"
	testBucket   = "orbit-private"
)

func liveStore(t *testing.T) *MinIO {
	t.Helper()
	m, err := NewMinIO(testEndpoint, testAccess, testSecret, false, testBucket, "orbit-public")
	if err != nil {
		t.Skipf("minio unreachable, skipping: %v", err)
	}
	// Confirm we can actually talk to it.
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if _, err := m.Exists(ctx, testBucket, "healthcheck/does-not-exist"); err != nil {
		t.Skipf("minio unreachable, skipping: %v", err)
	}
	return m
}

func TestPutGetRoundTrip(t *testing.T) {
	m := liveStore(t)
	ctx := context.Background()
	key := fmt.Sprintf("test/%s/roundtrip.txt", uuid.NewString())
	body := []byte("hello orbit storage")

	if err := m.Put(ctx, testBucket, key, bytes.NewReader(body), int64(len(body)), "text/plain"); err != nil {
		t.Fatalf("Put: %v", err)
	}
	defer m.Delete(ctx, testBucket, key)

	got, err := m.GetBytes(ctx, testBucket, key)
	if err != nil {
		t.Fatalf("GetBytes: %v", err)
	}
	if !bytes.Equal(got, body) {
		t.Fatalf("round trip mismatch: got %q want %q", got, body)
	}

	rc, err := m.Get(ctx, testBucket, key)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer rc.Close()
	got2, err := io.ReadAll(rc)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if !bytes.Equal(got2, body) {
		t.Fatalf("Get round trip mismatch: got %q want %q", got2, body)
	}
}

func TestExistsMissingKeyIsFalseNotError(t *testing.T) {
	m := liveStore(t)
	ctx := context.Background()
	key := fmt.Sprintf("test/%s/nope.txt", uuid.NewString())

	ok, err := m.Exists(ctx, testBucket, key)
	if err != nil {
		t.Fatalf("Exists on missing key returned error: %v", err)
	}
	if ok {
		t.Fatalf("Exists on missing key returned true")
	}
}

func TestExistsPresentKey(t *testing.T) {
	m := liveStore(t)
	ctx := context.Background()
	key := fmt.Sprintf("test/%s/present.txt", uuid.NewString())
	body := []byte("x")
	if err := m.Put(ctx, testBucket, key, bytes.NewReader(body), int64(len(body)), "text/plain"); err != nil {
		t.Fatalf("Put: %v", err)
	}
	defer m.Delete(ctx, testBucket, key)

	ok, err := m.Exists(ctx, testBucket, key)
	if err != nil {
		t.Fatalf("Exists: %v", err)
	}
	if !ok {
		t.Fatalf("Exists on present key returned false")
	}
}

func TestDeletePrefixRemovesMany(t *testing.T) {
	m := liveStore(t)
	ctx := context.Background()
	prefix := fmt.Sprintf("test/%s/many/", uuid.NewString())

	for i := 0; i < 12; i++ {
		key := fmt.Sprintf("%sobj-%02d.txt", prefix, i)
		body := []byte(fmt.Sprintf("object %d", i))
		if err := m.Put(ctx, testBucket, key, bytes.NewReader(body), int64(len(body)), "text/plain"); err != nil {
			t.Fatalf("Put %d: %v", i, err)
		}
	}

	if err := m.Delete(ctx, testBucket, prefix); err != nil {
		t.Fatalf("Delete prefix: %v", err)
	}

	for i := 0; i < 12; i++ {
		key := fmt.Sprintf("%sobj-%02d.txt", prefix, i)
		ok, err := m.Exists(ctx, testBucket, key)
		if err != nil {
			t.Fatalf("Exists after delete %d: %v", i, err)
		}
		if ok {
			t.Fatalf("object %d still exists after prefix delete", i)
		}
	}
}

func TestPresignPutAcceptsRealUpload(t *testing.T) {
	m := liveStore(t)
	ctx := context.Background()
	key := fmt.Sprintf("test/%s/presigned.bin", uuid.NewString())
	body := []byte("presigned put payload")

	url, err := m.PresignPut(ctx, testBucket, key, 5*time.Minute)
	if err != nil {
		t.Fatalf("PresignPut: %v", err)
	}
	defer m.Delete(ctx, testBucket, key)

	req, err := http.NewRequest(http.MethodPut, url, bytes.NewReader(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.ContentLength = int64(len(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do presigned PUT: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("presigned PUT status %d: %s", resp.StatusCode, b)
	}

	got, err := m.GetBytes(ctx, testBucket, key)
	if err != nil {
		t.Fatalf("GetBytes after presigned put: %v", err)
	}
	if !bytes.Equal(got, body) {
		t.Fatalf("content mismatch after presigned put: got %q want %q", got, body)
	}
}
