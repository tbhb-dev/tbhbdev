// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
)

const (
	storeDirFlag      = "-store-dir="
	testReadListen    = "-read-listen=127.0.0.1:0"
	testSessionListen = "-session-listen=127.0.0.1:0"
	tokenFileFlag     = "-read-token-file="
)

func TestRunPrintsVersion(t *testing.T) {
	t.Parallel()

	var out bytes.Buffer

	checkError(t, run(t.Context(), &out, slog.DiscardHandler, nil))
	checkEqual(t, out.String(), "event-gateway dev\n")

	reader, writer := io.Pipe()
	checkError(t, reader.Close())
	t.Cleanup(func() { checkError(t, writer.Close()) })

	err := run(t.Context(), writer, slog.DiscardHandler, nil)
	checkEqual(t, errors.Is(err, io.ErrClosedPipe), true)
	checkEqual(t, errors.Is(printParked(writer, strings.NewReader(`[{"seq":1}]`)), io.ErrClosedPipe), true)
}

func TestServeConfigErrors(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		err  error
		name string
		args []string
	}{
		{errUsage, "command", []string{"unknown"}},
		{nil, "help", []string{serveCommand, "-help"}},
		{errUsage, "extra argument", []string{serveCommand, "extra"}},
		{errBodyCap, "empty cap", []string{serveCommand, "-body-cap=0"}},
		{errBodyCap, "large cap", []string{serveCommand, "-body-cap=6000000"}},
		{errStoreLimit, "negative memory", []string{serveCommand, "-jetstream-max-memory=-1"}},
		{errStoreLimit, "negative disk", []string{serveCommand, "-jetstream-max-store=-1"}},
		{os.ErrNotExist, "missing read token file", []string{serveCommand, testReadListen, "-read-token-file=missing"}},
		{errServeFlags, "header without separator", []string{serveCommand, "-otlp-header=Authorization"}},
		{errServeFlags, "empty header name", []string{serveCommand, "-otlp-header==value"}},
		{errStoreDir, "store directory", []string{serveCommand, storeDirFlag}},
		{errOTLPEndpoint, "collector endpoint", []string{serveCommand, "-otlp-endpoint=invalid"}},
		{errCollectorCA, "collector CA", []string{serveCommand, "-otlp-endpoint=https://localhost", "-otlp-ca-file=missing"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			args := append(
				[]string{test.args[0], storeDirFlag + t.TempDir(), "-github-secret=" + fixtureKey},
				test.args[1:]...,
			)
			err := run(t.Context(), io.Discard, slog.DiscardHandler, args)
			checkEqual(t, errors.Is(err, test.err), true)
		})
	}
}

func TestServeTokenFlags(t *testing.T) {
	t.Parallel()

	const tokenFile = tokenFileFlag + "tokens"

	for _, test := range []struct {
		want error
		args []string
	}{
		{nil, []string{testSessionListen, tokenFile}},
		{nil, []string{testReadListen, tokenFile}},
		{nil, []string{testSessionListen, testReadListen, tokenFile}},
		{errReadFlags, []string{testSessionListen}},
		{errReadFlags, []string{testReadListen}},
		{errReadFlags, []string{tokenFile}},
		{errReadFlags, []string{"-read-listen=", tokenFile}},
		{errReadFlags, []string{"-read-listen=", testSessionListen, tokenFile}},
	} {
		var cfg serveConfig

		err := cfg.parse(io.Discard, test.args)
		if !errors.Is(err, test.want) {
			t.Errorf("flags %v: got %v, want %v", test.args, err, test.want)
		}
	}
}

func TestServeSessionRejectsInvalidTokenFile(t *testing.T) {
	t.Parallel()

	file := filepath.Join(t.TempDir(), "tokens")
	args := []string{
		serveCommand, testSessionListen, tokenFileFlag + file, storeDirFlag, "-github-secret=" + fixtureKey,
	}

	for _, data := range []string{"", "# no entries\n", "reader:invalid\n"} {
		checkError(t, os.WriteFile(file, []byte(data), 0o600))
		checkEqual(t, errors.Is(run(t.Context(), io.Discard, slog.DiscardHandler, args), errTokenFormat), true)
	}
}

