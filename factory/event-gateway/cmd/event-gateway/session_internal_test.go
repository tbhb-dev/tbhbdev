// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	pushEvent        = "push"
	pong             = "pong"
	pushSubscription = `{"provider":"github","event":"push","repository":"example/repo"}`
)

func exerciseSession(t *testing.T, read readAPI, hook *githubWebhook, consumer jetstream.Consumer) {
	t.Helper()

	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	defer cancel()

	api := newSessionAPI(read, 1200*time.Millisecond)
	server := httptest.NewServer(http.HandlerFunc(api.connect))

	defer server.Close()

	checkSessionHandshakes(t, api)
	checkClose(ctx, t, dialSession(ctx, t, server.URL), websocket.StatusPolicyViolation, "hello timeout")
	timeout := dialSession(ctx, t, server.URL)
	checkError(t, timeout.Write(ctx, websocket.MessageText, []byte(`{"type":"hello","more":true}`)))
	checkClose(ctx, t, timeout, websocket.StatusPolicyViolation, "hello timeout")

	const body = `{"repository":{"full_name":"example/repo"}}`

	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, pushEvent, "before", body), http.StatusAccepted)
	conn := dialSession(ctx, t, server.URL)

	chunk := `{"type":"hello","more":true,"subscriptions":[` + pushSubscription + `]}`
	checkError(t, conn.Write(ctx, websocket.MessageText, []byte(chunk)))

	checkSessionCommands(ctx, t, conn)

	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, "issues", "filtered", body), http.StatusAccepted)
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, pushEvent, "between", body), http.StatusAccepted)

	checkSessionReply(ctx, t, conn, `{"type":"hello","id":"ready","more":false}`, "id", "ready")
	checkSessionReply(ctx, t, conn, "", "seq", "16")

	for _, trial := range []struct {
		id   string
		kind websocket.MessageType
	}{{`"late"`, websocket.MessageText}, {"", websocket.MessageBinary}} {
		checkError(t, conn.Write(ctx, trial.kind, []byte(`{"type":"hello","id":"late"}`)))
		checkEqual(t, string(checkSessionReply(ctx, t, conn, "", codeKey, badFrame)["id"]), trial.id)
	}

	checkSessionFrameLimit(ctx, t, conn, server.URL)

	idle := dialSession(ctx, t, server.URL)
	writeReadTokens(t, api.tokens.path, "second", "second-fixture")
	api.tokens.reload(ctx, api.telemetry.logger)
	expectStatus(t, hook.ServeHTTP, signedRequest(ctx, pushEvent, "revoked", body), http.StatusAccepted)
	checkClose(ctx, t, conn, tokenRevoked, "token revoked")
	checkClose(ctx, t, idle, tokenRevoked, "token revoked")
	writeReadTokens(t, api.tokens.path, "reader", readFixture)
	api.tokens.reload(ctx, api.telemetry.logger)

	socket := dialSession(ctx, t, server.URL)

	close(api.stop)
	checkClose(ctx, t, socket, websocket.StatusGoingAway, "server shutdown")

	read.store.jet.Conn().Close()

	peer := session{stream: read.store.stream, conn: nil, events: nil, subscriptions: nil, replay: nil}
	for _, object := range []sessionObject{nil, {resumeKey: json.RawMessage("0")}} {
		reply := peer.hello(ctx, object)
		checkEqual(t, reply["message"], "stream unavailable")
		checkEqual(t, reply[codeKey], "internal")
	}

	read.store.stream = readFetchStream{Stream: read.store.stream, consumer: consumer}
	failed := httptest.NewServer(http.HandlerFunc(newSessionAPI(read, helloTimeout).connect))

	defer failed.Close()

	socket = dialSession(ctx, t, failed.URL)
	checkSessionReply(ctx, t, socket, `{"type":"hello"}`, typeKey, helloKind)
	checkClose(ctx, t, socket, websocket.StatusInternalError, "delivery failed")
	peer.conn = socket
	checkEqual(t, peer.write(ctx, sessionReply{typeKey: pong}) != nil, true)
}

