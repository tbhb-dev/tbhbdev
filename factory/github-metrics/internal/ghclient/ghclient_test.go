// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package ghclient_test

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
)

var errNoKey = errors.New("no key")

// fakeTokens hands out numbered fake tokens and counts renewals.
type fakeTokens struct {
	renewals atomic.Int32
}

func newTokens() *fakeTokens {
	return &fakeTokens{renewals: atomic.Int32{}}
}

func (f *fakeTokens) Token(context.Context) (string, error) {
	return "fake-" + strconv.Itoa(int(f.renewals.Load())), nil
}

func (f *fakeTokens) Renew(context.Context) (string, error) {
	return "fake-" + strconv.Itoa(int(f.renewals.Add(1))), nil
}

type failingTokens struct{}

func (failingTokens) Token(context.Context) (string, error) { return "", errNoKey }
func (failingTokens) Renew(context.Context) (string, error) { return "", errNoKey }

func newClient(
	t *testing.T,
	handler http.HandlerFunc,
	tokens ghclient.TokenSource,
) (*ghclient.Client, *httptest.Server) {
	t.Helper()

	srv := httptest.NewTLSServer(handler)
	t.Cleanup(srv.Close)

	client, err := ghclient.New(srv.URL, srv.Client(), budget.New(), tokens, ghclient.NewETags())
	if err != nil {
		t.Fatal(err)
	}

	return client, srv
}

func write(t *testing.T, w http.ResponseWriter, body string) {
	t.Helper()

	_, err := w.Write([]byte(body))
	if err != nil {
		t.Error(err)
	}
}

const (
	etagValue        = `"abc"`
	retryAfterHeader = "Retry-After"
)

func TestGetSendsHeaders(t *testing.T) {
	t.Parallel()

	client, _ := newClient(t, func(resp http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") != "Bearer fake-0" {
			t.Error("authorization header mismatch")
		}

		if req.Header.Get("X-Github-Api-Version") == "" || req.Header.Get("Accept") == "" ||
			req.Header.Get("User-Agent") != "github-metrics" {
			t.Error("missing version, accept or user agent header")
		}

		write(t, resp, "[]")
	}, newTokens())

	res, err := client.Get(t.Context(), budget.Webhook, "/repos/o/r/pulls")
	if err != nil || res.NotModified() || string(res.Body) != "[]" {
		t.Fatalf("res = %+v, %v", res, err)
	}
}

func TestConditionalRequest(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32

	client, _ := newClient(t, func(resp http.ResponseWriter, req *http.Request) {
		if calls.Add(1) == 1 {
			if req.Header.Get("If-None-Match") != "" {
				t.Error("first request sent If-None-Match")
			}

			resp.Header().Set("ETag", etagValue)
			write(t, resp, "[]")

			return
		}

		if req.Header.Get("If-None-Match") != etagValue {
			t.Errorf("If-None-Match = %q", req.Header.Get("If-None-Match"))
		}

		resp.WriteHeader(http.StatusNotModified)
	}, newTokens())

	first, err := client.Get(t.Context(), budget.Webhook, "/p")
	if err != nil || first.ETag != etagValue {
		t.Fatalf("first = %+v, %v", first, err)
	}

	second, err := client.Get(t.Context(), budget.Webhook, "/p")
	if err != nil || !second.NotModified() || second.ETag != etagValue || len(second.Body) != 0 {
		t.Fatalf("second = %+v, %v", second, err)
	}
}

func TestGetFollowsNextLink(t *testing.T) {
	t.Parallel()

	var base string

	client, srv := newClient(t, func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Query().Get("page") == "" {
			w.Header().
				Set("Link", `<`+base+`/x?page=2>; rel="next", <`+base+`/x?page=9>; rel="last"`)
		}
	}, newTokens())
	base = srv.URL

	res, err := client.Get(t.Context(), budget.Webhook, "/x")
	if err != nil || res.Next != srv.URL+"/x?page=2" {
		t.Fatalf("next = %+v, %v", res, err)
	}

	last, err := client.Get(t.Context(), budget.Webhook, res.Next)
	if err != nil || last.Next != "" {
		t.Fatalf("last = %+v, %v", last, err)
	}
}

