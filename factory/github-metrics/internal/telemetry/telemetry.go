// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package telemetry sets up metric export.
package telemetry

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"syscall"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

const idleConnTimeout = 45 * time.Second

var errDefaultTransport = errors.New("default HTTP transport is not an *http.Transport")

// Setup exports cumulative metrics every 60 seconds. Without an endpoint it
// returns a no-op meter. Shutdown exports the final observations before closing.
//
//nolint:ireturn // Setup returns the standard meter interface.
func Setup(
	ctx context.Context,
	serviceName, version string,
) (func(context.Context) error, metric.Meter, error) {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		return func(context.Context) error { return nil }, noop.NewMeterProvider().
				Meter(serviceName),
			nil
	}

	client, err := newHTTPClient()
	if err != nil {
		return nil, nil, err
	}

	exporter, err := otlpmetrichttp.New(ctx,
		otlpmetrichttp.WithHTTPClient(client),
		otlpmetrichttp.WithTemporalitySelector(sdkmetric.CumulativeTemporalitySelector),
	)
	if err != nil {
		return nil, nil, fmt.Errorf("create metric exporter: %w", err)
	}

	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(
			sdkmetric.NewPeriodicReader(
				retryExporter{exporter},
				sdkmetric.WithInterval(time.Minute),
			),
		),
		sdkmetric.WithResource(resource.NewSchemaless(
			attribute.String("service.name", serviceName),
			attribute.String("service.version", version),
		)),
	)

	return provider.Shutdown, provider.Meter(serviceName), nil
}

func newHTTPClient() (*http.Client, error) {
	defaultTransport, ok := http.DefaultTransport.(*http.Transport)
	if !ok {
		return nil, errDefaultTransport
	}

	transport := defaultTransport.Clone()
	transport.IdleConnTimeout = idleConnTimeout

	//nolint:exhaustruct_v5 // Use Go's client defaults with the tuned transport.
	return &http.Client{Transport: transport}, nil
}

type retryExporter struct {
	*otlpmetrichttp.Exporter
}

func (exporter retryExporter) Export(
	ctx context.Context,
	metrics *metricdata.ResourceMetrics,
) error {
	err := exporter.Exporter.Export(ctx, metrics)
	if err == nil {
		return nil
	}

	retryable := errors.Is(err, syscall.ECONNRESET) || errors.Is(err, io.EOF) ||
		errors.Is(err, io.ErrUnexpectedEOF)
	if !retryable || ctx.Err() != nil {
		return fmt.Errorf("export metrics: %w", err)
	}

	retryErr := exporter.Exporter.Export(ctx, metrics)
	if retryErr != nil {
		return fmt.Errorf("retry metric export: %w", retryErr)
	}

	return nil
}
