// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:words synctest

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/coder/websocket"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const mcpFixture = "fixture-mcp-token"

func fakeMCPGateway(
	t *testing.T, statuses []int, handle func(*websocket.Conn),
) (*httptest.Server, *atomic.Int32) {
	t.Helper()

	requests := new(atomic.Int32)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, req *http.Request) {
		status := statuses[min(int(requests.Add(1))-1, len(statuses)-1)]

		if req.Header.Get(authorization) != "Bearer "+mcpFixture ||
			req.Header.Get("Sec-WebSocket-Protocol") != sessionProtocol || req.URL.Path != sessionPath {
			t.Error("gateway handshake did not match")
		}

		if status != http.StatusSwitchingProtocols {
			if status == http.StatusFound {
				writer.Header().Set("Location", sessionPath+"?redirected")
			}

			http.Error(writer, mcpFixture, status)

			return
		}

		options := new(websocket.AcceptOptions)
		options.Subprotocols = []string{sessionProtocol}

		conn, err := websocket.Accept(writer, req, options)
		if err != nil {
			t.Error("gateway upgrade failed")

			return
		}
		defer func() {
			closeErr := conn.CloseNow()
			if closeErr != nil && !errors.Is(closeErr, net.ErrClosed) {
				t.Error("gateway socket cleanup failed")
			}
		}()

		handle(conn)
	}))
	t.Cleanup(server.Close)

	return server, requests
}

func receiveGatewayHello(
	ctx context.Context,
	t *testing.T,
	conn *websocket.Conn,
	resume, imported string,
) sessionObject {
	t.Helper()

	kind, data, err := conn.Read(ctx)
	checkEqual(t, err == nil, true)
	checkEqual(t, kind, websocket.MessageText)
	checkEqual(t, len(data) <= sessionFrameLimit, true)

	frame := *sessionJSON[sessionObject](data)
	checkEqual(t, string(frame[typeKey]) == `"hello"`, true)
	checkEqual(t, string(frame[resumeKey]), resume)
	checkEqual(t, string(frame["import"]), imported)

	return frame
}

func TestMCPGatewayFrames(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	subs := gatewayFrameSubscriptions(t)

	checkMCPGatewayHelloBoundary(t, subs[:12])

	server, requests := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		frame := receiveGatewayHello(ctx, t, conn, "0", "")
		checkEqual(t, string(frame["more"]) == "true", true)
		got := *sessionJSON[[]filter.Subscription](frame[subsKey])
		frame = receiveGatewayHello(ctx, t, conn, "", "")
		checkEqual(t, frame["more"] == nil, true)
		got = append(got, *sessionJSON[[]filter.Subscription](frame[subsKey])...)
		checkEqual(t, reflect.DeepEqual(got, subs), true)

		for _, data := range []string{
			`{"type":"hello","subscriptions":16}`, `{"type":"pong"}`, `{"type":"caught_up","seq":17}`,
			`{"type":"error","code":"bad_message"}`, `{"type":"unknown"}`, `{"type":"gap","from":3,"to":5}`,
		} {
			checkEqual(t, conn.Write(ctx, websocket.MessageText, []byte(data)) == nil, true)
		}

		checkEqual(t, conn.Write(ctx, websocket.MessageBinary, []byte(`{"type":"gap"}`)) == nil, true)

		event := sequenceEnvelope(19, []byte(`{"type":"event","provider":"github","event":"issues","delivery":"id",`+
			`"repository":"owner/repo","payload":{"action":"opened","body":"<>&"}}`))
		checkEqual(t, conn.Write(ctx, websocket.MessageText, event) == nil, true)
	})

	var messages []channelParameters

	state := new(mcpSubscriptions)
	state.subs = subs
	state.advanceCursor(0)
	err := runMCPGateway(ctx, slog.New(slog.DiscardHandler), server.URL+sessionPath, mcpFixture, state,
		func(event channelEvent) bool {
			messages = append(messages, channelParams(event))

			return true
		},
		func(context.Context, time.Duration) bool {
			cancel()

			return false
		})
	checkEqual(t, err == nil, true)
	checkEqual(t, requests.Load(), 1)

	checkGatewayFrameNotifications(t, messages)
}

