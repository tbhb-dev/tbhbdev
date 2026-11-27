// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package worker_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/worker"
)

const (
	maxQueueReads, batchSize = 10, 100
	removeFirst              = "remove 1"
	pullRequestEvent         = "pull_request"
	openedBody               = `{"repository":{"full_name":"example/widgets"},"action":"opened"}`
)

type queueStub struct {
	t       *testing.T
	pending func(int) ([]store.Entry, error)
	remove  func(uint64) error
	reads   int
}

func (q *queueStub) Pending(limit int) ([]store.Entry, error) {
	q.t.Helper()
	q.reads++

	// Bound broken drain loops during mutation checks.
	if q.reads > maxQueueReads {
		q.t.Fatal("worker kept reading the queue")
	}

	return q.pending(limit)
}

func (q *queueStub) Remove(seq uint64) error { return q.remove(seq) }

type recorderFunc func(context.Context, string, []byte) error

func (f recorderFunc) Record(ctx context.Context, event string, body []byte) error {
	return f(ctx, event, body)
}

type passCase struct {
	name, event, body, log string
	recordErr              error
	want                   []string
	queued                 int
}

func TestPass(t *testing.T) {
	t.Parallel()

	for _, test := range []passCase{
		{
			"record before remove", pullRequestEvent, openedBody, "", nil,
			[]string{"record " + pullRequestEvent + " " + openedBody, removeFirst},
			0,
		},
		{
			"raw body passed through", "ping", `{"action":`, "", nil,
			[]string{`record ping {"action":`, removeFirst},
			0,
		},
		{
			"recorder error kept", "ping\r\n", openedBody, "event=ping", store.ErrCorrupt,
			[]string{"record ping\r\n " + openedBody},
			1,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			checkPass(t, test)
		})
	}
}

func TestPassDrainsBatches(t *testing.T) {
	t.Parallel()

	const total = batchSize + 1

	var removed, recorded uint64

	queue := queueStub{
		t:     t,
		reads: 0,
		pending: func(limit int) ([]store.Entry, error) {
			if limit != batchSize {
				t.Fatalf("limit = %d, want %d", limit, batchSize)
			}

			var entries []store.Entry

			for seq := removed + 1; seq <= total && len(entries) < limit; seq++ {
				entries = append(entries, store.Entry{
					Delivery: "d1", Event: pullRequestEvent, Body: []byte(openedBody), Seq: seq,
				})
			}

			return entries, nil
		},
		remove: func(seq uint64) error {
			removed++

			if seq != removed || recorded != removed {
				t.Fatalf("Remove(%d): removed = %d, recorded = %d", seq, removed, recorded)
			}

			return nil
		},
	}
	consumer := worker.New(&queue, recorderFunc(func(_ context.Context, _ string, _ []byte) error {
		recorded++

		return nil
	}), slog.New(slog.DiscardHandler))

	err := consumer.Pass(t.Context())
	if err != nil || recorded != total || removed != total || queue.reads != 3 {
		t.Fatalf(
			"Pass: %v, recorded = %d, removed = %d, reads = %d",
			err,
			recorded,
			removed,
			queue.reads,
		)
	}
}

func TestPassRecorderRetry(t *testing.T) {
	t.Parallel()

	for _, passes := range []int{1, 2} {
		t.Run(fmt.Sprintf("passes %d", passes), func(t *testing.T) {
			t.Parallel()

			testRecorderRetry(t, passes)
		})
	}
}

func testRecorderRetry(t *testing.T, passes int) {
	t.Helper()

	queue, err := store.Open(filepath.Join(t.TempDir(), "queue.db"))
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { _ = queue.Close() })

	for _, body := range []string{openedBody, openedBody + " "} {
		_, err = queue.Enqueue(body, pullRequestEvent, []byte(body))
		if err != nil {
			t.Fatal(err)
		}
	}

	var records int

	recordErr := store.ErrCorrupt
	consumer := worker.New(queue, recorderFunc(func(_ context.Context, _ string, _ []byte) error {
		records++

		return recordErr
	}), slog.New(slog.DiscardHandler))

	for range passes {
		err = consumer.Pass(t.Context())
		if err != nil {
			t.Fatal(err)
		}

		recordErr = nil
	}

	entries, err := queue.Pending(batchSize)
	if err != nil || len(entries) != 2*(2-passes) || records != 2*passes-1 {
		t.Fatalf("pending: %v, entries = %v, records = %d", err, entries, records)
	}
}

