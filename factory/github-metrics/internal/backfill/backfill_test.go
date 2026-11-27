// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package backfill_test

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/backfill"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
)

type fakeTokens struct{}

func (fakeTokens) Token(context.Context) (string, error) { return "test-token", nil }
func (fakeTokens) Renew(context.Context) (string, error) { return "test-token", nil }

const firstPage = "/repos/o/r/pulls?state=all&sort=created&direction=asc&per_page=100"

func newClient(t *testing.T, handler http.HandlerFunc) (*ghclient.Client, *httptest.Server) {
	t.Helper()

	server := httptest.NewTLSServer(handler)
	t.Cleanup(server.Close)

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

	return client, server
}

func write(t *testing.T, resp http.ResponseWriter, body string) {
	t.Helper()

	_, err := resp.Write([]byte(body))
	if err != nil {
		t.Error(err)
	}
}

func TestListThreePages(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32

	client, _ := newClient(t, func(resp http.ResponseWriter, req *http.Request) {
		page := calls.Add(1)
		want := firstPage

		if page > 1 {
			want += "&page=" + strconv.Itoa(int(page))
		}

		if req.URL.RequestURI() != want {
			t.Errorf("request = %q, want %q", req.URL.RequestURI(), want)
		}

		if page < 3 {
			resp.Header().Set("Link", "<https://"+req.Host+firstPage+"&page="+
				strconv.Itoa(int(page+1))+`>; rel="next"`)
		}

		write(t, resp, fmt.Sprintf(`[{"number":%d},{"number":%d}]`, page*2, page*2+1))
	})

	result, err := backfill.List(t.Context(), client, "o/r", backfill.Options{MaxRequests: 3})
	if err != nil || result.Truncated || result.ResumeURL != "" || calls.Load() != 3 {
		t.Fatalf("result = %+v, err = %v, calls = %d", result, err, calls.Load())
	}

	numbers := make([]int, 0, len(result.Records))
	for _, record := range result.Records {
		numbers = append(numbers, record.Number)
	}

	if !reflect.DeepEqual(numbers, []int{2, 3, 4, 5, 6, 7}) {
		t.Errorf("numbers = %v", numbers)
	}
}

func TestListCapAndEmpty(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name      string
		body      string
		count     int
		truncated bool
	}{
		{name: "cap", body: `[{"number":1}]`, count: 1, truncated: true},
		{name: "empty", body: "[]", count: 0, truncated: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var calls atomic.Int32

			client, server := newClient(t, func(resp http.ResponseWriter, req *http.Request) {
				calls.Add(1)
				resp.Header().Set("Link", "<https://"+req.Host+firstPage+`&page=2>; rel="next"`)
				write(t, resp, test.body)
			})

			result, err := backfill.List(
				t.Context(),
				client,
				"o/r",
				backfill.Options{MaxRequests: 1},
			)
			wantResume := ""

			if test.truncated {
				wantResume = server.URL + firstPage + "&page=2"
			}

			if err != nil || result.Truncated != test.truncated || result.ResumeURL != wantResume ||
				len(result.Records) != test.count || calls.Load() != 1 {
				t.Fatalf("result = %+v, err = %v, calls = %d", result, err, calls.Load())
			}
		})
	}
}

func TestListNotModified(t *testing.T) {
	t.Parallel()

	const etag = `"page-one"`

	client, _ := newClient(t, func(resp http.ResponseWriter, req *http.Request) {
		if req.Header.Get("If-None-Match") == etag {
			resp.WriteHeader(http.StatusNotModified)

			return
		}

		resp.Header().Set("ETag", etag)
		write(t, resp, `[{"number":1}]`)
	})

	first, err := backfill.List(t.Context(), client, "o/r", backfill.Options{MaxRequests: 1})
	if err != nil || len(first.Records) != 1 {
		t.Fatalf("first = %+v, err = %v", first, err)
	}

	result, err := backfill.List(t.Context(), client, "o/r", backfill.Options{MaxRequests: 1})
	if err != nil || len(result.Records) != 0 || result.Truncated || result.ResumeURL != "" {
		t.Fatalf("result = %+v, err = %v", result, err)
	}
}

func TestListZeroCap(t *testing.T) {
	t.Parallel()

	_, err := backfill.List(t.Context(), nil, "o/r", backfill.Options{MaxRequests: 0})
	if !errors.Is(err, backfill.ErrMaxRequests) {
		t.Fatalf("err = %v", err)
	}
}

func TestListFields(t *testing.T) {
	t.Parallel()

	client, _ := newClient(t, func(resp http.ResponseWriter, _ *http.Request) {
		write(t, resp, `[{"number":7,"state":"closed","draft":true,
			"created_at":"2026-01-01T00:00:00Z","merged_at":"2026-01-02T00:00:00Z",
			"closed_at":"2026-01-03T00:00:00Z","head":{"sha":"abc","ref":{}},
			"base":{"ref":"main","label":[]},"title":{},"body":[],"user":{"login":{}}},
			{"number":8,"merged_at":null,"closed_at":null}]`)
	})

	result, err := backfill.List(t.Context(), client, "o/r", backfill.Options{MaxRequests: 1})
	if err != nil || len(result.Records) != 2 {
		t.Fatalf("result = %+v, err = %v", result, err)
	}

	want := backfill.Record{
		CreatedAt: time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC),
		MergedAt:  time.Date(2026, time.January, 2, 0, 0, 0, 0, time.UTC),
		ClosedAt:  time.Date(2026, time.January, 3, 0, 0, 0, 0, time.UTC),
		State:     "closed", HeadSHA: "abc", BaseRef: "main", Number: 7, Draft: true,
	}
	if !reflect.DeepEqual(result.Records[0], want) || !result.Records[1].MergedAt.IsZero() ||
		!result.Records[1].ClosedAt.IsZero() {
		t.Errorf("records = %+v", result.Records)
	}
}

func TestListWrapsClientError(t *testing.T) {
	t.Parallel()

	client, _ := newClient(t, func(resp http.ResponseWriter, _ *http.Request) {
		resp.WriteHeader(http.StatusNotFound)
	})

	_, err := backfill.List(t.Context(), client, "o/r", backfill.Options{MaxRequests: 1})

	var status *ghclient.StatusError
	if !errors.As(err, &status) || status.Status != http.StatusNotFound {
		t.Fatalf("err = %v", err)
	}
}
