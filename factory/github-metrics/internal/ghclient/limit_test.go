// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package ghclient_test

import (
	"context"
	"errors"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
)

// searchPath is the request path that the tests answer as a search resource.
const searchPath = "/search"

// spentHeader is a response header for a resource with no requests left.
func spentHeader(resource string, reset int64) http.Header {
	header := http.Header{}
	header.Set("X-Ratelimit-Resource", resource)
	header.Set("X-Ratelimit-Remaining", "0")
	header.Set("X-Ratelimit-Reset", strconv.FormatInt(reset, 10))

	return header
}

// limitedOnce answers the first request with the given status and header,
// and every later request with a 200.
func limitedOnce(status int, header http.Header, calls *atomic.Int32) roundTrip {
	return func(*http.Request) *http.Response {
		if calls.Add(1) == 1 {
			return reply(status, header)
		}

		return reply(http.StatusOK, http.Header{})
	}
}

// getWaited sends one request and returns how long it took on the fake clock.
func getWaited(ctx context.Context, t *testing.T, client *ghclient.Client) (time.Duration, error) {
	t.Helper()

	start := time.Now()
	_, err := client.Get(ctx, budget.Webhook, "/x")

	return time.Since(start), err
}

func TestSpentNonCoreLimitWaitsForItsOwnReset(t *testing.T) {
	t.Parallel()

	for _, status := range []int{http.StatusForbidden, http.StatusTooManyRequests} {
		synctestRun(t, func(ctx context.Context) {
			var calls atomic.Int32

			reset := time.Now().Add(5 * time.Minute).Unix()
			client := fakeClient(t, limitedOnce(status, spentHeader("search", reset), &calls))

			waited, err := getWaited(ctx, t, client)
			if err != nil || calls.Load() != 2 || waited < 4*time.Minute || waited > 6*time.Minute {
				t.Fatalf(
					"status %d: err = %v, calls = %d, waited %v",
					status,
					err,
					calls.Load(),
					waited,
				)
			}
		})
	}
}

func TestSpentNonCoreLimitLeavesCoreRunning(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		reset := time.Now().Add(10 * time.Minute).Unix()
		client := fakeClient(t, func(req *http.Request) *http.Response {
			if req.URL.Path == searchPath && time.Now().Unix() < reset {
				return reply(http.StatusForbidden, spentHeader("search", reset))
			}

			return reply(http.StatusOK, http.Header{})
		})

		done := make(chan error, 1)

		go func() {
			_, err := client.Get(ctx, budget.Webhook, searchPath)
			done <- err
		}()

		synctest.Wait() // search is now in its backoff

		start := time.Now()

		_, err := client.Get(ctx, budget.Webhook, "/core")
		if err != nil || time.Since(start) > time.Minute {
			t.Fatalf("core request: err = %v, waited %v", err, time.Since(start))
		}

		searchErr := <-done
		if searchErr != nil {
			t.Fatal(searchErr)
		}
	})
}

func TestSpentNonCoreLimitWithoutResetGivesUp(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		client := fakeClient(
			t,
			limitedOnce(http.StatusTooManyRequests, spentHeader("graphql", 0), &calls),
		)

		_, err := getWaited(ctx, t, client)
		if !errors.Is(err, ghclient.ErrGaveUp) || calls.Load() != 1 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}
	})
}

func TestSpentNonCoreSecondaryWithoutWaitRetries(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		client := fakeClient(t, func(*http.Request) *http.Response {
			if calls.Add(1) == 1 {
				res := reply(http.StatusForbidden, spentHeader("search", 0))
				res.Body = io.NopCloser(
					strings.NewReader(`{"message":"You have exceeded a secondary rate limit."}`),
				)

				return res
			}

			return reply(http.StatusOK, http.Header{})
		})

		_, err := getWaited(ctx, t, client)
		if err != nil || calls.Load() != 2 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}
	})
}

func TestSpentNonCorePastResetWaitsBrieflyAndRetries(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		// A local clock ahead of GitHub's makes a fresh reset look past.
		reset := time.Now().Add(-5 * time.Second).Unix()
		client := fakeClient(
			t,
			limitedOnce(http.StatusForbidden, spentHeader("search", reset), &calls),
		)

		waited, err := getWaited(ctx, t, client)
		if err != nil || calls.Load() != 2 || waited < time.Second || waited > 10*time.Second {
			t.Fatalf("err = %v, calls = %d, waited %v", err, calls.Load(), waited)
		}
	})
}

func TestSpentCore429WaitsForReset(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(10 * time.Minute).Unix()
		header := spentHeader(budget.CoreResource, reset)
		client := fakeClient(t, limitedOnce(http.StatusTooManyRequests, header, &calls))

		waited, err := getWaited(ctx, t, client)
		if err != nil || waited < 9*time.Minute {
			t.Fatalf("err = %v, waited %v", err, waited)
		}
	})
}