func gatewayFrameSubscriptions(t *testing.T) []filter.Subscription {
	t.Helper()

	const (
		subscriptionCount = 16
		labelSize         = 128
	)

	subs := make([]filter.Subscription, 0, subscriptionCount)
	for i := range subscriptionCount {
		raw := fmt.Sprintf(`{"provider":"github","event":"issues","repository":"owner/repo%d","label":["%s","%s"]}`,
			i, strings.Repeat("&", labelSize), strings.Repeat("<", labelSize))
		sub, err := filter.Parse([]byte(raw))
		checkError(t, err)

		subs = append(subs, sub)
	}

	return subs
}

func checkGatewayFrameNotifications(t *testing.T, messages []channelParameters) {
	t.Helper()

	want := *sessionJSON[[]channelParameters]([]byte(`[
		{"meta":{"provider":"event-gateway","event":"notice","delivery":""},
		"content":"{\"level\":\"warning\",\"text\":\"Gateway events 3 through 5 are no longer available.\"}"},
		{"meta":{"provider":"github","event":"issues","delivery":"id","repository":"owner/repo","action":"opened"},
		"content":"{\"action\":\"opened\",\"body\":\"\\u003c\\u003e\\u0026\"}"},
		{"meta":{"provider":"event-gateway","event":"notice","delivery":""},
		"content":"{\"level\":\"warning\",\"text\":\"Gateway connection lost.\"}"}
	]`))
	checkEqual(t, reflect.DeepEqual(messages, want), true)
}

func checkMCPGatewayHelloBoundary(t *testing.T, subscriptions []filter.Subscription) {
	t.Helper()

	subs := slices.Clone(subscriptions)
	subs[0].Repository += "xxxx" // Bring the combined frame from 4092 to 4096 bytes.
	frames, err := gatewayHelloFrames(subs, nil, false)
	checkEqual(t, err == nil, true)
	checkEqual(t, len(frames), 1)
	checkEqual(t, len(frames[0]), sessionFrameLimit)

	frames, err = gatewayHelloFrames(subs, nil, true)
	checkError(t, err)
	checkEqual(t, len(frames), 2)
	checkEqual(t, len(frames[0]) <= sessionFrameLimit, true)

	subs[0].Repository += "x"
	frames, err = gatewayHelloFrames(subs, nil, false)
	checkEqual(t, err == nil, true)
	checkEqual(t, len(frames), 2)

	for _, frame := range frames {
		checkEqual(t, len(frame) <= sessionFrameLimit, true)
	}
}

func TestMCPGatewayReconnect(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	sub, err := filter.Parse([]byte(`{"provider":"github","event":"issues","repository":"owner/repo"}`))
	checkError(t, err)

	server, requests := reconnectTestGateway(ctx, t, sub)

	var (
		logs         bytes.Buffer
		subs         mcpSubscriptions
		notices      []channelNotice
		seconds      []time.Duration
		noticeCounts []int
	)

	options := &slog.HandlerOptions{Level: slog.LevelDebug, AddSource: false, ReplaceAttr: nil}
	logger := slog.New(slog.NewTextHandler(&logs, options))
	err = runMCPGateway(ctx, logger, server.URL+sessionPath, mcpFixture, &subs, func(event channelEvent) bool {
		notices = append(notices, *sessionJSON[channelNotice](event.payload))

		return true
	}, func(_ context.Context, delay time.Duration) bool {
		seconds = append(seconds, delay/time.Second)
		noticeCounts = append(noticeCounts, len(notices))

		if len(seconds) == 3 {
			checkEqual(t, subs.set(sub, true), true)
		}

		return len(seconds) <= 10
	})
	checkEqual(t, errors.Is(err, errGatewayToken), true)
	checkEqual(t, requests.Load(), 11)
	checkGatewayReconnectSchedule(t, seconds, noticeCounts, notices)

	checkEqual(t, strings.Contains(logs.String(), "gateway retry scheduled"), true)
	checkEqual(t, strings.Contains(logs.String(), mcpFixture), false)
}