func checkSessionFrameLimit(ctx context.Context, t *testing.T, conn *websocket.Conn, url string) {
	t.Helper()

	const ping = `{"type":"ping"}`

	checkSessionReply(ctx, t, conn, ping+strings.Repeat(" ", sessionFrameLimit-len(ping)), typeKey, pong)
	other := dialSession(ctx, t, url)
	checkError(t, other.Write(ctx, websocket.MessageText, []byte(strings.Repeat("x", sessionFrameLimit+1))))
	checkClose(ctx, t, other, websocket.StatusMessageTooBig, "")
}

func checkSessionCommands(ctx context.Context, t *testing.T, conn *websocket.Conn) {
	t.Helper()

	maxID := strings.Repeat("x", idLimit)
	for _, trial := range []struct{ send, key, want, id string }{
		{`{"type":"ping"}`, typeKey, pong, ""},
		{`{"type": "ping"}`, typeKey, pong, ""},
		{`{"type":"ping","id":"p1"}`, typeKey, pong, `"p1"`},
		{`{"type":"ping","id":5}`, typeKey, pong, ""},
		{`{"type":"ping","id":"` + maxID + `x"}`, typeKey, pong, ""},
		{`{"type":"ping","id":"` + maxID + `"}`, typeKey, pong, `"` + maxID + `"`},
		{"{\"type\":\"ping\"}\n", typeKey, pong, ""},
		{`[]`, codeKey, badFrame, ""},
		{`{"type":"hello","subscriptions":[{"provider":"github"}]}`, codeKey, "bad_subscription", ""},
		{`{"type":"hello","resume_after":0}`, codeKey, badFrame, ""},
		{`{"type":"hello","more":3,"id":"` + maxID + `x"}`, codeKey, badFrame, ""},
		{`{"type":"hello","subscriptions":{}}`, codeKey, badFrame, ""},
		{`{"type":"hello","subscriptions":[` + strings.Repeat(`{},`, subscriptionLimit-1) + `{}]}`, codeKey, "limit", ""},
	} {
		checkEqual(t, string(checkSessionReply(ctx, t, conn, trial.send, trial.key, trial.want)["id"]), trial.id)
	}

	importReply := checkSessionReply(ctx, t, conn, `{"type":"hello","import":true}`, codeKey, badFrame)
	checkEqual(t, string(importReply["message"]), `"import is allowed only in the first hello frame"`)

	limit := session{stream: nil, conn: nil, events: make(chan jetstream.Msg), subscriptions: nil, replay: nil}
	subs := `[` + strings.Repeat(pushSubscription+",", subscriptionLimit-1) + pushSubscription + `]`
	reply := limit.hello(ctx, sessionObject{"more": json.RawMessage(jsonTrue), subsKey: json.RawMessage(subs)})
	checkEqual(t, reply == nil, true)
	checkEqual(t, len(limit.subscriptions), subscriptionLimit)
}

func TestSessionDeliveryErrors(t *testing.T) {
	t.Parallel()

	var sent []jetstream.Msg

	handler := consumeErrorHandler(func(msg jetstream.Msg) { sent = append(sent, msg) })
	handler(nil, jetstream.ErrNoHeartbeat)
	checkEqual(t, len(sent), 0)
	handler(nil, jetstream.ErrNotJSMessage)
	checkEqual(t, len(sent), 1)
	checkEqual(t, sent[0], nil)

	sub, err := filter.Parse([]byte(pushSubscription))
	checkError(t, err)

	peer := session{stream: nil, conn: nil, events: nil, subscriptions: []filter.Subscription{sub}, replay: nil}
	checkEqual[error](t, peer.deliver(t.Context(), invalidSessionMessage{Msg: nil}), jetstream.ErrNotJSMessage)
}

type invalidSessionMessage struct{ jetstream.Msg }

func (invalidSessionMessage) Data() []byte { return []byte(pushSubscription) }

func (invalidSessionMessage) Metadata() (*jetstream.MsgMetadata, error) {
	return nil, jetstream.ErrNotJSMessage
}

