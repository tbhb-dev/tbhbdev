// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/nats-io/nats.go/jetstream"
)

const readFixture = "first-fixture"

func writeReadTokens(t *testing.T, file, name, token string) {
	t.Helper()

	hash := sha256.Sum256([]byte(token))
	checkError(t, os.WriteFile(file, fmt.Appendf(nil, "%s:%x\r\n\r\n# comment\r\n", name, hash), 0o600))
}

func readRequest(t *testing.T, api readAPI, path, header string, status int) *httptest.ResponseRecorder {
	t.Helper()

	request := httptest.NewRequestWithContext(t.Context(), http.MethodGet, path, nil)
	request.Header.Set("Authorization", header)

	response := httptest.NewRecorder()
	api.handler().ServeHTTP(response, request)
	checkEqual(t, response.Code, status)

	return response
}

func TestReadTokenReload(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer

	var tokens readTokens

	signals := newTelemetry(slog.NewJSONHandler(&logs, nil), nil, nil)
	tokens.path = filepath.Join(t.TempDir(), "tokens")
	checkError(t, os.WriteFile(tokens.path, nil, 0o600))
	checkEqual(t, tokens.load(), errTokenFormat)
	writeReadTokens(t, tokens.path, "first", readFixture)
	checkError(t, tokens.load())

	for _, contents := range []string{"", "# comment\n"} {
		checkError(t, os.WriteFile(tokens.path, []byte(contents), 0o600))
		checkEqual(t, tokens.load(), errTokenFormat)
		checkEqual(t, tokens.authenticate("Bearer "+readFixture), "first")
	}

	api := readAPI{tokens: &tokens, store: eventStore{jet: nil, stream: nil, openOrdered: nil}, telemetry: signals}
	for _, header := range []string{"", "Basic " + readFixture, "Bearer wrong"} {
		response := readRequest(t, api, eventsPath, header, http.StatusUnauthorized)
		checkEqual(t, response.Body.Len(), 0)
	}

	readRequest(t, api, "/unknown", "", http.StatusUnauthorized)

	checkError(t, os.WriteFile(tokens.path, []byte("bad entry"), 0o600))
	tokens.reload(t.Context(), signals.logger)
	readRequest(t, api, "/unknown", "bearer "+readFixture, http.StatusNotFound)
	checkEqual(t, strings.Contains(logs.String(), "reload read token file"), true)

	writeReadTokens(t, tokens.path, "second", "second-fixture")
	tokens.reload(t.Context(), signals.logger)
	readRequest(t, api, eventsPath, "Bearer "+readFixture, http.StatusUnauthorized)
	readRequest(t, api, "/unknown", "Bearer second-fixture", http.StatusNotFound)
}

func TestReadEventRoutes(t *testing.T) {
	t.Parallel()

	var tokens readTokens

	tokens.path = filepath.Join(t.TempDir(), "tokens")
	writeReadTokens(t, tokens.path, "reader", readFixture)
	checkError(t, tokens.load())

	grafana, store := grafanaHook(t)
	api := readAPI{tokens: &tokens, store: store, telemetry: newTelemetry(slog.DiscardHandler, nil, nil)}
	header := "Bearer " + readFixture
	hook := testWebhook(t, store, defaultBodyCap)

	publishReadEvents(t, hook)

	expectStatus(t, grafana.ServeHTTP, grafanaSigned(t.Context(), alertsBody(hostAlert)), http.StatusAccepted)
	checkError(t, store.stream.DeleteMsg(t.Context(), 1))
	response := readRequest(t, api, eventsPath, header, http.StatusOK)

	var page eventPage

	checkError(t, json.Unmarshal(response.Body.Bytes(), &page))
	checkEqual(t, page.Gap["from"], uint64(1))
	checkEqual(t, page.Gap["to"], uint64(1))
	checkEqual(t, strings.Contains(response.Body.String(), `"last_seq":11`), true)
	checkEqual(t, len(page.Events), pageLimit)

	response = readRequest(t, api, eventsPath+"?after=11&provider=github&event=issues", header, http.StatusOK)
	checkEqual(t, strings.Contains(response.Body.String(), `"last_seq":12`), true)

	response = readRequest(t, api, eventsPath+"?provider=grafana&event=alert.firing", header, http.StatusOK)
	checkEqual(t, strings.Contains(response.Body.String(), `"provider":"grafana","event":"alert.firing"`), true)

	for path, status := range map[string]int{
		eventsPath + "?after=18446744073709551615": http.StatusOK,
		eventsPath + "?after=bad":                  http.StatusBadRequest,
		eventsPath + "?provider=*":                 http.StatusBadRequest,
		eventsPath + "?provider=a.b":               http.StatusBadRequest,
		eventsPath + "?event=alert.":               http.StatusBadRequest,
		eventsPath + "?event=.x":                   http.StatusBadRequest,
		eventsPath + "?event=a..b":                 http.StatusBadRequest,
		eventsPath + "/0":                          http.StatusNotFound,
		eventsPath + "/1":                          http.StatusGone,
		eventsPath + "/14":                         http.StatusNotFound,
		eventsPath + "/bad":                        http.StatusBadRequest,
	} {
		readRequest(t, api, path, header, status)
	}

	response = readRequest(t, api, eventsPath+"/2", header, http.StatusOK)
	checkEqual(t, strings.Contains(response.Body.String(), `"payload":{"message":"stored"}`), true)

	checkReadFailures(t, api, hook, &page, header)
}

func publishReadEvents(t *testing.T, hook *githubWebhook) {
	t.Helper()

	for index := range pageLimit + 2 {
		event := "push"
		if index == pageLimit+1 {
			event = "issues"
		}

		req := signedRequest(t.Context(), event, strconv.Itoa(index), `{"message":"stored"}`)
		expectStatus(t, hook.ServeHTTP, req, http.StatusAccepted)
	}
}

func checkReadFailures(t *testing.T, api readAPI, hook *githubWebhook, page *eventPage, header string) {
	t.Helper()

	store := api.store

	var config jetstream.OrderedConsumerConfig

	config.MaxResetAttempts = 1
	consumer, err := store.stream.OrderedConsumer(t.Context(), config)
	checkError(t, err)
	exerciseSession(t, api, hook, consumer)
	readRequest(t, api, eventsPath, header, http.StatusServiceUnavailable)
	readRequest(t, api, eventsPath+"/1", header, http.StatusServiceUnavailable)
	api.store.stream = expiredStream{Stream: store.stream}
	readRequest(t, api, eventsPath+"/1", header, http.StatusServiceUnavailable)

	checkEqual(t, page.fetch(t.Context(), store.stream, "events.>", store.stream.OrderedConsumer) != nil, true)
	api.store.stream = readFetchStream{Stream: store.stream}
	api.store.openOrdered = func(context.Context, jetstream.OrderedConsumerConfig) (jetstream.Consumer, error) {
		return consumer, nil
	}
	readRequest(t, api, eventsPath, header, http.StatusServiceUnavailable)
}

type readFetchStream struct{ jetstream.Stream }

func (s readFetchStream) Info(context.Context, ...jetstream.StreamInfoOpt) (*jetstream.StreamInfo, error) {
	return s.CachedInfo(), nil
}
