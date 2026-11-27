// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	"go.opentelemetry.io/otel/sdk/trace"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

func TestInvalidEventEncoding(t *testing.T) {
	t.Parallel()

	ctx := t.Context()
	store := testStore(t, retention)
	event := envelope{
		Type: eventType, Provider: "\xff", Event: "push", Delivery: "", Repository: nil,
		ReceivedAt: "", Payload: []byte(`{}`), projected: nil,
	}
	checkEqual(t, store.publish(ctx, event) != nil, true)

	_, _, err := openStream(ctx, func() {}, store.jet.Conn(), retention, func(*jetstream.JetStreamOptions) error {
		return errRetryDelivery
	})
	checkEqual(t, errors.Is(err, errRetryDelivery), true)
}

func TestGitHubWireFields(t *testing.T) {
	t.Parallel()

	_, err := normalizeGitHub("push", "fixture", time.Now(), []byte(`{"repository":{"full_name":7}}`), defaultBodyCap)
	checkEqual(t, err != nil, true)
	_, err = truncateGitHub([]byte(`[]`))
	checkEqual(t, err != nil, true)

	data, err := truncateGitHub([]byte(`{"action":null}`))
	checkError(t, err)
	checkEqual(t, string(data), `{"truncated":true}`)
}

func TestMCPNotificationErrors(t *testing.T) {
	t.Parallel()

	_, peer, notifier := connectMCPClient(t, claudeCodeClient, nil)
	ctx, cancel := context.WithCancel(t.Context())
	cancel()

	checkEqual(t, errors.Is(notifier.notify(ctx, nil), context.Canceled), true)
	checkEqual(t, notifier.notify(t.Context(), json.RawMessage(`{`)) != nil, true)

	var event channelEvent

	event.payload = []byte("\xff")
	checkEqual(t, notifier.deliverChannel(t.Context(), peer, event) != nil, true)

	notice := channelNotice{Level: channelWarning, Text: "\xff"}
	checkEqual(t, json.Valid(notice.channelEvent().payload), true)
}

func TestMCPMalformedCapability(t *testing.T) {
	t.Parallel()

	defer func() { checkEqual(t, recover() != nil, true) }()

	mcpCapabilities([]byte(`{`))
}

func TestMCPStateInaccessibleDirectory(t *testing.T) {
	t.Parallel()

	logger := slog.New(slog.DiscardHandler)
	dir := t.TempDir()
	state := mcpState{logger: logger, dir: dir, file: "sessions/state.json", writeFile: (*os.File).Write}
	sub, err := filter.Parse([]byte(mcpStateTestSubscription))
	checkError(t, err)

	sub.Provider = "\xff"
	checkEqual(t, state.write([]filter.Subscription{sub}, nil), false)

	info, err := os.Stat(dir)
	checkError(t, err)

	mode := info.Mode().Perm()

	checkError(t, os.Chmod(dir, 0))
	checkEqual(t, state.write(nil, nil), false)
	checkError(t, os.Chmod(dir, mode))

	sessions := filepath.Join(dir, "sessions")
	checkError(t, os.Mkdir(sessions, 0o500))
	checkEqual(t, state.write(nil, nil), false)
	checkError(t, os.Chmod(sessions, mode))
}

func TestMCPStateConcurrentCleanup(t *testing.T) {
	t.Parallel()

	root, err := os.OpenRoot(t.TempDir())
	checkError(t, err)
	t.Cleanup(func() { checkError(t, root.Close()) })

	file, err := root.Create("pending")
	checkError(t, err)

	written := replaceMCPState(root, file, "pending", "state.json", nil, func([]byte) (int, error) {
		checkError(t, file.Close())
		checkError(t, root.Remove("pending"))

		return 0, errRetryDelivery
	})
	checkEqual(t, written, false)
}

func TestParkedWireErrors(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)
	parkHook(
		store.jet,
		newTelemetry(slog.DiscardHandler, nil, nil),
		0,
	)(
		t.Context(),
		"\xff",
		Event{Seq: 1, Envelope: nil},
		ErrPermanent,
	)

	for _, raw := range []string{`[]`, `{"seq":"bad"}`} {
		var record parkedRecord
		checkEqual(t, json.Unmarshal([]byte(raw), &record) != nil, true)
	}

	err := runParked(t.Context(), io.Discard, []string{parkedRetryCommand, "\xff", "1"}, requestParked)
	checkEqual(t, errors.Is(err, errParkedRequest), true)
}

