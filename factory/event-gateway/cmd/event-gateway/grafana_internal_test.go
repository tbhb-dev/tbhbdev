// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

const (
	hostAlert = `{"status":"firing","labels":{"alertname":"CPU","host_name":"web-1"},` +
		`"valueString":"92","startsAt":"2026-01-01T00:00:00Z","endsAt":"0001-01-01T00:00:00Z",` +
		`"fingerprint":"abc","panelURL":"HTTPS://grafana.example/panel","dashboardURL":"https://grafana.example/d",` +
		`"annotations":{"secret":"hidden"}}`
	accountAlert = `{"status":"resolved","labels":{"agent_account":"acct_1"},"dashboardURL":"ftp://x","panelURL":5}`
	zeroDigest   = "0000000000000000000000000000000000000000000000000000000000000000"
)

func grafanaHook(t *testing.T) (*grafanaWebhook, eventStore) {
	t.Helper()

	store := testStore(t, retention)
	hook, err := newGrafanaWebhook([]byte(fixtureKey))
	checkError(t, err)

	hook.store, hook.telemetry = store, newTelemetry(slog.DiscardHandler, nil, nil)

	return hook, store
}

func grafanaSigned(ctx context.Context, body string) *http.Request {
	req := httptest.NewRequestWithContext(ctx, http.MethodPost, grafanaPath, strings.NewReader(body))
	mac := hmac.New(sha256.New, []byte(fixtureKey))

	_, _ = io.WriteString(mac, body)
	req.Header.Set(grafanaSignature, hex.EncodeToString(mac.Sum(nil)))

	return req
}

func alertsBody(entries ...string) string {
	return `{"alerts":[` + strings.Join(entries, ",") + `]}`
}

type failingBody struct{}

func (failingBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }

func TestGrafanaRequests(t *testing.T) {
	t.Parallel()

	hook, _ := grafanaHook(t)
	valid := func(s string) string { return s }
	wrong := func(string) string { return zeroDigest }
	alerts := func(n int, entry string) string { return alertsBody(strings.Repeat(entry+",", n-1) + entry) }
	scope := func(s string) string { return `{"status":"firing","labels":{"host_name":` + s + `}}` }

	for _, test := range []struct {
		sign   func(string) string
		body   string
		status int
	}{
		{valid, alertsBody(hostAlert), http.StatusAccepted},
		{strings.ToUpper, alertsBody(hostAlert), http.StatusAccepted},
		{wrong, alertsBody(hostAlert), http.StatusUnauthorized},
		{func(string) string { return strings.Repeat("g", 64) }, alertsBody(hostAlert), http.StatusUnauthorized},
		{func(string) string { return "" }, alertsBody(hostAlert), http.StatusUnauthorized},
		{wrong, "not json", http.StatusUnauthorized}, // The signature fails before the body is parsed.
		{valid, "not json", http.StatusBadRequest},
		{valid, `{"alerts":[]}`, http.StatusBadRequest},
		{valid, `{"alerts":{}}`, http.StatusBadRequest},
		{valid, `{}`, http.StatusBadRequest},
		{valid, alerts(101, hostAlert), http.StatusBadRequest},
		{valid, alerts(100, `{"status":"firing"}`), http.StatusAccepted},
		{valid, alertsBody(`{"status":"pending","labels":{"host_name":"h"}}`, `{"status":5}`, `"text"`), http.StatusAccepted},
		{valid, alertsBody(scope(`"bad value"`), scope("7"), `{"status":"firing","labels":"x"}`), http.StatusAccepted},
	} {
		req := grafanaSigned(t.Context(), test.body)
		req.Header.Set(grafanaSignature, test.sign(req.Header.Get(grafanaSignature)))
		expectStatus(t, hook.ServeHTTP, req, test.status)
	}
}

