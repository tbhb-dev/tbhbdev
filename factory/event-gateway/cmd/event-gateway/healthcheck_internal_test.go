// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHealthcheck(t *testing.T) {
	t.Parallel()

	okAddr, badAddr, redirect, closed := healthcheckAddresses(t)

	for _, test := range []struct {
		name    string
		wantErr error
		args    []string
	}{
		{"success", nil, []string{"-addr", okAddr}},
		{"non-200", errHealthcheck, []string{"-addr=" + badAddr}},
		{"redirect", errHealthcheck, []string{"-addr=" + strings.TrimPrefix(redirect.URL, "http://")}},
		{"empty address", errHealthcheckUsage, []string{"-addr="}},
		{"path", errHealthcheckUsage, []string{"-addr=127.0.0.1:80/healthz"}},
		{"host path", errHealthcheckUsage, []string{"-addr=127.0.0.1/x:80"}},
		{"query", errHealthcheckUsage, []string{"-addr=127.0.0.1:80?x=1"}},
		{"fragment", errHealthcheckUsage, []string{"-addr=127.0.0.1:80#x"}},
		{"whitespace", errHealthcheckUsage, []string{"-addr=127.0.0.1 :80"}},
		{"refused", errHealthcheck, []string{"-addr=" + closed}},
		{"bad address", errHealthcheckUsage, []string{"-addr=bad host"}},
		{"empty host", errHealthcheckUsage, []string{"-addr=:8080"}},
		{"zero port", errHealthcheckUsage, []string{"-addr=localhost:0"}},
		{"help", errHealthcheckUsage, []string{"-help"}},
		{"unknown flag", errHealthcheckUsage, []string{"-nope"}},
		{"extra args", errHealthcheckUsage, []string{"-addr=" + okAddr, "extra"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			runErr := run(
				t.Context(),
				&strings.Builder{},
				slog.DiscardHandler,
				append([]string{healthcheckCommand}, test.args...),
			)
			checkEqual(t, runErr, test.wantErr)

			if runErr != nil {
				for _, leak := range []string{okAddr, badAddr, closed, "secret-body"} {
					checkEqual(t, strings.Contains(runErr.Error(), leak), false)
				}
			}
		})
	}
}

func healthcheckAddresses(t *testing.T) (string, string, *httptest.Server, string) {
	t.Helper()

	healthy := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		checkEqual(t, r.URL.Path, "/healthz")
		checkEqual(t, r.Method, http.MethodGet)
	}))
	t.Cleanup(healthy.Close)

	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "secret-body", http.StatusServiceUnavailable)
	}))
	t.Cleanup(bad.Close)

	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	t.Cleanup(target.Close)

	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	t.Cleanup(redirect.Close)

	gone := httptest.NewServer(http.NotFoundHandler())
	closed := strings.TrimPrefix(gone.URL, "http://")
	gone.Close()

	okAddr := strings.TrimPrefix(healthy.URL, "http://")
	badAddr := strings.TrimPrefix(bad.URL, "http://")

	return okAddr, badAddr, redirect, closed
}
