// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"crypto/tls"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-logr/logr/funcr"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"google.golang.org/protobuf/proto"

	metricexport "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	traceexport "go.opentelemetry.io/proto/otlp/collector/trace/v1"
)

const (
	collectorToken = "Bearer collector-fixture"
	collectorURL   = "https://collector.example"
	authorization  = "Authorization"
)

func TestOTLPExplicitConfig(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://[invalid")
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "X-Bogus=env-fixture")

	var paths sync.Map

	collector := httptest.NewTLSServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		checkEqual(t, request.Header.Get(authorization), collectorToken)
		checkEqual(t, request.Header.Get("X-Bogus"), "")

		paths.Store(request.URL.Path, true)
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(collector.Close)

	caFile := filepath.Join(t.TempDir(), "collector.pem")
	cert := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Headers: nil, Bytes: collector.Certificate().Raw})
	checkError(t, os.WriteFile(caFile, cert, 0o600))

	_, _, err := openTelemetry(t.Context(), slog.DiscardHandler, otlpConfig{Endpoint: "", Headers: nil, CAFile: ""})
	checkError(t, err)

	signals, closeSignals, err := openTelemetry(t.Context(), slog.DiscardHandler, otlpConfig{
		Endpoint: collector.URL + "/otlp",
		Headers:  map[string]string{authorization: collectorToken},
		CAFile:   caFile,
	})
	checkError(t, err)

	hook := &githubWebhook{
		store: testStore(t, retention), telemetry: signals, secret: []byte(fixtureKey), bodyCap: defaultBodyCap,
	}
	expectStatus(t, hook.ServeHTTP, signedRequest(t.Context(), issuesEvent, "1", "{}"), http.StatusAccepted)
	conflict := signedRequest(t.Context(), issuesEvent, "1", `{"text":"raw-payload-fixture"}`)
	expectStatus(t, hook.ServeHTTP, conflict, http.StatusConflict)
	closeSignals()

	for _, path := range []string{"/otlp/v1/metrics", "/otlp/v1/traces", "/otlp/v1/logs"} {
		_, seen := paths.Load(path)
		checkEqual(t, seen, true)
	}
}

func TestCollectorTLS(t *testing.T) {
	t.Parallel()

	writer, err := newOTLPWriter(otlpConfig{Endpoint: collectorURL, Headers: nil, CAFile: ""})
	checkError(t, err)

	transport, ok := writer.client.Transport.(*http.Transport)
	checkEqual(t, ok, true)
	checkEqual(t, transport.TLSClientConfig.MinVersion, uint16(tls.VersionTLS12))
	checkEqual(t, transport.TLSClientConfig.RootCAs == nil, true)

	badCA := filepath.Join(t.TempDir(), "collector.pem")
	_, err = newOTLPWriter(otlpConfig{Endpoint: collectorURL, Headers: nil, CAFile: badCA})
	checkEqual(t, errors.Is(err, errCollectorCA), true)

	checkError(t, os.WriteFile(badCA, []byte("invalid"), 0o600))
	_, err = newOTLPWriter(otlpConfig{Endpoint: collectorURL, Headers: nil, CAFile: badCA})
	checkEqual(t, errors.Is(err, errCollectorCA), true)
}

func TestOTLPEndpointValidation(t *testing.T) {
	t.Parallel()

	for _, endpoint := range []string{
		"https://:4318", "http://[invalid", "https://user@collector.example",
		"ftp://collector.example", "http://192.0.2.1:4318", "http://collector.example",
		"https://host:4318/v1?tenant=x", "https://host:4318/#frag", "https://host:4318/?",
	} {
		cfg := otlpConfig{Endpoint: endpoint, Headers: nil, CAFile: ""}
		_, _, err := openTelemetry(t.Context(), slog.DiscardHandler, cfg)
		checkEqual(t, errors.Is(err, errOTLPEndpoint), true)
	}

	_, err := newOTLPWriter(otlpConfig{Endpoint: "http://localhost:4318", Headers: nil, CAFile: ""})
	checkError(t, err)
}