func TestGrafanaRequestErrors(t *testing.T) {
	t.Parallel()

	hook, _ := grafanaHook(t)
	ctx, cancel := context.WithCancel(t.Context())
	huge := grafanaSigned(ctx, strings.Repeat("x", grafanaBodyCap+1))
	huge.ContentLength = -1
	declared := grafanaSigned(ctx, "{}")
	declared.ContentLength = grafanaBodyCap + 1
	broken := grafanaSigned(ctx, "{}")
	broken.Body = io.NopCloser(failingBody{})
	broken.ContentLength = -1

	expectStatus(t, hook.ServeHTTP, huge, http.StatusRequestEntityTooLarge)
	expectStatus(t, hook.ServeHTTP, declared, http.StatusRequestEntityTooLarge)
	expectStatus(t, hook.ServeHTTP, broken, http.StatusBadRequest)

	get := httptest.NewRequestWithContext(ctx, http.MethodGet, grafanaPath, nil)
	expectStatus(t, hook.ServeHTTP, get, http.StatusMethodNotAllowed)

	// A store that cannot commit answers 503.
	cancel()
	expectStatus(t, hook.ServeHTTP, grafanaSigned(ctx, alertsBody(hostAlert)), http.StatusServiceUnavailable)
}

func TestGrafanaStoredEvents(t *testing.T) {
	t.Parallel()

	hook, store := grafanaHook(t)
	ctx := t.Context()
	both := `{"status":"firing","labels":{"host_name":"h1","agent_account":"a1"},"dashboardURL":"http://d"}`
	fallback := `{"status":"firing","labels":{"host_name":"bad value","agent_account":"a2"}}`
	body := alertsBody(`{"status":"firing"}`, hostAlert, accountAlert, both, fallback)

	// A retried body is a duplicate.
	expectStatus(t, hook.ServeHTTP, grafanaSigned(ctx, body), http.StatusAccepted)
	expectStatus(t, hook.ServeHTTP, grafanaSigned(ctx, body), http.StatusAccepted)

	info, infoErr := store.stream.Info(ctx)
	checkError(t, infoErr)
	checkEqual(t, info.State.Msgs, uint64(4))

	for index, want := range []string{
		`hosts/web-1|"payload":{"alertname":"CPU","ends_at":"0001-01-01T00:00:00Z","fingerprint":"abc",` +
			`"html_url":"HTTPS://grafana.example/panel","kind":"hosts","scope":"web-1",` +
			`"starts_at":"2026-01-01T00:00:00Z","status":"firing","value":"92"}}`,
		`capacity/acct_1|"payload":{"kind":"capacity","scope":"acct_1","status":"resolved"}}`,
		`hosts/h1|"payload":{"html_url":"http://d","kind":"hosts","scope":"h1","status":"firing"}}`,
		`capacity/a2|"payload":{"kind":"capacity","scope":"a2","status":"firing"}}`,
	} {
		msg, err := store.stream.GetMsg(ctx, uint64(index+1))
		checkError(t, err)

		data := string(msg.Data)
		repository, payload, _ := strings.Cut(want, "|")
		sum := sha256.Sum256([]byte(body + ":" + strconv.Itoa(index+1)))

		checkEqual(t, strings.Contains(data, `"repository":"`+repository+`"`), true)
		checkEqual(t, strings.HasSuffix(data, payload), true)
		// The index counts the skipped first entry; the ID is the first 16 bytes of the hash.
		checkEqual(t, strings.Contains(data, `"delivery":"gf-`+hex.EncodeToString(sum[:16])+`"`), true)
		checkEqual(t, strings.HasPrefix(msg.Subject, "events.grafana.alert."), true)
	}

	// A changed body under an existing ID keeps the store's conflict behavior.
	clashBody := alertsBody(hostAlert)
	clash, err := normalizeGrafana([]byte(hostAlert), grafanaDelivery([]byte(clashBody), 0), time.Now())
	checkError(t, err)

	clash.Payload = []byte(`{"x":2}`)
	checkError(t, store.publish(ctx, clash))
	expectStatus(t, hook.ServeHTTP, grafanaSigned(ctx, clashBody), http.StatusConflict)
}

func TestGrafanaWebhookRequiresSecret(t *testing.T) {
	t.Parallel()

	for _, secret := range [][]byte{nil, {}, []byte(" \t\n")} {
		_, err := newGrafanaWebhook(secret)
		checkEqual(t, errors.Is(err, errGrafanaSecret), true)
	}
}
