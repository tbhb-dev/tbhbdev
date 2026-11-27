// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
)

type fakeTokens struct{}

func (fakeTokens) Token(context.Context) (string, error) { return "test-token", nil }
func (fakeTokens) Renew(context.Context) (string, error) { return "test-token", nil }

const (
	firstPage = "/repos/o/r/pulls?state=all&sort=updated&direction=desc&per_page=100"
	repoPage  = "/installation/repositories?per_page=100"
)

// rig is a lister on a real client, and what its fake server received.
type rig struct {
	lister   *reconcile.Lister
	etags    *ghclient.ETags
	tags     map[string]string
	base     string
	requests []string
	mu       sync.Mutex
}

// newRig serves the handler over TLS.
func newRig(t *testing.T, handler func(http.ResponseWriter, *http.Request, string)) *rig {
	t.Helper()

	return newRigAt(t, "", handler)
}

// newRigAt serves the handler with the client's base URL ending in prefix.
func newRigAt(
	t *testing.T,
	prefix string,
	handler func(http.ResponseWriter, *http.Request, string),
) *rig {
	t.Helper()

	fake := &rig{
		lister:   nil,
		etags:    ghclient.NewETags(),
		tags:     map[string]string{},
		base:     "",
		requests: nil,
		mu:       sync.Mutex{},
	}
	server := httptest.NewTLSServer(
		http.HandlerFunc(func(resp http.ResponseWriter, req *http.Request) {
			fake.mu.Lock()
			fake.requests = append(fake.requests, req.URL.RequestURI())
			fake.tags[req.URL.RequestURI()] = req.Header.Get("If-None-Match")
			fake.mu.Unlock()
			handler(resp, req, "https://"+req.Host)
		}),
	)
	t.Cleanup(server.Close)

	client, err := ghclient.New(
		server.URL+prefix,
		server.Client(),
		budget.New(),
		fakeTokens{},
		fake.etags,
	)
	if err != nil {
		t.Fatal(err)
	}

	base, err := url.Parse(server.URL + prefix)
	if err != nil {
		t.Fatal(err)
	}

	fake.base = server.URL
	fake.lister = reconcile.NewLister(client, fake.etags, base)

	return fake
}

func write(t *testing.T, resp http.ResponseWriter, body string) {
	t.Helper()

	_, err := io.WriteString(resp, body)
	if err != nil {
		t.Error(err)
	}
}

func item(node, updated, closed, merged string) string {
	quote := func(value string) string {
		if value == "" {
			return "null"
		}

		return `"` + value + `"`
	}

	return fmt.Sprintf(
		`{"node_id":%q,"body":"b","created_at":"2026-01-01T00:00:00Z","updated_at":%s,`+
			`"closed_at":%s,"merged_at":%s}`,
		node,
		quote(updated),
		quote(closed),
		quote(merged),
	)
}

func opts(maxPages int) reconcile.WalkOptions {
	stop := time.Date(2026, time.January, 2, 0, 0, 0, 0, time.UTC)

	return reconcile.WalkOptions{Stop: stop, MaxPages: maxPages, Priority: budget.Incremental}
}

func TestUpdatedStopsAtStopTime(t *testing.T) {
	t.Parallel()

	fake := newRig(t, func(resp http.ResponseWriter, req *http.Request, host string) {
		resp.Header().Set("Link", "<"+host+req.URL.RequestURI()+`&page=2>; rel="next"`)
		write(t, resp, "["+item("open", "2026-01-02T04:00:00Z", "", "")+","+
			item("N3", "2026-01-02T03:00:00Z", "2026-01-02T03:00:00Z", "")+","+
			item("N2", "2026-01-02T02:00:00Z", "2026-01-02T01:00:00Z", "2026-01-02T01:00:00Z")+","+
			item("N1", "2026-01-01T23:59:59Z", "2026-01-01T23:00:00Z", "")+"]")
	})

	got, err := fake.lister.Updated(t.Context(), "o/r", opts(50))
	if err != nil || got.Truncated || got.Unchanged || len(fake.requests) != 1 {
		t.Fatalf("got = %+v, err = %v, requests = %v", got, err, fake.requests)
	}

	// The open item has no close time, so it is skipped, but it moves Newest.
	newest := time.Date(2026, time.January, 2, 4, 0, 0, 0, time.UTC)
	if !got.Newest.Equal(newest) {
		t.Errorf("newest = %v, want %v", got.Newest, newest)
	}

	checkStoppedItems(t, got.Items)
}

func checkStoppedItems(t *testing.T, items []reconcile.ClosedPR) {
	t.Helper()

	if len(items) != 2 || items[0].NodeID != "N3" || items[1].NodeID != "N2" ||
		!items[1].Merged || items[0].Merged || items[1].Repository != "o/r" {
		t.Errorf("stopped items = %+v", items)
	}
}

