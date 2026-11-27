// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	_ "embed"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	ci "github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
)

type ledger struct {
	claims map[string]bool
	err    error
}

func (l ledger) ClaimMilestone(key string) (bool, error) {
	if l.err != nil {
		return false, l.err
	}

	previous := l.claims[key]
	l.claims[key] = true

	return !previous, nil
}

//go:embed testdata/ci_jobs.json
var jobs string

const (
	capMode       = "cap"
	forbiddenMode = "forbidden"
	runs          = `{"workflow_runs":[{"id":1,"status":"completed","event":"schedule"},{"id":2,"status":"in_progress"}]}`
)

func TestCIBackfill(t *testing.T) {
	t.Parallel()

	for _, mode := range []string{"success", forbiddenMode, "decode_error", "claim_error", "cancelled", capMode} {
		t.Run(mode, func(t *testing.T) {
			t.Parallel()

			var (
				logs  bytes.Buffer
				calls atomic.Int32
			)

			server := httptest.NewTLSServer(history(t, mode, &calls))

			defer server.Close()

			client, err := ghclient.New(
				server.URL,
				server.Client(),
				budget.New(),
				fakeTokens{},
				ghclient.NewETags(),
			)
			if err != nil {
				t.Fatal(err)
			}

			claims := ledger{claims: map[string]bool{"2\x00ci_job": true}, err: nil}

			if mode == "claim_error" {
				claims.err = errors.ErrUnsupported
			}

			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()

			if mode == "cancelled" {
				cancel()
			}

			ci.BackfillCI(
				ctx,
				client,
				claims,
				slog.New(slog.NewJSONHandler(&logs, nil)),
				ci.CIConfig{
					Repository: listedRepo,
					Days:       7,
				},
				time.Date(2000, time.January, 1, 1, 0, 0, 0, time.UTC),
			)
			checkLogs(t, mode, calls.Load(), claims, logs.String())
		})
	}
}

//nolint:gocognit,cyclop // The fake server covers pagination and failure responses.
func history(t *testing.T, mode string, calls *atomic.Int32) http.HandlerFunc {
	t.Helper()

	return func(resp http.ResponseWriter, req *http.Request) {
		calls.Add(1)

		if mode == forbiddenMode {
			resp.WriteHeader(http.StatusForbidden)

			return
		}

		body := runs

		switch {
		case strings.HasSuffix(req.URL.Path, "/jobs"):
			body = jobs

			if req.URL.Query().Get("page") == "" && req.URL.Query().Get("filter") != "all" {
				t.Error("missing all attempts filter")
			}

			if req.URL.Query().Get("page") == "" {
				resp.Header().Set("Link", "<https://"+req.Host+req.URL.Path+`?page=2>; rel="next"`)
			}

		case strings.HasSuffix(req.URL.Path, "/actions/runs"):
			if req.URL.Query().Get("page") == "" &&
				req.URL.Query().Get("created") != ">=1999-12-25T01:00:00Z" &&
				mode != capMode {
				t.Errorf("created = %s", req.URL.Query().Get("created"))
			}

			if req.URL.Query().Get("page") != "" || mode == capMode {
				body = `{"workflow_runs":[]}`
			}

			if req.URL.Query().Get("page") == "" || mode == capMode {
				resp.Header().Set("Link", "<https://"+req.Host+req.URL.Path+`?page=2>; rel="next"`)
			}

		default:
			body = `{"default_branch":"main"}`
		}

		if mode == "decode_error" {
			body = "{"
		}

		write(t, resp, body)
	}
}

//nolint:gocognit,cyclop // Each case checks its terminal summary and job records.
func checkLogs(t *testing.T, mode string, calls int32, claims ledger, logs string) {
	t.Helper()

	entries := parseCILogs(t, logs)

	finished, failed := entries["ci backfill finished"], entries["ci backfill failed"]

	if len(entries["ci backfill started"]) != 1 || len(finished)+len(failed) != 1 {
		t.Fatalf("terminal logs: %s", logs)
	}

	if mode != "success" && mode != capMode {
		if len(failed) != 1 || len(entries["ci job finished"]) != 0 {
			t.Fatalf("want failure without job logs: %s", logs)
		}

		if mode == forbiddenMode && (failed[0]["status"] != float64(403) || calls != 1) {
			t.Fatalf("403 evidence: %s", logs)
		}

		return
	}

	if mode == capMode {
		if calls != 30 || finished[0]["pages"] != float64(30) {
			t.Fatalf("cap: calls=%d logs=%s", calls, logs)
		}

		return
	}

	logged := entries["ci job finished"]

	if len(logged) != 1 || !claims.claims["1\x00ci_job"] || len(claims.claims) != 2 {
		t.Fatalf("claims or duplicates: %s %v", logs, claims.claims)
	}

	for key, want := range map[string]any{
		"time": "2000-01-01T00:02:00Z", "event": "schedule", "event_class": "push",
		"source": "backfill", "runner": "linux", "queue_seconds": float64(60), "run_seconds": float64(60),
	} {
		if logged[0][key] != want {
			t.Errorf("%s = %v, want %v", key, logged[0][key], want)
		}
	}

	if finished[0]["pages"] != float64(5) || finished[0]["jobs_skipped"] != float64(9) {
		t.Fatalf("summary: %s", logs)
	}
}

func parseCILogs(t *testing.T, logs string) map[string][]map[string]any {
	t.Helper()

	entries := map[string][]map[string]any{}

	for line := range strings.SplitSeq(strings.TrimSpace(logs), "\n") {
		var entry map[string]any

		err := json.Unmarshal([]byte(line), &entry)
		if err != nil {
			t.Fatal(err)
		}

		message := fmt.Sprint(entry["msg"])

		entries[message] = append(entries[message], entry)
	}

	return entries
}