func reconnectTestGateway(
	ctx context.Context,
	t *testing.T,
	sub filter.Subscription,
) (*httptest.Server, *atomic.Int32) {
	t.Helper()

	const accept, fail = http.StatusSwitchingProtocols, http.StatusForbidden

	var connections atomic.Int32

	statuses := []int{fail, accept, accept, fail, fail, fail, fail, fail, fail, accept, accept}
	server, requests := fakeMCPGateway(t, statuses, func(conn *websocket.Conn) {
		frame := receiveGatewayHello(ctx, t, conn, "", "")
		got := *sessionJSON[[]filter.Subscription](frame[subsKey])

		switch connections.Add(1) {
		case 1: // Drop before the first frame, so backoff keeps increasing.
			checkEqual(t, len(got), 0)
		case 2:
			hello := []byte(`{"type":"hello","subscriptions":0}`)

			checkEqual(t, len(got), 0)
			checkEqual(t, conn.Write(ctx, websocket.MessageText, hello) == nil, true)
		case 3:
			gap := []byte(`{"type":"gap","from":8,"to":13}`)

			checkEqual(t, reflect.DeepEqual(got, []filter.Subscription{sub}), true)
			checkEqual(t, conn.Write(ctx, websocket.MessageBinary, []byte("ready")) == nil, true)
			checkEqual(t, conn.Write(ctx, websocket.MessageText, gap) == nil, true)
		default:
			checkEqual(t, conn.Close(tokenRevoked, mcpFixture) == nil, true)
		}
	})

	return server, requests
}

func checkGatewayReconnectSchedule(t *testing.T, seconds []time.Duration, noticeCounts []int, notices []channelNotice) {
	t.Helper()

	checkEqual(t, slices.Equal(seconds, []time.Duration{1, 2, 1, 2, 4, 8, 16, 30, 30, 1}), true)
	checkEqual(t, slices.Equal(noticeCounts, []int{0, 0, 1, 1, 1, 1, 1, 1, 1, 4}), true)
	checkEqual(t, slices.Equal(notices, []channelNotice{
		{Level: channelWarning, Text: "Gateway connection lost."},
		{Level: "info", Text: "Gateway reconnected."},
		{Level: channelWarning, Text: "Gateway events 8 through 13 are no longer available."},
		{Level: channelWarning, Text: "Gateway connection lost."},
		{Level: channelWarning, Text: "Gateway token was rejected."},
	}), true)
}

func TestMCPGatewayImport(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	const (
		subscriptionCount = 64
		caughtUp          = `{"type":"caught_up","seq":12}`
	)

	dir, _, legacy := awrelStatePaths(t)
	document := mcpStateDocument(mcpStateTestSubscriptions(subscriptionCount))
	checkError(t, os.WriteFile(legacy, []byte(document), 0o600))

	logger := slog.New(slog.DiscardHandler)
	subs := restoreMCPSubscriptions(ctx, logger, awrelStateEnv(dir, mcpStateTestKey))
	want := subs.list()
	checkEqual(t, len(want), subscriptionCount)

	steps := [...]struct{ resume, imported, reply string }{
		{"", jsonTrue, ""}, {"", jsonTrue, caughtUp}, {"12", "", ""},
	}

	var connections atomic.Int32

	server, requests := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		step := steps[connections.Add(1)-1]
		frame := receiveGatewayHello(ctx, t, conn, step.resume, step.imported)
		checkEqual(t, string(frame["more"]), jsonTrue)
		got := *sessionJSON[[]filter.Subscription](frame[subsKey])
		frame = receiveGatewayHello(ctx, t, conn, "", "")
		checkEqual(t, frame["more"] == nil, true)
		got = append(got, *sessionJSON[[]filter.Subscription](frame[subsKey])...)
		checkEqual(t, reflect.DeepEqual(got, want), true)

		if step.reply != "" {
			checkError(t, conn.Write(ctx, websocket.MessageText, []byte(step.reply)))
		}
	})

	retries := 0
	err := runMCPGateway(ctx, logger, server.URL+sessionPath, mcpFixture, subs,
		func(channelEvent) bool { return true }, func(context.Context, time.Duration) bool {
			retries++

			return retries < len(steps)
		})
	checkError(t, err)
	checkEqual(t, requests.Load(), int32(len(steps)))
}

