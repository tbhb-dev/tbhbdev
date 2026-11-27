// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package receiver_test

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/receiver"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
)

const (
	sigHeader        = "X-Hub-Signature-256"
	deliveryHeader   = "X-Github-Delivery"
	eventHeader      = "X-Github-Event"
	testSecret       = "test-secret"
	deliveryID       = "d-1"
	pingEvent        = "ping"
	pullRequestEvent = "pull_request"
	openedBody       = `{"action":"opened"}`
	errorLevel       = `"level":"ERROR"`
	loggedDelivery   = `"delivery":"` + deliveryID + `"`
	loggedEvent      = `"event":"` + pullRequestEvent + `"`
)

func sign(secret, body string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(body))

	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// tamper flips one hex digit of the digest in signature, at index pos, where
// a negative pos counts from the end.
func tamper(signature string, pos int) string {
	digits := []byte(strings.TrimPrefix(signature, "sha256="))
	if pos < 0 {
		pos += len(digits)
	}

	if digits[pos] == '0' {
		digits[pos] = '1'
	} else {
		digits[pos] = '0'
	}

	return "sha256=" + string(digits)
}

func openStore(t *testing.T) *store.Bolt {
	t.Helper()

	repo, err := store.Open(filepath.Join(t.TempDir(), "store.db"))
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { checkClose(t, repo) })

	return repo
}

func do(t *testing.T, secret, method, path, body string, header map[string]string) int {
	t.Helper()

	handler := receiver.Handler([]byte(secret), openStore(t), slog.New(slog.DiscardHandler))
	req := httptest.NewRequestWithContext(t.Context(), method, path, strings.NewReader(body))

	for name, value := range header {
		req.Header.Set(name, value)
	}

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	return rec.Code
}

func newDeliveryRequest(ctx context.Context, body io.Reader) *http.Request {
	req := httptest.NewRequestWithContext(ctx, http.MethodPost, "/webhook", body)
	req.Header.Set(sigHeader, sign(testSecret, openedBody))
	req.Header.Set(deliveryHeader, deliveryID)
	req.Header.Set(eventHeader, pullRequestEvent)

	return req
}

func postDelivery(t *testing.T, handler http.Handler) int {
	t.Helper()

	req := newDeliveryRequest(t.Context(), strings.NewReader(openedBody))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	return rec.Code
}

func assertQueuedDelivery(t *testing.T, repo *store.Bolt) {
	t.Helper()

	entries, err := repo.Pending(10)
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}

	if len(entries) != 1 || entries[0].Delivery != deliveryID ||
		entries[0].Event != pullRequestEvent ||
		string(entries[0].Body) != openedBody {
		t.Fatalf("pending = %+v", entries)
	}
}

func assertLogContains(t *testing.T, log string, fields ...string) {
	t.Helper()

	for _, field := range fields {
		if !strings.Contains(log, field) {
			t.Errorf("log = %s, want %s", log, field)
		}
	}
}

func webhookHeaders(signature string) map[string]string {
	return map[string]string{
		sigHeader:      signature,
		deliveryHeader: deliveryID,
		eventHeader:    pullRequestEvent,
	}
}

func TestWebhook(t *testing.T) {
	t.Parallel()

	const body = `{"action":"opened"}`

	badPrefix := map[string]string{
		sigHeader:      "sha1=" + strings.TrimPrefix(sign(testSecret, body), "sha256="),
		deliveryHeader: deliveryID,
	}
	valid := webhookHeaders(sign(testSecret, body))

	tests := []struct {
		header map[string]string
		name   string
		body   string
		want   int
	}{
		{valid, "valid", body, http.StatusAccepted},
		{map[string]string{deliveryHeader: deliveryID}, "no signature", body, http.StatusUnauthorized},
		{webhookHeaders(sign("other", body)), "wrong secret", body, http.StatusUnauthorized},
		{
			webhookHeaders(sign(testSecret, "x")),
			"signature of another body",
			body,
			http.StatusUnauthorized,
		},
		{
			map[string]string{sigHeader: "sha256=zz", deliveryHeader: deliveryID},
			"not hex",
			body,
			http.StatusUnauthorized,
		},
		{badPrefix, "wrong prefix", body, http.StatusUnauthorized},
		{valid, "too large", strings.Repeat("a", receiver.MaxBody+1), http.StatusRequestEntityTooLarge},
	}

	for _, spec := range tests {
		t.Run(spec.name, func(t *testing.T) {
			t.Parallel()

			got := do(t, testSecret, http.MethodPost, "/webhook", spec.body, spec.header)
			if got != spec.want {
				t.Errorf("status = %d, want %d", got, spec.want)
			}
		})
	}
}

