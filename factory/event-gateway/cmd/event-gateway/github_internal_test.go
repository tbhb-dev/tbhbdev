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
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	metricnoop "go.opentelemetry.io/otel/metric/noop"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

const fixtureKey = "fixture-signing-key"

func checkError(t *testing.T, err error) {
	t.Helper()

	if err != nil {
		t.Fatal(err)
	}
}

func checkEqual[T comparable](t *testing.T, got, want T) {
	t.Helper()

	if got != want {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func testStore(t *testing.T, age time.Duration) eventStore {
	t.Helper()

	store, stop, err := openStore(t.Context(), storeOptions(t.TempDir()), age)
	checkError(t, err)
	t.Cleanup(stop)

	return store
}

func testWebhook(t *testing.T, store eventStore, bodyCap int) *githubWebhook {
	t.Helper()

	signals := newTelemetry(slog.DiscardHandler, tracenoop.NewTracerProvider(), metricnoop.NewMeterProvider())

	return &githubWebhook{secret: []byte(fixtureKey), bodyCap: bodyCap, store: store, telemetry: signals}
}

func signedRequest(ctx context.Context, event, delivery, body string) *http.Request {
	req := httptest.NewRequestWithContext(ctx, http.MethodPost, webhookPath, strings.NewReader(body))
	req.Header.Set(eventHeader, event)
	req.Header.Set(deliveryHeader, delivery)

	mac := hmac.New(sha256.New, []byte(fixtureKey))

	_, _ = mac.Write([]byte(body))
	req.Header.Set("X-Hub-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))

	return req
}

func expectStatus(t *testing.T, h http.HandlerFunc, r *http.Request, status int) {
	t.Helper()

	checkEqual(t, responseStatus(h, r), status)
}

func responseStatus(h http.HandlerFunc, r *http.Request) int {
	w := httptest.NewRecorder()
	h(w, r)

	return w.Code
}

func TestGitHubDeliveryDuplicates(t *testing.T) {
	t.Parallel()

	ctx := t.Context()
	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)

	const (
		event    = "pull_request"
		delivery = "delivery-1"
		body     = `{ "repository": {"full_name": "example/repository"}, "action": "opened", "text": "<example>" }`
	)

	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, event, delivery, body), http.StatusAccepted)
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, event, delivery, body), http.StatusAccepted)
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, event, delivery, body+" "), http.StatusConflict)

	info, err := store.stream.Info(ctx)
	checkError(t, err)
	checkEqual(t, info.State.Msgs, uint64(1))

	msg, err := store.stream.GetMsg(ctx, info.State.FirstSeq)
	checkError(t, err)

	var stored map[string]json.RawMessage
	checkError(t, json.Unmarshal(msg.Data, &stored))
	checkEqual(t, string(stored["repository"]), `"example/repository"`)
	checkEqual(t, string(stored["payload"]), body)

	checkError(t, store.stream.DeleteMsg(ctx, msg.Sequence))
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, event, delivery, body), http.StatusServiceUnavailable)

	canceled, cancel := context.WithCancel(ctx)
	cancel()
	expectStatus(t, hook.ServeHTTP, signedRequest(canceled, event, delivery, body), http.StatusServiceUnavailable)
}

func TestGitHubTruncation(t *testing.T) {
	t.Parallel()

	const (
		projected = `"action":"unlabeled","check_run":{"name":"Build","conclusion":"success"},` +
			`"workflow_run":{"name":"CI","conclusion":"failure","path":".github/workflows/ci.yml"},` +
			`"issue":{"number":9,"labels":[{"name":"area:example"}]},` +
			`"pull_request":{"number":42,"labels":[{"name":"review"}],"head":{"sha":"123abc"}},` +
			`"label":{"name":"removed"}`
		body     = `{` + projected + `,"unneeded":"discard me"}`
		delivery = "truncated"
	)

	store := testStore(t, retention)
	hook := testWebhook(t, store, len(body)-1)
	ctx := t.Context()
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, "pull_request", delivery, body), http.StatusAccepted)
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, "pull_request", delivery, body+" "), http.StatusConflict)

	msg, err := store.stream.GetMsg(ctx, 1)
	checkError(t, err)

	var stored map[string]json.RawMessage
	checkError(t, json.Unmarshal(msg.Data, &stored))
	checkEqual(t, string(stored["repository"]), "null")

	var got, want map[string]any
	checkError(t, json.Unmarshal(stored["payload"], &got))
	checkError(t, json.Unmarshal([]byte(`{"truncated":true,`+projected+"}"), &want))

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("projected payload = %v, want %v", got, want)
	}
}