func TestUpdatedUncleanWalkClearsTag(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name      string
		maxPages  int32
		failPage  int32
		status    int
		pages     int
		truncated bool
	}{
		{name: "truncated at the cap", maxPages: 3, failPage: 0, status: 0, pages: 3, truncated: true},
		{
			name: "error mid-walk", maxPages: 50, failPage: 2,
			status: http.StatusInternalServerError, pages: 1, truncated: false,
		},
		{name: "decode failure", maxPages: 50, failPage: 2, status: http.StatusOK, pages: 2, truncated: false},
		{name: "budget gave up", maxPages: 50, failPage: 2, status: http.StatusForbidden, pages: 1, truncated: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var served atomic.Int32

			fake := newRig(t, func(resp http.ResponseWriter, _ *http.Request, host string) {
				page := served.Add(1)
				if page == test.failPage {
					failPullPage(t, resp, test.status)

					return
				}

				resp.Header().Set("ETag", `"tag"`)
				resp.Header().
					Set("Link", "<"+host+firstPage+"&page="+strconv.Itoa(int(page)+1)+`>; rel="next"`)
				write(
					t,
					resp,
					"["+item("N", "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z", "")+"]",
				)
			})

			got, err := fake.lister.Updated(t.Context(), "o/r", opts(int(test.maxPages)))
			if (err != nil) == test.truncated || got.Truncated != test.truncated ||
				!got.Newest.IsZero() ||
				got.Pages != test.pages ||
				(test.status == http.StatusForbidden && !errors.Is(err, ghclient.ErrGaveUp)) {
				t.Fatalf("got = %+v, err = %v", got, err)
			}

			if tag := fake.etags.Get(fake.base + firstPage); tag != "" {
				t.Errorf("first-page tag = %q, want cleared", tag)
			}
		})
	}
}

func failPullPage(t *testing.T, resp http.ResponseWriter, status int) {
	t.Helper()

	if status == http.StatusOK {
		write(t, resp, "[")

		return
	}

	if status == http.StatusForbidden {
		resp.Header().Set("X-Ratelimit-Resource", "search")
		resp.Header().Set("X-Ratelimit-Remaining", "0")
	}

	resp.WriteHeader(status)
}

func TestUpdatedFirstPageTagOnly(t *testing.T) {
	t.Parallel()

	var same atomic.Bool

	fake := taggedWalk(t, &same)

	for range 2 {
		_, err := fake.lister.Updated(t.Context(), "o/r", opts(50))
		if err != nil {
			t.Fatal(err)
		}
	}

	if fake.tags[firstPage] != `"first"` || fake.tags[firstPage+"&page=2"] != "" {
		t.Errorf("tags on the second walk = %v, want the first page's only", fake.tags)
	}

	same.Store(true)

	got, err := fake.lister.Updated(t.Context(), "o/r", opts(50))
	if err != nil || !got.Unchanged || len(got.Items) != 0 || !got.Newest.IsZero() {
		t.Fatalf("got = %+v, err = %v", got, err)
	}
}

func taggedWalk(t *testing.T, same *atomic.Bool) *rig {
	t.Helper()

	return newRig(t, func(resp http.ResponseWriter, req *http.Request, host string) {
		if same.Load() && req.Header.Get("If-None-Match") == `"first"` {
			resp.WriteHeader(http.StatusNotModified)

			return
		}

		resp.Header().Set("ETag", `"first"`)

		if req.URL.Query().Get("page") == "" {
			resp.Header().Set("Link", "<"+host+firstPage+`&page=2>; rel="next"`)
		}

		write(t, resp, "["+item("N", "2026-01-03T00:00:00Z", "2026-01-03T00:00:00Z", "")+"]")
	})
}

func TestRepositoriesSinglePageUsesTag(t *testing.T) {
	t.Parallel()

	fake := newRig(
		t,
		tagged(
			t,
			`"list"`,
			`{"repositories":[{"id":7,"full_name":"o/r"},{"id":8,"full_name":"o/s"}]}`,
		),
	)

	for range 2 {
		repos, truncated, err := fake.lister.Repositories(t.Context())
		if err != nil || truncated || len(repos) != 2 || repos[0].FullName != "o/r" ||
			repos[0].ID != 7 {
			t.Fatalf("repos = %+v, truncated = %v, err = %v", repos, truncated, err)
		}

		// A caller editing its copy must not change the cached list.
		repos[0] = repos[1]
	}

	if len(fake.requests) != 2 || fake.tags[repoPage] != `"list"` {
		t.Errorf("requests = %v, last tag = %q", fake.requests, fake.tags[repoPage])
	}
}