func TestWebhookHeaders(t *testing.T) {
	t.Parallel()

	const body = `{"action":"opened"}`

	const guid = "0b7e4f1a-6c1d-11f1-9c3e-2f4a8d1b7e55"

	tests := []struct {
		name     string
		delivery string
		event    string
		want     int
	}{
		{"real GUID", guid, pullRequestEvent, http.StatusAccepted},
		{"event with digits", deliveryID, "projects_v2_status_update", http.StatusAccepted},
		{"delivery id of 64 characters", strings.Repeat("a", 64), pingEvent, http.StatusAccepted},
		{"event of 64 characters", deliveryID, strings.Repeat("a", 64), http.StatusAccepted},
		{"missing delivery id", "", pingEvent, http.StatusBadRequest},
		{"missing event", deliveryID, "", http.StatusBadRequest},
		{"event with a space", deliveryID, "pull request", http.StatusBadRequest},
		{"event over 64 characters", deliveryID, strings.Repeat("a", 65), http.StatusBadRequest},
		{
			"delivery id over 64 characters",
			strings.Repeat("a", 65),
			pingEvent,
			http.StatusBadRequest,
		},
		{"delivery id with a space and text", "d-1 secret=x", pingEvent, http.StatusBadRequest},
		{"delivery id with an underscore", "d_1", pingEvent, http.StatusBadRequest},
		{"delivery id with non-ASCII text", "d-\u00e9", pingEvent, http.StatusBadRequest},
		{"event with uppercase", deliveryID, "Ping", http.StatusBadRequest},
		{"event with non-ASCII text", deliveryID, "pi\u00f1g", http.StatusBadRequest},
	}

	for _, spec := range tests {
		t.Run(spec.name, func(t *testing.T) {
			t.Parallel()

			header := map[string]string{sigHeader: sign(testSecret, body)}
			if spec.delivery != "" {
				header[deliveryHeader] = spec.delivery
			}

			if spec.event != "" {
				header[eventHeader] = spec.event
			}

			got := do(t, testSecret, http.MethodPost, "/webhook", body, header)
			if got != spec.want {
				t.Errorf("status = %d, want %d", got, spec.want)
			}
		})
	}
}

const noDeliveryIDCase = "bad signature and no delivery id"

func TestBadSignature(t *testing.T) {
	t.Parallel()

	const body = `{"action":"opened"}`

	good := sign(testSecret, body)
	tests := map[string]string{
		noDeliveryIDCase:            "", // set below
		"first digest byte differs": tamper(good, 0),
		"last digest byte differs":  tamper(good, -1),
		"digest truncated":          good[:len(good)-2],
		"digest extended":           good + "00",
	}
	tests[noDeliveryIDCase] = sign("other", body)

	for name, signature := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			header := map[string]string{sigHeader: signature, eventHeader: pullRequestEvent}
			if name != noDeliveryIDCase {
				header[deliveryHeader] = deliveryID
			}

			want := http.StatusUnauthorized
			if name == noDeliveryIDCase {
				// Header shape is checked before authentication, so no body is read.
				want = http.StatusBadRequest
			}

			got := do(t, testSecret, http.MethodPost, "/webhook", body, header)
			if got != want {
				t.Errorf("status = %d, want %d", got, want)
			}
		})
	}
}

type failReader struct {
	t *testing.T
}

func (r failReader) Read(_ []byte) (int, error) {
	r.t.Helper()
	r.t.Error("unexpected body read")

	return 0, io.EOF
}