func TestGitHubValidation(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	wrongSignature := "sha256=" + strings.Repeat("0", 64)

	for _, test := range []struct {
		edit                        func(*http.Request)
		name, event, delivery, body string
		status                      int
	}{
		{setSignature(""), "missing signature", issuesEvent, "1", "{", http.StatusUnauthorized},
		{setSignature("sha256=00"), "short signature", issuesEvent, "1", "{}", http.StatusUnauthorized},
		{setSignature("sha256=" + strings.Repeat("g", 64)), "bad hex", issuesEvent, "1", "{}", http.StatusUnauthorized},
		{setSignature(wrongSignature), "wrong signature", issuesEvent, "1", "{", http.StatusUnauthorized},
		{brokenBody, "read error", issuesEvent, "1", "{}", http.StatusBadRequest},
		{oversizeBody, "transport limit", issuesEvent, "1", "{}", http.StatusRequestEntityTooLarge},
		{nil, "bad event", "issue.>", "1", "{}", http.StatusBadRequest},
		{nil, "uppercase event", "Issues", "1", "{}", http.StatusBadRequest},
		{nil, "empty payload", issuesEvent, "1", "", http.StatusBadRequest},
		{nil, "missing delivery", issuesEvent, "", "{}", http.StatusBadRequest},
		{nil, "malformed signed JSON", issuesEvent, "1", "{", http.StatusBadRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			req := signedRequest(t.Context(), test.event, test.delivery, test.body)
			if test.edit != nil {
				test.edit(req)
			}

			expectStatus(t, hook.ServeHTTP, req, test.status)
		})
	}
}

func TestGitHubStoreCeiling(t *testing.T) {
	t.Parallel()

	const (
		githubBodyCap = 25_000_000 // GitHub caps deliveries at 25 MB.
		prefix        = `{"action":"`
		suffix        = `"}`
	)

	checkEqual(t, maxPayload < githubBodyCap, true)

	// The request fits the read limit, but the retained action and envelope exceed it.
	body := prefix + strings.Repeat("a", maxPayload-len(prefix)-len(suffix)) + suffix
	hook := testWebhook(t, testStore(t, retention), defaultBodyCap)
	response := httptest.NewRecorder()
	hook.ServeHTTP(response, signedRequest(t.Context(), issuesEvent, "oversize", body))
	checkEqual(t, response.Code, http.StatusRequestEntityTooLarge)
	checkEqual(t, strings.TrimSpace(response.Body.String()), "GitHub webhook body exceeds the store limit")
}

func TestGitHubPingIsNotStored(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	expectStatus(t, hook.ServeHTTP, signedRequest(t.Context(), "ping", "ping", "{}"), http.StatusAccepted)
	info, err := store.stream.Info(t.Context())
	checkError(t, err)
	checkEqual(t, info.State.Msgs, uint64(0))
}

func TestGitHubTruncationInvalidField(t *testing.T) {
	t.Parallel()

	var store eventStore

	hook := testWebhook(t, store, 1)
	req := signedRequest(t.Context(), issuesEvent, "delivery", `{"issue":{"number":"not an integer"}}`)
	expectStatus(t, hook.ServeHTTP, req, http.StatusBadRequest)
}

func setSignature(signature string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("X-Hub-Signature-256", signature) }
}

var errBrokenBody = errors.New("broken body")

type bodyReadError struct{}

func (bodyReadError) Read([]byte) (int, error) { return 0, errBrokenBody }

func brokenBody(r *http.Request) {
	r.Body = io.NopCloser(bodyReadError{})
}

func oversizeBody(r *http.Request) {
	r.Body = io.NopCloser(bytes.NewReader(make([]byte, maxPayload+1)))
}