func TestSpentCoreResetIsCappedAtOneHour(t *testing.T) {
	t.Parallel()

	// Each reset is built inside the bubble, so each is relative to the fake clock.
	year2099 := time.Date(2099, time.January, 1, 0, 0, 0, 0, time.UTC).Unix()

	for _, test := range []struct {
		reset   func(now time.Time) int64
		atLeast time.Duration
	}{
		{func(now time.Time) int64 { return now.Add(time.Hour).Unix() }, 59 * time.Minute},
		{func(now time.Time) int64 { return now.Add(48 * time.Hour).Unix() }, 59 * time.Minute},
		{func(time.Time) int64 { return year2099 }, 59 * time.Minute},
	} {
		synctestRun(t, func(ctx context.Context) {
			var calls atomic.Int32

			value := test.reset(time.Now())
			client := fakeClient(
				t,
				limitedOnce(http.StatusForbidden, spentHeader(budget.CoreResource, value), &calls),
			)

			waited, err := getWaited(ctx, t, client)
			if err != nil || waited < test.atLeast || waited > time.Hour+time.Minute {
				t.Fatalf("reset %d: err = %v, waited %v", value, err, waited)
			}
		})
	}
}

// TestImplausibleResetIsInvalid covers a reset after 2100, a reset within the
// first minute of 1970, and a signed one. None is a real reset.
func TestImplausibleResetIsInvalid(t *testing.T) {
	t.Parallel()

	for _, reset := range []string{strconv.FormatInt(math.MaxInt64, 10), "99999999999", "+99", "30", "59", "4102444801"} {
		synctestRun(t, func(ctx context.Context) {
			var calls atomic.Int32

			header := spentHeader("search", 0)
			header.Set("X-Ratelimit-Reset", reset)
			client := fakeClient(t, limitedOnce(http.StatusForbidden, header, &calls))

			_, err := getWaited(ctx, t, client)
			if !errors.Is(err, ghclient.ErrGaveUp) || calls.Load() != 1 {
				t.Fatalf("reset %q: err = %v, calls = %d", reset, err, calls.Load())
			}
		})
	}
}

// TestResetBoundsAreValid covers the first value each bound accepts. A reset
// of 60 is long past and waits pastResetWait. The top bound, 4102444800, is
// far ahead and waits the one-hour cap instead.
func TestResetBoundsAreValid(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		reset           string
		atLeast, atMost time.Duration
	}{
		// The 3 seconds are pastResetWait, the wait for a reset that's already past.
		{"60", 3 * time.Second, 3 * time.Second},
		{"4102444800", 59 * time.Minute, time.Hour + time.Minute},
	} {
		synctestRun(t, func(ctx context.Context) {
			var calls atomic.Int32

			header := spentHeader("search", 0)
			header.Set("X-Ratelimit-Reset", test.reset)
			client := fakeClient(t, limitedOnce(http.StatusForbidden, header, &calls))

			waited, err := getWaited(ctx, t, client)
			if err != nil || calls.Load() != 2 || waited < test.atLeast || waited > test.atMost {
				t.Fatalf(
					"reset %q: err = %v, calls = %d, waited %v",
					test.reset,
					err,
					calls.Load(),
					waited,
				)
			}
		})
	}
}

// TestSpentCoreImplausibleResetBacksOffBriefly proves that a spent core
// response with an unusable reset still pauses the shared limiter on its
// doubling backoff, for at least a minute. The request retries and succeeds. It
// neither gives up nor waits the one-hour cap.
func TestSpentCoreImplausibleResetBacksOffBriefly(t *testing.T) {
	t.Parallel()

	for _, reset := range []string{strconv.FormatInt(math.MinInt64, 10), "30", "99999999999"} {
		synctestRun(t, func(ctx context.Context) {
			var calls atomic.Int32

			header := spentHeader(budget.CoreResource, 0)
			header.Set("X-Ratelimit-Reset", reset)
			client := fakeClient(t, limitedOnce(http.StatusForbidden, header, &calls))

			waited, err := getWaited(ctx, t, client)
			if err != nil || calls.Load() != 2 || waited < time.Minute || waited > 2*time.Minute {
				t.Fatalf(
					"reset %q: err = %v, calls = %d, waited %v",
					reset,
					err,
					calls.Load(),
					waited,
				)
			}
		})
	}
}

// This test pins the give-up path for a missing or non-numeric reset. It
// passed before the past-reset retry too, so it doesn't prove that fix.
func TestSpentNonCoreWithoutUsableResetGivesUpAfterOneSend(t *testing.T) {
	t.Parallel()

	for _, reset := range []string{"", "soon", "1.5"} {
		synctestRun(t, func(ctx context.Context) {
			var calls atomic.Int32

			header := spentHeader("search", 0)
			header.Del("X-Ratelimit-Reset")

			if reset != "" {
				header.Set("X-Ratelimit-Reset", reset)
			}

			client := fakeClient(t, func(*http.Request) *http.Response {
				calls.Add(1)

				return reply(http.StatusForbidden, header)
			})

			waited, err := getWaited(ctx, t, client)
			if !errors.Is(err, ghclient.ErrGaveUp) || calls.Load() != 1 || waited != 0 {
				t.Fatalf(
					"reset %q: err = %v, calls = %d, waited %v",
					reset,
					err,
					calls.Load(),
					waited,
				)
			}
		})
	}
}