func TestWebhookEarlyRejectReadsNoBody(t *testing.T) {
	t.Parallel()

	validSig := sign(testSecret, openedBody)
	noSignature := webhookHeaders(validSig)
	delete(noSignature, sigHeader)

	badDelivery := webhookHeaders(validSig)
	badDelivery[deliveryHeader] = "not a delivery id!"

	badEvent := webhookHeaders(validSig)
	badEvent[eventHeader] = "pull request!"

	for _, spec := range []struct {
		header        map[string]string
		name, body    string
		contentLength int64
		want          int
	}{
		{noSignature, "missing signature", "", 0, http.StatusUnauthorized},
		{webhookHeaders("sha256=zz"), "malformed signature", "", 0, http.StatusUnauthorized},
		{map[string]string{sigHeader: validSig, deliveryHeader: deliveryID}, "missing event", "", 0, http.StatusBadRequest},
		{
			map[string]string{sigHeader: validSig, eventHeader: pullRequestEvent},
			"missing delivery id",
			"",
			0,
			http.StatusBadRequest,
		},
		{badDelivery, "malformed delivery id", "", 0, http.StatusBadRequest},
		{badEvent, "malformed event", "", 0, http.StatusBadRequest},
		{
			webhookHeaders(validSig), "content length above max body", "", receiver.MaxBody + 1,
			http.StatusRequestEntityTooLarge,
		},
		{webhookHeaders(validSig), "valid request", openedBody, int64(len(openedBody)), http.StatusAccepted},
	} {
		t.Run(spec.name, func(t *testing.T) {
			t.Parallel()

			runEarlyReject(t, spec.header, spec.body, spec.contentLength, spec.want)
		})
	}
}

func runEarlyReject(
	t *testing.T,
	header map[string]string,
	body string,
	contentLength int64,
	want int,
) {
	t.Helper()

	var bodyReader io.Reader = failReader{t: t}
	if body != "" {
		bodyReader = strings.NewReader(body)
	}

	handler := receiver.Handler([]byte(testSecret), openStore(t), slog.New(slog.DiscardHandler))
	req := httptest.NewRequestWithContext(t.Context(), http.MethodPost, "/webhook", bodyReader)
	req.ContentLength = contentLength

	for name, value := range header {
		req.Header.Set(name, value)
	}

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != want {
		t.Errorf("status = %d, want %d", rec.Code, want)
	}
}

func TestEmptySecretRejects(t *testing.T) {
	t.Parallel()

	header := map[string]string{sigHeader: sign("", "x"), deliveryHeader: deliveryID}

	got := do(t, "", http.MethodPost, "/webhook", "x", header)
	if got != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", got)
	}
}

func TestRouting(t *testing.T) {
	t.Parallel()

	tests := []struct {
		method, path string
		want         int
	}{
		{http.MethodGet, "/healthz", http.StatusOK},
		{http.MethodGet, "/webhook", http.StatusNotFound},
		{http.MethodGet, "/", http.StatusNotFound},
		{http.MethodGet, "/other", http.StatusNotFound},
		{http.MethodPost, "/healthz", http.StatusNotFound},
		{http.MethodPost, "//webhook", http.StatusNotFound},
		{http.MethodPost, "/webhook/", http.StatusNotFound},
		{http.MethodGet, "/webhook/../healthz", http.StatusNotFound},
	}

	for _, spec := range tests {
		t.Run(spec.method+" "+spec.path, func(t *testing.T) {
			t.Parallel()

			got := do(t, testSecret, spec.method, spec.path, "", nil)
			if got != spec.want {
				t.Errorf("status = %d, want %d", got, spec.want)
			}
		})
	}
}

func TestNewDeliveryQueued(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer

	repo := openStore(t)
	handler := receiver.Handler([]byte(testSecret), repo, slog.New(slog.NewJSONHandler(&logs, nil)))

	if got := postDelivery(t, handler); got != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", got)
	}

	assertQueuedDelivery(t, repo)
	assertLogContains(t, logs.String(), `"outcome":"enqueued"`, `"repository":"unknown"`)
}

func TestAcceptedDeliveryLogsRepository(t *testing.T) {
	t.Parallel()

	const body = `{"repository":{"full_name":"example/\r\nwidgets"},"action":"opened"}`

	var logs bytes.Buffer

	handler := receiver.Handler(
		[]byte(testSecret),
		openStore(t),
		slog.New(slog.NewJSONHandler(&logs, nil)),
	)
	req := newDeliveryRequest(t.Context(), strings.NewReader(body))
	req.Header.Set(sigHeader, sign(testSecret, body))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", rec.Code)
	}

	assertLogContains(
		t,
		logs.String(),
		`"msg":"accepted delivery"`,
		`"repository":"example/widgets"`,
	)
}

func TestReplayDeliveryIsDuplicate(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer

	repo := openStore(t)
	handler := receiver.Handler([]byte(testSecret), repo, slog.New(slog.NewJSONHandler(&logs, nil)))

	if got := postDelivery(t, handler); got != http.StatusAccepted {
		t.Fatalf("first status = %d, want 202", got)
	}

	logs.Reset()

	if got := postDelivery(t, handler); got != http.StatusAccepted {
		t.Fatalf("replay status = %d, want 202", got)
	}

	entries, err := repo.Pending(10)
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}

	if len(entries) != 1 {
		t.Fatalf("pending len = %d, want 1", len(entries))
	}

	if !strings.Contains(logs.String(), `"outcome":"duplicate"`) {
		t.Errorf("replay log = %s, want outcome duplicate", logs.String())
	}
}

