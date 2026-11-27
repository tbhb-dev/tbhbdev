// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package ghclient sends read requests to the GitHub REST API. Each request
// waits for a slot in the shared budget. It sends an installation token from a
// TokenSource and the stored `ETag`. The response's rate limit headers go back
// to the budget.
package ghclient

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
)

const (
	// maxAttempts bounds the sends of one Get, counting a send after a token
	// renewal and each retry after a rate limit wait, secondary or primary.
	maxAttempts = 4
	// maxBody caps a response body.
	maxBody = 16 << 20
	// maxRetryAfter clamps a `retry-after` wait, matching the budget's cap.
	maxRetryAfter = 15 * time.Minute
	// maxETags bounds the stored tags.
	maxETags = 10000
	// pastResetWait is the wait for a spent non-core response whose reset is
	// already past. It covers only a few seconds of clock skew or a reset
	// truncated to the second, not larger skew.
	pastResetWait = 3 * time.Second
	// minResetUnix and maxResetUnix bound a plausible `x-ratelimit-reset`: one
	// minute after the epoch, and the start of 2100. A value outside them is
	// invalid, not clamped, because no real reset is that early or that late.
	minResetUnix = 60
	maxResetUnix = 4102444800

	// bodyProbe is how much of a 403 body is searched for a secondary limit.
	bodyProbe = 4096

	apiVersion = "2022-11-28"
	userAgent  = "github-metrics"
)

// ErrGaveUp is wrapped by the error of a Get that used every attempt.
var ErrGaveUp = errors.New("github request gave up")

// ErrBodyTooLarge is wrapped by the error of a Get whose body exceeds 16 MiB.
var ErrBodyTooLarge = errors.New("github response body is too large")

// ErrInsecureBase is returned by New for a base URL that doesn't use TLS.
var ErrInsecureBase = errors.New("github base URL must use https")

// ErrTokenSource is wrapped by the error of a Get whose TokenSource failed.
// The source's own error text isn't included, because it may hold a token.
var ErrTokenSource = errors.New("installation token source failed")

// ErrOutsideBase is wrapped by the error of a Get whose target is on another
// host than the base URL, which would send the token there.
var ErrOutsideBase = errors.New("github request target is outside the base URL")

// TokenSource supplies installation tokens. The client never logs one, and
// never keeps one past a call. The client drops the text of an error from a
// source. The source logs its own failures.
type TokenSource interface {
	// Token returns the current token, renewed when little time remains.
	Token(ctx context.Context) (string, error)
	// Renew discards the current token and returns a new one. The client
	// calls it once after a 401 response.
	Renew(ctx context.Context) (string, error)
}

// StatusError reports a response the client doesn't retry and can't use.
type StatusError struct {
	Path   string
	Status int
}

// Error returns the path and status.
func (e *StatusError) Error() string {
	return fmt.Sprintf("github %s returned %d", e.Path, e.Status)
}

// ETags remembers the last `ETag` seen for each URL.
type ETags struct {
	seen map[string]string
	mu   sync.Mutex
}

// NewETags returns an empty store.
func NewETags() *ETags {
	return &ETags{seen: map[string]string{}, mu: sync.Mutex{}}
}

// Get returns the stored `ETag` for a URL, or an empty string.
func (e *ETags) Get(target string) string {
	e.mu.Lock()
	defer e.mu.Unlock()

	return e.seen[target]
}

// Set stores an `ETag` for a URL.
func (e *ETags) Set(target, etag string) {
	e.mu.Lock()
	defer e.mu.Unlock()

	if _, known := e.seen[target]; !known && len(e.seen) >= maxETags {
		for victim := range e.seen {
			delete(e.seen, victim)

			break
		}
	}

	e.seen[target] = etag
}

// Response is a successful or not-modified answer.
type Response struct {
	// ETag is the response's `ETag`, or the stored one after a 304.
	ETag string
	// Next is the URL of the next page, or empty on the last page.
	Next string
	// Body is empty after a 304.
	Body []byte
	// Status is 200 or 304.
	Status int
}

