// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package appauth_test

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/appauth"
)

type credentialError struct{}

func (credentialError) Error() string { return "credential-marker" }

type brokenBody struct{}

func (brokenBody) Read([]byte) (int, error) { return 0, credentialError{} }

func (brokenBody) Close() error { return nil }

type brokenTransport struct {
	read bool
}

func (transport brokenTransport) RoundTrip(*http.Request) (*http.Response, error) {
	if !transport.read {
		return nil, credentialError{}
	}

	rec := httptest.NewRecorder()
	rec.WriteHeader(http.StatusCreated)

	resp := rec.Result()
	resp.Body = brokenBody{}

	return resp, nil
}

func TestTransportFailures(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.NotFoundHandler())
	t.Cleanup(server.Close)

	for _, read := range []bool{false, true} {
		stage := "send"
		status := 0

		if read {
			stage = "read"
			status = http.StatusCreated
		}

		t.Run(stage, func(t *testing.T) {
			t.Parallel()

			var logs bytes.Buffer

			cfg := config(t, server, time.Now)
			cfg.HTTPClient.Transport = brokenTransport{read: read}
			cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
			source := newSource(t, cfg)

			checkFailure(t, source, &logs, stage, status)
		})
	}
}

func TestInvalidConfig(t *testing.T) {
	t.Parallel()

	server := rejectingAPI(t)

	cfg := config(t, server, time.Now)
	tests := map[string]func(*appauth.Config){
		"empty base":            func(cfg *appauth.Config) { cfg.BaseURL = "" },
		"invalid base":          func(cfg *appauth.Config) { cfg.BaseURL += "%" },
		"relative base":         func(cfg *appauth.Config) { cfg.BaseURL = "/api/v3" },
		"insecure base":         func(cfg *appauth.Config) { cfg.BaseURL = "http://example.invalid/api/v3" },
		"localhost suffix":      func(cfg *appauth.Config) { cfg.BaseURL = "http://localhost.example.invalid" },
		"loopback suffix":       func(cfg *appauth.Config) { cfg.BaseURL = "http://127.0.0.1.example.invalid" },
		"other scheme":          func(cfg *appauth.Config) { cfg.BaseURL = "ftp://localhost" },
		"query":                 func(cfg *appauth.Config) { cfg.BaseURL += "?credential-marker" },
		"empty query":           func(cfg *appauth.Config) { cfg.BaseURL += "?" },
		"fragment":              func(cfg *appauth.Config) { cfg.BaseURL += "#credential-marker" },
		"zero app":              func(cfg *appauth.Config) { cfg.AppID = 0 },
		"negative installation": func(cfg *appauth.Config) { cfg.InstallationID = -1 },
	}

	for name, change := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			var logs bytes.Buffer

			own := cfg
			own.Logger = slog.New(slog.NewJSONHandler(&logs, nil))

			change(&own)

			source, err := appauth.New(own)
			if source != nil {
				t.Error("New accepted an invalid configuration")
			}

			checkAuthError(t, err)
			checkLog(t, &logs, "config", 0)
		})
	}
}

func TestAllowedBaseURLs(t *testing.T) {
	t.Parallel()

	server := rejectingAPI(t)
	cfg := config(t, server, time.Now)

	for _, base := range []string{
		"https://example.invalid/api/v3",
		"http://127.0.0.1:8080/api/v3",
		"http://[::1]:8080/api/v3",
		"http://localhost:8080/api/v3",
		"http://LOCALHOST/api/v3",
	} {
		t.Run(base, func(t *testing.T) {
			t.Parallel()

			own := cfg
			own.BaseURL = base

			newSource(t, own)
		})
	}
}

func TestClientTimeout(t *testing.T) {
	t.Parallel()

	server := rejectingAPI(t)
	cfg := config(t, server, time.Now)
	tests := map[string]struct {
		timeout  time.Duration
		supplied bool
	}{
		"default":             {30 * time.Second, false},
		"supplied timeout":    {7 * time.Second, true},
		"supplied no timeout": {0, true},
	}

	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			own := cfg
			own.HTTPClient = nil

			if test.supplied {
				client := *cfg.HTTPClient
				client.Timeout = test.timeout
				own.HTTPClient = &client
			}

			client := newSource(t, own).HTTPClient()
			if client.Timeout != test.timeout {
				t.Error("client timeout didn't match the expected value")
			}

			if own.HTTPClient != nil &&
				(client == own.HTTPClient || client.Transport != own.HTTPClient.Transport ||
					own.HTTPClient.Timeout != test.timeout || own.HTTPClient.CheckRedirect != nil) {
				t.Error("supplied client settings weren't preserved")
			}
		})
	}
}
