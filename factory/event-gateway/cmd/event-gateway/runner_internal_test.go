// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

const (
	pullSubject   = "events.github.pull_request"
	issuesSubject = "events.github.issues"
	testEnvelope  = `{"type":"event","payload":{"sample":true}}`
)

// runnerWait bounds a hang and does not pace the tests.
const runnerWait = 30 * time.Second

var errRetryDelivery = errors.New("retry delivery")

type fakeTarget struct {
	deliver func(context.Context, Event) error
	name    string
}

func (target fakeTarget) Name() string { return target.name }

func (target fakeTarget) Deliver(ctx context.Context, event Event) error {
	return target.deliver(ctx, event)
}

func recordingTarget(name string, events chan<- Event) fakeTarget {
	return fakeTarget{name: name, deliver: func(_ context.Context, event Event) error {
		events <- event

		return nil
	}}
}

func startRunner(t *testing.T, run *runner) func() {
	t.Helper()

	ctx, cancel := context.WithCancel(t.Context())
	result := make(chan error, 1)

	go func() { result <- run.run(ctx) }()

	stop := sync.OnceFunc(func() {
		cancel()
		checkError(t, receiveRunner(t, result))
	})
	t.Cleanup(stop)

	return stop
}

func receiveRunner[T any](t *testing.T, results <-chan T) T {
	t.Helper()

	select {
	case result := <-results:
		return result
	case <-time.After(runnerWait):
		t.Fatal("timed out waiting for runner")

		return *new(T)
	}
}

func publishRunner(t *testing.T, store eventStore, subject string) {
	t.Helper()

	_, err := store.jet.Publish(t.Context(), subject, []byte(testEnvelope))
	checkError(t, err)
}

func expectRunner(t *testing.T, events <-chan Event, sequences ...uint64) {
	t.Helper()

	for _, seq := range sequences {
		event := receiveRunner(t, events)
		checkEqual(t, event.Seq, seq)
		checkEqual(t, string(event.Envelope), testEnvelope)
	}
}

func TestRunnerDelivery(t *testing.T) {
	t.Parallel()

	cases := []struct {
		fail func() error
		name string
		park string
		want []uint64
	}{
		{park: "", name: "transient", fail: func() error { return errRetryDelivery }, want: []uint64{1, 1, 2}},
		{
			name: "permanent", fail: func() error { return fmt.Errorf("delivery: %w", ErrPermanent) },
			want: []uint64{1, 2}, park: "delivery: permanent",
		},
		{
			name: "panic", fail: func() error { panic(testEnvelope) },
			want: []uint64{1, 2}, park: errTargetPanic.Error(),
		},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			store := testStore(t, retention)
			parked := testParkedStream(t, store)
			publishRunner(t, store, pullSubject)
			publishRunner(t, store, pullSubject)

			events := make(chan Event, 8)
			first := true

			target := fakeTarget{name: test.name, deliver: func(_ context.Context, event Event) error {
				events <- event

				if first {
					first = false

					return test.fail()
				}

				return nil
			}}
			run := &runner{stream: store.stream, retryBase: time.Millisecond, targets: []runnerTarget{{
				Target: target, Subjects: nil, Start: startPosition{at: nil, all: true},
			}}}
			park := parkHook(store.jet, newTelemetry(slog.DiscardHandler, nil, nil), 0)
			run.onPermanent = func(ctx context.Context, name string, event Event, err error) {
				logPermanent(ctx, name, event, err)
				park(ctx, name, event, err)
			}

			stop := startRunner(t, run)
			expectRunner(t, events, test.want...)
			stop()

			records, err := listParked(t.Context(), parked, "", 0)
			checkError(t, err)

			if test.park == "" {
				checkEqual(t, len(records), 0)
			} else {
				checkEqual(t, len(records), 1)
				checkEqual(t, records[0].Seq, uint64(1))
				checkEqual(t, records[0].Target, test.name)
				checkEqual(t, records[0].Error, test.park)
			}

			info, err := store.stream.Consumer(t.Context(), test.name)
			checkError(t, err)
			checkEqual(t, info.CachedInfo().AckFloor.Stream, uint64(2))
		})
	}
}

func TestRunnerRestart(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)
	publishRunner(t, store, issuesSubject)
	publishRunner(t, store, pullSubject)

	events := make(chan Event, 8)
	run := &runner{stream: store.stream, targets: []runnerTarget{
		{
			Target: recordingTarget(
				"restart",
				events,
			),
			Subjects: []string{pullSubject},
			Start:    startPosition{at: nil, all: true},
		},
	}}
	stop := startRunner(t, run)
	expectRunner(t, events, 2)
	stop()

	publishRunner(t, store, issuesSubject)
	publishRunner(t, store, pullSubject)
	stop = startRunner(t, run)
	expectRunner(t, events, 4)
	stop()

	publishRunner(t, store, issuesSubject)
	run.targets[0].Subjects = []string{pullSubject, issuesSubject}
	stop = startRunner(t, run)
	expectRunner(t, events, 5)
	stop()
	checkEqual(t, run.possibleGap.Load(), uint64(0))
}

