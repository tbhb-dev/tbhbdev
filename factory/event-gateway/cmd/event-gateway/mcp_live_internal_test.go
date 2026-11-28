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
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	liveTestTimeout    = 3 * time.Second
	liveTestBuffer     = 8
	issuesSubscription = `{"provider":"github","event":"issues","repository":"example/repo"}`
)

func waitLiveSignal(ctx context.Context, t *testing.T, signal <-chan struct{}, label string) {
	t.Helper()

	select {
	case <-signal:
	case <-ctx.Done():
		t.Fatalf("timed out waiting for %s", label)
	}
}

func waitLiveNotification(ctx context.Context, t *testing.T, notifications <-chan *jsonrpc.Request) channelParameters {
	t.Helper()

	select {
	case req := <-notifications:
		var params channelParameters
		checkError(t, json.Unmarshal(req.Params, &params))

		return params
	case <-ctx.Done():
		t.Fatal("timed out waiting for channel notification")

		return channelParameters{Meta: nil, Content: ""}
	}
}

func TestMCPGatewayLiveSubscriptions(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), liveTestTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	opened := make(chan struct{}, liveTestBuffer)

	observed := store
	observed.stream = sessionOpeningStream{
		Stream:  store.stream,
		publish: func() { opened <- struct{}{} },
	}
	server := sessionReplayServer(t, observed)

	subs := new(mcpSubscriptions)
	notifications := make(chan *jsonrpc.Request, liveTestBuffer)
	session, peer, notifier := connectMCPWithSubscriptions(t, subs, claudeCodeClient, notifications)

	done := make(chan error, 1)
	go func() {
		done <- runMCPGateway(ctx, slog.New(slog.DiscardHandler), server.URL+sessionPath, readFixture, subs,
			func(event channelEvent) bool {
				return notifier.deliverChannel(ctx, peer, event) == nil
			},
			func(context.Context, time.Duration) bool {
				t.Error("live subscription change scheduled a backoff retry")

				return false
			})
	}()

	waitLiveSignal(ctx, t, opened, "initial gateway connection")

	checkEqual(t, callMCPTool(t, session, subscribeTool, pushSubscription).IsError, false)
	waitLiveSignal(ctx, t, opened, "subscribe redial")

	publishSessionEvent(t, hook, pushEvent, "after-subscribe")

	got := waitLiveNotification(ctx, t, notifications)
	checkEqual(t, got.Meta[channelProviderKey], "github")
	checkEqual(t, got.Meta[channelEventKey], pushEvent)
	checkEqual(t, got.Meta["delivery"], "after-subscribe")

	checkEqual(t, callMCPTool(t, session, subscribeTool, issuesSubscription).IsError, false)
	waitLiveSignal(ctx, t, opened, "second subscribe redial")

	checkEqual(t, callMCPTool(t, session, unsubscribeTool, pushSubscription).IsError, false)
	waitLiveSignal(ctx, t, opened, "unsubscribe redial")

	publishSessionEvent(t, hook, pushEvent, "after-unsubscribe")
	publishSessionEvent(t, hook, issuesEvent, "still-subscribed")

	got = waitLiveNotification(ctx, t, notifications)
	checkEqual(t, got.Meta[channelEventKey], issuesEvent)
	checkEqual(t, got.Meta["delivery"], "still-subscribed")

	cancel()
	checkError(t, <-done)
	checkEqual(t, len(notifications), 0)
}

