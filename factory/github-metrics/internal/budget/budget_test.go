// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package budget_test

import (
	"context"
	"errors"
	"slices"
	"testing"
	"testing/synctest"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
)

func mustWait(t *testing.T, lim *budget.Limiter, p budget.Priority) {
	t.Helper()

	err := lim.Wait(t.Context(), p)
	if err != nil {
		t.Fatal(err)
	}
}

// drain takes every burst token, spaced as the limiter spaces them.
func drain(t *testing.T, lim *budget.Limiter) {
	t.Helper()

	for range budget.Burst {
		mustWait(t, lim, budget.Backfill)
	}
}

// TestRollingHourStaysUnderCap sends requests as fast as the limiter allows
// for three hours and checks the design's two bounds: at most 2,560 in any
// rolling hour and at least 250 ms between any two.
func TestRollingHourStaysUnderCap(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		start := time.Now()

		var times []time.Time

		for time.Since(start) < 3*time.Hour {
			mustWait(t, lim, budget.Backfill)

			times = append(times, time.Now())
		}

		for i := 1; i < len(times); i++ {
			if gap := times[i].Sub(times[i-1]); gap < budget.Spacing {
				t.Fatalf("requests %d and %d are %v apart", i-1, i, gap)
			}
		}

		for start, from := range times {
			end, _ := slices.BinarySearchFunc(
				times,
				from.Add(time.Hour),
				func(e, target time.Time) int {
					return e.Compare(target)
				},
			)
			if n := end - start; n > 2560 {
				t.Fatalf("%d requests in the hour from request %d", n, start)
			}
		}
	})
}

// TestHigherPriorityGoesFirst queues a backfill request ahead of a webhook
// request and expects the webhook request to go first.
func TestHigherPriorityGoesFirst(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		drain(t, lim)

		order := make(chan budget.Priority, 2)
		send := func(p budget.Priority) {
			err := lim.Wait(t.Context(), p)
			if err == nil {
				order <- p
			}
		}

		go send(budget.Backfill)

		synctest.Wait()

		go send(budget.Webhook)

		synctest.Wait()

		if first := <-order; first != budget.Webhook {
			t.Fatalf("first through was priority %d, want the webhook", first)
		}
	})
}

// TestAllPrioritiesGoInOrder queues one request per priority, lowest first,
// behind a drained bucket and expects the reverse order out.
func TestAllPrioritiesGoInOrder(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		drain(t, lim)

		order := make(chan budget.Priority, 4)

		for _, p := range []budget.Priority{budget.Backfill, budget.Sweep, budget.Incremental, budget.Webhook} {
			go func() {
				err := lim.Wait(t.Context(), p)
				if err == nil {
					order <- p
				}
			}()

			synctest.Wait()
		}

		for _, want := range []budget.Priority{budget.Webhook, budget.Incremental, budget.Sweep, budget.Backfill} {
			if got := <-order; got != want {
				t.Fatalf("priority %d went where %d belongs", got, want)
			}
		}
	})
}

// TestShortBackoffKeepsTheLongerReset checks that a short retry-after after a
// low remaining count doesn't shorten the wait until the reset.
func TestShortBackoffKeepsTheLongerReset(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		reset := time.Now().Add(20 * time.Minute)

		lim.Observe(budget.CoreResource, budget.Floor-1, reset)
		lim.Backoff(time.Second)
		mustWait(t, lim, budget.Webhook)

		if now := time.Now(); now.Before(reset) {
			t.Fatalf("request went at %v, before the reset at %v", now, reset)
		}
	})
}

// TestLowRemainingStopsUntilReset checks the floor on x-ratelimit-remaining.
func TestLowRemainingStopsUntilReset(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		reset := time.Now().Add(20 * time.Minute)

		lim.Observe(budget.CoreResource, budget.Floor, reset)
		mustWait(t, lim, budget.Webhook)

		lim.Observe(budget.CoreResource, budget.Floor-1, reset)
		mustWait(t, lim, budget.Webhook)

		if now := time.Now(); now.Before(reset) {
			t.Fatalf("request went at %v, before the reset at %v", now, reset)
		}
	})
}

// TestSecondaryBackoff checks the retry-after wait, the doubling wait, its
// 15 minute cap, and its reset by Success.
func TestSecondaryBackoff(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		waited := func(call func()) time.Duration {
			call()

			before := time.Now()

			mustWait(t, lim, budget.Webhook)

			return time.Since(before)
		}

		if got := waited(func() { lim.Backoff(7 * time.Second) }); got != 7*time.Second {
			t.Errorf("retry-after wait was %v", got)
		}

		doubling := []time.Duration{
			time.Minute,
			2 * time.Minute,
			4 * time.Minute,
			8 * time.Minute,
			15 * time.Minute,
			15 * time.Minute,
		}

		for _, want := range doubling {
			if got := waited(func() { lim.Backoff(0) }); got != want {
				t.Errorf("doubling wait was %v, want %v", got, want)
			}
		}

		lim.Success()

		if got := waited(func() { lim.Backoff(0) }); got != time.Minute {
			t.Errorf("wait after Success was %v, want a minute", got)
		}
	})
}

// TestCancelledWaitLeavesTheQueue checks that a caller whose context ends
// stops waiting and doesn't hold up the caller behind it.
func TestCancelledWaitLeavesTheQueue(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		drain(t, lim)

		ctx, cancel := context.WithCancel(t.Context())
		errs := make(chan error, 1)

		go func() { errs <- lim.Wait(ctx, budget.Webhook) }()

		synctest.Wait()
		cancel()

		err := <-errs
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("got %v, want context.Canceled", err)
		}

		mustWait(t, lim, budget.Backfill)
	})
}

// TestFarFutureResetIsCapped checks that a reset beyond MaxReset stops work
// for MaxReset only, and that another resource's headers stop nothing.
func TestFarFutureResetIsCapped(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		lim := budget.New()
		start := time.Now()

		lim.Observe("search", 0, start.Add(48*time.Hour))
		mustWait(t, lim, budget.Webhook)

		if waited := time.Since(start); waited > time.Second {
			t.Fatalf("another resource stopped work for %v", waited)
		}

		lim.Observe(budget.CoreResource, budget.Floor-1, start.Add(48*time.Hour))
		mustWait(t, lim, budget.Webhook)

		waited := time.Since(start)
		if waited < budget.MaxReset || waited > budget.MaxReset+time.Minute {
			t.Fatalf("waited %v, want about %v", waited, budget.MaxReset)
		}
	})
}