func TestStoreErrorAnswers500(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer

	repo := openStore(t)

	err := repo.Close()
	if err != nil {
		t.Fatalf("Close: %v", err)
	}

	handler := receiver.Handler([]byte(testSecret), repo, slog.New(slog.NewJSONHandler(&logs, nil)))

	if got := postDelivery(t, handler); got != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", got)
	}

	assertLogContains(
		t,
		logs.String(),
		errorLevel,
		`"msg":"enqueue delivery"`,
		loggedDelivery,
		loggedEvent,
	)
}

// gateBody signals on its first Read, then blocks until release closes.
type gateBody struct {
	started chan<- struct{}
	release <-chan struct{}
	once    sync.Once
}

func (g *gateBody) Read(_ []byte) (int, error) {
	g.once.Do(func() { g.started <- struct{}{} })
	<-g.release

	return 0, io.EOF
}

// holdSlots starts MaxInFlight deliveries that block mid-body and returns once
// all of them hold a slot. The returned function releases and waits for them.
func holdSlots(t *testing.T, handler http.Handler) func() {
	t.Helper()

	started := make(chan struct{}, receiver.MaxInFlight)
	release := make(chan struct{})

	var group sync.WaitGroup

	for range receiver.MaxInFlight {
		group.Go(func() {
			req := newDeliveryRequest(t.Context(),
				&gateBody{started: started, release: release, once: sync.Once{}})
			handler.ServeHTTP(httptest.NewRecorder(), req)
		})
	}

	for range receiver.MaxInFlight {
		<-started
	}

	return func() {
		close(release)
		group.Wait()
	}
}

func TestInFlightWaitAcceptsAndQueues(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		repo := openStore(t)
		handler := receiver.Handler([]byte(testSecret), repo, slog.New(slog.DiscardHandler))
		free := holdSlots(t, handler)

		done := make(chan int, 1)

		go func() { done <- postDelivery(t, handler) }()

		synctest.Wait()
		time.Sleep(time.Second)
		synctest.Wait()

		if len(done) != 0 {
			t.Error("delivery returned while all slots were held")
		}

		free()

		if got := <-done; got != http.StatusAccepted {
			t.Fatalf("after the slots free: status %d, want 202", got)
		}

		assertQueuedDelivery(t, repo)
	})
}

func TestInFlightWaitTimesOut(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		const wantWait = 8 * time.Second

		var logs bytes.Buffer

		handler := receiver.Handler(
			[]byte(testSecret),
			openStore(t),
			slog.New(slog.NewJSONHandler(&logs, nil)),
		)
		free := holdSlots(t, handler)

		defer free()

		rec := httptest.NewRecorder()
		req := newDeliveryRequest(t.Context(), failReader{t: t})
		start := time.Now()

		handler.ServeHTTP(rec, req)

		if got := time.Since(start); got != wantWait {
			t.Errorf("wait = %v, want %v", got, wantWait)
		}

		if rec.Code != http.StatusServiceUnavailable || rec.Header().Get("Retry-After") == "" {
			t.Errorf(
				"status %d, Retry-After %q, want 503 with Retry-After",
				rec.Code,
				rec.Header().Get("Retry-After"),
			)
		}

		if got := rec.Header().Get("Connection"); got != "close" {
			t.Errorf("Connection %q, want close", got)
		}

		assertLogContains(t, logs.String(), errorLevel, `"msg":"rejected delivery"`,
			`"reason":"no free slot after wait"`, loggedDelivery, loggedEvent)
	})
}

func TestInFlightWaitStopsOnCancellation(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		repo := openStore(t)
		handler := receiver.Handler([]byte(testSecret), repo, slog.New(slog.DiscardHandler))
		free := holdSlots(t, handler)

		defer free()

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		req := newDeliveryRequest(ctx, failReader{t: t})
		done := make(chan struct{})

		go func() {
			handler.ServeHTTP(httptest.NewRecorder(), req)
			close(done)
		}()

		synctest.Wait()

		select {
		case <-done:
			t.Fatal("delivery returned before cancellation")
		default:
		}

		start := time.Now()

		cancel()
		<-done

		if got := time.Since(start); got != 0 {
			t.Errorf("wait after cancellation = %v, want 0", got)
		}

		entries, err := repo.Pending(1)
		if err != nil || len(entries) != 0 {
			t.Fatalf("Pending = %+v, %v, want no entries", entries, err)
		}
	})
}