func TestGitHubIngestEvent(t *testing.T) {
	t.Parallel()

	hook := testWebhook(t, testStore(t, retention), defaultBodyCap)

	for _, test := range []struct {
		edit        func(*http.Request)
		name, event string
		delivery    string
		want        string
		status      int
	}{
		{oversizeBody, "transport limit", issuesEvent, "1", issuesEvent, http.StatusRequestEntityTooLarge},
		{nil, "missing delivery", issuesEvent, "", issuesEvent, http.StatusBadRequest},
		{nil, "malformed event", "Issues", "1", "", http.StatusBadRequest},
		{nil, "accepted", issuesEvent, "ingest-event", issuesEvent, http.StatusAccepted},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			req := signedRequest(t.Context(), test.event, test.delivery, "{}")
			if test.edit != nil {
				test.edit(req)
			}

			event, status := hook.ingest(httptest.NewRecorder(), req)
			checkEqual(t, event, test.want)
			checkEqual(t, status, test.status)
		})
	}
}

func TestGitHubTelemetry(t *testing.T) {
	t.Parallel()

	spans := tracetest.NewSpanRecorder()
	reader := sdkmetric.NewManualReader()

	var logs bytes.Buffer

	signals := newTelemetry(
		slog.NewJSONHandler(&logs, nil),
		sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans)),
		sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)),
	)
	hook := &githubWebhook{
		store:     testStore(t, retention),
		telemetry: signals,
		secret:    []byte(fixtureKey),
		bodyCap:   defaultBodyCap,
	}
	badSignature := signedRequest(t.Context(), issuesEvent, "2", "{}")
	badSignature.Header.Set("X-Hub-Signature-256", "sha256="+strings.Repeat("0", sha256.Size*2))

	oversized := signedRequest(t.Context(), issuesEvent, "3", "{}")
	oversizeBody(oversized)

	for _, req := range []*http.Request{
		signedRequest(t.Context(), issuesEvent, "1", "{}"),
		signedRequest(t.Context(), issuesEvent, "1", `{"text":"raw-payload-fixture"}`),
		badSignature,
		oversized,
		signedRequest(t.Context(), "Issues", "4", "{}"),
	} {
		hook.ServeHTTP(httptest.NewRecorder(), req)
	}

	ended := spans.Ended()
	got := make([]string, 0, len(ended))

	for _, span := range ended {
		attrs := span.Attributes()
		got = append(got, fmt.Sprintf("%s %d", attrs[0].Value.AsString(), attrs[1].Value.AsInt64()))
	}

	checkEqual(t, strings.Join(got, ","), "issues 202,issues 409, 401,issues 413, 400")

	checkGitHubCounter(t, reader)

	if strings.Contains(logs.String(), "raw-payload-fixture") {
		t.Fatal("logger received raw payload")
	}
}

func checkGitHubCounter(t *testing.T, reader *sdkmetric.ManualReader) {
	t.Helper()

	var metrics metricdata.ResourceMetrics
	checkError(t, reader.Collect(t.Context(), &metrics))

	counter, ok := metrics.ScopeMetrics[0].Metrics[0].Data.(metricdata.Sum[int64])
	checkEqual(t, ok, true)

	points := make([]string, 0, len(counter.DataPoints))

	for _, point := range counter.DataPoints {
		event, _ := point.Attributes.Value("event")
		status, _ := point.Attributes.Value("http.response.status_code")
		points = append(points, fmt.Sprintf("%s %d=%d", event.AsString(), status.AsInt64(), point.Value))
	}

	slices.Sort(points)
	checkEqual(t, strings.Join(points, ","), " 400=1, 401=1,issues 202=1,issues 409=1,issues 413=1")
}

func TestNewTelemetryNilProviders(t *testing.T) {
	t.Parallel()

	hook := &githubWebhook{
		store:     testStore(t, retention),
		telemetry: newTelemetry(slog.DiscardHandler, nil, nil),
		secret:    []byte(fixtureKey),
		bodyCap:   defaultBodyCap,
	}
	expectStatus(t, hook.ServeHTTP, signedRequest(t.Context(), issuesEvent, "1", "{}"), http.StatusAccepted)
}
