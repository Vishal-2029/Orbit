FROM golang:1.26-alpine AS build

WORKDIR /src
COPY backend/go.mod backend/go.sum ./backend/
RUN cd backend && go mod download
COPY backend ./backend
RUN cd backend && CGO_ENABLED=0 go build -o /out/orbit-api ./cmd/api

FROM alpine:3.22

WORKDIR /app
COPY --from=build /out/orbit-api /app/orbit-api
COPY web /app/web
EXPOSE 8080
ENTRYPOINT ["/app/orbit-api"]