func sessionReplayServer(t *testing.T, store eventStore) *httptest.Server {
	t.Helper()

	var tokens readTokens

	tokens.path = filepath.Join(t.TempDir(), "tokens")
	writeReadTokens(t, tokens.path, "reader", readFixture)
	checkError(t, tokens.load())

	read := readAPI{tokens: &tokens, store: store, telemetry: newTelemetry(slog.DiscardHandler, nil, nil)}
	server := httptest.NewServer(http.HandlerFunc(newSessionAPI(read, helloTimeout).connect))
	t.Cleanup(server.Close)

	return server
}

func publishSessionEvent(t *testing.T, hook *githubWebhook, event, delivery string) {
	t.Helper()

	const body = `{"repository":{"full_name":"example/repo"}}`

	expectStatus(t, hook.ServeHTTP, signedRequest(t.Context(), event, delivery, body), http.StatusAccepted)
}

func TestSessionResume(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	server := sessionReplayServer(t, store)

	for _, delivery := range []string{"expired", "saved", "first", "second"} {
		publishSessionEvent(t, hook, pushEvent, delivery)
	}

	publishSessionEvent(t, hook, issuesEvent, "filtered")

	const (
		start = `{"type":"hello","resume_after":2,"more":true}`
		final = `{"type":"hello","subscriptions":[` + pushSubscription + `]}`
		ping  = `{"type":"ping"}`
	)

	conn := dialSession(ctx, t, server.URL)
	for _, invalid := range []string{`-1`, `"2"`, `1.5`, `1e2`, `null`, `18446744073709551616`} {
		checkSessionReply(ctx, t, conn, `{"type":"hello","resume_after":`+invalid+`}`, codeKey, badFrame)
	}

	checkError(t, conn.Write(ctx, websocket.MessageText, []byte(start)))
	checkSessionReply(ctx, t, conn, ping, typeKey, pong)
	publishSessionEvent(t, hook, pushEvent, "live")

	checkSessionReply(ctx, t, conn, final, subsKey, "1")
	checkSessionReply(ctx, t, conn, "", "seq", "3")
	checkSessionReply(ctx, t, conn, "", "seq", "4")
	checkCaughtUp(ctx, t, conn, "5")
	checkSessionReply(ctx, t, conn, "", "seq", "6")
	checkSessionReply(ctx, t, conn, ping, typeKey, pong)

	checkError(t, store.stream.DeleteMsg(ctx, 1))
	checkError(t, store.stream.DeleteMsg(ctx, 2))

	gapped := dialSession(ctx, t, server.URL)
	checkSessionReply(ctx, t, gapped,
		`{"type":"hello","resume_after":0,"subscriptions":[`+pushSubscription+`]}`, typeKey, helloKind)
	gap := checkSessionReply(ctx, t, gapped, "", typeKey, "gap")
	checkEqual(t, string(gap["from"]), "1")
	checkEqual(t, string(gap["to"]), "2")

	for _, seq := range []string{"3", "4", "6"} {
		checkSessionReply(ctx, t, gapped, "", "seq", seq)
	}

	checkCaughtUp(ctx, t, gapped, "6")
	checkSessionReply(ctx, t, gapped, ping, typeKey, pong)
}

func checkCaughtUp(ctx context.Context, t *testing.T, conn *websocket.Conn, seq string) {
	t.Helper()

	reply := checkSessionReply(ctx, t, conn, "", typeKey, "caught_up")
	checkEqual(t, string(reply["seq"]), seq)
}

func TestSessionResumeWithoutBacklog(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	server := sessionReplayServer(t, store)

	for _, trial := range []struct{ resume, last, live string }{
		{"0", "0", "1"}, {"1", "1", "2"}, {"18446744073709551615", "2", ""},
	} {
		conn := dialSession(ctx, t, server.URL)
		hello := `{"type":"hello","resume_after":` + trial.resume + `,"subscriptions":[` + pushSubscription + `]}`
		checkSessionReply(ctx, t, conn, hello, typeKey, helloKind)
		checkCaughtUp(ctx, t, conn, trial.last)
		publishSessionEvent(t, hook, pushEvent, trial.resume)

		if trial.live != "" {
			checkSessionReply(ctx, t, conn, "", "seq", trial.live)
		}

		checkSessionReply(ctx, t, conn, `{"type":"ping"}`, typeKey, pong)
	}
}