func TestStoreErrorStops(t *testing.T) {
	t.Parallel()

	errStore := store.ErrCorrupt

	for _, operation := range []string{"pending", "remove"} {
		t.Run(operation, func(t *testing.T) {
			t.Parallel()

			var reads, records, removes int

			queue := queueStub{
				t:     t,
				reads: 0,
				pending: func(int) ([]store.Entry, error) {
					reads++

					if operation == "pending" {
						return nil, errStore
					}

					return twoEntries(), nil
				},
				remove: func(uint64) error {
					removes++

					return errStore
				},
			}
			consumer := worker.New(&queue, countRecords(&records), slog.New(slog.DiscardHandler))

			err := consumer.Run(t.Context(), time.Second)
			want := 0

			if operation == "remove" {
				want = 1
			}

			if !errors.Is(err, errStore) || reads != 1 || records != want || removes != want {
				t.Fatalf(
					"Run: %v, reads = %d, records = %d, removes = %d",
					err,
					reads,
					records,
					removes,
				)
			}
		})
	}
}

func TestRunPollsAndCancelStops(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		var reads int

		queue := queueStub{
			t:     t,
			reads: 0,
			pending: func(int) ([]store.Entry, error) {
				reads++

				return nil, nil
			},
			remove: func(uint64) error {
				t.Error("unexpected Remove")

				return nil
			},
		}
		consumer := worker.New(
			&queue,
			recorderFunc(func(_ context.Context, _ string, _ []byte) error {
				t.Error("unexpected record")

				return nil
			}),
			slog.New(slog.DiscardHandler),
		)
		done := make(chan error, 1)

		go func() { done <- consumer.Run(ctx, time.Second) }()

		synctest.Wait()

		if reads != 1 {
			t.Fatalf("startup reads = %d, want 1", reads)
		}

		time.Sleep(time.Second)
		synctest.Wait()

		if reads != 2 {
			t.Fatalf("reads after poll = %d, want 2", reads)
		}

		cancel()

		err := <-done
		if err != nil || reads != 2 {
			t.Fatalf("cancel: %v, reads = %d", err, reads)
		}
	})
}

func TestCancelDuringBatchStops(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()

	var records, removes int

	queue := queueStub{
		t:     t,
		reads: 0,
		pending: func(int) ([]store.Entry, error) {
			return twoEntries(), nil
		},
		remove: func(uint64) error {
			removes++

			return nil
		},
	}
	consumer := worker.New(
		&queue,
		recorderFunc(func(_ context.Context, _ string, _ []byte) error {
			records++

			cancel()

			return nil
		}),
		slog.New(slog.DiscardHandler),
	)

	err := consumer.Pass(ctx)
	if err != nil || records != 1 || removes != 1 {
		t.Fatalf("cancel: %v, records = %d, removes = %d", err, records, removes)
	}
}

func twoEntries() []store.Entry {
	return []store.Entry{
		{Delivery: "d1", Event: pullRequestEvent, Body: []byte(openedBody), Seq: 1},
		{Delivery: "d2", Event: pullRequestEvent, Body: []byte(openedBody), Seq: 2},
	}
}

func countRecords(records *int) recorderFunc {
	return func(_ context.Context, _ string, _ []byte) error {
		*records++

		return nil
	}
}

func checkPass(t *testing.T, test passCase) {
	t.Helper()

	var calls []string

	var logs bytes.Buffer

	entries := []store.Entry{
		{Delivery: "d1", Event: test.event, Body: []byte(test.body), Seq: 1},
	}
	queue := queueStub{
		t:       t,
		reads:   0,
		pending: func(int) ([]store.Entry, error) { return entries, nil },
		remove: func(seq uint64) error {
			calls = append(calls, fmt.Sprintf("remove %d", seq))
			entries = nil

			return nil
		},
	}
	recorder := recorderFunc(func(_ context.Context, event string, body []byte) error {
		calls = append(calls, "record "+event+" "+string(body))

		return test.recordErr
	})

	err := worker.New(&queue, recorder, slog.New(slog.NewTextHandler(&logs, nil))).
		Pass(t.Context())
	if err != nil || !reflect.DeepEqual(calls, test.want) ||
		!strings.Contains(logs.String(), test.log) {
		t.Fatalf("Pass: %v, calls = %v, logs = %q", err, calls, logs.String())
	}

	if len(entries) != test.queued {
		t.Fatalf("queued = %d, want %d", len(entries), test.queued)
	}

	if test.queued != 0 &&
		(strings.Count(logs.String(), "level=WARN") != 1 || strings.Contains(logs.String(), test.body)) {
		t.Fatalf("expected one warning without the body, logs = %q", logs.String())
	}
}
