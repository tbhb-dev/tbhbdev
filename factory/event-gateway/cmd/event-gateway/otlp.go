// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"sync"
	"time"

	"github.com/go-logr/logr"
	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace"
	"go.opentelemetry.io/otel/sdk/metric/exemplar"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"
	"google.golang.org/protobuf/proto"

	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	logexport "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	metricexport "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	traceexport "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	logpb "go.opentelemetry.io/proto/otlp/logs/v1"
	metricpb "go.opentelemetry.io/proto/otlp/metrics/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
)

const (
	exportTimeout = 5 * time.Second

	// The SDK defaults, pinned so that no OTEL_* variable can change them.
	batchDelay       = 5 * time.Second
	batchQueueSize   = 2048
	batchSize        = 512
	logBatchInterval = time.Second
	logBatchTimeout  = 30 * time.Second
	attributeLimit   = 128
	valueDepthLimit  = 64
	cardinalityLimit = 2000
)

var (
	errOTLPEndpoint = errors.New("OTLP endpoint requires HTTPS or loopback HTTP, with no query or fragment")
	errCollectorCA  = errors.New("OTLP CA file must contain PEM certificates")
	errOTLPExport   = errors.New("OTLP export failed")
)

// otlpConfig holds the gateway's explicit collector settings.
type otlpConfig struct {
	Endpoint string
	Headers  map[string]string
	CAFile   string
}

func openTelemetry(ctx context.Context, handler slog.Handler, cfg otlpConfig) (telemetry, func(), error) {
	if cfg.Endpoint == "" {
		return newTelemetry(handler, nil, nil), func() {}, nil
	}

	writer, err := newOTLPWriter(cfg)
	if err != nil {
		return telemetry{}, nil, err
	}

	// Keep SDK diagnostics and collector response text out of the gateway log.
	otel.SetLogger(logr.Discard())
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(error) {}))

	//nolint:errcheck // The HTTP client has no connection to start.
	traceExporter, _ := otlptrace.New(ctx, writer)
	// The SDK's WithResource options always merge OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME
	// into the provider resource, and v1.47.0 has no option to skip that. The wire resource is
	// therefore rebuilt in UploadTraces, and the metric and log exporters already build their own.
	res := resource.NewSchemaless(attribute.String("service.name", serviceName))
	tracer := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())),
		sdktrace.WithRawSpanLimits(sdktrace.SpanLimits{
			AttributeValueLengthLimit:   -1,
			AttributeValueDepthLimit:    valueDepthLimit,
			AttributeCountLimit:         attributeLimit,
			EventCountLimit:             attributeLimit,
			LinkCountLimit:              attributeLimit,
			AttributePerEventCountLimit: attributeLimit,
			AttributePerLinkCountLimit:  attributeLimit,
		}),
		sdktrace.WithBatcher(traceExporter,
			sdktrace.WithBatchTimeout(batchDelay),
			sdktrace.WithMaxQueueSize(batchQueueSize),
			sdktrace.WithMaxExportBatchSize(batchSize),
			sdktrace.WithExportTimeout(exportTimeout),
		),
	)
	meter := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithExemplarFilter(exemplar.TraceBasedFilter),
		sdkmetric.WithCardinalityLimit(cardinalityLimit),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(
			otlpMetrics{otlpWriter: writer, start: time.Now()},
			sdkmetric.WithInterval(time.Minute), sdkmetric.WithTimeout(exportTimeout),
		)),
	)
	logger := sdklog.NewLoggerProvider(
		sdklog.WithResource(res),
		sdklog.WithProcessor(sdklog.NewBatchProcessor(otlpLogs{writer},
			sdklog.WithMaxQueueSize(batchQueueSize),
			sdklog.WithExportInterval(logBatchInterval),
			sdklog.WithExportTimeout(logBatchTimeout),
			sdklog.WithExportMaxBatchSize(batchSize),
		)),
	)
	local := slog.New(handler)
	both := slog.NewMultiHandler(handler, otelslog.NewHandler(serviceName, otelslog.WithLoggerProvider(logger)))

	closeSignals := sync.OnceFunc(func() {
		const shutdownTimeout = 15 * time.Second

		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
		defer cancel()
		defer writer.client.CloseIdleConnections()

		if errors.Join(tracer.Shutdown(shutdown), meter.Shutdown(shutdown), logger.Shutdown(shutdown)) != nil {
			local.ErrorContext(shutdown, "OTLP shutdown failed")
		}
	})

	return newTelemetry(both, tracer, meter), closeSignals, nil
}

