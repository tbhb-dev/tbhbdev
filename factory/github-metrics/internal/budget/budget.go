// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package budget shares one GitHub request allowance among every caller.
// A token bucket holds the primary budget, a pacer spaces requests for the
// secondary limit, and a block window stops all work after a low
// `x-ratelimit-remaining` or a secondary rate limit response.
package budget

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Priority orders waiters. A lower value goes first. The order is strict and
// has no aging, so steady webhook and incremental load can starve sweep and
// backfill work, which is the intended trade: live events come first.
type Priority int

// The priorities, highest first.
const (
	Webhook Priority = iota
	Incremental
	Sweep
	Backfill
)

const (
	// Interval is the time between new tokens: 2,500 an hour, half of the
	// 5,000 an hour floor.
	Interval = 1440 * time.Millisecond
	// Burst is the most tokens the bucket holds.
	Burst = 60
	// Spacing is the least time between two requests, which keeps the
	// secondary limit at 240 points a minute at most.
	Spacing = 250 * time.Millisecond
	// Floor is the `x-ratelimit-remaining` value under which all work stops
	// until the reset.
	Floor = 500

	// CoreResource is the `x-ratelimit-resource` value of the budget this
	// limiter guards. Headers of any other resource are ignored.
	CoreResource = "core"
	// MaxReset caps how far ahead an observed reset may stop work. The
	// primary window is one hour, so a later reset is a skewed clock or a
	// bad header.
	MaxReset = time.Hour

	firstBackoff  = time.Minute
	backoffFactor = 2
	maxBackoff    = 15 * time.Minute
)

type waiter struct {
	priority Priority
	seq      uint64
}

// Limiter hands out request slots. Make one with New.
type Limiter struct {
	refilled    time.Time
	nextAllowed time.Time
	blockedTill time.Time
	changed     chan struct{}
	queue       []*waiter
	mu          sync.Mutex
	seq         uint64
	tokens      int
	backoff     time.Duration
}

// New returns a limiter with a full bucket.
func New() *Limiter {
	return &Limiter{
		refilled:    time.Now(),
		nextAllowed: time.Time{},
		blockedTill: time.Time{},
		changed:     make(chan struct{}),
		queue:       nil,
		mu:          sync.Mutex{},
		seq:         0,
		tokens:      Burst,
		backoff:     0,
	}
}

// Wait blocks until the caller may send one request, and takes a token. It
// returns the context's error if the context ends first.
func (l *Limiter) Wait(ctx context.Context, priority Priority) error {
	l.mu.Lock()

	self := &waiter{priority: priority, seq: l.seq}
	l.seq++
	l.queue = append(l.queue, self)
	l.mu.Unlock()

	for {
		l.mu.Lock()

		now := time.Now()
		l.refill(now)

		var wake time.Time

		if l.first() == self {
			wake = l.readyAt(now)
			if wake.IsZero() {
				l.take(now, self)
				l.mu.Unlock()

				return nil
			}
		}

		changed := l.changed
		l.mu.Unlock()

		err := l.sleep(ctx, now, wake, changed)
		if err != nil {
			l.mu.Lock()
			l.remove(self)
			l.mu.Unlock()

			return err
		}
	}
}

// Observe records the rate limit headers of a response. Under the floor it
// stops all work until reset, or for MaxReset if the reset is later than
// that. A zero or past reset time has no effect. Headers of a resource other
// than CoreResource are ignored, because their limits are far below the
// floor. An empty resource counts as core.
func (l *Limiter) Observe(resource string, remaining int, reset time.Time) {
	if remaining >= Floor || (resource != "" && resource != CoreResource) {
		return
	}

	l.mu.Lock()
	defer l.mu.Unlock()

	if horizon := time.Now().Add(MaxReset); reset.After(horizon) {
		reset = horizon
	}

	l.blockUntil(reset)
}

// Backoff records a secondary rate limit response. A positive `retry-after`
// value stops all work for that long. Without one, work stops for a minute,
// and each further call doubles the time, up to 15 minutes, until Success.
func (l *Limiter) Backoff(retry time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()

	wait := retry

	if wait <= 0 {
		l.backoff = min(max(l.backoff*backoffFactor, firstBackoff), maxBackoff)
		wait = l.backoff
	}

	l.blockUntil(time.Now().Add(wait))
}

// Success clears the doubling wait of Backoff.
func (l *Limiter) Success() {
	l.mu.Lock()
	defer l.mu.Unlock()

	l.backoff = 0
}

// sleep waits for a change, the wake time when one is set, or the context.
func (l *Limiter) sleep(ctx context.Context, now, wake time.Time, changed <-chan struct{}) error {
	var timer <-chan time.Time

	if !wake.IsZero() {
		t := time.NewTimer(wake.Sub(now))
		defer t.Stop()

		timer = t.C
	}

	select {
	case <-ctx.Done():
		return fmt.Errorf("budget wait: %w", ctx.Err())
	case <-changed:
	case <-timer:
	}

	return nil
}

func (l *Limiter) blockUntil(t time.Time) {
	if t.After(l.blockedTill) {
		l.blockedTill = t
		l.notify()
	}
}

// refill adds the tokens that have accrued since the last refill.
func (l *Limiter) refill(now time.Time) {
	accrued := int(now.Sub(l.refilled) / Interval)
	if accrued <= 0 {
		return
	}

	l.tokens += accrued
	l.refilled = l.refilled.Add(time.Duration(accrued) * Interval)

	if l.tokens >= Burst {
		l.tokens = Burst
		l.refilled = now
	}
}

// first returns the waiter next in line: the highest priority, then the
// oldest.
func (l *Limiter) first() *waiter {
	var best *waiter

	for _, w := range l.queue {
		if best == nil || w.priority < best.priority ||
			(w.priority == best.priority && w.seq < best.seq) {
			best = w
		}
	}

	return best
}

// readyAt returns the zero time when a request may go now, and otherwise the
// time to check again.
func (l *Limiter) readyAt(now time.Time) time.Time {
	ready := now

	if l.tokens < 1 {
		ready = l.refilled.Add(Interval)
	}

	ready = laterOf(ready, l.nextAllowed)
	ready = laterOf(ready, l.blockedTill)

	if !ready.After(now) {
		return time.Time{}
	}

	return ready
}

func laterOf(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}

	return a
}

func (l *Limiter) take(now time.Time, taker *waiter) {
	if l.tokens >= Burst {
		l.refilled = now
	}

	l.tokens--
	l.nextAllowed = now.Add(Spacing)
	l.remove(taker)
}

func (l *Limiter) remove(w *waiter) {
	for i, q := range l.queue {
		if q == w {
			l.queue = append(l.queue[:i], l.queue[i+1:]...)

			break
		}
	}

	l.notify()
}

// notify wakes every waiter to re-check its place. The cost is linear in the
// number of waiters for each event, which suits a few callers.
func (l *Limiter) notify() {
	close(l.changed)
	l.changed = make(chan struct{})
}
