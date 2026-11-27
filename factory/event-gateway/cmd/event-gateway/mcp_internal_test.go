// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"net/http"
	"os"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const (
	mcpTestTimeout = 10 * time.Second
	mcpTestInit    = `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{` +
		`"clientInfo":{"name":"claude-code","version":"test"},"protocolVersion":"2025-11-25","capabilities":{}}}` + "\n"
)

func TestMCPCommand(t *testing.T) {
	checkMCPGatewayLifetime(t, claudeCodeClient)
	checkMCPGatewayLifetime(t, mcpServerName)

	t.Setenv(gatewayURLEnv, "")
	t.Setenv(gatewayTokenEnv, "")

	for _, cancelEarly := range []bool{false, true} {
		reader, writer, err := os.Pipe()
		checkError(t, err)

		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		t.Cleanup(cancel)

		var want error

		if cancelEarly {
			cancel()
			t.Cleanup(func() { checkError(t, writer.Close()) })

			want = context.Canceled
		} else {
			checkError(t, writer.Close())
		}

		err = runMCP(ctx, slog.DiscardHandler, reader, io.Discard, nil)
		checkEqual(t, errors.Is(err, want), true)
	}

	err := run(t.Context(), io.Discard, slog.DiscardHandler, []string{mcpCommand, "unexpected"})
	checkEqual(t, errors.Is(err, errMCPUsage), true)
}

func TestMCPStdinErrors(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		want        error
		name, input string
	}{
		{io.ErrUnexpectedEOF, "truncated", `{"jsonrpc":"2.0"`},
		{nil, "malformed JSON", "{!\n"},
		{nil, "invalid protocol", "{}\n"},
		{os.ErrClosed, "closed stdin", ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			input, client := mcpTestPipe(t)
			_, err := io.WriteString(client, test.input)
			checkError(t, err)
			checkError(t, client.Close())

			if errors.Is(test.want, os.ErrClosed) {
				checkError(t, input.Close())
			}

			ctx, cancel := context.WithTimeout(t.Context(), mcpTestTimeout)
			defer cancel()

			err = serveMCPWithEnv(ctx, slog.DiscardHandler, input, io.Discard, func(string) string { return "" })
			checkMCPServeError(t, err)

			if test.want != nil {
				checkEqual(t, errors.Is(err, test.want), true)
			}
		})
	}
}

type failingMCPWriter struct{}

func (failingMCPWriter) Write([]byte) (int, error) { return 0, io.ErrShortWrite }

func TestMCPStdoutError(t *testing.T) {
	t.Setenv(gatewayURLEnv, "")
	t.Setenv(gatewayTokenEnv, "")

	input, client := mcpTestPipe(t)
	_, err := io.WriteString(client, mcpTestInit)
	checkError(t, err)

	ctx, cancel := context.WithTimeout(t.Context(), mcpTestTimeout)
	defer cancel()

	err = serveMCP(ctx, slog.DiscardHandler, input, failingMCPWriter{})
	checkMCPServeError(t, err)
	checkEqual(t, errors.Is(err, io.ErrShortWrite), true)
}

func TestMCPStdoutAfterCancel(t *testing.T) {
	t.Parallel()

	done := make(chan struct{})
	close(done)

	out := mcpStdout{writer: failingMCPWriter{}, done: done, mu: sync.Mutex{}}
	n, err := out.Write([]byte("notification"))
	checkEqual(t, n, 0)
	checkEqual(t, err, io.ErrClosedPipe)
}

func TestMCPStdinCloseStopsGateway(t *testing.T) {
	t.Parallel()
	checkMCPBlockedStdout(t, false)
}

func TestMCPMalformedInputWithBlockedStdout(t *testing.T) {
	t.Parallel()
	checkMCPBlockedStdout(t, true)
}