func TestSpentNonCorePastResetEveryTimeGivesUpAfterThreeWaits(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(-5 * time.Second).Unix()
		client := fakeClient(t, func(*http.Request) *http.Response {
			calls.Add(1)

			return reply(http.StatusForbidden, spentHeader("search", reset))
		})

		waited, err := getWaited(ctx, t, client)
		if !errors.Is(err, ghclient.ErrGaveUp) || calls.Load() != 4 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}

		if waited != 9*time.Second {
			t.Fatalf("waited %v", waited)
		}
	})
}

func TestSpentNonCorePastResetLeavesCoreRunning(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(-5 * time.Second).Unix()
		client := fakeClient(t, func(req *http.Request) *http.Response {
			if req.URL.Path == searchPath && calls.Add(1) == 1 {
				return reply(http.StatusForbidden, spentHeader("search", reset))
			}

			return reply(http.StatusOK, http.Header{})
		})

		done := make(chan error, 1)

		go func() {
			_, err := client.Get(ctx, budget.Webhook, searchPath)
			done <- err
		}()

		synctest.Wait() // search is now in its short wait

		start := time.Now()

		_, err := client.Get(ctx, budget.Webhook, "/core")
		if err != nil || time.Since(start) > time.Second {
			t.Fatalf("core request: err = %v, waited %v", err, time.Since(start))
		}

		searchErr := <-done
		if searchErr != nil {
			t.Fatal(searchErr)
		}
	})
}

func TestExactlyOneHourResetWaitsAnHour(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(budget.MaxReset).Unix()
		client := fakeClient(
			t,
			limitedOnce(http.StatusForbidden, spentHeader(budget.CoreResource, reset), &calls),
		)

		waited, err := getWaited(ctx, t, client)
		if err != nil || waited < 59*time.Minute || waited > time.Hour+time.Minute {
			t.Fatalf("err = %v, waited %v", err, waited)
		}
	})
}

func TestCancelDuringNonCoreBackoffStops(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(30 * time.Minute).Unix()
		client := fakeClient(
			t,
			limitedOnce(http.StatusForbidden, spentHeader("search", reset), &calls),
		)

		cancelled, cancel := context.WithTimeout(ctx, time.Minute)
		defer cancel()

		_, err := getWaited(cancelled, t, client)
		if !errors.Is(err, context.DeadlineExceeded) || calls.Load() != 1 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}
	})
}

func TestRetryAfterRefusesPlusSign(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		waited, err := waitAfterLimit(ctx, t, "+5")
		if err != nil || waited < time.Minute {
			t.Fatalf("err = %v, waited %v", err, waited)
		}
	})
}

func TestSpentLimitWaitsForLongerOfRetryAfterAndReset(t *testing.T) {
	t.Parallel()

	for _, resource := range []string{"search", budget.CoreResource} {
		for _, status := range []int{http.StatusForbidden, http.StatusTooManyRequests} {
			synctestRun(t, func(ctx context.Context) {
				var calls atomic.Int32

				header := spentHeader(resource, time.Now().Add(10*time.Minute).Unix())
				header.Set("Retry-After", "5")
				client := fakeClient(t, limitedOnce(status, header, &calls))

				waited, err := getWaited(ctx, t, client)
				if err != nil || calls.Load() != 2 || waited < 9*time.Minute {
					t.Fatalf(
						"%s %d: err = %v, calls = %d, waited %v",
						resource,
						status,
						err,
						calls.Load(),
						waited,
					)
				}
			})
		}
	}
}

func TestSpentNonCoreSecondaryBodyPausesCoreToo(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(time.Minute).Unix()
		client := fakeClient(t, func(req *http.Request) *http.Response {
			if req.URL.Path == searchPath && calls.Add(1) == 1 {
				res := reply(http.StatusForbidden, spentHeader("search", reset))
				res.Body = io.NopCloser(
					strings.NewReader(`{"message":"You have exceeded a secondary rate limit."}`),
				)

				return res
			}

			return reply(http.StatusOK, http.Header{})
		})

		done := make(chan error, 1)

		go func() {
			_, err := client.Get(ctx, budget.Webhook, searchPath)
			done <- err
		}()

		synctest.Wait() // search is now in its backoff

		start := time.Now()

		_, err := client.Get(ctx, budget.Webhook, "/core")
		if err != nil || time.Since(start) < 30*time.Second {
			t.Fatalf("core request: err = %v, waited %v", err, time.Since(start))
		}

		searchErr := <-done
		if searchErr != nil {
			t.Fatal(searchErr)
		}
	})
}

func TestSpentNonCoreGivesUpWithoutWaitingAfterLastSend(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		client := fakeClient(t, func(*http.Request) *http.Response {
			calls.Add(1)

			return reply(
				http.StatusForbidden,
				spentHeader("search", time.Now().Add(30*time.Minute).Unix()),
			)
		})

		waited, err := getWaited(ctx, t, client)
		if !errors.Is(err, ghclient.ErrGaveUp) || calls.Load() != 4 || waited > 100*time.Minute {
			t.Fatalf("err = %v, calls = %d, waited %v", err, calls.Load(), waited)
		}
	})
}
