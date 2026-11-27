// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

const (
	parkedPayloadFixture  = "private-payload-fixture"
	parkedPayloadJSON     = `{"value":"` + parkedPayloadFixture + `"}`
	parkedFixtureProvider = "github"
)

var errLongParked = errors.New(strings.Repeat("x", 512) + "overflow")

func shortSocketDir(t *testing.T) string {
	t.Helper()

	dir, err := os.MkdirTemp("/tmp", "eg-")
	if err != nil {
		dir, err = os.MkdirTemp("", "eg-")
		checkError(t, err)
	}

	t.Cleanup(func() { checkError(t, os.RemoveAll(dir)) })

	return dir
}

func testParkedStream(t *testing.T, store eventStore) jetstream.Stream {
	t.Helper()

	stream, err := openParkedStream(t.Context(), store.jet)
	checkError(t, err)

	return stream
}

func TestParkedStreamConfig(t *testing.T) {
	t.Parallel()

	ctx := t.Context()
	store := testStore(t, retention)
	before := store.stream.CachedInfo().Config
	info := testParkedStream(t, store).CachedInfo()
	checkEqual(t, slices.Equal(info.Config.Subjects, []string{"parked.>"}), true)
	checkEqual(t, info.Config.Storage, jetstream.FileStorage)
	checkEqual(t, info.Config.Replicas, 1)
	checkEqual(t, info.Config.MaxAge, 30*24*time.Hour)

	after, err := store.stream.Info(ctx)
	checkError(t, err)
	checkEqual(t, reflect.DeepEqual(after.Config, before), true)

	canceled, cancel := context.WithCancel(ctx)
	cancel()

	_, err = openParkedStream(canceled, store.jet)
	checkEqual(t, errors.Is(err, context.Canceled), true)
}

func TestParkedRecord(t *testing.T) {
	t.Parallel()

	const delivery = "delivery-1"

	ctx := t.Context()
	store := testStore(t, retention)
	stream := testParkedStream(t, store)
	req := signedRequest(ctx, issuesEvent, delivery, parkedPayloadJSON)
	testWebhook(t, store, defaultBodyCap).ServeHTTP(httptest.NewRecorder(), req)

	stored, readErr := store.stream.GetMsg(ctx, 1)
	checkError(t, readErr)

	valid := Event{Envelope: stored.Data, Seq: stored.Sequence}
	bodyErr := fmt.Errorf("%w: %s %s", ErrPermanent, stored.Data, parkedPayloadJSON)
	hook := parkHook(store.jet, newTelemetry(slog.DiscardHandler, nil, nil), 0)

	for _, test := range []struct {
		err   error
		name  string
		want  parkedRecord
		event Event
	}{
		{
			name: "wrapped-rule", event: valid,
			err: fmt.Errorf("delivery: %w", RuleError{Rule: "select", Err: bodyErr}),
			want: parkedRecord{
				Target:   "",
				ParkedAt: "",
				Seq:      0,

				Rule: "select", Provider: parkedFixtureProvider, Event: issuesEvent, Delivery: delivery,
				Error: "delivery: permanent: [redacted] [redacted]",
			},
		},
		{
			name:  "undecodable-envelope",
			event: Event{Envelope: []byte(`{"provider":"github","event":5,"delivery":"delivery-1"}`), Seq: 2},
			err:   errLongParked,
			want:  parkedRecord{Error: strings.Repeat("x", 512)},
		},
		{
			name:  "short-payload",
			event: Event{Seq: 0, Envelope: []byte(`{"payload":0}`)},
			err:   fmt.Errorf("%w: status 400", ErrPermanent),
			want:  parkedRecord{Error: "permanent: status 400"},
		},
		{
			name:  "eight-byte-payload",
			event: Event{Seq: 0, Envelope: []byte(`{"payload":12345678}`)},
			err:   fmt.Errorf("%w: 12345678 12345678", ErrPermanent),
			want:  parkedRecord{Error: "permanent: [redacted] [redacted]"},
		},
		{
			name: "typed-nil-error", event: valid,
			err: (*json.SyntaxError)(nil),
			want: parkedRecord{
				Target:   "",
				Rule:     "",
				ParkedAt: "",
				Seq:      0,

				Provider: parkedFixtureProvider, Event: issuesEvent, Delivery: delivery, Error: ErrPermanent.Error(),
			},
		},
		{
			name: "empty-rule-error", event: valid,
			err: RuleError{Err: nil, Rule: ""},
			want: parkedRecord{
				Target:   "",
				Rule:     "",
				ParkedAt: "",
				Seq:      0,

				Provider: parkedFixtureProvider, Event: issuesEvent, Delivery: delivery, Error: ErrPermanent.Error(),
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			subject := "parked." + test.name
			hook(t.Context(), test.name, test.event, test.err)

			msg, err := stream.GetLastMsgForSubject(t.Context(), subject)
			checkError(t, err)
			checkEqual(t, msg.Subject, subject)
			checkEqual(t, bytes.Contains(msg.Data, []byte(parkedPayloadFixture)), false)

			var record parkedRecord
			checkError(t, json.Unmarshal(msg.Data, &record))
			parkedAt, err := time.Parse(time.RFC3339, record.ParkedAt)
			checkError(t, err)
			checkEqual(t, parkedAt.Location(), time.UTC)

			test.want.ParkedAt, test.want.Seq, test.want.Target = record.ParkedAt, test.event.Seq, test.name
			checkEqual(t, record, test.want)
		})
	}
}