func TestRepositoriesTwoPagesSendNoTag(t *testing.T) {
	t.Parallel()

	fake := newRig(t, func(resp http.ResponseWriter, req *http.Request, host string) {
		resp.Header().Set("ETag", `"`+req.URL.RequestURI()+`"`)

		if req.URL.Query().Get("page") == "" {
			resp.Header().Set("Link", "<"+host+repoPage+`&page=2>; rel="next"`)
			write(t, resp, `{"repositories":[{"id":1,"full_name":"o/a"}]}`)

			return
		}

		write(t, resp, `{"repositories":[{"id":2,"full_name":"o/b"}]}`)
	})

	for range 2 {
		repos, truncated, err := fake.lister.Repositories(t.Context())
		if err != nil || truncated || len(repos) != 2 {
			t.Fatalf("repos = %+v, truncated = %v, err = %v", repos, truncated, err)
		}
	}

	if len(fake.requests) != 4 {
		t.Fatalf("requests = %v, want both pages twice", fake.requests)
	}

	if fake.tags[repoPage] != "" || fake.tags[repoPage+"&page=2"] != "" {
		t.Errorf("tags = %v, want none", fake.tags)
	}
}

func TestUpdatedWiderWindowReadsPastTag(t *testing.T) {
	t.Parallel()

	fake := newRig(t, func(resp http.ResponseWriter, req *http.Request, _ string) {
		if req.Header.Get("If-None-Match") == `"page"` {
			resp.WriteHeader(http.StatusNotModified)

			return
		}

		resp.Header().Set("ETag", `"page"`)
		write(t, resp, "["+item("N2", "2026-01-02T03:00:00Z", "2026-01-02T03:00:00Z", "")+","+
			item("N1", "2026-01-01T12:00:00Z", "2026-01-01T12:00:00Z", "")+"]")
	})

	wide := opts(50)
	wide.Stop = wide.Stop.Add(-24 * time.Hour)

	for want, window := range []reconcile.WalkOptions{opts(50), wide} {
		got, err := fake.lister.Updated(t.Context(), "o/r", window)
		if err != nil || got.Unchanged || len(got.Items) != want+1 {
			t.Fatalf("walk %d = %+v, err = %v", want, got, err)
		}
	}
}

func TestListerKeepsBasePath(t *testing.T) {
	t.Parallel()

	fake := newRigAt(t, "/api/v3/", func(resp http.ResponseWriter, req *http.Request, _ string) {
		if req.URL.Path == "/api/v3/installation/repositories" {
			write(t, resp, `{"repositories":[{"id":1,"full_name":"o/r"}]}`)

			return
		}

		write(t, resp, "[]")
	})

	_, _, err := fake.lister.Repositories(t.Context())
	if err == nil {
		_, err = fake.lister.Updated(t.Context(), "o/r", opts(50))
	}

	if want := []string{
		"/api/v3/installation/repositories?per_page=100",
		"/api/v3/repos/o/r/pulls?" +
			"state=all&sort=updated&direction=desc&per_page=100",
	}; err != nil ||
		!slices.Equal(fake.requests, want) {
		t.Errorf("requests = %v, err = %v", fake.requests, err)
	}
}

// tagged serves the body with the tag, and a 304 for a request that sends the tag.
func tagged(t *testing.T, tag, body string) func(http.ResponseWriter, *http.Request, string) {
	t.Helper()

	return func(resp http.ResponseWriter, req *http.Request, _ string) {
		if req.Header.Get("If-None-Match") == tag {
			resp.WriteHeader(http.StatusNotModified)

			return
		}

		resp.Header().Set("ETag", tag)
		write(t, resp, body)
	}
}

func TestClearUpdatedDropsFirstPageTag(t *testing.T) {
	t.Parallel()

	fake := newRig(t, tagged(t, `"v1"`, "[]"))
	walk := func() (reconcile.Walked, error) { return fake.lister.Updated(t.Context(), "o/r", opts(50)) }

	_, err := walk()
	if err == nil {
		_, err = walk()
	}

	if err != nil || fake.tags[firstPage] != `"v1"` {
		t.Fatalf(
			"err = %v, second walk sent tag %q, want the stored one",
			err,
			fake.tags[firstPage],
		)
	}

	fake.lister.ClearUpdated("o/r")

	got, err := walk()
	if err != nil || got.Unchanged || fake.tags[firstPage] != "" {
		t.Errorf(
			"got = %+v, err = %v, sent tag = %q, want a full read",
			got,
			err,
			fake.tags[firstPage],
		)
	}
}
