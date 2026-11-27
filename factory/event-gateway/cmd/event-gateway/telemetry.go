// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"log/slog"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	metricnoop "go.opentelemetry.io/otel/metric/noop"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

const serviceName = "event-gateway"

// telemetry carries the logger, tracer and counters the gateway records to.
// The caller supplies the providers, so nothing here reads the environment.
// A nil provider falls back to a no-op one.
type telemetry struct {
	logger *slog.Logger
	tracer trace.Tracer
	ingest metric.Int64Counter

	// parked counts successful park publishes. Failed publishes increment parkFailed instead.
	parked metric.Int64Counter
	// parkFailed counts failed park publishes, which do not increment parked.
	parkFailed metric.Int64Counter
}

func newTelemetry(handler slog.Handler, tracer trace.TracerProvider, meter metric.MeterProvider) telemetry {
	if tracer == nil {
		tracer = tracenoop.NewTracerProvider()
	}

	if meter == nil {
		meter = metricnoop.NewMeterProvider()
	}

	signals := meter.Meter(serviceName)

	result := telemetry{
		logger:     slog.New(handler),
		tracer:     tracer.Tracer(serviceName),
		ingest:     nil,
		parked:     nil,
		parkFailed: nil,
	}
	result.registerCounter(signals, "event_gateway_ingest_total", &result.ingest)
	result.registerCounter(signals, "event_gateway_parked_total", &result.parked)
	result.registerCounter(signals, "event_gateway_park_publish_failed_total", &result.parkFailed)

	return result
}

func (t telemetry) registerCounter(meter metric.Meter, name string, counter *metric.Int64Counter) {
	value, err := meter.Int64Counter(name)
	if err != nil {
		t.logger.Error("initialize event counter", "name", name)

		value = metricnoop.Int64Counter{Int64Counter: nil}
	}

	*counter = value
}

func (t telemetry) record(ctx context.Context, span trace.Span, event string, status int) {
	attrs := []attribute.KeyValue{
		attribute.String("event", event),
		attribute.Int("http.response.status_code", status),
	}
	span.SetAttributes(attrs...)
	t.ingest.Add(ctx, 1, metric.WithAttributes(attrs...))
}
