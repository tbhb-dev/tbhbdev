// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric/exemplar"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"

	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	logexport "go.opentelemetry.io/proto/otlp/collector/logs/v1"
	metricexport "go.opentelemetry.io/proto/otlp/collector/metrics/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	logpb "go.opentelemetry.io/proto/otlp/logs/v1"
	metricpb "go.opentelemetry.io/proto/otlp/metrics/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
)

type otlpMetrics struct {
	*otlpWriter

	start time.Time
}

func newOTLPMeter(
	ctx context.Context, res *resource.Resource, writer *otlpWriter,
) (*sdkmetric.MeterProvider, func(context.Context) error) {
	reader := sdkmetric.NewManualReader()
	meter := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithExemplarFilter(exemplar.TraceBasedFilter),
		sdkmetric.WithCardinalityLimit(cardinalityLimit),
		sdkmetric.WithReader(reader),
	)

	exporter := otlpMetrics{otlpWriter: writer, start: time.Now()}
	collect := func(ctx context.Context) error {
		var metrics metricdata.ResourceMetrics

		err := reader.Collect(ctx, &metrics)
		if err != nil {
			return fmt.Errorf("collect OTLP metrics: %w", err)
		}

		return exporter.Export(ctx, &metrics)
	}

	ctx, cancel := context.WithCancel(context.WithoutCancel(ctx))

	stopped := make(chan struct{})
	go func() {
		defer close(stopped)

		periodicMetrics(ctx, collect)
	}()

	return meter, func(ctx context.Context) error {
		cancel()

		select {
		case <-stopped:
			return errors.Join(collect(ctx), meter.Shutdown(ctx))
		case <-ctx.Done():
			return fmt.Errorf("stop OTLP metrics: %w", ctx.Err())
		}
	}
}

func periodicMetrics(ctx context.Context, collect func(context.Context) error) {
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			export, cancel := context.WithTimeout(ctx, exportTimeout)

			err := collect(export)
			if err != nil {
				otel.Handle(err)
			}

			cancel()
		}
	}
}

func (w otlpMetrics) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	metrics := &metricpb.ResourceMetrics{ScopeMetrics: nil, SchemaUrl: "", Resource: otlpResource()}

	for _, scope := range data.ScopeMetrics {
		group := &metricpb.ScopeMetrics{
			Metrics:   nil,
			SchemaUrl: "",
			Scope:     otlpScope(scope.Scope.Name),
		}
		for _, metric := range scope.Metrics {
			sum, ok := metric.Data.(metricdata.Sum[int64])
			if !ok {
				continue
			}

			value := &metricpb.Sum{
				DataPoints: nil,

				IsMonotonic:            sum.IsMonotonic,
				AggregationTemporality: metricpb.AggregationTemporality_AGGREGATION_TEMPORALITY_CUMULATIVE,
			}
			// The SDK forwards per-series start times when OTEL_GO_X_PER_SERIES_START_TIMESTAMPS is
			// set, and v1.47.0 has no option to turn that off. Every cumulative point therefore
			// reports the one start time fixed when the provider was built.
			for _, point := range sum.DataPoints {
				value.DataPoints = append(value.DataPoints, &metricpb.NumberDataPoint{
					Exemplars: nil,
					Flags:     0,

					Attributes:        otlpAttributes(point.Attributes.ToSlice()),
					StartTimeUnixNano: uint64(w.start.UnixNano()),
					TimeUnixNano:      uint64(point.Time.UnixNano()),
					Value:             &metricpb.NumberDataPoint_AsInt{AsInt: point.Value},
				})
			}

			metric := &metricpb.Metric{
				Description: "",
				Unit:        "",
				Metadata:    nil,
				Name:        metric.Name,
				Data:        &metricpb.Metric_Sum{Sum: value},
			}
			group.Metrics = append(group.Metrics, metric)
		}

		metrics.ScopeMetrics = append(metrics.ScopeMetrics, group)
	}

	message := &metricexport.ExportMetricsServiceRequest{ResourceMetrics: []*metricpb.ResourceMetrics{metrics}}

	return w.send(ctx, "metrics", message)
}

type otlpLogs struct{ *otlpWriter }

func (w otlpLogs) Export(ctx context.Context, records []sdklog.Record) error {
	group := &logpb.ScopeLogs{LogRecords: nil, SchemaUrl: "", Scope: otlpScope(serviceName)}

	for i := range records {
		record := &records[i]
		traceID, spanID := record.TraceID(), record.SpanID()
		group.LogRecords = append(group.LogRecords, &logpb.LogRecord{
			ObservedTimeUnixNano:   0,
			Attributes:             nil,
			DroppedAttributesCount: 0,
			Flags:                  0,
			EventName:              "",

			TimeUnixNano:   uint64(record.Timestamp().UnixNano()),
			SeverityNumber: otlpSeverity(int(record.Severity())),
			SeverityText:   record.SeverityText(),
			Body:           otlpString(record.Body().AsString()),
			TraceId:        traceID[:],
			SpanId:         spanID[:],
		})
	}

	logs := &logpb.ResourceLogs{SchemaUrl: "", Resource: otlpResource(), ScopeLogs: []*logpb.ScopeLogs{group}}

	return w.send(ctx, "logs", &logexport.ExportLogsServiceRequest{ResourceLogs: []*logpb.ResourceLogs{logs}})
}

func otlpScope(name string) *commonpb.InstrumentationScope {
	return &commonpb.InstrumentationScope{Name: name, Version: "", Attributes: nil, DroppedAttributesCount: 0}
}

func otlpSeverity(severity int) logpb.SeverityNumber {
	if severity < math.MinInt32 || severity > math.MaxInt32 {
		return logpb.SeverityNumber_SEVERITY_NUMBER_UNSPECIFIED
	}

	return logpb.SeverityNumber(severity)
}

func otlpResource() *resourcepb.Resource {
	return &resourcepb.Resource{
		DroppedAttributesCount: 0,
		EntityRefs:             nil,

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

		values = append(values, &commonpb.KeyValue{KeyStrindex: 0, Key: string(attr.Key), Value: value})
	}

	return values
}