func TestParkedTelemetry(t *testing.T) {
	t.Parallel()

	const timeout = 50 * time.Millisecond

	ctx := t.Context()
	store := testStore(t, retention)
	testParkedStream(t, store)

	var logs bytes.Buffer

	reader := sdkmetric.NewManualReader()
	meter := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	signals := newTelemetry(slog.NewJSONHandler(&logs, nil), nil, meter)
	envelopeJSON := `{"provider":"github","event":"issues","payload":"` + parkedPayloadFixture + `"}`
	event := Event{Envelope: []byte(envelopeJSON), Seq: 1}
	failure := RuleError{Rule: "match", Err: ErrPermanent}

	canceled, cancel := context.WithCancel(ctx)
	cancel()
	parkHook(store.jet, signals, 0)(canceled, "sample", event, failure)
	checkError(t, store.jet.DeleteStream(ctx, parkedStream))

	before := time.Now()

	parkHook(store.jet, signals, timeout)(ctx, "sample", event, failure)

	checkEqual(t, time.Since(before) < timeout+time.Second, true)
	checkEqual(t, strings.Contains(logs.String(), parkedPayloadFixture), false)

	checkEqual(t, strings.Count(logs.String(), "\n"), 2)
	checkEqual(t, strings.Contains(logs.String(), `"msg":"event parked"`), true)
	checkEqual(t, strings.Contains(logs.String(), `"msg":"park publish failed"`), true)

	var collected metricdata.ResourceMetrics
	checkError(t, reader.Collect(ctx, &collected))

	counts := map[string]int64{}

	for _, instrument := range collected.ScopeMetrics[0].Metrics {
		data, ok := instrument.Data.(metricdata.Sum[int64])
		checkEqual(t, ok, true)
		checkEqual(t, len(data.DataPoints), 1)
		point := data.DataPoints[0]
		target, ok := point.Attributes.Value("target")
		checkEqual(t, ok, true)
		checkEqual(t, target.AsString(), "sample")

		counts[instrument.Name] = point.Value
	}

	checkEqual(t, counts["event_gateway_parked_total"], int64(1))
	checkEqual(t, counts["event_gateway_park_publish_failed_total"], int64(1))
}

