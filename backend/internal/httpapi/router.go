// Package httpapi wires the REST + WebSocket surface.
package httpapi

import (
	"context"
	"errors"
	"log"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gofiber/contrib/websocket"
	"github.com/gofiber/fiber/v2"
	"github.com/gofiber/fiber/v2/middleware/cors"
	"github.com/gofiber/fiber/v2/middleware/logger"
	"github.com/gofiber/fiber/v2/middleware/recover"
	"github.com/gofiber/fiber/v2/middleware/requestid"

	"github.com/vishal/orbit/backend/internal/config"
	"github.com/vishal/orbit/backend/internal/domain"
	"github.com/vishal/orbit/backend/internal/realtime"
	"github.com/vishal/orbit/backend/internal/repo"
	"github.com/vishal/orbit/backend/internal/service"
	"github.com/vishal/orbit/backend/internal/storage"
)

type Server struct {
	svc   *service.Capture
	hub   *realtime.Hub
	store storage.Store
	cfg   config.Config
}

func NewServer(svc *service.Capture, hub *realtime.Hub, store storage.Store, cfg config.Config) *fiber.App {
	s := &Server{svc: svc, hub: hub, store: store, cfg: cfg}

	app := fiber.New(fiber.Config{
		AppName:      "Orbit 360 API",
		BodyLimit:    cfg.MaxUploadMB * 1024 * 1024,
		ErrorHandler: errorHandler,
	})
	app.Use(recover.New())
	app.Use(requestid.New())
	app.Use(logger.New(logger.Config{Format: "${time} ${status} ${latency} ${method} ${path}\n"}))
	app.Use(cors.New(cors.Config{
		AllowOrigins: "*",
		AllowHeaders: "Origin, Content-Type, Accept, Authorization",
		AllowMethods: "GET, POST, PATCH, DELETE, OPTIONS",
	}))

	app.Get("/health", s.health)

	v1 := app.Group("/api/v1")
	v1.Post("/captures", s.createCapture)
	v1.Get("/captures", s.listCaptures)
	v1.Get("/captures/:id", s.getCapture)
	v1.Patch("/captures/:id", s.patchCapture)
	v1.Delete("/captures/:id", s.deleteCapture)
	v1.Get("/captures/:id/plan", s.getPlan)
	v1.Post("/captures/:id/photos", s.uploadPhoto)
	v1.Post("/captures/:id/process", s.process)
	v1.Get("/captures/:id/frames", s.listFrames)
	v1.Get("/captures/:id/manifest", s.getManifest)
	v1.Get("/captures/:id/image/panorama", s.servePanorama)
	v1.Get("/captures/:id/image/:kind/:idx", s.serveImage)

	// Internal callbacks used by the CV worker.
	w := v1.Group("/internal")
	w.Post("/captures/:id/frames/:frameId/done", s.workerFrameDone)
	w.Post("/captures/:id/frames/:frameId/failed", s.workerFrameFailed)
	w.Post("/captures/:id/finalize", s.workerFinalize)

	// Public share page data by slug.
	app.Get("/s/:slug/manifest", s.manifestBySlug)

	// Serve the web client from the API itself. This means a phone only ever
	// needs ONE reachable URL (and one https tunnel) instead of two origins,
	// which also sidesteps CORS and mixed-content entirely.
	if st, err := os.Stat(cfg.WebDir); err == nil && st.IsDir() {
		app.Static("/", cfg.WebDir, fiber.Static{Index: "index.html"})
		log.Printf("serving web client from %s at /", cfg.WebDir)
	} else {
		log.Printf("web client dir %q not found; API only (set WEB_DIR)", cfg.WebDir)
	}

	app.Use("/ws", func(c *fiber.Ctx) error {
		if websocket.IsWebSocketUpgrade(c) {
			return c.Next()
		}
		return fiber.ErrUpgradeRequired
	})
	app.Get("/ws/captures/:id", websocket.New(s.wsProgress))

	return app
}

func errorHandler(c *fiber.Ctx, err error) error {
	code := fiber.StatusInternalServerError
	var fe *fiber.Error
	if errors.As(err, &fe) {
		code = fe.Code
	}
	if errors.Is(err, repo.ErrNotFound) {
		code = fiber.StatusNotFound
	}
	return c.Status(code).JSON(fiber.Map{"error": err.Error()})
}

func (s *Server) health(c *fiber.Ctx) error {
	return c.JSON(fiber.Map{"status": "ok", "service": "orbit-api", "time": time.Now()})
}

func (s *Server) createCapture(c *fiber.Ctx) error {
	var in service.CreateInput
	if err := c.BodyParser(&in); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid JSON body")
	}
	cap, plan, err := s.svc.Create(c.Context(), in)
	if err != nil {
		return err
	}
	return c.Status(fiber.StatusCreated).JSON(fiber.Map{"capture": cap, "plan": plan})
}

