// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package appauth_test

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/appauth"
)

const (
	stageDecode = "decode"
	failureBody = `{"token":"response-marker"}`
)

func checkAuthError(t *testing.T, err error) {
	t.Helper()

	if err != appauth.ErrAuth { //nolint:err113,errorlint // The contract requires the same static error without a wrapper.
		t.Error("failure didn't return the static error")
	}
}

func rejectingAPI(t *testing.T) *httptest.Server {
	t.Helper()

	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) {
		t.Error("unexpected API request")
	}))
	t.Cleanup(server.Close)

	return server
}

func checkFailure(
	t *testing.T,
	source *appauth.Source,
	logs *bytes.Buffer,
	stage string,
	status int,
) {
	t.Helper()

	token, err := source.Token(t.Context())
	if token != "" {
		t.Error("failure didn't return an empty token")
	}

	checkAuthError(t, err)
	checkLog(t, logs, stage, status)
}

func checkLog(t *testing.T, logs *bytes.Buffer, stage string, status int) {
	t.Helper()

	var record map[string]any

	if json.Unmarshal(logs.Bytes(), &record) != nil {
		t.Fatal("expected one failure log")
	}

	if len(record) != 5 || record["stage"] != stage || record["status"] != float64(status) ||
		record["msg"] != appauth.ErrAuth.Error() || record["level"] != "ERROR" {
		t.Error("failure log must contain only stage, status, and standard log fields")
	}
}

type responseFailure struct {
	body   string
	stage  string
	status int
}

func responseFailures() map[string]responseFailure {
	return map[string]responseFailure{
		"unauthorized":  {failureBody, "status", http.StatusUnauthorized},
		"wrong success": {failureBody, "status", http.StatusOK},
		"malformed":     {`{"token":"response-marker"`, stageDecode, http.StatusCreated},
		"invalid expiry": {
			`{"token":"response-marker","expires_at":"invalid"}`,
			stageDecode,
			http.StatusCreated,
		},
		"missing expiry": {failureBody, stageDecode, http.StatusCreated},
		"missing token": {
			`{"expires_at":"2026-01-01T01:00:00Z"}`,
			stageDecode,
			http.StatusCreated,
		},
		"expired": {
			`{"token":"response-marker","expires_at":"2026-01-01T00:00:00Z"}`,
			stageDecode,
			http.StatusCreated,
		},
		"expiry beyond limit": {
			`{"token":"response-marker","expires_at":"2026-01-01T02:00:00.000000001Z"}`,
			stageDecode,
			http.StatusCreated,
		},
		"large body": {strings.Repeat("response-marker", 1<<20), "read", http.StatusCreated},
		"trailing data": {
			`{"token":"response-marker","expires_at":"2026-01-01T01:00:00Z"}x`,
			stageDecode,
			http.StatusCreated,
		},
	}
}

func TestResponseFailures(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)
	key := encodeKey(t, newKey(t), false)

	for name, test := range responseFailures() {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			server := httptest.NewServer(
				http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
					resp.WriteHeader(test.status)

					_, err := resp.Write([]byte(test.body))
					if err != nil && name != "large body" {
						t.Error("write response failed")
					}
				}),
			)
			t.Cleanup(server.Close)

			var logs bytes.Buffer

			cfg := config(t, server, func() time.Time { return now })
			cfg.PrivateKeyPEM = key
			cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
			source := newSource(t, cfg)

			checkFailure(t, source, &logs, test.stage, test.status)

			if strings.Contains(logs.String(), "response-marker") {
				t.Error("failure log exposed response text")
			}
		})
	}
}

func TestRejectedResponseAllowsConnectionReuse(t *testing.T) {
	t.Parallel()

	var connections atomic.Int64

	server := httptest.NewUnstartedServer(
		http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
			resp.WriteHeader(http.StatusUnauthorized)

			_, err := resp.Write([]byte(failureBody))
			if err != nil {
				t.Error("write response failed")
			}
		}),
	)
	server.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			connections.Add(1)
		}
	}
	server.Start()
	t.Cleanup(server.Close)

	var logs bytes.Buffer

	cfg := config(t, server, time.Now)
	cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	source := newSource(t, cfg)

	checkFailure(t, source, &logs, "status", http.StatusUnauthorized)
	logs.Reset()
	checkFailure(t, source, &logs, "status", http.StatusUnauthorized)

	if connections.Load() != 1 {
		t.Error("rejected response prevented connection reuse")
	}
}

func TestTokenRefreshFailureKeepsValidCache(t *testing.T) {
	t.Parallel()

	start := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)
	expires := start.Add(time.Hour)

	var (
		calls   atomic.Int64
		elapsed atomic.Int64
	)

	server := httptest.NewServer(http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
		switch calls.Add(1) {
		case 1:
			resp.WriteHeader(http.StatusCreated)

			_, _ = fmt.Fprintf(
				resp,
				`{"token":"cached-token","expires_at":%q}`,
				expires.Format(time.RFC3339),
			)

			return
		case 3:
			elapsed.Store(int64(time.Hour))
		}

		resp.WriteHeader(http.StatusForbidden)
	}))
	t.Cleanup(server.Close)

	var logs bytes.Buffer

	cfg := config(t, server, func() time.Time { return start.Add(time.Duration(elapsed.Load())) })
	cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	source := newSource(t, cfg)

	checkToken(t, source.Token, "cached-token")
	elapsed.Store(int64(time.Hour - time.Minute))
	checkToken(t, source.Token, "cached-token")
	checkLog(t, &logs, "status", http.StatusForbidden)

	logs.Reset()
	checkFailure(t, source, &logs, "status", http.StatusForbidden)
}