type sessionOpeningStream struct {
	jetstream.Stream

	publish func()
}

//nolint:ireturn // Match the JetStream stream interface.
func (s sessionOpeningStream) OrderedConsumer(
	ctx context.Context, config jetstream.OrderedConsumerConfig,
) (jetstream.Consumer, error) {
	s.publish()

	return s.Stream.OrderedConsumer(ctx, config) //nolint:wrapcheck // Preserve the stream's result.
}

func TestSessionResumeMissingTail(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	publishSessionEvent(t, hook, pushEvent, "kept")
	publishSessionEvent(t, hook, pushEvent, "removed")
	checkError(t, store.stream.DeleteMsg(ctx, 2))

	const hello = `{"type":"hello","resume_after":0,"subscriptions":[` + pushSubscription + `]}`

	server := sessionReplayServer(t, store)
	conn := dialSession(ctx, t, server.URL)
	checkSessionReply(ctx, t, conn, hello, typeKey, helloKind)
	checkSessionReply(ctx, t, conn, "", "seq", "1")
	checkCaughtUp(ctx, t, conn, "2")

	opening := store
	opening.stream = sessionOpeningStream{Stream: store.stream, publish: func() {
		publishSessionEvent(t, hook, pushEvent, "concurrent")
	}}
	server = sessionReplayServer(t, opening)
	conn = dialSession(ctx, t, server.URL)
	checkSessionReply(ctx, t, conn, hello, typeKey, helloKind)
	checkSessionReply(ctx, t, conn, "", "seq", "1")
	checkCaughtUp(ctx, t, conn, "2")
	checkSessionReply(ctx, t, conn, "", "seq", "3")

	checkError(t, store.stream.Purge(ctx))
	server = sessionReplayServer(t, store)
	conn = dialSession(ctx, t, server.URL)
	checkSessionReply(ctx, t, conn, hello, typeKey, helloKind)
	gap := checkSessionReply(ctx, t, conn, "", typeKey, "gap")
	checkEqual(t, string(gap["from"]), "1")
	checkEqual(t, string(gap["to"]), "3")
	checkCaughtUp(ctx, t, conn, "3")
	publishSessionEvent(t, hook, pushEvent, "live")
	checkSessionReply(ctx, t, conn, "", "seq", "4")
}

func TestSessionImport(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	server := sessionReplayServer(t, store)
	conn := dialSession(ctx, t, server.URL)

	for _, invalid := range []string{`false`, `null`, `"true"`, `{}`} {
		reply := checkSessionReply(ctx, t, conn, `{"type":"hello","import":`+invalid+`}`, codeKey, badFrame)
		checkEqual(t, string(reply["message"]), `"import must be true"`)
	}

	reply := checkSessionReply(ctx, t, conn, `{"type":"hello","resume_after":0,"import":true}`, codeKey, badFrame)
	checkEqual(t, string(reply["message"]), `"import and resume_after are mutually exclusive"`)

	publishSessionEvent(t, hook, pushEvent, "expired")
	publishSessionEvent(t, hook, pushEvent, "first")
	publishSessionEvent(t, hook, issuesEvent, "unmatched-between")
	publishSessionEvent(t, hook, pushEvent, "second")
	publishSessionEvent(t, hook, issuesEvent, "unmatched-tail")
	checkError(t, store.stream.DeleteMsg(ctx, 1))

	const (
		start = `{"type":"hello","import":true,"more":true}`
		final = `{"type":"hello","subscriptions":[` + pushSubscription + `]}`
		ping  = `{"type":"ping"}`
	)

	checkError(t, conn.Write(ctx, websocket.MessageText, []byte(start)))
	checkSessionReply(ctx, t, conn, ping, typeKey, pong)
	publishSessionEvent(t, hook, pushEvent, "live")

	checkSessionReply(ctx, t, conn, final, subsKey, "1")
	checkSessionReply(ctx, t, conn, "", "seq", "2")
	checkSessionReply(ctx, t, conn, "", "seq", "4")
	checkCaughtUp(ctx, t, conn, "5")
	checkSessionReply(ctx, t, conn, "", "seq", "6")
	checkSessionReply(ctx, t, conn, ping, typeKey, pong)
}