// NotModified reports whether the stored `ETag` still matched.
func (r *Response) NotModified() bool {
	return r.Status == http.StatusNotModified
}

// Client sends the requests. Make one with New.
type Client struct {
	base   *url.URL
	http   *http.Client
	budget *budget.Limiter
	tokens TokenSource
	etags  *ETags
}

// New returns a client for the API at baseURL.
func New(
	baseURL string,
	httpClient *http.Client,
	lim *budget.Limiter,
	tokens TokenSource,
	etags *ETags,
) (*Client, error) {
	base, err := url.Parse(baseURL)
	if err != nil {
		return nil, fmt.Errorf("parse base URL: %w", err)
	}

	if base.Scheme != "https" {
		return nil, fmt.Errorf("base URL %q: %w", baseURL, ErrInsecureBase)
	}

	// A redirect would skip the budget and could keep the token on another
	// port or a child domain. The client returns the redirect response.
	own := *httpClient
	own.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

	return &Client{base: base, http: &own, budget: lim, tokens: tokens, etags: etags}, nil
}

// Get fetches a path such as `/repos/o/r/pulls?per_page=100`, or the Next
// URL of an earlier response. A new `ETag` is stored on receipt, so a caller
// that can't use a 200 body gets only a 304 on the next call and must clear
// the tag first. A redirect returns a status error. It waits for a budget slot at the given
// priority. A 401 renews the token once. A secondary rate limit response, or
// a 403 or 429 with no primary budget left, backs the budget off and retries, up to
// four sends in all.
func (c *Client) Get(
	ctx context.Context,
	priority budget.Priority,
	target string,
) (*Response, error) {
	full, err := c.resolve(target)
	if err != nil {
		return nil, err
	}

	renew := false
	renewed := false

	for attempt := range maxAttempts {
		token, readyErr := c.ready(ctx, priority, full.Path, renew)
		if readyErr != nil {
			return nil, readyErr
		}

		renewed = renewed || renew
		renew = false

		got, sendErr := c.send(ctx, full, token)
		if sendErr != nil {
			return nil, sendErr
		}

		c.observe(got.header)

		if got.status == http.StatusUnauthorized && !renewed {
			renew = true

			continue
		}

		response, retry, responseErr := c.handleReply(ctx, full, got, attempt == maxAttempts-1)
		if !retry {
			return response, responseErr
		}
	}

	return nil, fmt.Errorf("get %s: %w", full.Path, ErrGaveUp)
}

// handleReply reports whether a response can be retried.
func (c *Client) handleReply(
	ctx context.Context, full *url.URL, got *reply, last bool,
) (*Response, bool, error) {
	switch {
	case got.status == http.StatusOK || got.status == http.StatusNotModified:
		c.budget.Success()

		return c.response(full.String(), got), false, nil
	case isPrimaryLimit(got):
		err := c.primaryBackoff(ctx, got, last)
		if err != nil {
			return nil, false, fmt.Errorf("get %s: %w", full.Path, err)
		}
	case isSecondaryLimit(got):
		c.budget.Backoff(retryAfter(got.header))
	default:
		return nil, false, &StatusError{Path: full.Path, Status: got.status}
	}

	return nil, true, nil
}

// ready waits for a budget slot and returns the token for one send.
func (c *Client) ready(
	ctx context.Context,
	priority budget.Priority,
	path string,
	renew bool,
) (string, error) {
	waitErr := c.budget.Wait(ctx, priority)
	if waitErr != nil {
		return "", fmt.Errorf("get %s: %w", path, waitErr)
	}

	return c.token(ctx, renew)
}

// reply is what one send received.
type reply struct {
	header http.Header
	body   []byte
	status int
}