func TestListParked(t *testing.T) {
	t.Parallel()

	ctx := t.Context()
	store := testStore(t, retention)
	api := parkedAPI{targets: nil, store: store}
	callParked(t, api, http.MethodGet, parkedListPath, "", http.StatusServiceUnavailable)
	stream := testParkedStream(t, store)
	empty, listErr := listParked(ctx, stream, "", 0)
	checkError(t, listErr)
	checkEqual(t, len(empty), 0)

	const firstTarget = "one"

	hook := parkHook(store.jet, newTelemetry(slog.DiscardHandler, nil, nil), 0)
	for i, target := range []string{"two", firstTarget, firstTarget} {
		hook(ctx, target, Event{Envelope: nil, Seq: uint64(i + 1)}, ErrPermanent)
	}

	for _, test := range []struct {
		target string
		want   []uint64
		limit  int
	}{
		{limit: 0, target: firstTarget, want: []uint64{2, 3}},
		{target: "", limit: 0, want: []uint64{1, 2, 3}},
		{target: "", limit: 2, want: []uint64{1, 2}},
	} {
		records, err := listParked(ctx, stream, test.target, test.limit)
		checkError(t, err)

		checkEqual(t, len(records), len(test.want))

		for i, record := range records {
			checkEqual(t, record.Seq, test.want[i])
		}
	}

	var filtered []parkedRecord

	body := callParked(t, api, http.MethodGet, parkedListPath+"?target="+firstTarget+"&limit=1", "", http.StatusOK)
	checkError(t, json.Unmarshal([]byte(body), &filtered))
	checkEqual(t, len(filtered), 1)
	checkEqual(t, filtered[0].Seq, uint64(2))

	for _, query := range []string{"?limit=no", "?limit=-1"} {
		callParked(t, api, http.MethodGet, parkedListPath+query, "", http.StatusBadRequest)
	}

	_, err := listParked(ctx, stream, "", -1)
	checkEqual(t, errors.Is(err, errParkedLimit), true)
	_, err = listParked(ctx, parkedPullStream{Stream: stream}, "", 0)
	checkEqual(t, errors.Is(err, errRetryDelivery), true)
	_, err = store.jet.Publish(ctx, "parked.one", []byte("invalid JSON"))
	checkError(t, err)
	_, err = listParked(ctx, stream, "", 0)

	callParked(t, api, http.MethodGet, parkedListPath, "", http.StatusServiceUnavailable)

	var syntax *json.SyntaxError

	checkEqual(t, errors.As(err, &syntax), true)
	checkError(t, store.jet.DeleteStream(ctx, parkedStream))
	_, err = listParked(ctx, stream, "", 0)
	checkEqual(t, errors.Is(err, jetstream.ErrStreamNotFound), true)
}

type failedParkedPull struct{ jetstream.Consumer }

func (failedParkedPull) FetchNoWait(int) (jetstream.MessageBatch, error) {
	return nil, errRetryDelivery
}

type parkedPullStream struct{ jetstream.Stream }

func (stream parkedPullStream) OrderedConsumer(
	ctx context.Context, config jetstream.OrderedConsumerConfig,
) (jetstream.Consumer, error) {
	consumer, err := stream.Stream.OrderedConsumer(ctx, config)

	return failedParkedPull{Consumer: consumer}, err
}

func TestRetryParked(t *testing.T) {
	t.Parallel()

	ctx := t.Context()
	store := testStore(t, retention)
	stream := testParkedStream(t, store)
	publishRunner(t, store, issuesSubject)
	publishRunner(t, store, issuesSubject)
	checkError(t, store.stream.DeleteMsg(ctx, 1))
	parkHook(
		store.jet,
		newTelemetry(slog.DiscardHandler, nil, nil),
		0,
	)(
		ctx,
		"retry",
		Event{Envelope: nil, Seq: 2},
		ErrPermanent,
	)

	for _, test := range []struct {
		deliveryErr error
		want        error
		seq         uint64
		calls       int
		panics      bool
	}{
		{deliveryErr: nil, want: nil, panics: false, seq: 2, calls: 1},
		{panics: false, seq: 2, calls: 1, deliveryErr: errRetryDelivery, want: errRetryDelivery},
		{deliveryErr: nil, seq: 2, calls: 1, panics: true, want: ErrPermanent},
		{panics: false, seq: 2, calls: 1, deliveryErr: fmt.Errorf("%w: %s", ErrPermanent, parkedPayloadFixture), want: ErrPermanent},
		{deliveryErr: nil, calls: 0, panics: false, seq: 1, want: jetstream.ErrMsgNotFound},
		{deliveryErr: nil, calls: 0, panics: false, seq: 3, want: jetstream.ErrMsgNotFound},
	} {
		calls := 0
		target := fakeTarget{name: "retry", deliver: func(_ context.Context, event Event) error {
			calls++

			checkEqual(t, event.Seq, test.seq)
			checkEqual(t, string(event.Envelope), testEnvelope)

			if test.panics {
				panic(testEnvelope)
			}

			return test.deliveryErr
		}}
		err := retryParked(ctx, store, target, test.seq)
		checkEqual(t, errors.Is(err, test.want), true)
		checkEqual(t, calls, test.calls)

		api := parkedAPI{store: store, targets: []runnerTarget{{Start: startPosition{}, Subjects: nil, Target: target}}}
		body := fmt.Sprintf(`{"target":%q,"seq":%d}`, target.name, test.seq)

		status, wantBody := http.StatusInternalServerError, "retry failed\n"
		if test.want == nil {
			status, wantBody = http.StatusNoContent, ""
		}

		checkEqual(t, callParked(t, api, http.MethodPost, parkedRetryPath, body, status), wantBody)
		checkEqual(t, calls, 2*test.calls)
	}

	api := parkedAPI{
		store:   store,
		targets: []runnerTarget{{Start: startPosition{}, Subjects: nil, Target: fakeTarget{name: "present"}}},
	}
	for _, body := range []string{"{", `{}`, `{"target":"retry"}`, `{"target":"retry","seq":1}[]`} {
		callParked(t, api, http.MethodPost, parkedRetryPath, body, http.StatusBadRequest)
	}

	callParked(t, api, http.MethodPost, parkedRetryPath, `{"target":"missing","seq":2}`, http.StatusNotFound)

	info, err := stream.Info(ctx)
	checkError(t, err)
	checkEqual(t, info.State.Msgs, uint64(1))
}