func TestRunnerGap(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)

	events := make(chan Event, 4)
	run := &runner{stream: store.stream, targets: []runnerTarget{{
		Target: recordingTarget("gap", events), Start: startPosition{at: nil, all: true},
	}}}
	stop := startRunner(t, run)

	publishRunner(t, store, pullSubject)
	expectRunner(t, events, 1)
	stop()

	// Shorten the expiry only now so the first delivery cannot race it.
	info, err := store.stream.Info(t.Context())
	checkError(t, err)

	config := info.Config
	config.MaxAge, config.Duplicates = time.Second, time.Second
	_, err = store.jet.UpdateStream(t.Context(), config)
	checkError(t, err)

	publishRunner(t, store, pullSubject)
	waitRunner(t, func() bool {
		current, infoErr := store.stream.Info(t.Context())
		checkError(t, infoErr)

		return current.State.FirstSeq == 3
	})

	stop = startRunner(t, run)
	waitRunner(t, func() bool { return run.possibleGap.Load() == 1 })
	time.Sleep(3 * pullWait)
	publishRunner(t, store, pullSubject)
	expectRunner(t, events, 3)
	stop()
	checkEqual(t, run.possibleGap.Load(), uint64(1))
}

func waitRunner(t *testing.T, ready func() bool) {
	t.Helper()

	for deadline := time.Now().Add(5 * time.Second); !ready(); time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("timed out waiting for JetStream state")
		}
	}
}

func TestRunnerStarts(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)
	publishRunner(t, store, pullSubject)

	since := time.Now().UTC()

	publishRunner(t, store, pullSubject)

	allEvents, timedEvents, newEvents := make(chan Event, 4), make(chan Event, 4), make(chan Event, 4)
	all := fakeTarget{name: "all", deliver: func(ctx context.Context, event Event) error {
		allEvents <- event

		if event.Seq == 2 {
			<-ctx.Done()
		}

		return nil
	}}

	run := &runner{stream: store.stream, targets: []runnerTarget{
		{Subjects: nil, Target: all, Start: startPosition{all: true}},
		{Subjects: nil, Target: recordingTarget("time", timedEvents), Start: startPosition{at: &since}},
		{Start: startPosition{}, Subjects: nil, Target: recordingTarget("new", newEvents)},
	}}

	startRunner(t, run)
	waitRunner(t, func() bool {
		_, err := store.stream.Consumer(t.Context(), "new")

		return err == nil
	})
	expectRunner(t, allEvents, 1, 2)
	publishRunner(t, store, pullSubject)
	expectRunner(t, timedEvents, 2, 3)
	expectRunner(t, newEvents, 3)
}

func TestRunnerConsumerError(t *testing.T) {
	t.Parallel()

	store := testStore(t, retention)
	run := &runner{stream: store.stream, targets: []runnerTarget{{Target: fakeTarget{name: "."}}}}
	err := run.run(t.Context())
	checkEqual(t, errors.Is(err, jetstream.ErrInvalidConsumerName), true)

	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	checkError(t, run.run(ctx))
}

func TestRunnerRetryDelay(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		attempt uint64
		want    time.Duration
	}{
		{attempt: 1, want: time.Second},
		{attempt: 2, want: 2 * time.Second},
		{attempt: 10, want: 5 * time.Minute},
		{attempt: math.MaxUint64, want: 5 * time.Minute},
	} {
		checkEqual(t, retryDelay(time.Second, test.attempt), test.want)
	}
}

type runnerMessage struct {
	jetstream.Msg

	ackError error
}

func (runnerMessage) Metadata() (*jetstream.MsgMetadata, error) {
	return new(jetstream.MsgMetadata), nil
}

func (runnerMessage) Data() []byte { return []byte(testEnvelope) }

func (message runnerMessage) DoubleAck(context.Context) error { return message.ackError }

func TestRunnerAcknowledgment(t *testing.T) {
	t.Parallel()

	target := runnerTarget{
		Start:    startPosition{},
		Subjects: nil,
		Target: fakeTarget{name: "ack", deliver: func(_ context.Context, event Event) error {
			return fmt.Errorf("%w: %s", ErrPermanent, event.Envelope)
		}},
	}

	var run runner

	checkEqual(t, run.deliver(t.Context(), target, runnerMessage{Msg: nil, ackError: errRetryDelivery}, 1), uint64(1))
	checkEqual(t, run.deliver(t.Context(), target, runnerMessage{Msg: nil, ackError: nil}, 1), uint64(0))
}