func TestServeSessionOnly(t *testing.T) {
	t.Parallel()

	var listenConfig net.ListenConfig

	listener, err := listenConfig.Listen(t.Context(), "tcp", "127.0.0.1:0")
	checkError(t, err)

	address := listener.Addr().String()
	checkError(t, listener.Close())

	dir := t.TempDir()
	file := filepath.Join(dir, "tokens")
	writeReadTokens(t, file, "reader", readFixture)
	args := []string{
		serveCommand, "-listen=127.0.0.1:0", "-session-listen=" + address,
		storeDirFlag + dir, tokenFileFlag + file, "-github-secret=" + fixtureKey,
	}
	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	done := make(chan error, 1)

	go func() { done <- run(ctx, io.Discard, slog.DiscardHandler, args) }()

	t.Cleanup(func() {
		cancel()
		checkError(t, <-done)
	})

	base := "http://" + address
	checkServeSession(ctx, t, base, readFixture)

	for _, test := range []struct {
		path, bearer string
		status       int
	}{
		{sessionPath, "Bearer invalid", http.StatusUnauthorized},
		{sessionPath, "", http.StatusUnauthorized},
		{eventsPath, "Bearer " + readFixture, http.StatusNotFound},
	} {
		req, requestErr := http.NewRequestWithContext(ctx, http.MethodGet, base+test.path, nil)
		checkError(t, requestErr)

		if test.bearer != "" {
			req.Header.Set(authorization, test.bearer)
		}

		expectHTTPStatus(t, http.DefaultClient, req, test.status)
	}

	const replacement = "replacement-fixture"

	writeReadTokens(t, file, "replacement", replacement)
	checkServeSession(ctx, t, base, replacement)
}

func checkServeSession(ctx context.Context, t *testing.T, base, token string) {
	t.Helper()

	tick := time.NewTicker(25 * time.Millisecond)
	defer tick.Stop()

	options := new(websocket.DialOptions)
	options.HTTPHeader = http.Header{authorization: {"Bearer " + token}}
	options.Subprotocols = []string{sessionProtocol}

	for {
		conn, response, err := websocket.Dial(ctx, base+sessionPath, options)
		if response != nil && response.Body != nil {
			checkError(t, response.Body.Close())
		}

		if err == nil {
			defer func() { closeTestSocket(t, conn) }()

			checkSessionReply(ctx, t, conn, `{"type":"ping"}`, typeKey, pong)

			return
		}

		select {
		case <-ctx.Done():
			t.Fatal("session did not accept the token before the deadline")
		case <-tick.C:
		}
	}
}

func TestServeRejectsInvalidSecret(t *testing.T) {
	t.Setenv("GITHUB_WEBHOOK_SECRET", "")
	checkError(t, os.Unsetenv("GITHUB_WEBHOOK_SECRET"))
	missingErr := run(t.Context(), io.Discard, slog.DiscardHandler, []string{serveCommand, storeDirFlag})
	checkEqual(t, errors.Is(missingErr, errSecret), true)

	testInvalidSecretSources(t, "github", "GITHUB_WEBHOOK_SECRET", errSecret)
	t.Setenv("GITHUB_WEBHOOK_SECRET", fixtureKey)
	testInvalidSecretSources(t, grafanaProvider, grafanaEnv, errGrafanaSecret)
}

func testInvalidSecretSources(t *testing.T, provider, env string, secretErr error) {
	t.Helper()

	dir := t.TempDir()
	empty := filepath.Join(dir, "empty")
	blank := filepath.Join(dir, "blank")

	checkError(t, os.WriteFile(empty, nil, 0o600))
	checkError(t, os.WriteFile(blank, []byte(" \t\r\n"), 0o600))

	secretFlag := "-" + provider + "-secret"
	for _, test := range []struct {
		err  error
		name string
		env  string
		args []string
	}{
		{secretErr, "empty environment", "", nil},
		{secretErr, "blank environment", " \t", nil},
		{secretErr, "empty flag", fixtureKey, []string{secretFlag + "="}},
		{secretErr, "blank flag", fixtureKey, []string{secretFlag + "= \t"}},
		{secretErr, "empty file", fixtureKey, []string{secretFlag + "-file=" + empty}},
		{secretErr, "blank file", fixtureKey, []string{secretFlag + "-file=" + blank}},
		{os.ErrNotExist, "missing file", fixtureKey, []string{secretFlag + "-file=" + filepath.Join(dir, "missing")}},
	} {
		t.Run(provider+"/"+test.name, func(t *testing.T) {
			t.Setenv(env, test.env)

			args := append([]string{serveCommand, storeDirFlag, "-listen=127.0.0.1:-1"}, test.args...)
			err := run(t.Context(), io.Discard, slog.DiscardHandler, args)
			checkEqual(t, errors.Is(err, test.err), true)
		})
	}
}

func TestGitHubWebhookRequiresSecret(t *testing.T) {
	t.Parallel()

	for _, secret := range [][]byte{nil, {}, []byte(" \t")} {
		hook, err := newGitHubWebhook(secret, defaultBodyCap)
		checkEqual(t, hook, nil)
		checkEqual(t, errors.Is(err, errSecret), true)
	}
}

func TestServeInvalidListenAddress(t *testing.T) {
	t.Parallel()

	args := []string{serveCommand, "-listen=127.0.0.1:-1", storeDirFlag + t.TempDir(), "-github-secret=" + fixtureKey}

	err := run(t.Context(), io.Discard, slog.DiscardHandler, args)
	if err == nil {
		t.Fatal("serve accepted an invalid listen address")
	}
}

