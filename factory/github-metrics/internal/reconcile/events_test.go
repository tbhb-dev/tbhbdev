// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile_test

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
)

const (
	closedEvent = "closed"
	eventsPath  = "/repos/o/r/issues/7/events?per_page=100"
)

func event(kind, created string) string {
	return `{"event":"` + kind + `","created_at":"` + created + `"}`
}

func TestFirstEndRules(t *testing.T) {
	t.Parallel()

	first := time.Date(2026, time.January, 2, 1, 0, 0, 0, time.UTC)

	for _, test := range []struct {
		at          time.Time
		name, state string
		events      []string
	}{
		{
			first, "a later reopen and close doesn't move the first", closedEvent, []string{
				event("labeled", "2026-01-02T00:30:00Z"), event(closedEvent, "2026-01-02T01:00:00Z"),
				event("reopened", "2026-01-02T02:00:00Z"), event(closedEvent, "2026-01-02T03:00:00Z"),
				event("merged", "2026-01-02T03:00:00Z"),
			},
		},
		{
			first, "a merged event at the same time", "merged", []string{
				event("merged", "2026-01-02T01:00:00Z"), event(closedEvent, "2026-01-02T01:00:00Z"),
			},
		},
		{time.Time{}, "no closed event", "", []string{event("merged", "2026-01-02T01:00:00Z")}},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			fake := newRig(t, func(resp http.ResponseWriter, _ *http.Request, _ string) {
				write(t, resp, "["+strings.Join(test.events, ",")+"]")
			})

			got, err := fake.lister.FirstEnd(t.Context(), "o/r", 7, budget.Incremental)
			if err != nil || !got.At.Equal(test.at) || got.State != test.state || got.Pages != 1 ||
				!slices.Equal(fake.requests, []string{eventsPath}) {
				t.Errorf("got = %+v, err = %v, requests = %v", got, err, fake.requests)
			}
		})
	}
}

func TestFirstEndPastThreePagesIsAmbiguous(t *testing.T) {
	t.Parallel()

	fake := newRig(t, func(resp http.ResponseWriter, req *http.Request, host string) {
		resp.Header().Set("Link", "<"+host+req.URL.Path+"?per_page=100&page=2>; rel=\"next\"")
		write(t, resp, "["+event(closedEvent, "2026-01-02T01:00:00Z")+"]")
	})

	got, err := fake.lister.FirstEnd(t.Context(), "o/r", 7, budget.Incremental)
	if err != nil || !got.At.IsZero() || got.State != "" || got.Pages != reconcile.MaxEventPages ||
		len(fake.requests) != reconcile.MaxEventPages {
		t.Errorf("got = %+v, err = %v, requests = %v", got, err, fake.requests)
	}
}

func TestFirstEndClearsTags(t *testing.T) {
	t.Parallel()

	fake := newRig(t, tagged(t, `"v1"`, "["+event(closedEvent, "2026-01-02T01:00:00Z")+"]"))

	for range 2 {
		got, err := fake.lister.FirstEnd(t.Context(), "o/r", 7, budget.Incremental)
		if err != nil || got.At.IsZero() || got.Pages != 1 {
			t.Fatalf("got = %+v, err = %v", got, err)
		}
	}

	if want := []string{
		eventsPath,
		eventsPath,
	}; !slices.Equal(fake.requests, want) ||
		fake.tags[eventsPath] != "" {
		t.Errorf(
			"requests = %v, last sent tag = %q, want two requests and no tag",
			fake.requests,
			fake.tags[eventsPath],
		)
	}
}

func TestFirstEndKeepsBasePath(t *testing.T) {
	t.Parallel()

	fake := newRigAt(t, "/api/v3/", func(resp http.ResponseWriter, _ *http.Request, _ string) {
		write(t, resp, "["+event(closedEvent, "2026-01-02T01:00:00Z")+"]")
	})

	got, err := fake.lister.FirstEnd(t.Context(), "o/r", 7, budget.Incremental)
	if err != nil || got.At.IsZero() ||
		!slices.Equal(fake.requests, []string{"/api/v3" + eventsPath}) {
		t.Errorf("got = %+v, err = %v, requests = %v", got, err, fake.requests)
	}
}

func TestFirstEndReadFailure(t *testing.T) {
	t.Parallel()

	const secondPage = eventsPath + "&page=2"

	for _, test := range []struct {
		name        string
		pages       int
		interrupted bool
	}{
		{name: "decode", pages: 2, interrupted: false},
		{name: "transport", pages: 1, interrupted: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			fake := newRig(t, func(resp http.ResponseWriter, req *http.Request, host string) {
				if req.URL.RequestURI() == eventsPath {
					resp.Header().Set("Link", "<"+host+secondPage+`>; rel="next"`)
					write(t, resp, "["+event(closedEvent, "2026-01-02T01:00:00Z")+"]")

					return
				}

				if test.interrupted {
					resp.Header().Set("Content-Length", "10")
				}

				write(t, resp, "[")
			})

			got, err := fake.lister.FirstEnd(t.Context(), "o/r", 7, budget.Incremental)
			_, decodeErr := errors.AsType[*json.SyntaxError](err)

			interrupted := errors.Is(err, io.ErrUnexpectedEOF)
			if decodeErr == test.interrupted || interrupted != test.interrupted ||
				!got.At.IsZero() ||
				got.State != "" ||
				got.Pages != test.pages ||
				!slices.Equal(fake.requests, []string{eventsPath, secondPage}) {
				t.Errorf("got = %+v, err = %v, requests = %v", got, err, fake.requests)
			}
		})
	}
}