func TestOTLPRedirects(t *testing.T) {
	t.Parallel()

	for _, status := range []int{
		http.StatusMovedPermanently, http.StatusFound, http.StatusSeeOther,
		http.StatusTemporaryRedirect, http.StatusPermanentRedirect,
	} {
		collector := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
			if request.URL.Path == "/v1/traces" {
				http.Redirect(response, request, "/target", status)

				return
			}

			t.Errorf("redirect %d sent %s to %s", status, request.Method, request.URL.Path)
		}))
		t.Cleanup(collector.Close)

		writer, err := newOTLPWriter(otlpConfig{Endpoint: collector.URL, Headers: nil, CAFile: ""})
		checkError(t, err)
		checkEqual(t, writer.UploadTraces(t.Context(), nil), errOTLPExport)

		for _, target := range []string{"https" + strings.TrimPrefix(collector.URL, "http"), "http://other.example"} {
			next := httptest.NewRequestWithContext(t.Context(), http.MethodGet, target, nil)
			checkEqual(t, writer.client.CheckRedirect(next, nil), http.ErrUseLastResponse)
		}
	}
}

func TestOTLPFailureKeepsWebhookAccepted(t *testing.T) {
	collector := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		http.Error(response, collectorToken, http.StatusBadRequest)
	}))
	t.Cleanup(collector.Close)

	previous := otel.GetErrorHandler()

	t.Cleanup(func() { otel.SetErrorHandler(previous) })
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) { t.Errorf("SDK exposed an error: %v", err) }))
	otel.SetLogger(funcr.New(func(prefix, args string) {
		t.Errorf("SDK exposed a diagnostic: %s %s", prefix, args)
	}, funcr.Options{}))

	var logs bytes.Buffer

	signals, closeSignals, err := openTelemetry(t.Context(), slog.NewJSONHandler(&logs, nil), otlpConfig{
		Endpoint: collector.URL, Headers: map[string]string{authorization: collectorToken}, CAFile: "",
	})
	checkError(t, err)
	hook := &githubWebhook{
		store: testStore(t, retention), telemetry: signals, secret: []byte(fixtureKey), bodyCap: defaultBodyCap,
	}
	expectStatus(t, hook.ServeHTTP, signedRequest(t.Context(), issuesEvent, "1", "{}"), http.StatusAccepted)

	otel.Handle(errFixtureExport)
	closeSignals()
	checkEqual(t, strings.Contains(logs.String(), collectorToken), false)
	checkEqual(t, strings.Contains(logs.String(), "OTLP shutdown failed"), true)

	collector.Close()
	writer, err := newOTLPWriter(otlpConfig{Endpoint: collector.URL, Headers: nil, CAFile: ""})
	checkError(t, err)

	unusedMetric := &metricdata.ResourceMetrics{
		Resource: nil,

		ScopeMetrics: []metricdata.ScopeMetrics{{Metrics: []metricdata.Metrics{{}}}},
	}
	checkEqual(
		t,
		(otlpMetrics{otlpWriter: writer, start: time.Time{}}).Export(t.Context(), unusedMetric),
		errOTLPExport,
	)
	checkEqual(t, writer.send(t.Context(), "traces", otlpString("\xff")), errOTLPExport)

	writer.endpoint.Host = "invalid host"
	checkEqual(t, writer.UploadTraces(t.Context(), nil), errOTLPExport)
}

var errFixtureExport = errors.New("collector reported " + collectorToken)

const enabled = "true"

// setOTELVariables sets every OTEL_* variable that could change the exported telemetry.
func setOTELVariables(t *testing.T) {
	t.Helper()

	for key, value := range map[string]string{
		"OTEL_SDK_DISABLED":               enabled,
		"OTEL_TRACES_SAMPLER":             "always_off",
		"OTEL_RESOURCE_ATTRIBUTES":        "env.leak=yes,service.name=env-service",
		"OTEL_SERVICE_NAME":               "env-service",
		"OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT": "0",
		"OTEL_BSP_MAX_QUEUE_SIZE":         "1",
		"OTEL_BLRP_MAX_QUEUE_SIZE":        "1",
		"OTEL_METRIC_EXPORT_INTERVAL":     "1",
		"OTEL_METRICS_EXEMPLAR_FILTER":    "always_on",
		"OTEL_METRIC_EXPORT_TIMEOUT":      "1",
		"OTEL_BSP_SCHEDULE_DELAY":         "1",
		"OTEL_BLRP_SCHEDULE_DELAY":        "1",
		"OTEL_GO_X_CARDINALITY_LIMIT":     "1",
		"OTEL_GO_X_OBSERVABILITY":         enabled,

		"OTEL_GO_X_PER_SERIES_START_TIMESTAMPS": enabled,
	} {
		t.Setenv(key, value)
	}
}

