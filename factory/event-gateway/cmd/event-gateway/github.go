// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/json/jsontext"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	jsonv2 "encoding/json/v2"
)

const eventType = "event"

const defaultBodyCap = 5 << 20

const (
	webhookPath    = "/webhooks/github"
	healthPath     = "/healthz"
	eventHeader    = "X-Github-Event"
	deliveryHeader = "X-Github-Delivery"
)

var (
	githubEvent    = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	githubDelivery = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
)

type envelope struct {
	Type       string  `json:"type"`
	Provider   string  `json:"provider"`
	Event      string  `json:"event"`
	Delivery   string  `json:"delivery"`
	Repository *string `json:"repository"`
	ReceivedAt string  `json:"-"`
	Payload    []byte  `json:"-"`
	projected  []byte
}

func (event envelope) encode() ([]byte, error) {
	data, headerErr := jsonv2.Marshal(event, jsontext.EscapeForHTML(true), jsontext.EscapeForJS(true))
	received, timeErr := json.Marshal(event.ReceivedAt)

	payload := event.Payload
	if event.projected != nil {
		payload = event.projected
	}

	// Append the retained JSON directly so full payloads keep their signed bytes.
	data = append(bytes.TrimSuffix(data, []byte("}")), `,"received_at":`...)
	data = append(data, received...)
	data = append(data, `,"payload":`...)
	data = append(data, payload...)

	return append(data, '}'), errors.Join(headerErr, timeErr)
}

type githubPayload struct {
	Repository map[string]*json.RawMessage `json:"repository"`
}

type githubCheck struct {
	Name       *string `json:"name,omitempty"`
	Conclusion *string `json:"conclusion,omitempty"`
	Path       *string `json:"path,omitempty"`
}

type githubLabel struct {
	Name string `json:"name"`
}

type githubItem struct {
	Number *int64        `json:"number,omitempty"`
	Labels []githubLabel `json:"labels,omitempty"`
}

type githubPullRequest struct {
	Head   *githubHead   `json:"head,omitempty"`
	Number *int64        `json:"number,omitempty"`
	Labels []githubLabel `json:"labels,omitempty"`
}

type githubHead struct {
	SHA string `json:"sha"`
}

type githubWebhook struct {
	store     eventStore
	telemetry telemetry
	secret    []byte
	bodyCap   int
}

var errSecret = errors.New("GitHub webhook secret is required and must contain a non-whitespace character")

func newGitHubWebhook(secret []byte, bodyCap int) (*githubWebhook, error) {
	if len(bytes.TrimSpace(secret)) == 0 {
		return nil, errSecret
	}

	return &githubWebhook{
		store:     eventStore{jet: nil, stream: nil, openOrdered: nil},
		telemetry: telemetry{logger: nil, tracer: nil, ingest: nil, parked: nil, parkFailed: nil},
		secret:    secret,
		bodyCap:   bodyCap,
	}, nil
}

func (h *githubWebhook) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	path := request.URL.EscapedPath()

	switch {
	case request.Method == http.MethodGet && path == healthPath:
		response.WriteHeader(http.StatusOK)
	case request.Method == http.MethodPost && path == webhookPath:
		h.receive(response, request)
	default:
		http.NotFound(response, request)
	}
}

func (h *githubWebhook) receive(response http.ResponseWriter, request *http.Request) {
	ctx, span := h.telemetry.tracer.Start(request.Context(), "github webhook")
	defer span.End()

	event, status := h.ingest(response, request.WithContext(ctx))
	h.telemetry.record(ctx, span, event, status)

	if status == http.StatusRequestEntityTooLarge {
		http.Error(response, "GitHub webhook body exceeds the store limit", status)

		return
	}

	response.WriteHeader(status)
}

