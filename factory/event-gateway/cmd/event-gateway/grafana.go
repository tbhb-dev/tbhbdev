// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	grafanaProvider    = "grafana"
	grafanaPath        = "/webhooks/grafana"
	grafanaEnv         = "GRAFANA_WEBHOOK_SECRET"
	grafanaSignature   = "X-Grafana-Alerting-Signature"
	grafanaBodyCap     = 1 << 20
	grafanaMaxAlerts   = 100
	grafanaDeliveryLen = 16
)

var (
	errGrafanaSecret = errors.New("the Grafana webhook secret is required and must contain a non-whitespace character")

	grafanaScopeValue = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}$`)

	errSkipped = errors.New("alert has no storable status or scope")
)

// grafanaHandler returns nil when no source enables the route. Once any source
// is present, an empty, blank or unreadable secret fails startup.
func (cfg *serveConfig) grafanaEnabled() bool {
	_, fromEnv := os.LookupEnv(grafanaEnv)

	return cfg.grafSecret != nil || cfg.grafFile != nil || fromEnv
}

func (cfg *serveConfig) grafanaHandler() (*grafanaWebhook, error) {
	secret, err := readSecret(cfg.grafSecret, cfg.grafFile, grafanaEnv)
	if err != nil {
		return nil, err
	}

	return newGrafanaWebhook(secret)
}

// grafanaWebhook serves the Grafana route and passes every other path on.
type grafanaWebhook struct {
	next      http.Handler
	store     eventStore
	telemetry telemetry
	secret    []byte
}

func newGrafanaWebhook(secret []byte) (*grafanaWebhook, error) {
	if len(bytes.TrimSpace(secret)) == 0 {
		return nil, errGrafanaSecret
	}

	return &grafanaWebhook{
		next:      nil,
		store:     eventStore{jet: nil, stream: nil, openOrdered: nil},
		telemetry: telemetry{logger: nil, tracer: nil, ingest: nil, parked: nil, parkFailed: nil},
		secret:    secret,
	}, nil
}

// withGrafana puts the Grafana route in front of the GitHub handler when enabled.
func withGrafana(grafana *grafanaWebhook, github http.Handler, store eventStore, signals telemetry) http.Handler {
	if grafana == nil {
		return github
	}

	grafana.store, grafana.telemetry, grafana.next = store, signals, github

	return grafana
}

func (h *grafanaWebhook) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	if request.URL.EscapedPath() != grafanaPath {
		h.next.ServeHTTP(response, request)

		return
	}

	if request.Method != http.MethodPost {
		response.WriteHeader(http.StatusMethodNotAllowed)

		return
	}

	ctx, span := h.telemetry.tracer.Start(request.Context(), "grafana webhook")
	defer span.End()

	status := h.ingest(response, request.WithContext(ctx))
	h.telemetry.record(ctx, span, "alert", status)
	response.WriteHeader(status)
}

func (h *grafanaWebhook) ingest(response http.ResponseWriter, request *http.Request) int {
	receivedAt := time.Now().UTC()

	if request.ContentLength > grafanaBodyCap {
		return http.StatusRequestEntityTooLarge
	}

	// The decoder accepts either hex case and rejects any other character.
	digest, err := hex.DecodeString(request.Header.Get(grafanaSignature))
	if err != nil || len(digest) != sha256.Size {
		return http.StatusUnauthorized
	}

	body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, grafanaBodyCap))
	if _, tooLarge := errors.AsType[*http.MaxBytesError](err); tooLarge {
		return http.StatusRequestEntityTooLarge
	}

	if err != nil {
		return http.StatusBadRequest
	}

	if !verifySignature(h.secret, body, digest) {
		return http.StatusUnauthorized
	}

	var alerts struct {
		Alerts []json.RawMessage `json:"alerts"`
	}

	err = json.Unmarshal(body, &alerts)
	if err != nil || len(alerts.Alerts) == 0 || len(alerts.Alerts) > grafanaMaxAlerts {
		return http.StatusBadRequest
	}

	for index, raw := range alerts.Alerts {
		message, skip := normalizeGrafana(raw, grafanaDelivery(body, index), receivedAt)
		if skip != nil {
			continue
		}

		err = h.store.publish(request.Context(), message)
		if errors.Is(err, errBodyConflict) {
			h.telemetry.logger.WarnContext(request.Context(), "Grafana delivery already has a different body")

			return http.StatusConflict
		}

		if err != nil {
			h.telemetry.logger.ErrorContext(request.Context(), "Grafana event storage unavailable")

			return http.StatusServiceUnavailable
		}
	}

	return http.StatusAccepted
}

// grafanaDelivery names an alert by the body and its position, so a retried
// body keeps its IDs.
func grafanaDelivery(body []byte, index int) string {
	sum := sha256.Sum256(fmt.Appendf(nil, "%s:%d", body, index))

	return "gf-" + hex.EncodeToString(sum[:grafanaDeliveryLen])
}

// normalizeGrafana fails with errSkipped for an alert that is skipped.
func normalizeGrafana(raw json.RawMessage, delivery string, receivedAt time.Time) (envelope, error) {
	var entry map[string]any

	err := json.Unmarshal(raw, &entry)
	if err != nil {
		return envelope{}, errSkipped
	}

	status := text(entry, "status")

	labels, ok := entry["labels"].(map[string]any)
	if !ok {
		return envelope{}, errSkipped
	}

	kind, scopeID := "", ""

	for _, scope := range [][2]string{{"host_name", "hosts"}, {"agent_account", "capacity"}} {
		if id := text(labels, scope[0]); grafanaScopeValue.MatchString(id) {
			kind, scopeID = scope[1], id

			break
		}
	}

	if (status != "firing" && status != "resolved") || kind == "" {
		return envelope{}, errSkipped
	}

	data, err := grafanaPayload(entry, labels, status, kind, scopeID)

	repository := kind + "/" + scopeID

	return envelope{
		Type:       eventType,
		Provider:   grafanaProvider,
		Event:      "alert." + status,
		Delivery:   delivery,
		Repository: &repository,
		ReceivedAt: receivedAt.Format(time.RFC3339Nano),
		Payload:    data,
		projected:  nil,
	}, err
}

func grafanaPayload(entry, labels map[string]any, status, kind, scopeID string) ([]byte, error) {
	payload := map[string]string{"status": status, "scope": scopeID, "kind": kind}
	setString(payload, "alertname", labels, "alertname")

	for name, key := range map[string]string{
		"value": "valueString", "starts_at": "startsAt", "ends_at": "endsAt", "fingerprint": "fingerprint",
	} {
		setString(payload, name, entry, key)
	}

	for _, key := range []string{"panelURL", "dashboardURL"} {
		if isWebLink(text(entry, key)) {
			payload["html_url"] = text(entry, key)

			break
		}
	}

	data, err := json.Marshal(payload)

	return data, errors.Join(err)
}

func isWebLink(link string) bool {
	lower := strings.ToLower(link)

	return strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://")
}

// text returns a string value, or "" for an absent or non-string one.
func text(src map[string]any, key string) string {
	if value, ok := src[key].(string); ok {
		return value
	}

	return ""
}

// setString copies a string value; absent and non-string values stay out.
func setString(dst map[string]string, name string, src map[string]any, key string) {
	if value, ok := src[key].(string); ok {
		dst[name] = value
	}
}