func TestServeValidSecretAndContextShutdown(t *testing.T) {
	var exports atomic.Int32

	var sentHeader atomic.Bool

	collector := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		exports.Add(1)

		if request.Header.Get("X-Collector-Test") == "test=value" {
			sentHeader.Store(true)
		}

		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(collector.Close)
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.URL)
	t.Setenv("OTEL_EXPORTER_OTLP_HEADERS", "X-Collector-Test=test=value")
	t.Setenv(grafanaEnv, "")
	checkError(t, os.Unsetenv(grafanaEnv))

	file := filepath.Join(t.TempDir(), "secret")
	checkError(t, os.WriteFile(file, []byte(fixtureKey+"\r\n"), 0o600))

	exerciseServe(t, file, http.StatusNotFound)
	checkEqual(t, exports.Load(), 0)

	exerciseServe(t, file, http.StatusUnauthorized, "-grafana-secret-file="+file,
		"-otlp-endpoint="+collector.URL, "-otlp-header=X-Collector-Test=test=value", "-session-listen=127.0.0.1:0")
	checkEqual(t, exports.Load() > 0, true)
	checkEqual(t, sentHeader.Load(), true)
}

func exerciseServe(t *testing.T, file string, grafanaStatus int, flags ...string) {
	t.Helper()

	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()

	var listenConfig net.ListenConfig

	listener, err := listenConfig.Listen(ctx, "tcp", "127.0.0.1:0")
	checkError(t, err)

	address := listener.Addr().String()
	checkError(t, listener.Close())

	dir := t.TempDir()
	tokens := filepath.Join(dir, "tokens")
	writeReadTokens(t, tokens, "reader", readFixture)

	args := append(
		[]string{
			serveCommand, "-listen=" + address, storeDirFlag + dir, "-github-secret-file=" + file,
			testReadListen, "-read-token-file=" + tokens,
		},
		flags...,
	)

	var logs bytes.Buffer

	done := make(chan error, 1)
	go func() {
		done <- run(ctx, io.Discard, slog.NewJSONHandler(&logs, nil), args)
	}()

	var client http.Client

	t.Cleanup(client.CloseIdleConnections)

	waitForGatewayHealth(ctx, t, &client, address, done)

	checkServedWebhooks(ctx, t, &client, address, grafanaStatus)

	cancel()

	select {
	case serveErr := <-done:
		checkError(t, serveErr)
	case <-time.After(30 * time.Second):
		t.Fatal("gateway did not shut down after context cancellation")
	}

	checkEqual(t, strings.Contains(logs.String(), fixtureKey), false)
	checkEqual(t, strings.Contains(logs.String(), "private test payload"), false)
}

func waitForGatewayHealth(ctx context.Context, t *testing.T, client *http.Client, address string, done <-chan error) {
	t.Helper()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+address+healthPath, nil)
	checkError(t, err)

	for {
		response, requestErr := client.Do(req)
		if requestErr == nil {
			checkError(t, response.Body.Close())
			checkEqual(t, response.StatusCode, http.StatusOK)

			break
		}

		select {
		case serveErr := <-done:
			t.Fatalf("gateway stopped before serving /healthz: %v", serveErr)
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-time.After(10 * time.Millisecond):
		}
	}
}

func checkServedWebhooks(ctx context.Context, t *testing.T, client *http.Client, address string, grafanaStatus int) {
	t.Helper()

	const body = `{"repository":{"full_name":"example/repository"},"message":"private test payload"}`

	webhook := signedRequest(ctx, "push", "serve-test", body)
	webhook.RequestURI = ""
	webhook.URL.Scheme = "http"
	webhook.URL.Host = address
	webhook.Host = address
	expectHTTPStatus(t, client, webhook, http.StatusAccepted)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+address+grafanaPath, nil)
	checkError(t, err)
	expectHTTPStatus(t, client, req, grafanaStatus)
}

func expectHTTPStatus(t *testing.T, client *http.Client, req *http.Request, status int) {
	t.Helper()

	response, err := client.Do(req)
	checkError(t, err)
	checkError(t, response.Body.Close())
	checkEqual(t, response.StatusCode, status)
}

func TestExitCode(t *testing.T) {
	t.Parallel()

	logger := slog.New(slog.DiscardHandler)
	checkEqual(t, exitCode(t.Context(), logger, nil), 0)
	checkEqual(t, exitCode(t.Context(), logger, errUsage), 1)
}

func TestRoutes(t *testing.T) {
	t.Parallel()

	var hook githubWebhook

	for _, test := range []struct {
		method string
		path   string
		status int
	}{
		{http.MethodGet, healthPath, http.StatusOK},
		{http.MethodGet, webhookPath, http.StatusNotFound},
		{http.MethodPost, healthPath, http.StatusNotFound},
		{http.MethodPost, "/" + webhookPath, http.StatusNotFound},
	} {
		req := httptest.NewRequestWithContext(t.Context(), test.method, test.path, nil)
		expectStatus(t, hook.ServeHTTP, req, test.status)
	}
}