func TestGetRefusesOtherHost(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32

	client, _ := newClient(
		t,
		func(http.ResponseWriter, *http.Request) { calls.Add(1) },
		newTokens(),
	)

	_, err := client.Get(t.Context(), budget.Webhook, "http://127.0.0.1:1/steal")
	if !errors.Is(err, ghclient.ErrOutsideBase) || calls.Load() != 0 {
		t.Fatalf("err = %v, calls = %d", err, calls.Load())
	}
}

func TestUnauthorizedRenewsOnce(t *testing.T) {
	t.Parallel()

	tokens := newTokens()
	client, _ := newClient(t, func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get("Authorization") == "Bearer fake-0" {
			w.WriteHeader(http.StatusUnauthorized)
		}
	}, tokens)

	_, err := client.Get(t.Context(), budget.Webhook, "/x")
	if err != nil || tokens.renewals.Load() != 1 {
		t.Fatalf("err = %v, renewals = %d", err, tokens.renewals.Load())
	}
}

func TestSecondUnauthorizedStops(t *testing.T) {
	t.Parallel()

	tokens := newTokens()
	client, _ := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}, tokens)

	_, err := client.Get(t.Context(), budget.Webhook, "/x")

	var status *ghclient.StatusError
	if !errors.As(err, &status) || status.Status != http.StatusUnauthorized ||
		tokens.renewals.Load() != 1 {
		t.Fatalf("err = %v, renewals = %d", err, tokens.renewals.Load())
	}
}

func TestOtherStatusFails(t *testing.T) {
	t.Parallel()

	client := statusClient(t, http.StatusNotFound)

	var status *ghclient.StatusError

	_, err := client.Get(t.Context(), budget.Webhook, "/x")
	if !errors.As(err, &status) || status.Status != http.StatusNotFound {
		t.Fatalf("err = %v", err)
	}
}

func TestTokenErrorStops(t *testing.T) {
	t.Parallel()

	client, _ := newClient(t, func(http.ResponseWriter, *http.Request) {
		t.Error("request sent without a token")
	}, failingTokens{})

	_, err := client.Get(t.Context(), budget.Webhook, "/x")
	if err == nil {
		t.Fatal("want an error")
	}
}

// roundTrip adapts a function to http.RoundTripper. The fake-clock tests use
// it because sockets aren't durably blocking inside a synctest bubble.
type roundTrip func(*http.Request) *http.Response

func (f roundTrip) RoundTrip(req *http.Request) (*http.Response, error) { return f(req), nil }

func reply(status int, header http.Header) *http.Response {
	rec := httptest.NewRecorder()
	rec.WriteHeader(status)

	resp := rec.Result()
	resp.Header = header
	resp.Body = http.NoBody

	return resp
}

func fakeClient(t *testing.T, transport roundTrip) *ghclient.Client {
	t.Helper()

	client, err := ghclient.New(
		"https://api.test",
		&http.Client{Transport: transport, CheckRedirect: nil, Jar: nil, Timeout: 0},
		budget.New(),
		newTokens(),
		ghclient.NewETags(),
	)
	if err != nil {
		t.Fatal(err)
	}

	return client
}

func TestSecondaryLimitBacksOffThenRetries(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		client := fakeClient(t, func(*http.Request) *http.Response {
			if calls.Add(1) == 1 {
				return reply(http.StatusForbidden, http.Header{retryAfterHeader: {"120"}})
			}

			return reply(http.StatusOK, http.Header{})
		})

		start := time.Now()

		_, err := client.Get(ctx, budget.Webhook, "/x")
		if err != nil || calls.Load() != 2 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}

		if waited := time.Since(start); waited < 120*time.Second {
			t.Fatalf("waited %v, want at least the retry-after", waited)
		}
	})
}

func TestSecondaryLimitGivesUp(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		client := fakeClient(t, func(*http.Request) *http.Response {
			calls.Add(1)

			return reply(http.StatusTooManyRequests, http.Header{})
		})

		_, err := client.Get(ctx, budget.Webhook, "/x")
		if !errors.Is(err, ghclient.ErrGaveUp) || calls.Load() != 4 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}
	})
}

