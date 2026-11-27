// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
)

const (
	incrementalKind = "incremental"
	sweepKind       = "sweep"
	sweepStart      = 3 * time.Hour
)

func TestLoopSchedule(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		var calls atomic.Int32

		release := make(chan struct{})
		pass := func(ctx context.Context) (reconcile.RunResult, error) {
			return waitForPass(ctx, t, calls.Add(1), release), nil
		}

		loop, err := reconcile.NewLoop(
			pass,
			noop.NewMeterProvider().Meter("test"),
			slog.New(slog.DiscardHandler),
			nil,
		)
		if err != nil {
			t.Fatal(err)
		}

		done := make(chan error, 1)
		go func() { done <- loop.Start(ctx) }()

		synctest.Wait()

		if got := calls.Load(); got != 1 {
			t.Errorf("startup calls = %d, want 1", got)
		}

		time.Sleep(17 * time.Minute)
		synctest.Wait()

		if got := calls.Load(); got != 1 {
			t.Errorf("calls after the busy tick = %d, want 1", got)
		}

		time.Sleep(13 * time.Minute)
		synctest.Wait()

		if got := calls.Load(); got != 2 {
			t.Errorf("calls at the second tick = %d, want 2", got)
		}

		checkLoopShutdown(t, cancel, release, done)
	})
}

func checkLoopShutdown(
	t *testing.T,
	cancel context.CancelFunc,
	release chan<- struct{},
	done <-chan error,
) {
	t.Helper()

	cancel()
	synctest.Wait()

	select {
	case earlyErr := <-done:
		t.Fatalf("loop returned %v before its running pass finished", earlyErr)
	default:
	}

	close(release)

	err := <-done
	if err != nil {
		t.Fatal(err)
	}
}

func waitForPass(
	ctx context.Context,
	t *testing.T,
	call int32,
	release <-chan struct{},
) reconcile.RunResult {
	t.Helper()

	deadline, ok := ctx.Deadline()
	if !ok || time.Until(deadline) != 14*time.Minute {
		t.Errorf("pass deadline = %v, want 14 minutes from now", deadline)
	}

	<-ctx.Done()

	if call == 1 {
		if !errors.Is(ctx.Err(), context.DeadlineExceeded) {
			t.Errorf("first pass context error = %v, want deadline exceeded", ctx.Err())
		}

		// Leave the startup pass busy past the first tick.
		time.Sleep(2 * time.Minute)
	} else {
		<-release
	}

	var result reconcile.RunResult

	result.Result = reconcile.RunPartial

	return result
}

func TestLoopReportsPasses(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		const sweepIndex = 3

		started, due := approachSweep(sweepIndex)
		reader := sdkmetric.NewManualReader()
		provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))

		var logs bytes.Buffer

		const firstFailure = sweepIndex + 1

		statuses := []reconcile.RunStatus{
			reconcile.RunOK,
			reconcile.RunPartial,
			reconcile.RunAuthFailed,
			reconcile.RunOK,
			reconcile.RunPartial,
			reconcile.RunPartial,
			reconcile.RunPartial,
		}
		calls := 0
		pass := func(ctx context.Context) (reconcile.RunResult, error) {
			want := started.Add(time.Duration(calls) * 15 * time.Minute)
			checkPassTime(ctx, t, want, due)
			time.Sleep(2 * time.Second)

			result := loopResult(statuses[calls])
			calls++

			if calls > firstFailure {
				result.Result = ""

				return result, fmt.Errorf("store error with request details: %w", io.ErrClosedPipe)
			}

			return result, nil
		}

		loop, err := reconcile.NewLoop(
			pass,
			provider.Meter("test"),
			slog.New(slog.NewJSONHandler(&logs, nil)),
			nil,
		)
		if err != nil {
			t.Fatal(err)
		}

		err = loop.Start(t.Context())
		if !errors.Is(err, io.ErrClosedPipe) || calls != len(statuses) {
			t.Fatalf("loop returned %v after %d passes", err, calls)
		}

		checkLoopMetrics(t, reader, started.Add(2*time.Second), due.Add(2*time.Second))
		checkLoopLogs(t, &logs, statuses, firstFailure, sweepIndex)
	})
}