var errNoDeadline = errors.New("no deadline")

// spyConn records what rejectBusy does to a hijacked connection.
type spyConn struct {
	net.Conn

	deadline    time.Time
	deadlineErr error
	written     bytes.Buffer
	closed      bool
}

func (c *spyConn) SetWriteDeadline(deadline time.Time) error {
	c.deadline = deadline

	return c.deadlineErr
}

func (c *spyConn) Write(data []byte) (int, error) {
	size, err := c.written.Write(data)
	if err != nil {
		return size, fmt.Errorf("write fake connection: %w", err)
	}

	return size, nil
}

func (c *spyConn) Close() error {
	c.closed = true

	return nil
}

// hijackWriter lets a test hand rejectBusy a connection it can inspect.
type hijackWriter struct {
	http.ResponseWriter

	conn *spyConn
}

func (w *hijackWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return w.conn, bufio.NewReadWriter(
		bufio.NewReader(strings.NewReader("")),
		bufio.NewWriter(w.conn),
	), nil
}

// serveBusy runs the 503 path on a fake clock and returns the time the handler ends.
func serveBusy(t *testing.T, conn *spyConn) time.Time {
	t.Helper()

	var end time.Time

	synctest.Test(t, func(t *testing.T) {
		handler := receiver.Handler([]byte(testSecret), openStore(t), slog.New(slog.DiscardHandler))
		free := holdSlots(t, handler)

		defer free()

		req := newDeliveryRequest(t.Context(), failReader{t: t})
		handler.ServeHTTP(&hijackWriter{ResponseWriter: httptest.NewRecorder(), conn: conn}, req)

		end = time.Now()
	})

	return end
}

// TestInFlightRejectBoundsWrite checks that the 503 on a hijacked connection has a write deadline of about 5 s.
func TestInFlightRejectBoundsWrite(t *testing.T) {
	t.Parallel()

	conn := new(spyConn)
	end := serveBusy(t, conn)

	if !conn.closed {
		t.Error("connection stayed open")
	}

	if !strings.HasPrefix(conn.written.String(), "HTTP/1.1 503 ") {
		t.Errorf("wrote %q, want a 503", conn.written.String())
	}

	const want = 5 * time.Second
	if got := conn.deadline.Sub(end); got != want {
		t.Errorf("write deadline %v after the response, want %v", got, want)
	}
}

// TestInFlightRejectDeadlineFailure checks that a failed deadline sends nothing and closes the connection.
func TestInFlightRejectDeadlineFailure(t *testing.T) {
	t.Parallel()

	conn := new(spyConn)
	conn.deadlineErr = errNoDeadline

	serveBusy(t, conn)

	if !conn.closed {
		t.Error("connection stayed open")
	}

	if conn.written.Len() != 0 {
		t.Errorf("wrote %q without a deadline", conn.written.String())
	}
}

// countReader supplies endless bytes and counts how many the server took.
type countReader struct{ read *atomic.Int64 }

func (c countReader) Read(buf []byte) (int, error) {
	c.read.Add(int64(len(buf)))

	return len(buf), nil
}

// A chunked body has no Content-Length, so only the read bound stops it.
func TestChunkedBodyStopsAtCap(t *testing.T) {
	t.Parallel()

	handler := receiver.Handler([]byte(testSecret), openStore(t), slog.New(slog.DiscardHandler))

	var read atomic.Int64

	req := httptest.NewRequestWithContext(
		t.Context(),
		http.MethodPost,
		"/webhook",
		countReader{read: &read},
	)
	req.ContentLength = -1
	req.Header.Set(sigHeader, sign(testSecret, openedBody))
	req.Header.Set(deliveryHeader, deliveryID)
	req.Header.Set(eventHeader, pingEvent)

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status %d, want 413", rec.Code)
	}

	// The bound allows one byte past the cap and the copy buffer reads in blocks.
	const slack = 64 << 10
	if got := read.Load(); got > receiver.MaxBody+slack {
		t.Errorf(
			"server read %d bytes of an endless chunked body, want at most %d",
			got,
			receiver.MaxBody+slack,
		)
	}
}