func TestMCPGatewayResume(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	logger := slog.New(slog.DiscardHandler)

	t.Setenv(mcpStateHomeEnv, t.TempDir())
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})
	sub := setupGatewayResume(ctx, t, logger)

	steps := [...]struct {
		resume string
		frames []string
		cursor uint64
	}{
		{"4", []string{gatewayTestEvent(7, "github", "id")}, 7},
		{"7", []string{gatewayTestEvent(9, "github", "id")}, 9},
		{"9", []string{
			`{"type":"caught_up","seq":12}`, `{"type":"gap","from":3,"to":5}`,
			`{"type":"caught_up","seq":8}`,
		}, 12},
	}

	var connections atomic.Int32

	server, requests := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		step := steps[connections.Add(1)-1]
		frame := receiveGatewayHello(ctx, t, conn, step.resume, "")
		checkEqual(t, string(frame[subsKey]), "["+mcpStateTestSubscription+"]")

		for _, data := range step.frames {
			checkEqual(t, conn.Write(ctx, websocket.MessageText, []byte(data)) == nil, true)
		}
	})

	var (
		messages []string
		retries  int
	)

	err := runMCPGateway(ctx, logger, server.URL+sessionPath, mcpFixture, loadMCPSubscriptions(ctx, logger),
		func(event channelEvent) bool {
			messages = append(messages, string(event.payload))

			return true
		}, func(context.Context, time.Duration) bool {
			got, saved := loadMCPSubscriptions(ctx, logger).snapshot()
			checkEqual(t, reflect.DeepEqual(got, []filter.Subscription{sub}), true)
			checkEqual(t, saved != nil, true)
			checkEqual(t, *saved, steps[retries].cursor)
			retries++

			return retries < len(steps)
		})
	checkError(t, err)
	checkEqual(t, requests.Load(), int32(len(steps)))
	checkGatewayResumeNotices(t, messages)
}

func checkGatewayResumeNotices(t *testing.T, messages []string) {
	t.Helper()

	checkEqual(t, slices.Equal(messages, []string{
		`{}`, string(gatewayConnectionLost().channelEvent().payload),
		string(gatewayReconnected().channelEvent().payload), string(gatewayConnectionLost().channelEvent().payload),
		string(gatewayReconnected().channelEvent().payload), string(gatewayGap(3, 5).channelEvent().payload),
		string(gatewayConnectionLost().channelEvent().payload),
	}), true)
}

func setupGatewayResume(ctx context.Context, t *testing.T, logger *slog.Logger) filter.Subscription {
	t.Helper()

	subs := loadMCPSubscriptions(ctx, logger)
	subs.advanceCursor(4)
	subs = loadMCPSubscriptions(ctx, logger)
	_, cursor := subs.snapshot()
	checkEqual(t, cursor != nil, true)
	checkEqual(t, *cursor, uint64(4))

	sub, err := filter.Parse([]byte(mcpStateTestSubscription))
	checkError(t, err)
	checkEqual(t, subs.set(sub, true), true)

	return sub
}

func gatewayTestEvent(seq int, provider, delivery string) string {
	return fmt.Sprintf(`{"type":"event","seq":%d,"provider":%q,"delivery":%q,"payload":{}}`, seq, provider, delivery)
}

func TestMCPGatewayDeliveryLimit(t *testing.T) {
	t.Parallel()

	subs := new(mcpSubscriptions)

	var deliveries gatewayDeliveries

	deliveries.ids = make(map[string]struct{})
	count := 0
	forward := func(data string) {
		forwardGatewayFrame(*sessionJSON[sessionObject]([]byte(data)), subs, &deliveries, func(channelEvent) bool {
			count++

			return true
		})
	}

	forward(`{"type":"event","provider":"github"}`)

	_, cursor := subs.snapshot()
	checkEqual(t, cursor, nil)

	for seq := 1; seq <= 1000; seq++ {
		forward(gatewayTestEvent(seq, "github", strconv.Itoa(seq)))
	}

	forward(gatewayTestEvent(1001, "github", "1"))
	checkEqual(t, count, 1001)
	forward(gatewayTestEvent(1002, "grafana", "1"))
	checkEqual(t, count, 1002)
	forward(gatewayTestEvent(1003, "github", "1"))
	checkEqual(t, count, 1003)
	checkEqual(t, len(deliveries.ids), 1000)
}