func checkMCPBlockedStdout(t *testing.T, malformed bool) {
	t.Helper()

	ctx := t.Context()
	disconnected := make(chan struct{})
	gateway, _ := fakeMCPGateway(t, []int{http.StatusSwitchingProtocols}, func(conn *websocket.Conn) {
		defer close(disconnected)

		receiveGatewayHello(ctx, t, conn, "", "")
		checkError(t, conn.Write(ctx, websocket.MessageText, []byte(`{"type":"gap","from":3,"to":5}`)))
		_, _, err := conn.Read(ctx)
		checkEqual(t, err != nil, true)
	})
	env := map[string]string{gatewayURLEnv: gateway.URL + sessionPath, gatewayTokenEnv: mcpFixture}

	input, clientInput := mcpTestPipe(t)
	clientOutput, output := io.Pipe()

	t.Cleanup(func() { checkError(t, clientOutput.Close()) })
	t.Cleanup(func() { checkError(t, output.Close()) })

	stopped := make(chan error, 1)
	go func() {
		stopped <- serveMCPWithEnv(ctx, slog.DiscardHandler, input, output, func(key string) string { return env[key] })
	}()

	started := make(chan error, 1)
	go func() { started <- startMCPPipeClient(clientInput, clientOutput) }()

	select {
	case err := <-started:
		checkError(t, err)
	case <-time.After(mcpTestTimeout):
		t.Fatal("gateway notification never started writing to stdout")
	}

	if malformed {
		// Keep stdin open so only the SDK's parser error can start shutdown.
		_, err := io.WriteString(clientInput, "{!\n")
		checkError(t, err)
	} else {
		checkError(t, clientInput.Close())
	}

	select {
	case err := <-stopped:
		if malformed {
			checkMCPServeError(t, err)
			_, syntaxErr := errors.AsType[*json.SyntaxError](err)
			checkEqual(t, syntaxErr, true)
		} else {
			checkError(t, err)
		}
	case <-time.After(mcpTestTimeout):
		t.Fatal("MCP server did not return after stdin ended the session with stdout blocked")
	}

	select {
	case <-disconnected:
	case <-time.After(mcpTestTimeout):
		t.Fatal("MCP gateway stayed connected after the server returned")
	}
}

func startMCPPipeClient(input io.Writer, output io.Reader) error {
	_, err := io.WriteString(input, mcpTestInit)
	if err != nil {
		return fmt.Errorf("write initialize: %w", err)
	}

	_, err = bufio.NewReader(output).ReadBytes('\n')
	if err != nil {
		return fmt.Errorf("read initialize response: %w", err)
	}

	_, err = io.WriteString(input, `{"jsonrpc":"2.0","method":"notifications/initialized"}`+"\n")
	if err != nil {
		return fmt.Errorf("write initialized notification: %w", err)
	}

	// Read only the first byte so the notification stays blocked in the pipe write.
	var firstByte [1]byte

	_, err = io.ReadFull(output, firstByte[:])
	if err != nil {
		return fmt.Errorf("read notification: %w", err)
	}

	return nil
}

func mcpTestPipe(t *testing.T) (*os.File, *os.File) {
	t.Helper()

	reader, writer, err := os.Pipe()
	checkError(t, err)

	for _, file := range []*os.File{reader, writer} {
		t.Cleanup(func() {
			closeErr := file.Close()
			if !errors.Is(closeErr, os.ErrClosed) {
				checkError(t, closeErr)
			}
		})
	}

	return reader, writer
}

func checkMCPServeError(t *testing.T, err error) {
	t.Helper()

	checkEqual(t, errors.Unwrap(err) != nil, true)
	checkEqual(t, strings.HasPrefix(err.Error(), "serve MCP: "), true)
	checkEqual(t, errors.Is(err, context.DeadlineExceeded), false)
}

func connectMCP(t *testing.T) *mcp.ClientSession {
	t.Helper()

	session, _, _ := connectMCPClient(t, "test", nil)

	return session
}

func connectMCPClient(
	t *testing.T, name string, notifications chan<- *jsonrpc.Request,
) (*mcp.ClientSession, *mcp.ServerSession, *mcpNotifier) {
	t.Helper()

	return connectMCPWithSubscriptions(t, new(mcpSubscriptions), name, notifications)
}

func connectMCPWithSubscriptions(
	t *testing.T, subs *mcpSubscriptions, name string, notifications chan<- *jsonrpc.Request,
) (*mcp.ClientSession, *mcp.ServerSession, *mcpNotifier) {
	t.Helper()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	logger := slog.New(slog.DiscardHandler)
	in, out := mcp.NewInMemoryTransports()
	notifier := &mcpNotifier{output: captureMCPNotifications{notifications: notifications}}
	server := newMCPServer(logger, subs, notifier, nil)
	peer, err := server.Connect(ctx, in, nil)
	checkError(t, err)
	t.Cleanup(func() { checkError(t, peer.Close()) })

	var options mcp.ClientOptions

	options.Logger = logger
	client := mcp.NewClient(mcpImplementation(name), &options)
	session, err := client.Connect(ctx, out, nil)
	checkError(t, err)
	t.Cleanup(func() { checkError(t, session.Close()) })

	return session, peer, notifier
}