// resolve turns a path or a Next URL into a URL on the base host.
func (c *Client) resolve(target string) (*url.URL, error) {
	ref, err := url.Parse(target)
	if err != nil {
		return nil, fmt.Errorf("parse target: %w", err)
	}

	full := c.base.ResolveReference(ref)
	if full.Scheme != c.base.Scheme || full.Host != c.base.Host {
		return nil, fmt.Errorf("get %s: %w", full.Host, ErrOutsideBase)
	}

	return full, nil
}

func (c *Client) token(ctx context.Context, renew bool) (string, error) {
	var (
		token string
		err   error
	)

	if renew {
		token, err = c.tokens.Renew(ctx)
	} else {
		token, err = c.tokens.Token(ctx)
	}

	if err != nil {
		return "", ErrTokenSource
	}

	return token, nil
}

func (c *Client) send(ctx context.Context, full *url.URL, token string) (*reply, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, full.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-Github-Api-Version", apiVersion)
	req.Header.Set("User-Agent", userAgent)

	if etag := c.etags.Get(full.String()); etag != "" {
		req.Header.Set("If-None-Match", etag)
	}

	res, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("get %s: %w", full.Path, err)
	}

	body, err := io.ReadAll(io.LimitReader(res.Body, maxBody+1))

	err = errors.Join(err, res.Body.Close())
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", full.Path, err)
	}

	if len(body) > maxBody {
		return nil, fmt.Errorf("get %s: %w", full.Path, ErrBodyTooLarge)
	}

	return &reply{header: res.Header, body: body, status: res.StatusCode}, nil
}

// observe passes the rate limit headers to the budget.
func (c *Client) observe(header http.Header) {
	remaining, err := strconv.Atoi(strings.TrimSpace(header.Get("X-Ratelimit-Remaining")))
	if err != nil {
		return
	}

	reset, ok := resetUnix(header)
	if !ok {
		return
	}

	c.budget.Observe(header.Get("X-Ratelimit-Resource"), remaining, time.Unix(reset, 0))
}

// response builds the result of a 200 or 304 and stores a new `ETag`.
func (c *Client) response(key string, got *reply) *Response {
	etag := got.header.Get("ETag")

	switch {
	case got.status == http.StatusNotModified:
		etag = c.etags.Get(key)
	case etag != "":
		c.etags.Set(key, etag)
	}

	return &Response{ETag: etag, Next: nextLink(got.header), Body: got.body, Status: got.status}
}

// isSecondaryLimit matches a 429, and a 403 that mentions a secondary limit or
// has a `retry-after` header. isPrimaryLimit takes a response first, so a 429
// with zero requests left isn't secondary.
func isSecondaryLimit(got *reply) bool {
	if got.status == http.StatusTooManyRequests {
		return true
	}

	if got.status != http.StatusForbidden {
		return false
	}

	return namesSecondary(got)
}

// isPrimaryLimit matches a 403 or 429 whose `x-ratelimit-remaining` is zero:
// the primary budget of the response's resource is spent. A `retry-after`
// header doesn't change that. The wait covers both.
func isPrimaryLimit(got *reply) bool {
	return (got.status == http.StatusForbidden || got.status == http.StatusTooManyRequests) &&
		strings.TrimSpace(got.header.Get("X-Ratelimit-Remaining")) == "0"
}

// namesSecondary is true when a response has any `retry-after` header or a
// body that mentions a secondary limit. A `retry-after` header alone counts
// as secondary, so such a response pauses the shared limiter too.
func namesSecondary(got *reply) bool {
	probe := strings.ToLower(string(got.body[:min(len(got.body), bodyProbe)]))

	return got.header.Get("Retry-After") != "" || strings.Contains(probe, "secondary rate limit")
}