func TestLowRemainingStopsUntilReset(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Now().Add(30 * time.Minute)

		client := fakeClient(t, func(*http.Request) *http.Response {
			header := http.Header{}
			if calls.Add(1) == 1 {
				header.Set("X-Ratelimit-Remaining", "10")
				header.Set("X-Ratelimit-Reset", strconv.FormatInt(reset.Unix(), 10))
			}

			return reply(http.StatusOK, header)
		})

		for range 2 {
			_, err := client.Get(ctx, budget.Webhook, "/x")
			if err != nil {
				t.Fatal(err)
			}
		}

		if time.Now().Before(reset) {
			t.Fatalf("second request ran at %v, before the reset %v", time.Now(), reset)
		}
	})
}

// waitAfterLimit sends one request whose first answer is a 429 with the given
// `retry-after` value, and returns how long the call took on the fake clock.
func waitAfterLimit(ctx context.Context, t *testing.T, value string) (time.Duration, error) {
	t.Helper()

	var calls atomic.Int32

	client := fakeClient(t, func(*http.Request) *http.Response {
		if calls.Add(1) == 1 {
			return reply(http.StatusTooManyRequests, http.Header{retryAfterHeader: {value}})
		}

		return reply(http.StatusOK, http.Header{})
	})

	start := time.Now()
	_, err := client.Get(ctx, budget.Webhook, "/x")

	return time.Since(start), err
}

func TestRetryAfterIsClamped(t *testing.T) {
	t.Parallel()

	for _, value := range []string{"864000", "9223372037", "18446744074"} {
		synctestRun(t, func(ctx context.Context) {
			waited, err := waitAfterLimit(ctx, t, value)
			if err != nil || waited < time.Minute || waited > 16*time.Minute {
				t.Fatalf("retry-after %s: err = %v, waited %v", value, err, waited)
			}
		})
	}
}

func TestRetryAfterHTTPDate(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		date := time.Now().Add(5 * time.Minute).UTC().Format(http.TimeFormat)

		waited, err := waitAfterLimit(ctx, t, date)
		if err != nil || waited < 4*time.Minute {
			t.Fatalf("err = %v, waited %v", err, waited)
		}
	})
}

func TestRedirectIsNotFollowed(t *testing.T) {
	t.Parallel()

	var hits atomic.Int32

	other := httptest.NewTLSServer(
		http.HandlerFunc(func(http.ResponseWriter, *http.Request) { hits.Add(1) }),
	)
	t.Cleanup(other.Close)

	client, _ := newClient(t, func(w http.ResponseWriter, req *http.Request) {
		http.Redirect(w, req, other.URL, http.StatusFound)
	}, newTokens())

	_, err := client.Get(t.Context(), budget.Webhook, "/x")

	var status *ghclient.StatusError
	if !errors.As(err, &status) || status.Status != http.StatusFound || hits.Load() != 0 {
		t.Fatalf("err = %v, hits = %d", err, hits.Load())
	}
}

func TestOversizedBodyIsRefused(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32

	client, _ := newClient(t, func(w http.ResponseWriter, req *http.Request) {
		if calls.Add(1) == 1 {
			w.Header().Set("ETag", etagValue)
			write(t, w, strings.Repeat("x", 16<<20+1))

			return
		}

		if req.Header.Get("If-None-Match") != "" {
			t.Error("an oversized body stored its ETag")
		}
	}, newTokens())

	_, err := client.Get(t.Context(), budget.Webhook, "/x")
	if !errors.Is(err, ghclient.ErrBodyTooLarge) {
		t.Fatalf("err = %v", err)
	}

	_, err = client.Get(t.Context(), budget.Webhook, "/x")
	if err != nil {
		t.Fatal(err)
	}
}

func TestTokenErrorTextIsDropped(t *testing.T) {
	t.Parallel()

	client, _ := newClient(t, func(http.ResponseWriter, *http.Request) {}, leakyTokens{})

	_, err := client.Get(t.Context(), budget.Webhook, "/x")
	if !errors.Is(err, ghclient.ErrTokenSource) ||
		strings.Contains(err.Error(), "leaked-installation-credential") {
		t.Fatalf("err = %v", err)
	}
}

type leakyTokens struct{}

func (leakyTokens) Token(context.Context) (string, error) { return "", errLeak }
func (leakyTokens) Renew(context.Context) (string, error) { return "", errLeak }

var errLeak = errors.New("sign failed for leaked-installation-credential")

func TestNotModifiedWithoutStoredTag(t *testing.T) {
	t.Parallel()

	client := statusClient(t, http.StatusNotModified)

	res, err := client.Get(t.Context(), budget.Webhook, "/x")
	if err != nil || !res.NotModified() || res.ETag != "" {
		t.Fatalf("res = %+v, %v", res, err)
	}
}