func TestOTLPIgnoresOTELVariables(t *testing.T) {
	setOTELVariables(t)

	var (
		lock    sync.Mutex
		traces  traceexport.ExportTraceServiceRequest
		metrics metricexport.ExportMetricsServiceRequest
		early   = map[string]int{}
		closed  bool
	)

	collector := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		checkError(t, err)

		lock.Lock()
		defer lock.Unlock()

		if !closed {
			early[request.URL.Path]++
		}

		switch request.URL.Path {
		case "/v1/traces":
			checkError(t, proto.Unmarshal(body, &traces))
		case "/v1/metrics":
			checkError(t, proto.Unmarshal(body, &metrics))
		}

		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(collector.Close)

	signals, closeSignals, err := openTelemetry(t.Context(), slog.DiscardHandler, otlpConfig{
		Endpoint: collector.URL, Headers: nil, CAFile: "",
	})
	checkError(t, err)

	opened := time.Now()
	hook := &githubWebhook{
		store: testStore(t, retention), telemetry: signals, secret: []byte(fixtureKey), bodyCap: defaultBodyCap,
	}
	// Two series (202 and 409), created after the provider, so per-series start times would differ.
	expectStatus(t, hook.ServeHTTP, signedRequest(t.Context(), issuesEvent, "1", "{}"), http.StatusAccepted)
	time.Sleep(20 * time.Millisecond)

	conflict := signedRequest(t.Context(), issuesEvent, "1", `{"text":"x"}`)
	expectStatus(t, hook.ServeHTTP, conflict, http.StatusConflict)

	// The pinned one-minute reader interval and batch delays keep every signal queued until shutdown.
	// A 1ms interval or delay from the environment would export within this wait.
	time.Sleep(200 * time.Millisecond)
	lock.Lock()
	checkEqual(t, len(early), 0)

	closed = true

	lock.Unlock()
	closeSignals()

	lock.Lock()
	defer lock.Unlock()

	// The sampler keeps the span and the attribute limit keeps its attributes.
	checkEqual(t, len(traces.GetResourceSpans()), 1)
	spans := traces.GetResourceSpans()[0]
	checkEqual(t, len(spans.GetScopeSpans()[0].GetSpans()), 2)
	checkEqual(t, len(spans.GetScopeSpans()[0].GetSpans()[0].GetAttributes()), 2)

	// The resource holds only the explicit service name.
	checkEqual(t, len(spans.GetResource().GetAttributes()), 1)
	checkEqual(t, spans.GetResource().GetAttributes()[0].GetKey(), "service.name")
	checkEqual(t, spans.GetResource().GetAttributes()[0].GetValue().GetStringValue(), serviceName)

	checkEqual(t, len(metrics.GetResourceMetrics()), 1)
	resource := metrics.GetResourceMetrics()[0].GetResource()
	checkEqual(t, len(resource.GetAttributes()), 1)
	checkEqual(t, resource.GetAttributes()[0].GetValue().GetStringValue(), serviceName)

	metric := metrics.GetResourceMetrics()[0].GetScopeMetrics()[0].GetMetrics()[0]
	checkEqual(t, metric.GetName(), "event_gateway_ingest_total")

	// The pinned cardinality limit keeps both series; a limit of 1 would fold one into an overflow series.
	points := metric.GetSum().GetDataPoints()
	checkEqual(t, len(points), 2)

	// The wire start time is one explicit value, never the SDK's per-series start time. It is the
	// provider's creation time, so it cannot be later than the first webhook request.
	for _, point := range points {
		checkEqual(t, point.GetAsInt(), int64(1))
		checkEqual(t, point.GetStartTimeUnixNano(), points[0].GetStartTimeUnixNano())
		checkEqual(t, point.GetStartTimeUnixNano() <= uint64(opened.UnixNano()), true)
		checkEqual(t, len(point.GetAttributes()), 2)
	}

	// Pins with no observable effect here: the metric exemplar filter (the exporter does not
	// forward exemplars) and OTEL_METRIC_EXPORT_TIMEOUT (a local collector replies too fast to
	// time out reliably).
}
