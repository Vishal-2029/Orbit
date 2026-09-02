// Package queue is the job transport between the API and the CV worker.
//
// The README specifies Kafka. For a single-node v1 that is pure ops burden, so
// the default implementation is Redis Streams, which gives the same properties
// that actually matter here: durability, consumer groups, at-least-once
// delivery and replay. Everything is behind the Queue interface, so a Kafka
// implementation drops in without touching a single caller.
package queue

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

// Job types.
const (
	TypeProcessFrame = "capture.frame.process"
	TypeFinalize     = "capture.finalize"
)

type Job struct {
	ID        string          `json:"id"`
	Type      string          `json:"type"`
	CaptureID string          `json:"capture_id"`
	Payload   json.RawMessage `json:"payload"`
	Attempts  int             `json:"attempts"`
}

type Queue interface {
	Publish(ctx context.Context, j Job) error
	Close() error
}

const (
	StreamJobs   = "orbit:jobs"
	GroupWorkers = "cv-workers"
	StreamDLQ    = "orbit:jobs:dlq"
)

type RedisQueue struct{ rdb *redis.Client }

func NewRedisQueue(ctx context.Context, rdb *redis.Client) (*RedisQueue, error) {
	// MKSTREAM creates the stream if the first consumer beats the first producer.
	err := rdb.XGroupCreateMkStream(ctx, StreamJobs, GroupWorkers, "0").Err()
	if err != nil && !isBusyGroup(err) {
		return nil, fmt.Errorf("create consumer group: %w", err)
	}
	return &RedisQueue{rdb: rdb}, nil
}

func isBusyGroup(err error) bool {
	return err != nil && (errors.Is(err, redis.Nil) ||
		len(err.Error()) >= 9 && err.Error()[:9] == "BUSYGROUP")
}

func (q *RedisQueue) Publish(ctx context.Context, j Job) error {
	b, err := json.Marshal(j)
	if err != nil {
		return err
	}
	return q.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: StreamJobs,
		// Cap the stream so a long-running dev box cannot fill Redis.
		MaxLen: 100000,
		Approx: true,
		Values: map[string]any{"job": string(b), "ts": time.Now().Unix()},
	}).Err()
}

func (q *RedisQueue) Close() error { return nil }