func TestInsecureBaseIsRejected(t *testing.T) {
	t.Parallel()

	_, err := ghclient.New(
		"http://api.github.com",
		http.DefaultClient,
		budget.New(),
		newTokens(),
		ghclient.NewETags(),
	)
	if !errors.Is(err, ghclient.ErrInsecureBase) {
		t.Fatalf("err = %v", err)
	}
}

func TestNextLinkWithCommasAndSeveralHeaders(t *testing.T) {
	t.Parallel()

	client, _ := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Add("Link", `<https://x/y?labels=a,b&page=1>; rel="prev"`)
		w.Header().
			Add("Link", `<https://x/y?labels=a,b&page=3>; rel="next", <https://x/y?page=9>; rel="last"`)
	}, newTokens())

	res, err := client.Get(t.Context(), budget.Webhook, "/x")
	if err != nil || res.Next != "https://x/y?labels=a,b&page=3" {
		t.Fatalf("next = %q, %v", res.Next, err)
	}
}

func TestETagsAreBounded(t *testing.T) {
	t.Parallel()

	etags := ghclient.NewETags()
	for i := range 10050 {
		etags.Set("k"+strconv.Itoa(i), "v")
	}

	kept := 0

	for i := range 10050 {
		if etags.Get("k"+strconv.Itoa(i)) != "" {
			kept++
		}
	}

	if kept > 10000 {
		t.Fatalf("kept %d tags", kept)
	}
}

func statusClient(t *testing.T, status int) *ghclient.Client {
	t.Helper()

	client, _ := newClient(
		t,
		func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(status) },
		newTokens(),
	)

	return client
}

func TestPrimaryLimitWaitsForResetThenRetries(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		var calls atomic.Int32

		reset := time.Unix(time.Now().Add(10*time.Minute).Unix(), 0)

		client := fakeClient(t, func(*http.Request) *http.Response {
			if calls.Add(1) == 1 {
				header := http.Header{}
				header.Set("X-Ratelimit-Resource", budget.CoreResource)
				header.Set("X-Ratelimit-Remaining", "0")
				header.Set("X-Ratelimit-Reset", strconv.FormatInt(reset.Unix(), 10))

				return reply(http.StatusForbidden, header)
			}

			return reply(http.StatusOK, http.Header{})
		})

		_, err := client.Get(ctx, budget.Webhook, "/x")
		if err != nil || calls.Load() != 2 {
			t.Fatalf("err = %v, calls = %d", err, calls.Load())
		}

		if time.Now().Before(reset) {
			t.Fatalf("retry ran at %v, before the reset %v", time.Now(), reset)
		}
	})
}

func TestOtherResourceDoesNotStopWork(t *testing.T) {
	t.Parallel()

	synctestRun(t, func(ctx context.Context) {
		reset := time.Now().Add(30 * time.Minute)

		client := fakeClient(t, func(*http.Request) *http.Response {
			header := http.Header{}
			header.Set("X-Ratelimit-Resource", "search")
			header.Set("X-Ratelimit-Remaining", "5")
			header.Set("X-Ratelimit-Reset", strconv.FormatInt(reset.Unix(), 10))

			return reply(http.StatusOK, header)
		})

		for range 2 {
			_, err := client.Get(ctx, budget.Webhook, "/x")
			if err != nil {
				t.Fatal(err)
			}
		}

		if waited := time.Until(reset); waited < 29*time.Minute {
			t.Fatalf("second request waited for another resource's reset, %v left", waited)
		}
	})
}

func TestServerErrorFailsWithoutRetry(t *testing.T) {
	t.Parallel()

	var calls atomic.Int32

	client, _ := newClient(t, func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusBadGateway)
	}, newTokens())

	_, err := client.Get(t.Context(), budget.Webhook, "/x")
	if got := statusOf(err); got != http.StatusBadGateway || calls.Load() != 1 {
		t.Fatalf("status = %d, calls = %d, err = %v", got, calls.Load(), err)
	}
}

// statusOf returns the HTTP status code that err holds, or 0 when err has none.
func statusOf(err error) int {
	if status, ok := errors.AsType[*ghclient.StatusError](err); ok {
		return status.Status
	}

	return 0
}
