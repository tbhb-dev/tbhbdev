// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestMCPCheckDeliveryGateway(t *testing.T) {
	store := testStore(t, retention)
	publishSessionEvent(t, testWebhook(t, store, defaultBodyCap), pushEvent, "ready")
	server := sessionReplayServer(t, store)
	t.Setenv(gatewayURLEnv, server.URL+sessionPath)
	t.Setenv(gatewayTokenEnv, readFixture)
	t.Setenv(mcpStateHomeEnv, t.TempDir())
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})

	subs := loadMCPSubscriptions(t.Context(), slog.New(slog.DiscardHandler))
	notifications := make(chan *jsonrpc.Request, liveTestBuffer)
	session, peer, notifier := connectMCPWithSubscriptions(t, subs, claudeCodeClient, notifications)
	checkEqual(t, callMCPTool(t, session, subscribeTool, pushSubscription).IsError, false)
	subs.advanceCursor(0)
	initializeDeliveryGateway(t, subs, notifier, peer)

	ctx, cancel := context.WithTimeout(t.Context(), mcpTestTimeout)
	defer cancel()

	checkEqual(t, waitLiveNotification(ctx, t, notifications).Meta["delivery"], "ready")
	var lastID string

	for range 2 {
		result := checkDeliveryStatus(t, callMCPTool(t, session, checkDeliveryTool, ""))
		checkEqual(t, waitLiveNotification(ctx, t, notifications).Meta[channelEventKey], "self_test")
		echo := waitLiveNotification(ctx, t, notifications)
		id := echo.Meta["delivery"]
		checkEqual(t, id != "" && id != lastID, true)
		lastID = id
		checkEqual(t, echo.Meta[channelEventKey], "echo")
		checkEqual(t, echo.Meta[channelProviderKey], "event-gateway")
		payload := sessionJSON[sessionObject]([]byte(echo.Content))
		checkEqual(t, string(payload["round_trip_ms"]), string(result["round_trip_ms"]))
		checkEqual(t, sessionJSON[float64](result["round_trip_ms"]) > 0, true)
		checkEqual(t, sessionJSON[string](result["delivery_id"]), id)
		checkEqual(t, string(result["connection"]), `"connected"`)
		checkEqual(t, sessionJSON[time.Time](result["connected_since"]).IsZero(), false)
		checkEqual(t, string(result["last_outage"]), "null")
		checkEqual(t, string(result[subsKey]), "1")
		checkEqual(t, string(result["cursor"]), "1")
		checkEqual(t, string(result["persistence"]), "true")
		checkEqual(t, sessionJSON[string](result["state_file"]), filepath.Join(subs.state.dir, subs.state.file))
	}
}

func TestMCPCheckDeliveryDisconnected(t *testing.T) {
	server, _ := fakeMCPGateway(t, []int{http.StatusForbidden}, nil)
	server.Close()
	t.Setenv(gatewayURLEnv, server.URL+sessionPath)
	t.Setenv(gatewayTokenEnv, mcpFixture)

	subs := new(mcpSubscriptions)
	notifications := make(chan *jsonrpc.Request, liveTestBuffer)
	session, peer, notifier := connectMCPWithSubscriptions(t, subs, claudeCodeClient, notifications)
	initializeDeliveryGateway(t, subs, notifier, peer)

	start := time.Now()
	result := checkDeliveryStatus(t, callMCPTool(t, session, checkDeliveryTool, ""))
	checkEqual(t, time.Since(start) < liveTestTimeout, true)
	checkEqual(t, string(result["connection"]), `"not connected"`)
	checkEqual(t, string(result["echo"]), `"not connected"`)
	checkEqual(t, sessionJSON[time.Time](result["disconnected_since"]).IsZero(), false)
	checkEqual(t, len(notifications), 1)
	checkEqual(t, readChannelNotification(t, <-notifications).Meta[channelEventKey], "self_test")
}

func TestMCPCheckDeliveryOlderGateway(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), mcpTestTimeout)
	defer cancel()

	server, _ := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		receiveGatewayHello(ctx, t, conn, "", "")
		hello := `{"type":"hello","subscriptions":0,"seq":0}`
		gap := `{"type":"gap","from":1,"to":1}`
		checkError(t, conn.Write(ctx, websocket.MessageText, []byte(hello)))
		checkError(t, conn.Write(ctx, websocket.MessageText, []byte(gap)))

		_, raw, err := conn.Read(ctx)
		if err != nil {
			return
		}

		frame := sessionJSON[sessionObject](raw)
		checkEqual(t, string(frame[typeKey]), `"echo"`)
		reply := `{"type":"error","code":"bad_frame","message":"unexpected message","id":` + string(frame["id"]) + `}`
		checkError(t, conn.Write(ctx, websocket.MessageText, []byte(reply)))
		conn.CloseRead(ctx)
		<-ctx.Done()
	})
	t.Setenv(gatewayURLEnv, server.URL+sessionPath)
	t.Setenv(gatewayTokenEnv, mcpFixture)

	subs := new(mcpSubscriptions)
	notifications := make(chan *jsonrpc.Request, liveTestBuffer)
	session, peer, notifier := connectMCPWithSubscriptions(t, subs, claudeCodeClient, notifications)
	initializeDeliveryGateway(t, subs, notifier, peer)

	checkEqual(t, waitLiveNotification(ctx, t, notifications).Meta[channelEventKey], "notice")
	start := time.Now()
	result := checkDeliveryStatus(t, callMCPTool(t, session, checkDeliveryTool, ""))
	checkEqual(t, time.Since(start) < liveTestTimeout, true)
	checkEqual(t, string(result["connection"]), `"connected"`)
	checkEqual(t, string(result["echo"]), `"not supported by the server"`)
	checkEqual(t, len(notifications), 1)
	checkEqual(t, readChannelNotification(t, <-notifications).Meta[channelEventKey], "self_test")
}

func initializeDeliveryGateway(
	t *testing.T, subs *mcpSubscriptions, notifier *notifyingTransport, peer *mcp.ServerSession,
) {
	t.Helper()

	initialized, stop := startMCPGateway(t.Context(), subs, notifier, slog.New(slog.DiscardHandler))
	t.Cleanup(stop)
	initialized(t.Context(), &mcp.InitializedRequest{Session: peer, Params: nil, Extra: nil})
}

func checkDeliveryStatus(t *testing.T, result *mcp.CallToolResult) sessionObject {
	t.Helper()

	checkEqual(t, result.IsError, false)
	data, err := json.Marshal(result.StructuredContent)
	checkError(t, err)
	checkEqual(t, strings.Contains(string(data), mcpFixture) || strings.Contains(string(data), readFixture), false)

	return sessionJSON[sessionObject](data)
}
