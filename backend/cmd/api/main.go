// Command api runs the Orbit HTTP + WebSocket server.
package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/vishal/orbit/backend/internal/config"
	"github.com/vishal/orbit/backend/internal/httpapi"
	"github.com/vishal/orbit/backend/internal/queue"
	"github.com/vishal/orbit/backend/internal/realtime"
	"github.com/vishal/orbit/backend/internal/repo"
	"github.com/vishal/orbit/backend/internal/service"
	"github.com/vishal/orbit/backend/internal/storage"
)

func main() {
	cfg := config.Load()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	boot, bootCancel := context.WithTimeout(ctx, 30*time.Second)
	defer bootCancel()

	r, err := repo.New(boot, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("postgres: %v (is `docker compose up -d` running?)", err)
	}
	defer r.Close()

	store, err := storage.NewMinIO(cfg.MinIOEndpoint, cfg.MinIOAccess, cfg.MinIOSecret,
		cfg.MinIOUseSSL, cfg.BucketPrivate, cfg.BucketPublic)
	if err != nil {
		log.Fatalf("minio: %v (is `docker compose up -d` running?)", err)
	}

	// Managed Redis providers hand out a rediss:// URL carrying a password and
	// requiring TLS, which a bare host:port cannot express. REDIS_URL wins when
	// set; REDIS_ADDR stays the plain-connection path used by Docker Compose.
	redisOpts := &redis.Options{Addr: cfg.RedisAddr}
	if cfg.RedisURL != "" {
		parsed, err := redis.ParseURL(cfg.RedisURL)
		if err != nil {
			log.Fatalf("redis: bad REDIS_URL: %v", err)
		}
		redisOpts = parsed
	}
	rdb := redis.NewClient(redisOpts)
	if err := rdb.Ping(boot).Err(); err != nil {
		log.Fatalf("redis: %v (is `docker compose up -d` running?)", err)
	}
	defer rdb.Close()

	q, err := queue.NewRedisQueue(boot, rdb)
	if err != nil {
		log.Fatalf("queue: %v", err)
	}
	defer q.Close()

	hub := realtime.NewHub(rdb)
	hub.StartBridge(ctx)

	svc := service.NewCapture(r, store, q, hub, cfg)

	// Backstop for captures whose worker died: without this they sit at
	// "processing" forever and the user watches a bar that will never move.
	svc.StartReaper(ctx, 2*time.Minute, service.StuckAfter)

	app := httpapi.NewServer(svc, hub, store, cfg)

	go func() {
		log.Printf("Orbit API listening on :%s  (public base %s)", cfg.Port, cfg.PublicBaseURL)
		if err := app.Listen(":" + cfg.Port); err != nil {
			log.Fatalf("listen: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("shutting down...")
	cancel()
	if err := app.ShutdownWithTimeout(10 * time.Second); err != nil {
		log.Printf("shutdown: %v", err)
	}
}