type captureMCPNotifications struct{ notifications chan<- *jsonrpc.Request }

func (capture captureMCPNotifications) Write(data []byte) (int, error) {
	msg, err := jsonrpc.DecodeMessage(data)
	if err != nil {
		return 0, fmt.Errorf("decode notification: %w", err)
	}

	if request, ok := msg.(*jsonrpc.Request); ok && capture.notifications != nil {
		capture.notifications <- request
	}

	return len(data), nil
}

func TestMCPTools(t *testing.T) {
	t.Parallel()
	session := connectMCP(t)

	checkMCPCapabilities(t, session.InitializeResult())

	result, err := session.ListTools(t.Context(), nil)
	checkError(t, err)

	names := make([]string, 0, len(result.Tools))
	for _, tool := range result.Tools {
		names = append(names, tool.Name)

		data, marshalErr := json.Marshal(tool.InputSchema)
		checkError(t, marshalErr)

		var schema struct {
			AdditionalProperties *bool                      `json:"additionalProperties"`
			Properties           map[string]json.RawMessage `json:"properties"`
			Type                 string                     `json:"type"`
			Required             []string                   `json:"required"`
		}
		checkError(t, json.Unmarshal(data, &schema))
		checkEqual(t, schema.Type, "object")
		checkEqual(t, schema.AdditionalProperties != nil && !*schema.AdditionalProperties, true)

		switch tool.Name {
		case subscribeTool:
			checkEqual(
				t,
				slices.Equal(schema.Required, []string{channelProviderKey, eventKey, channelRepositoryKey}),
				true,
			)
		case unsubscribeTool:
			checkEqual(t, slices.Equal(schema.Required, []string{channelProviderKey}), true)
		case checkDeliveryTool, listSubscriptionsTool:
			checkEqual(t, len(schema.Required), 0)
			checkEqual(t, len(schema.Properties), 0)

			continue
		}

		keys := []string{
			channelProviderKey, eventKey, channelRepositoryKey, "action", "conclusion", "name", "workflow_path",
			"number", "label",
		}
		checkEqual(t, len(schema.Properties), len(keys))

		for _, key := range keys {
			checkEqual(t, schema.Properties[key] != nil, true)
		}
	}

	slices.Sort(names)
	checkEqual(
		t,
		slices.Equal(names, []string{checkDeliveryTool, listSubscriptionsTool, subscribeTool, unsubscribeTool}),
		true,
	)
}

func checkMCPCapabilities(t *testing.T, init *mcp.InitializeResult) {
	t.Helper()

	checkEqual(t, init.ServerInfo.Name, "event-gateway")
	checkEqual(t, init.ProtocolVersion, "2025-11-25")
	capabilitiesJSON, err := json.Marshal(init.Capabilities)
	checkError(t, err)
	checkEqual(t, bytes.Contains(capabilitiesJSON, []byte(`"logging":{}`)), true)
	checkEqual(t, init.Capabilities.Tools.ListChanged, false)
	capabilities, err := json.Marshal(init.Capabilities.Experimental)
	checkError(t, err)
	checkEqual(t, string(capabilities), `{"claude/channel":{}}`)
}

func TestMCPCheckDelivery(t *testing.T) {
	t.Parallel()

	const clientName = "claude-code"

	for _, name := range []string{clientName, "test"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			notifications := make(chan *jsonrpc.Request, 3)
			session, _, _ := connectMCPClient(t, name, notifications)
			checkEqual(t, callMCPTool(t, session, checkDeliveryTool, `{"extra":true}`).IsError, true)
			checkEqual(t, callMCPTool(t, session, checkDeliveryTool, "").IsError, name != clientName)

			// The tool response follows any notifications on the same connection.
			if name == clientName {
				checkEqual(t, len(notifications), 2)

				for _, event := range []string{"self_test", "echo"} {
					params := readChannelNotification(t, <-notifications)
					checkEqual(t, params.Meta[eventKey], event)
					checkEqual(t, params.Content, `{"message":"Local stdio delivery test"}`)
				}
			}

			checkEqual(t, len(notifications), 0)
		})
	}
}

func readChannelNotification(t *testing.T, req *jsonrpc.Request) channelParameters {
	t.Helper()
	checkEqual(t, req.ID, jsonrpc.ID{})

	var params channelParameters
	checkError(t, json.Unmarshal(req.Params, &params))
	checkEqual(t, params.Meta[channelProviderKey], mcpServerName)

	return params
}

