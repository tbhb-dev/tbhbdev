// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package telemetry_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/metric/noop"
	"google.golang.org/protobuf/proto"

	collectorpb "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	metricspb "go.opentelemetry.io/proto/otlp/metrics/v1"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/telemetry"
)

const endpointEnv = "OTEL_EXPORTER_OTLP_ENDPOINT"

func TestNewHTTPClient(t *testing.T) {
	t.Parallel()

	client, err := telemetry.NewHTTPClient()
	if err != nil {
		t.Fatal(err)
	}

	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("transport has type %T, want *http.Transport", client.Transport)
	}

	const want = 45 * time.Second
	if transport.IdleConnTimeout != want {
		t.Errorf("idle connection timeout = %v, want %v", transport.IdleConnTimeout, want)
	}
}

func TestSetupWithoutEndpoint(t *testing.T) {
	t.Setenv(endpointEnv, "")

	shutdown, meter, err := telemetry.Setup(t.Context(), "test-service", "test-version")
	if err != nil {
		t.Fatal(err)
	}

	if _, ok := meter.(noop.Meter); !ok {
		t.Fatalf("meter has type %T, want noop.Meter", meter)
	}

	shutdownErr := shutdown(t.Context())
	if shutdownErr != nil {
		t.Fatal(shutdownErr)
	}
}

func TestSetupExportsOnShutdown(t *testing.T) {
	requests := make(chan []byte, 1)
	server := exportServer(t, requests)

	defer server.Close()

	t.Setenv(endpointEnv, server.URL)

	shutdown := recordTestMetric(t)

	err := shutdown(t.Context())
	if err != nil {
		t.Fatal(err)
	}

	if got := len(requests); got != 1 {
		t.Fatalf("export requests = %d, want 1", got)
	}

	checkExport(t, <-requests)
}

func TestSetupRetriesConnectionClose(t *testing.T) {
	for _, test := range []struct {
		wantErr     error
		name        string
		alwaysClose bool
	}{
		{name: "first connection closes", alwaysClose: false, wantErr: nil},
		{name: "every connection closes", alwaysClose: true, wantErr: io.EOF},
	} {
		t.Run(test.name, func(t *testing.T) {
			var attempts atomic.Int32

			server := retryServer(t, &attempts, test.alwaysClose)
			defer server.Close()

			t.Setenv(endpointEnv, server.URL)

			shutdown := recordTestMetric(t)
			err := shutdown(t.Context())

			if !errors.Is(err, test.wantErr) {
				t.Errorf("shutdown error = %v, want %v", err, test.wantErr)
			}

			if got := attempts.Load(); got != 2 {
				t.Errorf("export requests = %d, want 2", got)
			}
		})
	}
}

func recordTestMetric(t *testing.T) func(context.Context) error {
	t.Helper()

	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta")
	t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", "3600000")

	shutdown, meter, err := telemetry.Setup(t.Context(), "test-service", "test-version")
	if err != nil {
		t.Fatal(err)
	}

	counter, err := meter.Int64Counter("test.counter")
	if err != nil {
		t.Fatal(err)
	}

	counter.Add(t.Context(), 1)

	return shutdown
}

func retryServer(t *testing.T, attempts *atomic.Int32, alwaysClose bool) *httptest.Server {
	t.Helper()

	return httptest.NewServer(
		http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			attempt := attempts.Add(1)

			_, err := io.Copy(io.Discard, request.Body)
			if err != nil {
				t.Error(err)
			}

			if attempt == 1 || alwaysClose {
				closeConnection(t, writer)
			}
		}),
	)
}

func closeConnection(t *testing.T, writer http.ResponseWriter) {
	t.Helper()

	conn, _, err := http.NewResponseController(writer).Hijack()
	if err != nil {
		t.Error(err)

		return
	}

	closeErr := conn.Close()
	if closeErr != nil {
		t.Error(closeErr)
	}
}

func exportServer(t *testing.T, requests chan<- []byte) *httptest.Server {
	t.Helper()

	return httptest.NewServer(
		http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
			if request.Method != http.MethodPost || request.URL.Path != "/v1/metrics" {
				t.Errorf("request = %s %s, want POST /v1/metrics", request.Method, request.URL.Path)
			}

			if got := request.Header.Get("Content-Type"); got != "application/x-protobuf" {
				t.Errorf("Content-Type = %q, want application/x-protobuf", got)
			}

			body, err := io.ReadAll(request.Body)
			if err != nil {
				t.Error(err)
			}

			requests <- body

			writer.Header().Set("Content-Type", "application/x-protobuf")
		}),
	)
}

func checkExport(t *testing.T, body []byte) {
	t.Helper()

	var request collectorpb.ExportMetricsServiceRequest

	err := proto.Unmarshal(body, &request)
	if err != nil {
		t.Fatal(err)
	}

	metrics := request.GetResourceMetrics()
	if len(metrics) != 1 {
		t.Fatalf("resource metrics = %d, want 1", len(metrics))
	}

	checkResourceAttributes(t, metrics[0])

	scopes := metrics[0].GetScopeMetrics()
	if len(scopes) != 1 || len(scopes[0].GetMetrics()) != 1 {
		t.Fatalf("scope metrics = %v, want one counter", scopes)
	}

	exported := scopes[0].GetMetrics()[0]
	sum := exported.GetSum()

	if exported.GetName() != "test.counter" ||
		sum.GetAggregationTemporality() != metricspb.AggregationTemporality_AGGREGATION_TEMPORALITY_CUMULATIVE {
		t.Errorf("exported metric = %v, want cumulative test.counter", exported)
	}

	points := sum.GetDataPoints()
	if len(points) != 1 || points[0].GetAsInt() != 1 {
		t.Errorf("data points = %v, want one observation of 1", points)
	}
}

func checkResourceAttributes(t *testing.T, resource *metricspb.ResourceMetrics) {
	t.Helper()

	attrs := make(map[string]string)
	for _, attr := range resource.GetResource().GetAttributes() {
		attrs[attr.GetKey()] = attr.GetValue().GetStringValue()
	}

	if attrs["service.name"] != "test-service" || attrs["service.version"] != "test-version" {
		t.Errorf("resource attributes = %v, want service name and version", attrs)
	}
}
