package config

import (
	"os"
	"strconv"
	"strings"
)

type Config struct {
	Port          string
	PublicBaseURL string // how the browser reaches this API, used to build manifest URLs
	DatabaseURL   string
	RedisAddr     string
	RedisURL      string // full redis:// or rediss:// URL, wins over RedisAddr when set
	MinIOEndpoint string
	MinIOAccess   string
	MinIOSecret   string
	MinIOUseSSL   bool
	BucketPrivate string
	BucketPublic  string
	JWTSecret     string
	MaxUploadMB   int
	WebDir        string // static web client, served at / so one URL covers everything
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// endpoint reads an S3 endpoint, which must be host[:port] rather than a URL.
// minio-go quietly accepts a scheme while the worker's Python client rejects
// it, so normalising here keeps both halves of the app agreeing on one value.
func endpoint(k, def string) string {
	v := env(k, def)
	for _, scheme := range []string{"https://", "http://"} {
		v = strings.TrimPrefix(v, scheme)
	}
	return strings.TrimSuffix(v, "/")
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func Load() Config {
	return Config{
		Port:          env("PORT", "8080"),
		PublicBaseURL: env("PUBLIC_BASE_URL", "http://localhost:8080"),
		DatabaseURL:   env("DATABASE_URL", "postgres://orbit:orbit@localhost:5433/orbit?sslmode=disable"),
		RedisAddr:     env("REDIS_ADDR", "localhost:6380"),
		RedisURL:      env("REDIS_URL", ""),
		MinIOEndpoint: endpoint("MINIO_ENDPOINT", "localhost:9010"),
		MinIOAccess:   env("MINIO_ACCESS_KEY", "orbitadmin"),
		MinIOSecret:   env("MINIO_SECRET_KEY", "orbitadmin123"),
		MinIOUseSSL:   env("MINIO_USE_SSL", "false") == "true",
		BucketPrivate: env("BUCKET_PRIVATE", "orbit-private"),
		BucketPublic:  env("BUCKET_PUBLIC", "orbit-public"),
		JWTSecret:     env("JWT_SECRET", "dev-only-change-me"),
		MaxUploadMB:   envInt("MAX_UPLOAD_MB", 25),
		WebDir:        env("WEB_DIR", "../web"),
	}
}