type failedJSONWriter struct{ *httptest.ResponseRecorder }

func (failedJSONWriter) Write([]byte) (int, error) { return 0, errRetryDelivery }

type readMessageBatch struct {
	jetstream.MessageBatch

	messages <-chan jetstream.Msg
}

func (batch readMessageBatch) Messages() <-chan jetstream.Msg { return batch.messages }

func TestReadEncodingFailures(t *testing.T) {
	t.Parallel()

	response := httptest.NewRecorder()
	writeReadJSON(response, json.RawMessage(`{`))
	checkEqual(t, response.Code, http.StatusInternalServerError)
	writeReadJSON(failedJSONWriter{ResponseRecorder: httptest.NewRecorder()}, "event")

	messages := make(chan jetstream.Msg, 1)
	messages <- invalidSessionMessage{Msg: nil}

	close(messages)

	var page eventPage

	err := page.appendBatch(readMessageBatch{MessageBatch: nil, messages: messages})
	checkEqual(t, errors.Is(err, jetstream.ErrNotJSMessage), true)

	var peer session
	checkEqual(t, peer.write(t.Context(), json.RawMessage(`{`)) != nil, true)
}

func TestReadTokenDecodingError(t *testing.T) {
	t.Parallel()

	var tokens readTokens

	tokens.path = filepath.Join(t.TempDir(), "tokens")
	checkError(t, os.WriteFile(tokens.path, []byte("reader:"+strings.Repeat("x", 64)), 0o600))
	checkEqual(t, errors.Is(tokens.load(), errTokenFormat), true)
}

func TestRunnerDeliveryMetadataAndRetryErrors(t *testing.T) {
	t.Parallel()

	var run runner

	target := runnerTarget{
		Start: startPosition{at: nil, all: false}, Subjects: nil,
		Target: fakeTarget{name: "fixture", deliver: func(context.Context, Event) error { return errRetryDelivery }},
	}
	checkEqual(t, run.deliver(t.Context(), target, invalidSessionMessage{Msg: nil}, 1), uint64(1))
	checkEqual(
		t,
		run.deliver(t.Context(), target, runnerMessage{Msg: nil, ackError: nil, nakError: errRetryDelivery}, 1),
		uint64(1),
	)
}

type failedTraceClient struct{ otlptrace.Client }

func (failedTraceClient) Start(context.Context) error { return errRetryDelivery }

func TestOTLPInitializationFailures(t *testing.T) {
	t.Setenv("OTEL_GO_X_OBSERVABILITY", "false")

	ctx := t.Context()
	_, _, err := openTelemetryWithTrace(ctx, slog.DiscardHandler,
		otlpConfig{Endpoint: "http://127.0.0.1:4318", Headers: nil, CAFile: ""},
		func(ctx context.Context, res *resource.Resource, _ otlptrace.Client) (*trace.TracerProvider, error) {
			return newOTLPTracer(ctx, res, failedTraceClient{Client: nil})
		})
	checkEqual(t, errors.Is(err, errRetryDelivery), true)

	reader := sdkmetric.NewManualReader()

	var export otlpMetrics
	checkEqual(t, export.collect(ctx, reader) != nil, true)

	checkEqual(t, otlpSeverity(math.MinInt32-1), 0)
	checkEqual(t, otlpSeverity(math.MaxInt32+1), 0)

	var counter metric.Int64Counter

	signals := newTelemetry(slog.DiscardHandler, nil, nil)
	signals.registerCounter(sdkmetric.NewMeterProvider().Meter("fixture"), "", &counter)
	checkEqual(t, counter != nil, true)
}

func TestPeriodicMetricCollection(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()

	ticks := make(chan time.Time, 2)
	ticks <- time.Now()

	ticks <- time.Now()

	calls := 0

	periodicMetrics(ctx, ticks, func(context.Context) error {
		calls++
		if calls == 1 {
			return errRetryDelivery
		}

		cancel()

		return nil
	})
	checkEqual(t, calls, 2)
}