func TestLoopFirstSweepAfterStartup(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		time.Sleep(sweepStart + 15*time.Minute)

		started := time.Now()
		due := started.UTC().AddDate(0, 0, 1).Add(-15 * time.Minute)

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		calls := 0
		pass := func(ctx context.Context) (reconcile.RunResult, error) {
			want := started.Add(time.Duration(calls) * 15 * time.Minute)
			calls++

			checkPassTime(ctx, t, want, due)

			if !time.Now().Before(due) {
				cancel()
			}

			var result reconcile.RunResult

			result.Result = reconcile.RunOK

			return result, nil
		}

		loop, err := reconcile.NewLoop(
			pass,
			noop.NewMeterProvider().Meter("test"),
			slog.New(slog.DiscardHandler),
			nil,
		)
		if err != nil {
			t.Fatal(err)
		}

		err = loop.Start(ctx)
		if err != nil || !time.Now().Equal(due) || calls != 96 {
			t.Fatalf(
				"loop returned %v at %v after %d passes, want %v after 96 passes",
				err,
				time.Now(),
				calls,
				due,
			)
		}
	})
}

func loopResult(status reconcile.RunStatus) reconcile.RunResult {
	return reconcile.RunResult{
		Result:              status,
		Repositories:        3,
		Pages:               4,
		NotModified:         1,
		Truncated:           1,
		Recorded:            2,
		AlreadyCounted:      5,
		TooOld:              0,
		PreFloor:            0,
		Skipped:             0,
		EndedRecorded:       0,
		EndedAlreadyCounted: 0,
		EndedTooOld:         0,
		EndedPreFloor:       0,
		Ambiguous:           0,
	}
}

func checkPassTime(ctx context.Context, t *testing.T, want, due time.Time) {
	t.Helper()

	if now := time.Now(); !now.Equal(want) {
		t.Errorf("pass started at %v, want %v", now, want)
	}

	timeout := 14 * time.Minute
	if want.Equal(due) {
		timeout = 45 * time.Minute
	}

	deadline, ok := ctx.Deadline()
	if !ok || time.Until(deadline) != timeout {
		t.Errorf("pass deadline = %v, want %v from now", deadline, timeout)
	}
}

func TestLoopResetsConsecutivePassErrors(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		time.Sleep(sweepStart - time.Hour)

		started := time.Now()
		due := started.Add(time.Hour)
		passErrors := []error{
			io.ErrClosedPipe,
			io.ErrClosedPipe,
			nil,
			io.ErrClosedPipe,
			io.ErrClosedPipe,
			io.ErrUnexpectedEOF,
		}
		calls := 0
		pass := func(ctx context.Context) (reconcile.RunResult, error) {
			checkPassTime(ctx, t, started.Add(time.Duration(calls)*15*time.Minute), due)

			var result reconcile.RunResult

			result.Result = reconcile.RunPartial
			err := passErrors[calls]
			calls++

			return result, err
		}

		loop, err := reconcile.NewLoop(
			pass,
			noop.NewMeterProvider().Meter("test"),
			slog.New(slog.DiscardHandler),
			nil,
		)
		if err != nil {
			t.Fatal(err)
		}

		err = loop.Start(t.Context())
		if !errors.Is(err, io.ErrUnexpectedEOF) || calls != len(passErrors) {
			t.Fatalf(
				"loop returned %v after %d passes, want %v after %d",
				err,
				calls,
				io.ErrUnexpectedEOF,
				len(passErrors),
			)
		}
	})
}