func TestSessionImportWithoutSubscriptions(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), shutdownTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	server := sessionReplayServer(t, store)

	const hello = `{"type":"hello","import":true}`

	for _, seq := range []string{"0", "1"} {
		conn := dialSession(ctx, t, server.URL)
		checkSessionReply(ctx, t, conn, hello, subsKey, "0")
		checkCaughtUp(ctx, t, conn, seq)
		publishSessionEvent(t, hook, pushEvent, seq)
		checkSessionReply(ctx, t, conn, `{"type":"ping"}`, typeKey, pong)
	}
}

func checkSessionHandshakes(t *testing.T, api *sessionAPI) {
	t.Helper()

	const bearer = "Bearer " + readFixture

	upgrade := http.Header{authorization: {bearer}, "Connection": {"upgrade"}, upgradeHeader: {"websocket"}}
	protocol := upgrade.Clone()
	protocol.Set("Sec-WebSocket-Protocol", sessionProtocol)

	for _, trial := range []struct {
		headers      http.Header
		path, method string
		status       int
	}{
		{nil, "/other", http.MethodGet, http.StatusNotFound},
		{nil, sessionPath, http.MethodHead, http.StatusMethodNotAllowed},
		{nil, sessionPath, http.MethodGet, http.StatusUnauthorized},
		{http.Header{authorization: {"Bearer wrong"}}, sessionPath, http.MethodGet, http.StatusUnauthorized},
		{http.Header{authorization: {bearer}}, sessionPath, http.MethodGet, http.StatusUpgradeRequired},
		{http.Header{authorization: {bearer}, "Connection": {upgradeHeader}}, sessionPath, http.MethodGet, 426},
		{upgrade, sessionPath, http.MethodGet, http.StatusBadRequest},
		{protocol, sessionPath, http.MethodGet, http.StatusBadRequest},
	} {
		req := httptest.NewRequestWithContext(t.Context(), trial.method, trial.path, nil)
		req.Header = trial.headers
		expectStatus(t, api.connect, req, trial.status)
	}
}

func checkClose(ctx context.Context, t *testing.T, conn *websocket.Conn, code websocket.StatusCode, reason string) {
	t.Helper()

	_, _, err := conn.Read(ctx)
	checkEqual(t, websocket.CloseStatus(err), code)
	checkEqual(t, strings.Contains(err.Error(), reason), true)
}

func dialSession(ctx context.Context, t *testing.T, url string) *websocket.Conn {
	t.Helper()
	//nolint:exhaustruct_v5,bodyclose // A successful upgrade transfers ownership to the socket.
	conn, _, err := websocket.Dial(ctx, url+sessionPath, &websocket.DialOptions{
		HTTPHeader: http.Header{authorization: {"Bearer " + readFixture}}, Subprotocols: []string{sessionProtocol},
	})
	checkError(t, err)
	checkEqual(t, conn.Subprotocol(), sessionProtocol)
	//nolint:errcheck // The server may have already closed the socket.
	t.Cleanup(func() { _ = conn.CloseNow() })

	return conn
}

func checkSessionReply(ctx context.Context, t *testing.T, conn *websocket.Conn, send, key, want string) sessionObject {
	t.Helper()

	if send != "" {
		checkError(t, conn.Write(ctx, websocket.MessageText, []byte(send)))
	}

	_, data, err := conn.Read(ctx)
	checkError(t, err)

	reply := sessionJSON[sessionObject](data)
	checkEqual(t, strings.Trim(string(reply[key]), `"`), want)

	return reply
}