type otlpWriter struct {
	endpoint *url.URL
	headers  http.Header
	client   *http.Client
}

func newOTLPWriter(cfg otlpConfig) (*otlpWriter, error) {
	endpoint, err := url.Parse(cfg.Endpoint)
	if err != nil || endpoint.Hostname() == "" || endpoint.User != nil ||
		endpoint.RawQuery != "" || endpoint.ForceQuery || endpoint.Fragment != "" {
		return nil, errOTLPEndpoint
	}

	if endpoint.Scheme != "https" && (endpoint.Scheme != "http" || !loopbackHost(endpoint.Hostname())) {
		return nil, errOTLPEndpoint
	}

	//nolint:exhaustruct_v5 // A nil root pool uses the system trust store.
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}

	if cfg.CAFile != "" {
		pem, readErr := os.ReadFile(cfg.CAFile)
		tlsConfig.RootCAs = x509.NewCertPool()

		if readErr != nil || !tlsConfig.RootCAs.AppendCertsFromPEM(pem) {
			return nil, errCollectorCA
		}
	}

	headers := make(http.Header, len(cfg.Headers)+1)

	for key, value := range cfg.Headers {
		headers.Set(key, value)
	}

	headers.Set("Content-Type", "application/x-protobuf")

	//nolint:exhaustruct_v5 // The exporter uses a direct connection with its own TLS policy.
	transport := &http.Transport{TLSClientConfig: tlsConfig}

	//nolint:exhaustruct_v5 // Collector requests use this transport and redirect policy.
	client := &http.Client{
		Transport:     transport,
		Timeout:       exportTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}

	return &otlpWriter{endpoint: endpoint, headers: headers, client: client}, nil
}

func loopbackHost(host string) bool {
	return host == "localhost" || net.ParseIP(host).IsLoopback()
}

func (*otlpWriter) Start(context.Context) error      { return nil }
func (*otlpWriter) Stop(context.Context) error       { return nil }
func (*otlpWriter) ForceFlush(context.Context) error { return nil }
func (*otlpWriter) Shutdown(context.Context) error   { return nil }

func (w *otlpWriter) UploadTraces(ctx context.Context, spans []*tracepb.ResourceSpans) error {
	for _, group := range spans {
		group.Resource, group.SchemaUrl = otlpResource(), ""
	}

	return w.send(ctx, "traces", &traceexport.ExportTraceServiceRequest{ResourceSpans: spans})
}

func (w *otlpWriter) send(ctx context.Context, signal string, message proto.Message) error {
	payload, err := proto.Marshal(message)
	if err != nil {
		return errOTLPExport
	}

	endpoint := w.endpoint.JoinPath("v1", signal).String()

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return errOTLPExport
	}

	request.Header = w.headers.Clone()

	response, err := w.client.Do(request)
	if err != nil {
		return errOTLPExport
	}
	//nolint:errcheck // Closing the body is cleanup after receiving the response.
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		return errOTLPExport
	}

	return nil
}

type otlpMetrics struct {
	*otlpWriter

	start time.Time
}

func (otlpMetrics) Temporality(kind sdkmetric.InstrumentKind) metricdata.Temporality {
	return sdkmetric.DefaultTemporalitySelector(kind)
}

//nolint:ireturn // The SDK exporter interface requires this return type.
func (otlpMetrics) Aggregation(kind sdkmetric.InstrumentKind) sdkmetric.Aggregation {
	return sdkmetric.DefaultAggregationSelector(kind)
}