func TestMCPNotifyWithoutConnection(t *testing.T) {
	t.Parallel()

	notifier := new(mcpNotifier)
	checkEqual(t, notifier.notify(t.Context(), nil), errNoConnection)
}

func TestMCPChannelBeforeClientName(t *testing.T) {
	t.Parallel()

	var event channelEvent

	for _, init := range []*mcp.InitializeParams{nil, new(mcp.InitializeParams)} {
		serverSide, _ := mcp.NewInMemoryTransports()
		notifier := new(mcpNotifier)
		server := newMCPServer(slog.New(slog.DiscardHandler), new(mcpSubscriptions), notifier, nil)

		opts := &mcp.ServerSessionOptions{
			State: &mcp.ServerSessionState{
				InitializedParams:         nil,
				NegotiatedProtocolVersion: "",
				LogLevel:                  "",
				InitializeParams:          init,
			},
		}
		peer, err := server.Connect(t.Context(), serverSide, opts)
		checkError(t, err)
		checkEqual(t, notifier.deliverChannel(t.Context(), peer, event), errChannelClient)
		checkError(t, peer.Close())
	}
}

func TestMCPChannelParams(t *testing.T) {
	t.Parallel()

	prefix := strings.Repeat("x", 252) + "<>&"
	long := prefix + "é"
	got := channelParams(channelEvent{
		provider: long, event: long, repository: &long, delivery: long,
		payload: json.RawMessage(`{"action":"` + long + `","body":"<>&"}`),
	})
	clipped := strings.Repeat("x", 252) + `\u003c\u003e\u0026`
	checkEqual(t, maps.Equal(got.Meta, map[string]string{
		channelProviderKey: clipped, eventKey: clipped, channelRepositoryKey: clipped,
		"delivery": clipped, "action": clipped,
	}), true)
	checkEqual(t, got.Content, `{"action":"`+clipped+`é","body":"\u003c\u003e\u0026"}`)

	for _, payload := range []string{`[]`, `{}`, `{"action":null}`, `{"action":false}`} {
		plain := channelParams(channelEvent{
			provider: "example", event: pushEvent, repository: nil, delivery: "id", payload: json.RawMessage(payload),
		})
		checkEqual(
			t,
			maps.Equal(
				plain.Meta,
				map[string]string{channelProviderKey: "example", eventKey: pushEvent, "delivery": "id"},
			),
			true,
		)
		checkEqual(t, plain.Content, payload)
	}
}

func TestMCPChannelContentLimit(t *testing.T) {
	t.Parallel()

	const (
		prefix = `{"action":"opened","body":"`
		suffix = `"}`
	)

	body := strings.Repeat("<", 65536-len(prefix)-len(suffix))
	event := channelEvent{
		provider: "sample", event: "issues", repository: nil, delivery: "id",
		payload: json.RawMessage(prefix + body + suffix),
	}
	got := channelParams(event)
	checkEqual(t, got.Meta["truncated"], "")
	checkEqual(t, got.Content == prefix+strings.Repeat(`\u003c`, len(body))+suffix, true)

	event.payload = append(event.payload, ' ')
	got = channelParams(event)
	checkEqual(t, got.Meta["truncated"], "true")
	checkEqual(t, got.Content, "sample issues (action 'opened'): payload of 65537 bytes omitted.")
}

