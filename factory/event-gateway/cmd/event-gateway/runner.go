// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:ignore nilerr

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// Event carries the stream sequence and the stored envelope.
type Event struct {
	Envelope []byte
	Seq      uint64
}

// Target delivers events from its own durable consumer.
// Deliver errors that wrap ErrPermanent are stored in PARKED for 30 days and logged.
// These errors must omit event payload and envelope text, including quoted fields.
type Target interface {
	Name() string
	Deliver(ctx context.Context, e Event) error
}

// ErrPermanent marks a delivery failure that should not be retried.
var ErrPermanent = errors.New("permanent")

var errTargetPanic = fmt.Errorf("%w: target panicked", ErrPermanent)

const (
	pullWait     = 100 * time.Millisecond
	retryCeiling = 5 * time.Minute
)

type startPosition struct {
	at  *time.Time
	all bool
}

type runnerTarget struct {
	Target

	Start    startPosition
	Subjects []string
}

type runner struct {
	onPermanent func(context.Context, string, Event, error)
	stream      jetstream.Stream
	ready       chan struct{}
	targets     []runnerTarget
	retryBase   time.Duration
	possibleGap atomic.Uint64
}

func (r *runner) run(ctx context.Context) error {
	consumers := make([]jetstream.Consumer, len(r.targets))
	for index, target := range r.targets {
		consumer, err := r.stream.CreateOrUpdateConsumer(ctx, target.consumerConfig())
		if ctx.Err() != nil {
			return nil //nolint:nilerr // Cancellation stops the runner successfully.
		}

		if err != nil {
			return fmt.Errorf("create consumer for target %q: %w", target.Name(), err)
		}

		consumers[index] = consumer
	}

	if r.ready != nil {
		close(r.ready)
	}

	var loops sync.WaitGroup
	for i, target := range r.targets {
		loops.Go(func() { r.runTarget(ctx, target, consumers[i]) })
	}

	<-ctx.Done()
	loops.Wait()

	return nil
}

func (target runnerTarget) consumerConfig() jetstream.ConsumerConfig {
	policy := jetstream.DeliverNewPolicy

	switch {
	case target.Start.at != nil:
		policy = jetstream.DeliverByStartTimePolicy
	case target.Start.all:
		policy = jetstream.DeliverAllPolicy
	}

	//nolint:exhaustruct_v5 // JetStream defaults to unlimited delivery attempts.
	return jetstream.ConsumerConfig{
		Durable:        target.Name(),
		DeliverPolicy:  policy,
		OptStartTime:   target.Start.at,
		AckPolicy:      jetstream.AckExplicitPolicy,
		MaxAckPending:  1,
		FilterSubjects: target.Subjects,
	}
}

func (r *runner) runTarget(ctx context.Context, target runnerTarget, consumer jetstream.Consumer) {
	var lastAck, lastGap uint64
	if floor := consumer.CachedInfo().AckFloor; floor.Consumer != 0 {
		lastAck = floor.Stream
	}

	for ctx.Err() == nil {
		lastGap = r.checkGap(ctx, target.Name(), lastAck, lastGap)

		pull, cancel := context.WithTimeout(ctx, pullWait)

		message, err := consumer.Next(jetstream.FetchContext(pull))
		if err != nil {
			// Also pace retries when JetStream returns an error immediately.
			<-pull.Done()
		} else {
			lastAck = r.deliver(ctx, target, message, lastAck)
		}

		cancel()
	}
}

func (r *runner) checkGap(ctx context.Context, name string, lastAck, lastGap uint64) uint64 {
	// On an info error the last known first sequence still provides a lower bound.
	//nolint:errcheck // CachedInfo retains the last successful response.
	_, _ = r.stream.Info(ctx)

	first := r.stream.CachedInfo().State.FirstSeq
	if lastAck != 0 && first > lastAck+1 && first != lastGap {
		slog.Default().WarnContext(ctx, "possible gap", "target", name, "last_ack", lastAck, "first_seq", first)
		r.possibleGap.Add(1)

		return first
	}

	return lastGap
}

func (r *runner) deliver(ctx context.Context, target runnerTarget, message jetstream.Msg, lastAck uint64) uint64 {
	//nolint:errcheck // Embedded JetStream supplies the acknowledgment metadata.
	meta, _ := message.Metadata()
	event := Event{Seq: meta.Sequence.Stream, Envelope: message.Data()}
	err := deliverEvent(ctx, target, event)

	if err != nil && !errors.Is(err, ErrPermanent) {
		delay := retryDelay(cmp.Or(r.retryBase, time.Second), meta.NumDelivered)
		//nolint:errcheck // JetStream redelivers at AckWait if the negative ack is lost.
		_ = message.NakWithDelay(delay)

		return lastAck
	}

	// Confirm the acknowledgment even if cancellation arrived during delivery.
	ack, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Second)
	defer cancel()

	if message.DoubleAck(ack) != nil {
		return lastAck
	}

	if err != nil {
		hook := r.onPermanent
		if hook == nil {
			hook = logPermanent
		}

		// Ack first so a permanent failure is never redelivered. A failed park publish is logged
		// at error level and counted by event_gateway_park_publish_failed_total.
		// The event stays readable in EVENTS.
		hook(ctx, target.Name(), event, err)
	}

	return event.Seq
}

//nolint:wrapcheck // The hook receives the target's original delivery error.
func deliverEvent(ctx context.Context, target Target, event Event) (err error) {
	defer func() {
		if recover() != nil {
			err = errTargetPanic
		}
	}()

	return target.Deliver(ctx, event)
}

func logPermanent(ctx context.Context, name string, event Event, err error) {
	// Target errors and panic values can contain event data.
	safeErr := ErrPermanent
	if errors.Is(err, errTargetPanic) {
		safeErr = errTargetPanic
	}

	slog.Default().ErrorContext(ctx, "delivery failed", "target", name, "seq", event.Seq, "error", safeErr.Error())
}

func retryDelay(base time.Duration, attempt uint64) time.Duration {
	for ; attempt > 1 && base < retryCeiling; attempt-- {
		base *= 2
	}

	return min(base, retryCeiling)
}