func callParked(t *testing.T, api parkedAPI, method, path, body string, status int) string {
	t.Helper()

	response := httptest.NewRecorder()
	api.handler().
		ServeHTTP(response, httptest.NewRequestWithContext(t.Context(), method, path, strings.NewReader(body)))
	checkEqual(t, response.Code, status)

	return response.Body.String()
}

func TestParkedCLI(t *testing.T) {
	t.Setenv(parkedSocketEnv, "from-env.sock")
	ctx := t.Context()

	for _, args := range [][]string{
		nil,
		{"other"},
		{parkedListCommand, "-bad"},
		{parkedListCommand, "extra-arg"},
		{parkedRetryCommand, "-bad"},
		{parkedRetryCommand, "target"},
		{parkedRetryCommand, "target", "invalid"},
	} {
		err := run(ctx, io.Discard, slog.DiscardHandler, append([]string{parkedCommand}, args...))
		checkEqual(t, err, errParkedUsage)
	}

	for _, transport := range []error{os.ErrNotExist, syscall.ECONNREFUSED} {
		err := parkedResponse(io.Discard, "missing.sock", nil, transport)
		checkEqual(t, err.Error(), "event-gateway is not running (cannot reach missing.sock)")
		checkEqual(t, exitCode(ctx, slog.New(slog.DiscardHandler), err), 1)
	}

	response := &http.Response{StatusCode: http.StatusNoContent, Body: http.NoBody}

	checkError(t, runParked(ctx, io.Discard, []string{parkedRetryCommand, "sample", "3"},
		func(string, *http.Request) (*http.Response, error) { return response, nil }))

	response.StatusCode = http.StatusNotFound
	checkEqual(t, errors.Is(parkedResponse(io.Discard, "", response, nil), errParkedRequest), true)
	checkEqual(t, printParked(io.Discard, strings.NewReader("invalid")), errParkedRequest)

	checkEqual(t, parkedResponse(io.Discard, "", nil, os.ErrPermission), errParkedRequest)

	const record = `{"target":"sample","rule":"select","provider":"github","event":"push",` +
		`"delivery":"fixture","error":"failed","parked_at":"2026-11-24T00:00:00Z","seq":3}`

	var out bytes.Buffer

	args := []string{parkedListCommand, "-target=sample", "-limit=1"}
	err := runParked(ctx, &out, args, func(path string, req *http.Request) (*http.Response, error) {
		checkEqual(t, path, "from-env.sock")
		checkEqual(t, req.URL.RequestURI(), parkedListPath+"?limit=1&target=sample")

		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("[" + record + "]"))}, nil
	})
	checkError(t, err)
	checkEqual(t, out.String(), record+"\n")
}