func TestExpiryBound(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)

	var calls atomic.Int64

	server := httptest.NewServer(http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
		lifetime := 2 * time.Hour
		if calls.Add(1) == 2 {
			lifetime += time.Nanosecond
		}

		resp.WriteHeader(http.StatusCreated)

		_, _ = fmt.Fprintf(resp, `{"token":"response-marker","expires_at":%q}`,
			now.Add(lifetime).Format(time.RFC3339Nano))
	}))
	t.Cleanup(server.Close)

	var logs bytes.Buffer

	cfg := config(t, server, func() time.Time { return now })
	cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	source := newSource(t, cfg)

	checkToken(t, source.Token, "response-marker")

	token, err := source.Renew(t.Context())
	if token != "" {
		t.Error("Renew failure didn't discard the token")
	}

	checkAuthError(t, err)
	checkLog(t, &logs, stageDecode, http.StatusCreated)
	checkToken(t, source.Token, "response-marker")

	if calls.Load() != 3 {
		t.Error("Token reused the cache after an invalid expiry")
	}

	if strings.Contains(logs.String(), "response-marker") {
		t.Error("failure log exposed response text")
	}
}

func TestRenewFailureDiscardsCache(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)

	var calls atomic.Int64

	server := httptest.NewServer(http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 2 {
			resp.WriteHeader(http.StatusForbidden)

			return
		}

		resp.WriteHeader(http.StatusCreated)

		_, _ = fmt.Fprintf(
			resp,
			`{"token":"fresh-token","expires_at":%q}`,
			now.Add(time.Hour).Format(time.RFC3339),
		)
	}))
	t.Cleanup(server.Close)

	source := newSource(t, config(t, server, func() time.Time { return now }))

	checkToken(t, source.Token, "fresh-token")

	token, err := source.Renew(t.Context())
	if token != "" {
		t.Error("Renew failure didn't discard the token")
	}

	checkAuthError(t, err)
	checkToken(t, source.Token, "fresh-token")

	if calls.Load() != 3 {
		t.Error("Token reused the cache after a failed Renew")
	}
}

func TestRedirectRejected(t *testing.T) {
	t.Parallel()

	var redirected atomic.Bool

	target := httptest.NewServer(http.HandlerFunc(func(resp http.ResponseWriter, _ *http.Request) {
		redirected.Store(true)
		resp.WriteHeader(http.StatusCreated)
	}))
	t.Cleanup(target.Close)

	server := httptest.NewServer(
		http.HandlerFunc(func(resp http.ResponseWriter, req *http.Request) {
			http.Redirect(resp, req, target.URL, http.StatusTemporaryRedirect)
		}),
	)
	t.Cleanup(server.Close)

	var logs bytes.Buffer

	cfg := config(t, server, time.Now)
	cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	source := newSource(t, cfg)

	checkFailure(t, source, &logs, "status", http.StatusTemporaryRedirect)

	if redirected.Load() {
		t.Error("client followed a redirect with an assertion")
	}
}

func TestCanceledRequest(t *testing.T) {
	t.Parallel()

	server := rejectingAPI(t)

	var logs bytes.Buffer

	cfg := config(t, server, time.Now)
	cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	source := newSource(t, cfg)
	ctx, cancel := context.WithCancel(t.Context())

	cancel()

	token, err := source.Token(ctx)
	if token != "" {
		t.Error("canceled request returned a token")
	}

	checkAuthError(t, err)
	checkLog(t, &logs, "send", 0)
}

func TestBadKey(t *testing.T) {
	t.Parallel()

	_, otherKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}

	otherDER, err := x509.MarshalPKCS8PrivateKey(otherKey)
	if err != nil {
		t.Fatal(err)
	}

	keys := map[string][]byte{
		"empty":   nil,
		"not PEM": []byte("invalid-key-marker"),
		"bad PKCS1": pem.EncodeToMemory(
			&pem.Block{Type: "RSA PRIVATE KEY", Headers: nil, Bytes: []byte("invalid-key-marker")},
		),
		"bad PKCS8": pem.EncodeToMemory(
			&pem.Block{Type: privateKeyType, Headers: nil, Bytes: []byte("invalid-key-marker")},
		),
		"other type": pem.EncodeToMemory(
			&pem.Block{Type: "PUBLIC KEY", Headers: nil, Bytes: otherDER},
		),
		"other algorithm": pem.EncodeToMemory(
			&pem.Block{Type: privateKeyType, Headers: nil, Bytes: otherDER},
		),
	}

	server := rejectingAPI(t)

	for name, key := range keys {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			var logs bytes.Buffer

			cfg := config(t, server, time.Now)
			cfg.PrivateKeyPEM = key
			cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))

			source, newErr := appauth.New(cfg)
			if source != nil {
				t.Error("New accepted an invalid key")
			}

			checkAuthError(t, newErr)
			checkLog(t, &logs, "key", 0)
		})
	}
}