func TestMCPGatewayCancelBackoff(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		checkEqual(t, waitGatewayRetry(ctx, time.Millisecond), true)

		endpoint := "ws://" + mcpFixture + "@example.invalid:invalid"
		err := runMCPGateway(ctx, slog.New(slog.DiscardHandler), endpoint, mcpFixture, new(mcpSubscriptions),
			rejectTestNotification(t),
			func(waitCtx context.Context, delay time.Duration) bool {
				checkEqual(t, delay, time.Second)
				time.AfterFunc(time.Millisecond, cancel)

				return waitGatewayRetry(waitCtx, delay)
			})
		checkEqual(t, err == nil, true)
		checkEqual(t, ctx.Err() != nil, true)
		checkEqual(t, runMCPGateway(ctx, slog.New(slog.DiscardHandler), "", mcpFixture, nil, nil, nil) == nil, true)
	})
}

func TestMCPGatewayRedirect(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	server, requests := fakeMCPGateway(t, []int{http.StatusFound, http.StatusUnauthorized}, nil)
	err := runMCPGateway(ctx, slog.New(slog.DiscardHandler), server.URL+sessionPath, mcpFixture,
		new(mcpSubscriptions), rejectTestNotification(t),
		func(context.Context, time.Duration) bool { return false })
	checkEqual(t, err == nil, true)
	checkEqual(t, requests.Load(), 1)
}

func TestMCPGatewayInsecureWarning(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		url  string
		warn bool
	}{
		{url: "wss://example.invalid", warn: false},
		{url: "ws://localhost", warn: false},
		{url: "ws://192.168.1.1", warn: false},
		{url: "ws://example.invalid", warn: true},
	} {
		var logs bytes.Buffer

		warnInsecureGateway(t.Context(), slog.New(slog.NewTextHandler(&logs, nil)), test.url)
		checkEqual(t, strings.Contains(logs.String(), "unencrypted WebSocket"), test.warn)
	}
}

func TestMCPGatewayRejection(t *testing.T) {
	t.Parallel()

	for _, status := range []int{http.StatusSwitchingProtocols, http.StatusUnauthorized} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			t.Parallel()

			ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
			defer cancel()

			server, requests := fakeMCPGateway(t, []int{status}, func(conn *websocket.Conn) {
				checkEqual(t, string(receiveGatewayHello(ctx, t, conn, "", "")[subsKey]) == "[]", true)
				checkEqual(t, conn.Close(tokenRevoked, mcpFixture) == nil, true)
			})

			var (
				notices []channelParameters
				subs    mcpSubscriptions
			)

			logger := slog.New(slog.DiscardHandler)
			err := runMCPGateway(ctx, logger, server.URL+sessionPath, mcpFixture, &subs,
				func(event channelEvent) bool {
					notices = append(notices, channelParams(event))

					return true
				},
				func(context.Context, time.Duration) bool {
					t.Error("token rejection scheduled a retry")

					return false
				})
			checkEqual(t, strings.Contains(fmt.Sprint(err), mcpFixture), false)
			checkEqual(t, errors.Unwrap(err) == nil, true)
			checkEqual(t, errors.Is(err, errGatewayToken), true)
			checkEqual(t, requests.Load(), 1)
			checkEqual(t, len(notices), 1)
			checkEqual(t, strings.Contains(fmt.Sprint(notices), mcpFixture), false)
			checkEqual(t, notices[0].Content, `{"level":"warning","text":"Gateway token was rejected."}`)
		})
	}
}

func TestMCPGatewayRejectionNotice(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	notifications := make(chan *jsonrpc.Request, 1)
	_, peer, notifier := connectMCPClient(t, claudeCodeClient, notifications)
	server, requests := fakeMCPGateway(t, []int{http.StatusUnauthorized}, nil)
	t.Setenv(gatewayURLEnv, server.URL+sessionPath)
	t.Setenv(gatewayTokenEnv, mcpFixture)

	initialized, stop := startMCPGateway(ctx, new(mcpSubscriptions), notifier, slog.New(slog.DiscardHandler), os.Getenv)
	defer stop()

	initialized(ctx, &mcp.InitializedRequest{Session: peer, Params: nil, Extra: nil})

	select {
	case req := <-notifications:
		notice := readChannelNotification(t, req)
		checkEqual(t, notice.Meta[eventKey], "notice")
		checkEqual(t, notice.Content == `{"level":"warning","text":"Gateway token was rejected."}`, true)
	case <-ctx.Done():
		t.Fatal("gateway rejection notice did not arrive")
	}

	stop()
	checkEqual(t, requests.Load(), 1)
	checkEqual(t, len(notifications), 0)
}

