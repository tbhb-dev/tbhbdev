// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

const (
	passInterval = 15 * time.Minute
	passTimeout  = 14 * time.Minute
	// A sweep gets 45 minutes to read the full repair window at the shared request rate.
	sweepTimeout = 45 * time.Minute
	sweepHour    = 3

	incremental  = "incremental"
	sweep        = "sweep"
	metricPrefix = "github_metrics.reconcile."
	seconds      = "s"

	maxConsecutivePassErrors = 3
)

// Pass runs one reconciliation and returns any store error.
type Pass func(ctx context.Context) (RunResult, error)

// LoopClock supplies pass times and a ticker with a stop function.
type LoopClock interface {
	Clock
	NewTicker(interval time.Duration) (<-chan time.Time, func())
}

// Loop schedules and reports incremental passes and daily sweeps.
type Loop struct {
	pass          Pass
	clock         LoopClock
	log           *slog.Logger
	runs, repairs metric.Int64Counter
	duration      metric.Float64Histogram
	lastSuccess   metric.Int64Gauge
}

// NewLoop creates a scheduler. A nil clock uses the system clock and ticker.
func NewLoop(pass Pass, meter metric.Meter, log *slog.Logger, clock LoopClock) (*Loop, error) {
	if clock == nil {
		clock = systemLoopClock{}
	}

	var loop Loop

	loop.pass, loop.clock, loop.log = pass, clock, log

	var err error

	loop.runs, err = meter.Int64Counter(metricPrefix + "runs")
	if err != nil {
		return nil, fmt.Errorf("create reconcile runs counter: %w", err)
	}

	loop.repairs, err = meter.Int64Counter(metricPrefix + "repairs")
	if err != nil {
		return nil, fmt.Errorf("create reconcile repairs counter: %w", err)
	}

	loop.duration, err = meter.Float64Histogram(metricPrefix+"duration", metric.WithUnit(seconds))
	if err != nil {
		return nil, fmt.Errorf("create reconcile duration histogram: %w", err)
	}

	loop.lastSuccess, err = meter.Int64Gauge(metricPrefix+"last_success", metric.WithUnit(seconds))
	if err != nil {
		return nil, fmt.Errorf("create reconcile last success gauge: %w", err)
	}

	return &loop, nil
}

// Start runs passes serially until cancellation or three consecutive pass
// errors. Call it once from the goroutine that owns the loop.
func (l *Loop) Start(ctx context.Context) error {
	if ctx.Err() != nil {
		return nil
	}

	started := l.clock.Now()

	nextSweep := sweepTime(started)
	if started.After(nextSweep) {
		nextSweep = nextSweep.AddDate(0, 0, 1)
	}

	ticks, stop := l.clock.NewTicker(passInterval)
	defer stop()

	consecutiveErrors := 0

	for {
		kind := incremental
		if !started.Before(nextSweep) {
			kind = sweep
			nextSweep = sweepTime(started).AddDate(0, 0, 1)
		}

		finished, err := l.runPass(ctx, kind, started)
		if err != nil {
			consecutiveErrors++
		} else {
			consecutiveErrors = 0
		}

		if consecutiveErrors == maxConsecutivePassErrors || ctx.Err() != nil {
			return err
		}

		if !waitForNextPass(ctx, ticks, finished) {
			return nil
		}

		started = l.clock.Now()
	}
}

func sweepTime(now time.Time) time.Time {
	year, month, day := now.UTC().Date()

	return time.Date(year, month, day, sweepHour, 0, 0, 0, time.UTC)
}

func waitForNextPass(ctx context.Context, ticks <-chan time.Time, finished time.Time) bool {
	for {
		select {
		case <-ctx.Done():
			return false
		case tick := <-ticks:
			// The ticker may still hold a tick from the previous pass.
			if tick.After(finished) {
				return ctx.Err() == nil
			}
		}
	}
}

// sweepPassKey marks a sweep through the pass callback that calls Run.
type sweepPassKey struct{}

func (l *Loop) runPass(ctx context.Context, kind string, started time.Time) (time.Time, error) {
	timeout := passTimeout
	if kind == sweep {
		timeout = sweepTimeout
		ctx = context.WithValue(ctx, sweepPassKey{}, true)
	}

	passCtx, cancel := context.WithTimeout(ctx, timeout)
	result, err := l.pass(passCtx)

	cancel()

	finished := l.clock.Now()
	elapsed := finished.Sub(started)
	level := slog.LevelInfo

	if err != nil {
		result.Result = RunPartial
		level = slog.LevelError
	}

	kindAttr := attribute.String("kind", kind)
	kindOnly := metric.WithAttributes(kindAttr)

	l.runs.Add(
		ctx,
		1,
		metric.WithAttributes(kindAttr, attribute.String("result", string(result.Result))),
	)
	l.duration.Record(ctx, elapsed.Seconds(), kindOnly)

	// RunResult currently counts recorded merges. First ends arrive with the issue events read.
	l.repairs.Add(
		ctx,
		int64(result.Recorded),
		metric.WithAttributes(kindAttr, attribute.String("milestone", "merged")),
	)

	if result.Result == RunOK {
		l.lastSuccess.Record(ctx, finished.Unix(), kindOnly)
	}

	l.log.LogAttrs(
		ctx, level, "reconcile pass finished",
		slog.String("kind", kind), slog.String("result", string(result.Result)),
		slog.Int("repos", result.Repositories), slog.Int("pages", result.Pages),
		slog.Int("not_modified", result.NotModified), slog.Int("recorded", result.Recorded),
		slog.Int("already_counted", result.AlreadyCounted), slog.Int("truncated", result.Truncated),
		slog.Duration("duration", elapsed),
	)

	return finished, err
}

type systemLoopClock struct{}

func (systemLoopClock) Now() time.Time { return time.Now() }

func (systemLoopClock) NewTicker(interval time.Duration) (<-chan time.Time, func()) {
	ticker := time.NewTicker(interval)

	return ticker.C, ticker.Stop
}