func TestMCPChannelNotice(t *testing.T) {
	t.Parallel()

	notifications := make(chan *jsonrpc.Request, 1)
	_, peer, notifier := connectMCPClient(t, claudeCodeClient, notifications)

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	for _, test := range []struct {
		notice  channelNotice
		content string
	}{
		{gatewayConnectionLost(), `{"level":"warning","text":"Gateway connection lost."}`},
		{gatewayReconnected(), `{"level":"info","text":"Gateway reconnected."}`},
		{gatewayGap(3, 5), `{"level":"warning","text":"Gateway events 3 through 5 are no longer available."}`},
	} {
		checkError(t, notifier.deliverChannel(ctx, peer, test.notice.channelEvent()))

		select {
		case req := <-notifications:
			params := readChannelNotification(t, req)
			checkEqual(t, params.Meta[eventKey], "notice")
			checkEqual(t, params.Content, test.content)
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
}

func TestMCPSubscriptions(t *testing.T) {
	t.Parallel()
	session := connectMCP(t)

	const (
		scope     = `"provider":"github","event":"issues","repository":"Owner/Repo"`
		filters   = `{"provider":"github","event":"issues","repository":"owner/repo","number":[1,2],"label":["Type: Bug"]}`
		other     = `{"provider":"github","event":"issues","repository":"owner/repo","number":[2]}`
		empty     = `{"subscriptions":[]}`
		changed   = `{"changed":true}`
		unchanged = `{"changed":false}`
	)

	for _, test := range []struct {
		tool, arguments, want string
	}{
		{listSubscriptionsTool, "", empty},
		{subscribeTool, "{" + scope + `,"number":[2,1,1],"label":["Type: Bug"]}`, changed},
		{subscribeTool, filters, unchanged},
		{subscribeTool, other, changed},
		{unsubscribeTool, "{" + scope + "}", unchanged},
		{unsubscribeTool, "{" + scope + `,"number":[1,2],"label":["type: bug"]}`, unchanged},
		{unsubscribeTool, "{" + scope + `,"number":[1],"label":["Type: Bug"]}`, unchanged},
		{listSubscriptionsTool, "", `{"subscriptions":[` + filters + "," + other + "]}"},
		{unsubscribeTool, filters, changed},
		{unsubscribeTool, filters, unchanged},
		{listSubscriptionsTool, "", `{"subscriptions":[` + other + "]}"},
		{unsubscribeTool, other, changed},
		{subscribeTool, `{"provider":"grafana","event":"alert.firing","repository":"owner/repo"}`, changed},
		{unsubscribeTool, `{"provider":"grafana"}`, unchanged},
		{unsubscribeTool, `{"provider":"grafana","event":"alert.firing","repository":"owner/repo"}`, changed},
		{subscribeTool, "{" + scope + `,"number":[1,2,3,4,5,6,7,9007199254740991],` +
			`"label":["A","B","C","D","E","F","G","` + strings.Repeat("z", 128) + `"]}`, changed},
	} {
		got := callMCPTool(t, session, test.tool, test.arguments)
		checkEqual(t, got.IsError, false)
		data, err := json.Marshal(got.StructuredContent)
		checkError(t, err)

		var expected any
		checkError(t, json.Unmarshal([]byte(test.want), &expected))
		want, err := json.Marshal(expected)
		checkError(t, err)
		checkEqual(t, string(data), string(want))
	}
}

func TestMCPRejectsInvalidArguments(t *testing.T) {
	t.Parallel()

	const (
		issue = `{"provider":"github","event":"issues","repository":"owner/repo",`
		push  = `{"provider":"github","event":"push","repository":"owner/repo",`
	)

	session := connectMCP(t)

	for _, arguments := range []string{
		`{}`, `{"provider":"github","event":"issues"}`, `{"provider":"github","repository":"owner/repo"}`,
		issue + `"extra":true}`, issue + `"number":[]}`, issue + `"number":[1,2,3,4,5,6,7,8,9]}`,
		issue + `"number":[0]}`, issue + `"number":[-1]}`, issue + `"number":[1.1]}`,
		issue + `"number":[1.0]}`, issue + `"number":[9007199254740992]}`, push + `"number":[1]}`,
		issue + `"label":[]}`, issue + `"label":["a","b","c","d","e","f","g","h","i"]}`,
		issue + `"label":[""]}`, issue + `"label":["caf\u00e9"]}`,
		issue + `"label":["` + strings.Repeat("z", 129) + `"]}`, push + `"label":["bug"]}`,
	} {
		for _, tool := range []string{subscribeTool, unsubscribeTool} {
			got := callMCPTool(t, session, tool, arguments)
			if !got.IsError {
				t.Errorf("%s accepted %s", tool, arguments)
			}
		}
	}

	checkEqual(t, callMCPTool(t, session, subscribeTool, `{"provider":"grafana"}`).IsError, true)
	checkEqual(t, callMCPTool(t, session, listSubscriptionsTool, `{"extra":true}`).IsError, true)
}

func callMCPTool(t *testing.T, session *mcp.ClientSession, tool, arguments string) *mcp.CallToolResult {
	t.Helper()

	var raw json.RawMessage
	if arguments != "" {
		raw = json.RawMessage(arguments)
	}

	result, err := session.CallTool(
		t.Context(),
		&mcp.CallToolParams{Meta: nil, InputResponses: nil, RequestState: "", Name: tool, Arguments: raw},
	)
	checkError(t, err)

	return result
}