func TestMCPGatewayInvalidHello(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	server, _ := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(*websocket.Conn) {})

	for _, provider := range []string{"\xff", strings.Repeat("x", sessionFrameLimit)} {
		var sub filter.Subscription

		sub.Provider = provider
		subs := new(mcpSubscriptions)
		subs.subs = []filter.Subscription{sub}
		err := runMCPGateway(ctx, slog.New(slog.DiscardHandler), server.URL+sessionPath, mcpFixture,
			subs, rejectTestNotification(t), waitGatewayRetry)
		checkEqual(t, errors.Is(err, errGatewayHello), true)
	}
}

func checkMCPGatewayLifetime(t *testing.T, name string) {
	t.Helper()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	notifications := make(chan *jsonrpc.Request, 1)
	_, peer, notifier := connectMCPClient(t, name, notifications)
	logger := slog.New(slog.DiscardHandler)

	t.Setenv(mcpStateHomeEnv, t.TempDir())
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})

	subs := loadMCPSubscriptions(ctx, logger)
	file := filepath.Join(subs.state.dir, subs.state.file)
	checkError(t, os.MkdirAll(filepath.Dir(file), 0o700))
	checkError(t, os.WriteFile(file, []byte(mcpStateDocument(mcpStateTestSubscription)), 0o600))

	checkDisabledGateway(ctx, t, logger, notifier)

	disconnected := make(chan struct{})
	server, requests := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		defer close(disconnected)

		checkEqual(t, string(receiveGatewayHello(ctx, t, conn, "", "")[subsKey]), "["+mcpStateTestSubscription+"]")
		checkEqual(t, conn.Write(ctx, websocket.MessageText, []byte(`{"type":"gap","from":3,"to":5}`)) == nil, true)

		if name != claudeCodeClient {
			checkEqual(t, conn.Close(websocket.StatusNormalClosure, "") == nil, true)

			return
		}

		_, _, err := conn.Read(ctx)
		checkEqual(t, err != nil, true)
		checkEqual(t, ctx.Err() == nil, true)
	})
	t.Setenv(gatewayURLEnv, server.URL+sessionPath)
	t.Setenv(gatewayTokenEnv, mcpFixture)

	_, stop := startMCPGateway(ctx, nil, notifier, logger, os.Getenv)
	stop()
	checkEqual(t, requests.Load(), 0)

	initialized, stop := startMCPGateway(ctx, loadMCPSubscriptions(ctx, logger), notifier, logger, os.Getenv)
	defer stop()

	initialized(ctx, &mcp.InitializedRequest{Session: peer, Params: nil, Extra: nil})

	checkGatewayLifetimeNotice(ctx, t, name, notifications, stop)

	select {
	case <-disconnected:
	case <-ctx.Done():
		t.Fatal("gateway socket stayed open")
	}

	stop()
	checkEqual(t, requests.Load(), 1)
	checkEqual(t, len(notifications), 0)
}

func checkDisabledGateway(ctx context.Context, t *testing.T, logger *slog.Logger, notifier *mcpNotifier) {
	t.Helper()

	for _, test := range [][2]string{{"", ""}, {"ws://example.invalid", ""}, {"", mcpFixture}} {
		t.Setenv(gatewayURLEnv, test[0])
		t.Setenv(gatewayTokenEnv, test[1])

		initialized, stop := startMCPGateway(ctx, nil, notifier, logger, os.Getenv)
		checkEqual(t, initialized == nil, true)
		stop()
	}
}

func checkGatewayLifetimeNotice(
	ctx context.Context,
	t *testing.T,
	name string,
	notifications <-chan *jsonrpc.Request,
	stop func(),
) {
	t.Helper()

	if name == claudeCodeClient {
		select {
		case req := <-notifications:
			checkEqual(t, readChannelNotification(t, req).Meta[eventKey], "notice")
		case <-ctx.Done():
			t.Fatal("gateway notification did not arrive")
		}

		stop()
	}
}

func rejectTestNotification(t *testing.T) func(channelEvent) bool {
	t.Helper()

	return func(channelEvent) bool {
		t.Error("unexpected gateway notification")

		return false
	}
}