func (s *Server) listCaptures(c *fiber.Ctx) error {
	limit := clampInt(c.QueryInt("limit", 50), 1, 200)
	offset := clampInt(c.QueryInt("offset", 0), 0, 1<<20)
	list, err := s.svc.List(c.Context(), limit, offset)
	if err != nil {
		return err
	}
	return c.JSON(fiber.Map{"captures": list})
}

func (s *Server) getCapture(c *fiber.Ctx) error {
	cap, err := s.svc.Get(c.Context(), c.Params("id"))
	if err != nil {
		return err
	}
	return c.JSON(fiber.Map{"capture": cap, "progress": cap.Progress()})
}

func (s *Server) getPlan(c *fiber.Ctx) error {
	cap, err := s.svc.Get(c.Context(), c.Params("id"))
	if err != nil {
		return err
	}
	return c.JSON(s.svc.Plan(cap))
}

func (s *Server) patchCapture(c *fiber.Ctx) error {
	var body struct {
		Title    string `json:"title"`
		IsPublic *bool  `json:"is_public"`
	}
	if err := c.BodyParser(&body); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid JSON body")
	}
	cap, err := s.svc.Get(c.Context(), c.Params("id"))
	if err != nil {
		return err
	}
	title := cap.Title
	if strings.TrimSpace(body.Title) != "" {
		title = body.Title
	}
	pub := cap.IsPublic
	if body.IsPublic != nil {
		pub = *body.IsPublic
	}
	if err := s.svc.Update(c.Context(), cap.ID, title, pub); err != nil {
		return err
	}
	updated, err := s.svc.Get(c.Context(), cap.ID)
	if err != nil {
		return err
	}
	return c.JSON(fiber.Map{"capture": updated})
}

func (s *Server) deleteCapture(c *fiber.Ctx) error {
	if err := s.svc.Delete(c.Context(), c.Params("id")); err != nil {
		return err
	}
	return c.SendStatus(fiber.StatusNoContent)
}

// uploadPhoto accepts one multipart photo plus the guidance metadata that says
// which slot it belongs to and where the phone was pointing.
func (s *Server) uploadPhoto(c *fiber.Ctx) error {
	fh, err := c.FormFile("photo")
	if err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "missing 'photo' file field")
	}
	f, err := fh.Open()
	if err != nil {
		return err
	}
	defer f.Close()

	idx, err := strconv.Atoi(c.FormValue("index", "-1"))
	if err != nil || idx < 0 {
		return fiber.NewError(fiber.StatusBadRequest, "missing or invalid 'index' field")
	}
	yaw, _ := strconv.ParseFloat(c.FormValue("yaw", "0"), 64)
	pitch, _ := strconv.ParseFloat(c.FormValue("pitch", "0"), 64)
	hasHeading := c.FormValue("has_heading") == "true"

	// The quaternion is optional: only devices with a usable motion sensor
	// send one, and all four components must be present to mean anything.
	var quat *domain.Quaternion
	if qs := c.FormValue("qw", ""); qs != "" {
		qx, e1 := strconv.ParseFloat(c.FormValue("qx", ""), 64)
		qy, e2 := strconv.ParseFloat(c.FormValue("qy", ""), 64)
		qz, e3 := strconv.ParseFloat(c.FormValue("qz", ""), 64)
		qw, e4 := strconv.ParseFloat(qs, 64)
		if e1 == nil && e2 == nil && e3 == nil && e4 == nil {
			quat = &domain.Quaternion{X: qx, Y: qy, Z: qz, W: qw}
		}
	}

	frame, err := s.svc.AddPhoto(c.Context(), c.Params("id"), service.UploadInput{
		Index: idx, SlotID: c.FormValue("slot_id", ""), Yaw: yaw, Pitch: pitch,
		HasHeading: hasHeading, Quat: quat,
		OrientationSource: c.FormValue("orientation_source", ""),
		Body:              f, Size: fh.Size, CType: fh.Header.Get("Content-Type"),
	})
	// A photo pointing the same way as one we already have is a user mistake
	// with a specific fix, so it gets its own status and a structured body.
	var dup *service.DuplicateDirectionError
	if errors.As(err, &dup) {
		return c.Status(fiber.StatusConflict).JSON(fiber.Map{
			"error":         dup.Message,
			"code":          "duplicate_direction",
			"clash_index":   dup.ClashIndex,
			"clash_label":   dup.ClashLabel,
			"degrees_apart": dup.Degrees,
		})
	}
	if err != nil {
		return err
	}
	return c.Status(fiber.StatusCreated).JSON(fiber.Map{"frame": frame})
}

func (s *Server) process(c *fiber.Ctx) error {
	cap, err := s.svc.Process(c.Context(), c.Params("id"))
	if err != nil {
		return fiber.NewError(fiber.StatusBadRequest, err.Error())
	}
	return c.JSON(fiber.Map{"capture": cap})
}

func (s *Server) listFrames(c *fiber.Ctx) error {
	frames, err := s.svc.Frames(c.Context(), c.Params("id"))
	if err != nil {
		return err
	}
	return c.JSON(fiber.Map{"frames": frames})
}

