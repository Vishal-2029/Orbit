// Package realtime pushes live processing progress to connected clients.
//
// Local subscribers are held in a map; a Redis Pub/Sub bridge relays events
// between API instances so any pod can serve any client's socket.
package realtime

import (
	"context"
	"encoding/json"
	"log"
	"sync"

	"github.com/redis/go-redis/v9"
)

type Event struct {
	Type      string `json:"type"` // status | frame_done | ready | error
	CaptureID string `json:"capture_id"`
	Status    string `json:"status,omitempty"`
	Index     int    `json:"index,omitempty"`
	Processed int    `json:"processed,omitempty"`
	Total     int    `json:"total,omitempty"`
	Progress  int    `json:"progress"`
	Message   string `json:"message,omitempty"`
	Manifest  any    `json:"manifest,omitempty"`
}

type Hub struct {
	mu   sync.RWMutex
	subs map[string]map[chan Event]struct{} // captureID -> set of subscriber channels
	rdb  *redis.Client
}

func NewHub(rdb *redis.Client) *Hub {
	return &Hub{subs: make(map[string]map[chan Event]struct{}), rdb: rdb}
}

func channelFor(captureID string) string { return "orbit:capture:" + captureID }

// Subscribe returns a buffered channel of events plus an unsubscribe func.
func (h *Hub) Subscribe(captureID string) (<-chan Event, func()) {
	ch := make(chan Event, 32)
	h.mu.Lock()
	if h.subs[captureID] == nil {
		h.subs[captureID] = make(map[chan Event]struct{})
	}
	h.subs[captureID][ch] = struct{}{}
	h.mu.Unlock()

	return ch, func() {
		h.mu.Lock()
		if set, ok := h.subs[captureID]; ok {
			if _, ok := set[ch]; ok {
				delete(set, ch)
				close(ch)
			}
			if len(set) == 0 {
				delete(h.subs, captureID)
			}
		}
		h.mu.Unlock()
	}
}

// Publish sends an event to Redis so every API instance (including this one,
// via the bridge) delivers it. Falls back to local-only if Redis is down.
func (h *Hub) Publish(ctx context.Context, ev Event) {
	if h.rdb != nil {
		b, err := json.Marshal(ev)
		if err == nil {
			if err := h.rdb.Publish(ctx, channelFor(ev.CaptureID), b).Err(); err == nil {
				return // the bridge will deliver it locally
			}
		}
	}
	h.deliverLocal(ev)
}

func (h *Hub) deliverLocal(ev Event) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	for ch := range h.subs[ev.CaptureID] {
		select {
		case ch <- ev:
		default: // slow client: drop rather than block the pipeline
		}
	}
}

// StartBridge relays Redis Pub/Sub messages to local subscribers.
// It uses a pattern subscription so one goroutine covers every capture.
func (h *Hub) StartBridge(ctx context.Context) {
	if h.rdb == nil {
		return
	}
	ps := h.rdb.PSubscribe(ctx, "orbit:capture:*")
	go func() {
		defer ps.Close()
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ps.Channel():
				if !ok {
					return
				}
				var ev Event
				if err := json.Unmarshal([]byte(msg.Payload), &ev); err != nil {
					log.Printf("realtime: bad payload on %s: %v", msg.Channel, err)
					continue
				}
				h.deliverLocal(ev)
			}
		}
	}()
}