//nolint:exhaustruct_v5 // Optional OTLP fields are omitted for the gateway's counter.
func (w otlpMetrics) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	metrics := &metricpb.ResourceMetrics{Resource: otlpResource()}

	for _, scope := range data.ScopeMetrics {
		group := &metricpb.ScopeMetrics{Scope: &commonpb.InstrumentationScope{Name: scope.Scope.Name}}
		for _, metric := range scope.Metrics {
			sum, ok := metric.Data.(metricdata.Sum[int64])
			if !ok {
				continue
			}

			value := &metricpb.Sum{
				IsMonotonic:            sum.IsMonotonic,
				AggregationTemporality: metricpb.AggregationTemporality_AGGREGATION_TEMPORALITY_CUMULATIVE,
			}
			// The SDK forwards per-series start times when OTEL_GO_X_PER_SERIES_START_TIMESTAMPS is
			// set, and v1.47.0 has no option to turn that off. Every cumulative point therefore
			// reports the one start time fixed when the provider was built.
			for _, point := range sum.DataPoints {
				value.DataPoints = append(value.DataPoints, &metricpb.NumberDataPoint{
					Attributes:        otlpAttributes(point.Attributes.ToSlice()),
					StartTimeUnixNano: uint64(w.start.UnixNano()),
					TimeUnixNano:      uint64(point.Time.UnixNano()),
					Value:             &metricpb.NumberDataPoint_AsInt{AsInt: point.Value},
				})
			}

			metric := &metricpb.Metric{Name: metric.Name, Data: &metricpb.Metric_Sum{Sum: value}}
			group.Metrics = append(group.Metrics, metric)
		}

		metrics.ScopeMetrics = append(metrics.ScopeMetrics, group)
	}

	message := &metricexport.ExportMetricsServiceRequest{ResourceMetrics: []*metricpb.ResourceMetrics{metrics}}

	return w.send(ctx, "metrics", message)
}

type otlpLogs struct{ *otlpWriter }

//nolint:exhaustruct_v5,gosec // The bridge supplies valid OTLP severity numbers and the gateway omits optional fields.
func (w otlpLogs) Export(ctx context.Context, records []sdklog.Record) error {
	group := &logpb.ScopeLogs{Scope: &commonpb.InstrumentationScope{Name: serviceName}}

	for i := range records {
		record := &records[i]
		traceID, spanID := record.TraceID(), record.SpanID()
		group.LogRecords = append(group.LogRecords, &logpb.LogRecord{
			TimeUnixNano:   uint64(record.Timestamp().UnixNano()),
			SeverityNumber: logpb.SeverityNumber(record.Severity()),
			SeverityText:   record.SeverityText(),
			Body:           otlpString(record.Body().AsString()),
			TraceId:        traceID[:],
			SpanId:         spanID[:],
		})
	}

	logs := &logpb.ResourceLogs{Resource: otlpResource(), ScopeLogs: []*logpb.ScopeLogs{group}}

	return w.send(ctx, "logs", &logexport.ExportLogsServiceRequest{ResourceLogs: []*logpb.ResourceLogs{logs}})
}

func otlpResource() *resourcepb.Resource {
	//nolint:exhaustruct_v5 // The gateway sets only the service name.
	return &resourcepb.Resource{
		Attributes: otlpAttributes([]attribute.KeyValue{attribute.String("service.name", serviceName)}),
	}
}

func otlpString(value string) *commonpb.AnyValue {
	return &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}}
}

func otlpAttributes(attributes []attribute.KeyValue) []*commonpb.KeyValue {
	values := make([]*commonpb.KeyValue, 0, len(attributes))
	for _, attr := range attributes {
		value := otlpString(attr.Value.AsString())
		if attr.Value.Type() == attribute.INT64 {
			value.Value = &commonpb.AnyValue_IntValue{IntValue: attr.Value.AsInt64()}
		}

		//nolint:exhaustruct_v5 // Attribute keys use inline strings.
		values = append(values, &commonpb.KeyValue{Key: string(attr.Key), Value: value})
	}

	return values
}