func checkLoopMetrics(
	t *testing.T,
	reader *sdkmetric.ManualReader,
	incrementalSuccess, sweepSuccess time.Time,
) {
	t.Helper()

	const (
		prefix = "github_metrics.reconcile."
		kind   = "kind=" + incrementalKind
		swept  = "kind=" + sweepKind
		runs   = prefix + "runs//counter/" + kind
	)

	got := collectLoopMetrics(t, reader)

	want := map[string]float64{
		runs + ",result=ok":                                       1,
		runs + ",result=partial":                                  4,
		runs + ",result=auth_failed":                              1,
		prefix + "duration/s/" + kind:                             12,
		prefix + "duration/s/" + kind + "/count":                  6,
		prefix + "repairs//counter/" + kind + ",milestone=merged": 12,
		prefix + "last_success/s/gauge/" + kind: float64(
			incrementalSuccess.Unix(),
		),
		prefix + "runs//counter/" + swept + ",result=ok":           1,
		prefix + "duration/s/" + swept:                             2,
		prefix + "duration/s/" + swept + "/count":                  1,
		prefix + "repairs//counter/" + swept + ",milestone=merged": 2,
		prefix + "last_success/s/gauge/" + swept:                   float64(sweepSuccess.Unix()),
	}
	if !maps.Equal(got, want) {
		t.Errorf("metrics = %v, want %v", got, want)
	}
}

func checkLoopLogs(
	t *testing.T,
	logs io.Reader,
	statuses []reconcile.RunStatus,
	firstFailure, sweepIndex int,
) {
	t.Helper()

	decoder := json.NewDecoder(logs)

	for index, status := range statuses {
		var got map[string]any

		err := decoder.Decode(&got)
		if err != nil {
			t.Fatal(err)
		}

		delete(got, slog.TimeKey)

		level := slog.LevelInfo
		if index >= firstFailure {
			level = slog.LevelError
		}

		kind := incrementalKind
		if index == sweepIndex {
			kind = sweepKind
		}

		want := map[string]any{
			slog.LevelKey:   level.String(),
			slog.MessageKey: "reconcile pass finished",
			"kind":          kind,
			"result":        string(status),
			"repos": float64(
				3,
			),
			"pages":        float64(4),
			"not_modified": float64(1),
			"recorded":     float64(2),
			"already_counted": float64(
				5,
			),
			"truncated": float64(1),
			"duration":  float64(2 * time.Second),
		}
		if !maps.Equal(got, want) {
			t.Errorf("log = %v, want %v", got, want)
		}
	}

	if decoder.More() {
		t.Error("extra pass log")
	}
}

func collectLoopMetrics(t *testing.T, reader *sdkmetric.ManualReader) map[string]float64 {
	t.Helper()

	var data metricdata.ResourceMetrics

	err := reader.Collect(t.Context(), &data)
	if err != nil {
		t.Fatal(err)
	}

	if len(data.ScopeMetrics) != 1 {
		t.Fatalf("metric scopes = %d, want 1", len(data.ScopeMetrics))
	}

	got := map[string]float64{}
	encoder := attribute.DefaultEncoder()

	for _, measurement := range data.ScopeMetrics[0].Metrics {
		key := measurement.Name + "/" + measurement.Unit + "/"

		var intPoints []metricdata.DataPoint[int64]

		switch points := measurement.Data.(type) {
		case metricdata.Sum[int64]:
			intPoints = points.DataPoints
			key += "counter/"
		case metricdata.Gauge[int64]:
			intPoints = points.DataPoints
			key += "gauge/"
		case metricdata.Histogram[float64]:
			for _, point := range points.DataPoints {
				series := key + point.Attributes.Encoded(encoder)
				got[series] = point.Sum
				got[series+"/count"] = float64(point.Count)
			}
		default:
			t.Fatalf("unexpected metric %v", measurement)
		}

		for _, point := range intPoints {
			got[key+point.Attributes.Encoded(encoder)] = float64(point.Value)
		}
	}

	return got
}

func approachSweep(index int) (time.Time, time.Time) {
	delay := time.Duration(index) * 15 * time.Minute
	time.Sleep(sweepStart - delay)

	start := time.Now()

	return start, start.Add(delay)
}
