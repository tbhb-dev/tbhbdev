// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package appauth_test

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const concurrentCallers = 32

func TestConcurrentTokens(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)

	var (
		calls    atomic.Int64
		renewals sync.WaitGroup
	)

	server := httptest.NewServer(http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
		count := calls.Add(1)
		if count == 2 {
			renewals.Wait()
			// Hold the response while the other callers enter Renew.
			time.Sleep(100 * time.Millisecond)
		}

		resp.WriteHeader(http.StatusCreated)

		_, _ = fmt.Fprintf(
			resp,
			`{"token":"installation-%d","expires_at":%q}`,
			count,
			now.Add(time.Hour).Format(time.RFC3339),
		)
	}))
	t.Cleanup(server.Close)

	source := newSource(t, config(t, server, func() time.Time { return now }))
	concurrentCalls(t, source.Token, "installation-1")

	if calls.Load() != 1 {
		t.Fatal("concurrent calls didn't share the cache")
	}

	renewals.Add(concurrentCallers)
	concurrentCalls(t, func(ctx context.Context) (string, error) {
		renewals.Done()

		return source.Renew(ctx)
	}, "installation-2")

	if calls.Load() != 2 {
		t.Error("concurrent Renew calls didn't share the fetched token")
	}
}

func concurrentCalls(t *testing.T, call func(context.Context) (string, error), want string) {
	t.Helper()

	start := make(chan struct{})

	var workers sync.WaitGroup

	for range concurrentCallers {
		workers.Go(func() {
			<-start

			token, err := call(t.Context())
			if err != nil || token != want {
				t.Error("concurrent token call failed")
			}
		})
	}

	close(start)
	workers.Wait()
}