func (s *Server) getManifest(c *fiber.Ctx) error {
	cap, err := s.svc.Get(c.Context(), c.Params("id"))
	if err != nil {
		return err
	}
	if len(cap.Manifest) == 0 {
		return fiber.NewError(fiber.StatusConflict,
			"this 360 view is not ready yet (status: "+cap.Status+")")
	}
	c.Set("Content-Type", "application/json")
	return c.Send(cap.Manifest)
}

func (s *Server) manifestBySlug(c *fiber.Ctx) error {
	cap, err := s.svc.GetBySlug(c.Context(), c.Params("slug"))
	if err != nil {
		return err
	}
	if !cap.IsPublic {
		return fiber.NewError(fiber.StatusForbidden, "this 360 view is private")
	}
	if len(cap.Manifest) == 0 {
		return fiber.NewError(fiber.StatusConflict, "not ready yet (status: "+cap.Status+")")
	}
	c.Set("Content-Type", "application/json")
	return c.Send(cap.Manifest)
}

func (s *Server) serveImage(c *fiber.Ctx) error {
	id := c.Params("id")
	idx, err := strconv.Atoi(c.Params("idx"))
	if err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "bad frame index")
	}
	var key, bucket string
	switch c.Params("kind") {
	case "processed":
		key, bucket = storage.ProcessedKey(id, idx), s.cfg.BucketPublic
	case "thumb":
		key, bucket = storage.ThumbKey(id, idx), s.cfg.BucketPublic
	case "original":
		key, bucket = storage.OriginalKey(id, idx), s.cfg.BucketPrivate
	default:
		return fiber.NewError(fiber.StatusBadRequest, "kind must be processed, thumb or original")
	}
	return s.streamObject(c, bucket, key)
}

func (s *Server) servePanorama(c *fiber.Ctx) error {
	return s.streamObject(c, s.cfg.BucketPublic, storage.PanoramaKey(c.Params("id")))
}

func (s *Server) streamObject(c *fiber.Ctx, bucket, key string) error {
	b, err := s.store.GetBytes(c.Context(), bucket, key)
	if err != nil {
		return fiber.NewError(fiber.StatusNotFound, "image not found: "+key)
	}
	c.Set("Content-Type", "image/jpeg")
	c.Set("Cache-Control", "public, max-age=31536000, immutable")
	return c.Send(b)
}

// --- worker callbacks ---

func (s *Server) workerFrameDone(c *fiber.Ctx) error {
	var body struct{ Index, Width, Height int }
	if err := c.BodyParser(&body); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid JSON body")
	}
	if err := s.svc.FrameDone(c.Context(), c.Params("id"), c.Params("frameId"),
		body.Index, body.Width, body.Height); err != nil {
		return err
	}
	return c.JSON(fiber.Map{"ok": true})
}

func (s *Server) workerFrameFailed(c *fiber.Ctx) error {
	var body struct {
		Index  int    `json:"index"`
		Reason string `json:"reason"`
	}
	if err := c.BodyParser(&body); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid JSON body")
	}
	if err := s.svc.FrameFailed(c.Context(), c.Params("id"), c.Params("frameId"),
		body.Index, body.Reason); err != nil {
		return err
	}
	return c.JSON(fiber.Map{"ok": true})
}

func (s *Server) workerFinalize(c *fiber.Ctx) error {
	var in service.FinalizeInput
	if err := c.BodyParser(&in); err != nil {
		return fiber.NewError(fiber.StatusBadRequest, "invalid JSON body")
	}
	m, err := s.svc.Finalize(c.Context(), c.Params("id"), in)
	if err != nil {
		return err
	}
	return c.JSON(fiber.Map{"manifest": m})
}

// wsProgress streams processing events for one capture until the client leaves.
func (s *Server) wsProgress(conn *websocket.Conn) {
	captureID := conn.Params("id")
	events, unsubscribe := s.hub.Subscribe(captureID)
	defer unsubscribe()
	defer conn.Close()

	// A reader goroutine exists purely to observe the socket closing; without
	// it a client disconnect would go unnoticed until the next write.
	closed := make(chan struct{})
	go func() {
		defer close(closed)
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}()

	// Send current state immediately so a client that connects late is correct.
	if cap, err := s.svc.Get(context.Background(), captureID); err == nil {
		_ = conn.WriteJSON(realtime.Event{
			Type: "status", CaptureID: captureID, Status: cap.Status,
			Processed: cap.ProcessedCount, Total: cap.FrameCount, Progress: cap.Progress(),
		})
	}

	ping := time.NewTicker(30 * time.Second)
	defer ping.Stop()
	for {
		select {
		case <-closed:
			return
		case ev, ok := <-events:
			if !ok {
				return
			}
			if err := conn.WriteJSON(ev); err != nil {
				return
			}
			if ev.Type == "ready" || (ev.Type == "error" && ev.Status == domain.StatusFailed) {
				return
			}
		case <-ping.C:
			if err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second)); err != nil {
				return
			}
		}
	}
}

func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
