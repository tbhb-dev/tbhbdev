// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package receiver_test

import (
	"bufio"
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/receiver"
)

// serve starts the real server on a loopback listener and returns its address.
func serve(t *testing.T, network, addr string) string {
	t.Helper()

	var lc net.ListenConfig

	lis, err := lc.Listen(t.Context(), network, addr)
	if err != nil {
		t.Skipf("cannot listen on %s: %v", addr, err)
	}

	srv := receiver.NewServer(
		addr,
		receiver.Handler([]byte(testSecret), openStore(t), slog.New(slog.DiscardHandler)),
	)

	go func() {
		serveErr := srv.Serve(lis)
		if !errors.Is(serveErr, http.ErrServerClosed) {
			t.Errorf("serve: %v", serveErr)
		}
	}()

	t.Cleanup(func() { _ = srv.Close() })

	return lis.Addr().String()
}

// rawStatus sends request bytes as given, so a request line such as
// "OPTIONS *" reaches the server unchanged, and returns the status code.
func rawStatus(t *testing.T, addr, request string) int {
	t.Helper()

	var dialer net.Dialer

	conn, err := dialer.DialContext(context.Background(), "tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	defer func() { _ = conn.Close() }()

	_, err = conn.Write([]byte(request))
	if err != nil {
		t.Fatalf("write: %v", err)
	}

	resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}

	defer func() { _ = resp.Body.Close() }()

	return resp.StatusCode
}

func TestServerOffPathMethods(t *testing.T) {
	t.Parallel()

	addr := serve(t, "tcp", "127.0.0.1:0")
	tests := map[string]string{
		"OPTIONS star":     "OPTIONS * HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
		"OPTIONS /webhook": "OPTIONS /webhook HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
		"OPTIONS /other":   "OPTIONS /other HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
		"HEAD /healthz":    "HEAD /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
		"GET /other":       "GET /other HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
	}

	for name, request := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			got := rawStatus(t, addr, request)
			if got != http.StatusNotFound {
				t.Errorf("status = %d, want 404", got)
			}
		})
	}
}

func TestHealthURL(t *testing.T) {
	t.Parallel()

	tests := map[string]string{
		":8080":          "http://127.0.0.1:8080/healthz",
		"0.0.0.0:8080":   "http://127.0.0.1:8080/healthz",
		"[::]:8080":      "http://[::1]:8080/healthz",
		"[::1]:8080":     "http://[::1]:8080/healthz",
		"127.0.0.1:9":    "http://127.0.0.1:9/healthz",
		"10.1.2.3:8080":  "http://10.1.2.3:8080/healthz",
		"localhost:8080": "http://localhost:8080/healthz",
	}

	for addr, want := range tests {
		t.Run(addr, func(t *testing.T) {
			t.Parallel()

			got, err := receiver.HealthURL(addr)
			if err != nil || got != want {
				t.Errorf("HealthURL(%q) = %q, %v; want %q", addr, got, err, want)
			}
		})
	}

	_, err := receiver.HealthURL("no-port")
	if err == nil {
		t.Error("HealthURL(no-port) succeeded, want an error")
	}
}

func TestHealthURLReachesListener(t *testing.T) {
	t.Parallel()

	for _, spec := range []struct{ network, addr string }{
		{"tcp4", "127.0.0.1:0"},
		{"tcp6", "[::1]:0"},
	} {
		t.Run(spec.network, func(t *testing.T) {
			t.Parallel()

			listen := serve(t, spec.network, spec.addr)

			url, err := receiver.HealthURL(listen)
			if err != nil {
				t.Fatal(err)
			}

			req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, url, nil)
			if err != nil {
				t.Fatal(err)
			}

			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatalf("GET %s: %v", url, err)
			}

			defer func() { _ = resp.Body.Close() }()

			if resp.StatusCode != http.StatusOK ||
				strings.Contains(url, "::") != (spec.network == "tcp6") {
				t.Errorf("status = %d for %s", resp.StatusCode, url)
			}
		})
	}
}