func (h *githubWebhook) ingest(response http.ResponseWriter, request *http.Request) (string, int) {
	receivedAt := time.Now().UTC()
	signature := request.Header.Get("X-Hub-Signature-256")

	// The signature covers the body, not the headers, so only a well-formed
	// event name is recorded.
	event := request.Header.Get(eventHeader)
	if !githubEvent.MatchString(event) {
		event = ""
	}

	digest, err := decodeSignature(signature)
	if err != nil {
		return "", http.StatusUnauthorized
	}

	body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, maxPayload))
	if err != nil {
		_, tooLarge := errors.AsType[*http.MaxBytesError](err)
		if tooLarge {
			return event, http.StatusRequestEntityTooLarge
		}

		return event, http.StatusBadRequest
	}

	if !verifySignature(h.secret, body, digest) {
		return "", http.StatusUnauthorized
	}

	delivery := request.Header.Get(deliveryHeader)

	err = validateEvent(event, body)
	if err != nil {
		return event, h.publishStatus(request.Context(), err)
	}

	if !githubDelivery.MatchString(delivery) {
		return event, http.StatusBadRequest
	}

	normalized, err := normalizeGitHub(event, delivery, receivedAt, body, h.bodyCap)
	if err != nil {
		return event, http.StatusBadRequest
	}

	if event == "ping" {
		return event, http.StatusAccepted
	}

	err = h.store.publish(request.Context(), normalized)

	return event, h.publishStatus(request.Context(), err)
}

func (h *githubWebhook) publishStatus(ctx context.Context, err error) int {
	switch {
	case err == nil:
		return http.StatusAccepted
	case errors.Is(err, errInvalidEvent), errors.Is(err, errEmptyPayload):
		return http.StatusBadRequest
	case errors.Is(err, nats.ErrMaxPayload):
		return http.StatusRequestEntityTooLarge
	case errors.Is(err, errBodyConflict):
		h.telemetry.logger.WarnContext(ctx, "GitHub delivery already has a different body")

		return http.StatusConflict
	default:
		h.telemetry.logger.ErrorContext(ctx, "GitHub event storage unavailable")

		return http.StatusServiceUnavailable
	}
}

var errSignature = errors.New("invalid GitHub signature")

func decodeSignature(header string) ([]byte, error) {
	signature, ok := strings.CutPrefix(header, "sha256=")
	if !ok || len(signature) != hex.EncodedLen(sha256.Size) {
		return nil, errSignature
	}

	digest, err := hex.DecodeString(signature)
	if err != nil {
		return nil, errSignature
	}

	return digest, nil
}

func verifySignature(secret, body, signature []byte) bool {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write(body)

	return hmac.Equal(mac.Sum(nil), signature)
}

func normalizeGitHub(event, delivery string, receivedAt time.Time, body []byte, bodyCap int) (envelope, error) {
	var scope githubPayload

	err := json.Unmarshal(body, &scope)
	if err != nil {
		return envelope{}, fmt.Errorf("decode GitHub payload: %w", err)
	}

	var repository *string
	if name := scope.Repository["full_name"]; name != nil {
		err = json.Unmarshal(*name, &repository)
		if err != nil {
			return envelope{}, fmt.Errorf("decode GitHub repository: %w", err)
		}
	}

	message := envelope{
		Type:       eventType,
		Provider:   "github",
		Event:      event,
		Delivery:   delivery,
		Repository: repository,
		ReceivedAt: receivedAt.Format(time.RFC3339Nano),
		Payload:    body,
		projected:  nil,
	}

	if len(body) > bodyCap {
		message.projected, err = truncateGitHub(body)
	}

	return message, err
}

func truncateGitHub(body []byte) ([]byte, error) {
	var fields map[string]json.RawMessage

	err := json.Unmarshal(body, &fields)
	if err != nil {
		return nil, fmt.Errorf("decode GitHub filter fields: %w", err)
	}

	payload := map[string]any{"truncated": true}

	for name, field := range map[string]any{
		"action": new(*string), "check_run": new(*githubCheck), "check_suite": new(*githubCheck),
		"workflow_run": new(*githubCheck), "workflow_job": new(*githubCheck), "issue": new(*githubItem),
		"pull_request": new(*githubPullRequest), "label": new(*githubLabel),
	} {
		raw := fields[name]
		if raw == nil || bytes.Equal(raw, []byte("null")) {
			continue
		}

		err = json.Unmarshal(raw, field)
		if err != nil {
			return nil, fmt.Errorf("decode GitHub filter field %q: %w", name, err)
		}

		payload[name] = field
	}

	data, err := json.Marshal(payload)

	return data, errors.Join(err)
}