// primaryBackoff waits out a spent primary budget for the longer of the
// `retry-after` and the reset. The core budget stops the shared limiter for
// that time. Another resource, such as search or GraphQL, pauses only this
// request, so core callers keep running, unless the response is also
// secondary, which pauses the shared limiter too. Any `retry-after` header
// makes a response secondary, as does a body with the secondary-limit text. A
// secondary response with no wait of its own retries, since the shared pause
// is the wait and the next send waits it out in the limiter. It returns
// ErrGaveUp for another resource that isn't secondary and has no usable
// reset, because a retry would fail the same way. A reset that's already
// past counts as usable: the clocks may differ, so it waits pastResetWait and
// retries. On the last attempt it skips the own wait,
// since no send is left.
func (c *Client) primaryBackoff(ctx context.Context, got *reply, last bool) error {
	resource := got.header.Get("X-Ratelimit-Resource")
	wait := max(untilReset(got.header), retryAfter(got.header))

	if resource == "" || resource == budget.CoreResource {
		c.budget.Backoff(wait)

		return nil
	}

	secondary := namesSecondary(got)
	if secondary {
		c.budget.Backoff(retryAfter(got.header))
	}

	if wait <= 0 {
		if secondary {
			return nil
		}

		if !hasReset(got.header) {
			return ErrGaveUp
		}

		wait = pastResetWait
	}

	if last {
		return nil
	}

	timer := time.NewTimer(wait)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return fmt.Errorf("wait for reset: %w", ctx.Err())
	case <-timer.C:
		return nil
	}
}

// resetUnix parses `x-ratelimit-reset` as Unix seconds. It refuses a value
// that's absent, not a plain integer (a leading plus sign too, as in
// the `retry-after` parse), before minResetUnix, or past maxResetUnix.
func resetUnix(header http.Header) (int64, bool) {
	value := strings.TrimSpace(header.Get("X-Ratelimit-Reset"))

	reset, err := strconv.ParseInt(value, 10, 64)
	if err != nil || strings.HasPrefix(value, "+") || reset < minResetUnix || reset > maxResetUnix {
		return 0, false
	}

	return reset, true
}

// hasReset is true when `x-ratelimit-reset` is valid, even if it's past.
func hasReset(header http.Header) bool {
	_, ok := resetUnix(header)

	return ok
}

// untilReset returns the time to the `x-ratelimit-reset` of a response,
// clamped to the budget's reset cap. It's zero when the header is absent,
// invalid or past. A core response then waits on the budget's doubling
// backoff. Another resource waits pastResetWait for a past reset and gives up
// on an invalid one.
func untilReset(header http.Header) time.Duration {
	reset, ok := resetUnix(header)
	if !ok {
		return 0
	}

	return min(max(time.Until(time.Unix(reset, 0)), 0), budget.MaxReset)
}

// retryAfter returns the `retry-after` wait, as seconds or an HTTP date,
// clamped to maxRetryAfter. It's zero when the header is absent or invalid.
func retryAfter(header http.Header) time.Duration {
	value := strings.TrimSpace(header.Get("Retry-After"))

	seconds, err := strconv.ParseInt(value, 10, 64)
	if err == nil && !strings.HasPrefix(value, "+") {
		if seconds <= 0 {
			return 0
		}

		return time.Duration(min(seconds, int64(maxRetryAfter/time.Second))) * time.Second
	}

	date, err := http.ParseTime(value)
	if err != nil {
		return 0
	}

	return min(max(time.Until(date), 0), maxRetryAfter)
}

// nextLink returns the `rel="next"` URL of the Link headers. A URL may hold
// commas. It reads each angle-bracket span before its parameters.
func nextLink(header http.Header) string {
	for _, value := range header.Values("Link") {
		for rest := value; ; {
			open := strings.Index(rest, "<")
			if open < 0 {
				break
			}

			end := strings.Index(rest[open:], ">")
			if end < 0 {
				break
			}

			target := rest[open+1 : open+end]
			rest = rest[open+end+1:]

			params := rest
			if next := strings.Index(rest, "<"); next >= 0 {
				params = rest[:next]
			}

			if strings.Contains(params, `rel="next"`) {
				return target
			}
		}
	}

	return ""
}
