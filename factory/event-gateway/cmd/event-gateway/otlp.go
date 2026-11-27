// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
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
	"go.opentelemetry.io/otel/sdk/resource"
	"google.golang.org/protobuf/proto"

	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	traceexport "go.opentelemetry.io/proto/otlp/collector/trace/v1"
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
	return openTelemetryWithTrace(ctx, handler, cfg, newOTLPTracer)
}

func openTelemetryWithTrace(
	ctx context.Context, handler slog.Handler, cfg otlpConfig,
	startTrace func(context.Context, *resource.Resource, otlptrace.Client) (*sdktrace.TracerProvider, error),
) (telemetry, func(), error) {
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

	res := resource.NewSchemaless(attribute.String("service.name", serviceName))

	tracer, err := startTrace(ctx, res, writer)
	if err != nil {
		return telemetry{}, nil, err
	}

	meter, stopMetrics := newOTLPMeter(ctx, res, writer)
	logger := newOTLPLogger(res, writer)
	local := slog.New(handler)
	both := slog.NewMultiHandler(handler, otelslog.NewHandler(serviceName, otelslog.WithLoggerProvider(logger)))

	closeSignals := sync.OnceFunc(func() {
		const shutdownTimeout = 15 * time.Second

		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
		defer cancel()
		defer writer.client.CloseIdleConnections()

		if errors.Join(tracer.Shutdown(shutdown), stopMetrics(shutdown), logger.Shutdown(shutdown)) != nil {
			local.ErrorContext(shutdown, "OTLP shutdown failed")
		}
	})

	return newTelemetry(both, tracer, meter), closeSignals, nil
}

func newOTLPTracer(
	ctx context.Context,
	res *resource.Resource,
	client otlptrace.Client,
) (*sdktrace.TracerProvider, error) {
	traceExporter, err := otlptrace.New(ctx, client)
	if err != nil {
		return nil, fmt.Errorf("start OTLP traces: %w", err)
	}
	// WithResource merges OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME. The wire
	// resource is rebuilt in UploadTraces to honor the gateway's explicit settings.
	return sdktrace.NewTracerProvider(
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
	), nil
}

func newOTLPLogger(res *resource.Resource, writer *otlpWriter) *sdklog.LoggerProvider {
	return sdklog.NewLoggerProvider(
		sdklog.WithResource(res),
		sdklog.WithProcessor(sdklog.NewBatchProcessor(otlpLogs{writer},
			sdklog.WithMaxQueueSize(batchQueueSize),
			sdklog.WithExportInterval(logBatchInterval),
			sdklog.WithExportTimeout(logBatchTimeout),
			sdklog.WithExportMaxBatchSize(batchSize),
		)),
	)
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

	var tlsConfig tls.Config

	tlsConfig.MinVersion = tls.VersionTLS12

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

	var transport http.Transport

	transport.TLSClientConfig = &tlsConfig
	client := &http.Client{
		Jar:           nil,
		Transport:     &transport,
		Timeout:       exportTimeout,
		CheckRedirect: rejectRedirect,
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
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		return errOTLPExport
	}

	return nil
}