func TestMCPGatewayLiveCursorReconnect(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), liveTestTimeout)
	defer cancel()

	store := testStore(t, retention)
	hook := testWebhook(t, store, defaultBodyCap)
	publishSessionEvent(t, hook, pushEvent, "before-connect")

	var tokens readTokens

	tokens.path = filepath.Join(t.TempDir(), "tokens")
	writeReadTokens(t, tokens.path, "reader", readFixture)
	checkError(t, tokens.load())

	read := readAPI{tokens: &tokens, store: store, telemetry: newTelemetry(slog.DiscardHandler, nil, nil)}
	first := newSessionAPI(read, helloTimeout)
	second := newSessionAPI(read, helloTimeout)

	var (
		connections atomic.Int32
		stopFirst   sync.Once
	)

	first.store.stream = sessionOpeningStream{
		Stream:  store.stream,
		publish: func() { stopFirst.Do(func() { close(first.stop) }) },
	}

	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, req *http.Request) {
		if connections.Add(1) == 1 {
			first.connect(writer, req)

			return
		}

		second.connect(writer, req)
	}))
	t.Cleanup(server.Close)

	sub, err := filter.Parse([]byte(pushSubscription))
	checkError(t, err)

	subs := new(mcpSubscriptions)
	checkEqual(t, subs.set(sub, true), true)

	delivered := make(chan channelEvent, liveTestBuffer)
	reconnected := make(chan struct{}, 1)
	disconnected := make(chan struct{}, 1)
	resume := make(chan struct{})
	done := make(chan error, 1)

	go func() {
		done <- runMCPGateway(ctx, slog.New(slog.DiscardHandler), server.URL+sessionPath, readFixture, subs,
			func(event channelEvent) bool {
				if event.provider == mcpServerName {
					if string(event.payload) == string(gatewayReconnected().channelEvent().payload) {
						reconnected <- struct{}{}
					}

					return true
				}

				delivered <- event

				return true
			},
			func(waitCtx context.Context, _ time.Duration) bool {
				disconnected <- struct{}{}

				select {
				case <-resume:
					return true
				case <-waitCtx.Done():
					return false
				}
			})
	}()

	waitLiveSignal(ctx, t, disconnected, "first connection disconnect")
	publishSessionEvent(t, hook, pushEvent, "while-disconnected")
	close(resume)
	waitLiveSignal(ctx, t, reconnected, "second connection ready")
	publishSessionEvent(t, hook, pushEvent, "after-reconnect")

	for _, want := range []string{"while-disconnected", "after-reconnect"} {
		select {
		case event := <-delivered:
			checkEqual(t, event.delivery, want)
		case <-ctx.Done():
			t.Fatalf("timed out waiting for event %s", want)
		}
	}

	cancel()
	checkError(t, <-done)
	checkEqual(t, connections.Load(), int32(2))
	checkEqual(t, len(delivered), 0)
}

func TestMCPGatewayNoOpSubscribe(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), liveTestTimeout)
	defer cancel()

	pushSub, err := filter.Parse([]byte(pushSubscription))
	checkError(t, err)

	issuesSub, err := filter.Parse([]byte(issuesSubscription))
	checkError(t, err)

	subs := new(mcpSubscriptions)
	checkEqual(t, subs.set(pushSub, true), true)

	connected := make(chan struct{}, 1)
	sendEvent := make(chan struct{})
	server, requests := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		receiveGatewayHello(ctx, t, conn, "", "")
		checkError(t, conn.Write(ctx, websocket.MessageText, []byte(`{"type":"hello","subscriptions":1,"seq":0}`)))
		connected <- struct{}{}

		select {
		case <-sendEvent:
			event := []byte(gatewayTestEvent(1, "github", "same-connection"))
			checkError(t, conn.Write(ctx, websocket.MessageText, event))
			<-ctx.Done()
		case <-ctx.Done():
		}
	})

	delivered := make(chan channelEvent, liveTestBuffer)
	done := make(chan error, 1)

	go func() {
		done <- runMCPGateway(ctx, slog.New(slog.DiscardHandler), server.URL+sessionPath, mcpFixture, subs,
			func(event channelEvent) bool {
				delivered <- event

				return true
			},
			func(context.Context, time.Duration) bool {
				t.Error("no-op subscription scheduled a retry")

				return false
			})
	}()

	waitLiveSignal(ctx, t, connected, "initial gateway connection")
	checkEqual(t, subs.set(pushSub, true), false)
	checkEqual(t, subs.set(issuesSub, false), false)
	close(sendEvent)

	select {
	case event := <-delivered:
		checkEqual(t, event.delivery, "same-connection")
	case <-ctx.Done():
		t.Fatal("timed out waiting for event on existing connection")
	}

	cancel()
	checkError(t, <-done)
	checkEqual(t, requests.Load(), int32(1))
	checkEqual(t, len(delivered), 0)
}
