// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"reflect"
	"slices"
	"strings"
	"sync"

	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	mcpCommand            = "mcp"
	subscribeTool         = "subscribe"
	unsubscribeTool       = "unsubscribe"
	listSubscriptionsTool = "list_subscriptions"
	checkDeliveryTool     = "check_delivery"
	noArgumentsSchema     = `{"type":"object","additionalProperties":false}`
	channelCapability     = "claude/channel"
	channelMethod         = "notifications/claude/channel"
	channelEventKey       = "event"
	channelProviderKey    = "provider"
	channelRepositoryKey  = "repository"
	channelTruncated      = "true"
	channelWarning        = "warning"
	claudeCodeClient      = "claude-code"
	mcpServerName         = "event-gateway"
	maxChannelContent     = 64 * 1024
	maxChannelAttribute   = 256
)

var (
	errMCPUsage      = errors.New("usage: event-gateway mcp")
	errNoConnection  = errors.New("no MCP connection")
	errChannelClient = errors.New("channel delivery requires a claude-code client")
)

var channelFraming = strings.NewReplacer("<", `\u003c`, ">", `\u003e`, "&", `\u0026`)

func runMCP(ctx context.Context, logHandler slog.Handler, args []string) error {
	if len(args) != 0 {
		return errMCPUsage
	}

	return serveMCP(ctx, logHandler, os.Stdin, os.Stdout)
}

func serveMCP(ctx context.Context, logHandler slog.Handler, stdin io.ReadCloser, stdout io.Writer) error {
	defer stdin.Close()

	logger := slog.New(logHandler)
	subs := loadMCPSubscriptions(ctx, logger)

	runCtx, cancel := context.WithCancel(ctx)
	transport := &mcp.IOTransport{
		Reader:        mcpStdin{Reader: stdin, cancel: cancel},
		Writer:        mcpStdout{writer: stdout, done: runCtx.Done()},
		MaxLineLength: 0,
	}
	notifier := &notifyingTransport{inner: transport, conn: nil, mu: sync.Mutex{}}
	initialized, stopGateway := startMCPGateway(runCtx, subs, notifier, logger)

	defer stopGateway()

	server := newMCPServer(logger, subs, notifier, initialized)

	// Let the SDK handle EOF and report any transport error.
	err := server.Run(ctx, notifier)

	cancel() // Release blocked notification writes before waiting for the gateway worker.

	if err != nil {
		return fmt.Errorf("serve MCP: %w", err)
	}

	return nil
}

type mcpStdin struct {
	io.Reader

	cancel context.CancelFunc
}

// serveMCP closes stdin after the SDK returns so shutdown cannot mask a write error with a read error.
func (mcpStdin) Close() error { return nil }

func (in mcpStdin) Read(p []byte) (int, error) {
	n, err := in.Reader.Read(p)
	if errors.Is(err, io.EOF) {
		in.cancel()
	}

	return n, err
}

type mcpStdout struct {
	writer io.Writer
	done   <-chan struct{}
}

func (mcpStdout) Close() error { return nil }

func (out mcpStdout) Write(data []byte) (int, error) {
	select {
	case <-out.done:
		return 0, io.ErrClosedPipe
	default:
	}

	type result struct {
		err error
		n   int
	}

	written := make(chan result, 1)
	payload := bytes.Clone(data)

	// An inherited stdout pipe that never drains can hold this goroutine until process exit.
	go func() {
		n, err := out.writer.Write(payload)
		written <- result{err: err, n: n}
	}()

	select {
	case <-out.done:
		return 0, io.ErrClosedPipe
	case got := <-written:
		return got.n, got.err
	}
}

func newMCPServer(
	logger *slog.Logger,
	subs *mcpSubscriptions,
	notifier *notifyingTransport,
	initialized func(context.Context, *mcp.InitializedRequest),
) *mcp.Server {
	server := mcp.NewServer(
		&mcp.Implementation{
			Title:       "",
			Description: "",
			WebsiteURL:  "",
			Icons:       nil,
			Name:        mcpServerName,
			Version:     version,
		},
		&mcp.ServerOptions{
			Logger:             logger,
			InitializedHandler: initialized,
			Instructions: "After a restart or reconnect, call check_delivery and confirm both events arrive: " +
				"self_test and echo.",
			Capabilities: &mcp.ServerCapabilities{
				Extensions:  nil,
				Completions: nil,
				Prompts:     nil,
				Resources:   nil,

				Experimental: map[string]any{channelCapability: map[string]any{}},
				Logging:      &mcp.LoggingCapabilities{},
				Tools:        &mcp.ToolCapabilities{ListChanged: false},
			},
			// These versions use initialize, which supplies the client's name.
			SupportedProtocolVersions: []string{"2025-11-25", "2025-06-18", "2025-03-26"},
		},
	)

	mcp.AddTool(server, &mcp.Tool{
		Meta:         nil,
		Annotations:  nil,
		OutputSchema: nil,
		Title:        "",
		Icons:        nil,

		Name: subscribeTool, Description: "Start receiving matching webhook events in this session.",
		InputSchema: subscriptionSchema(`["provider","event","repository"]`),
	}, subs.change)

	mcp.AddTool(server, &mcp.Tool{
		Meta:         nil,
		Annotations:  nil,
		OutputSchema: nil,
		Title:        "",
		Icons:        nil,

		Name: unsubscribeTool, Description: "Stop receiving the events a subscription matched.",
		InputSchema: subscriptionSchema(`["provider"]`),
	}, subs.change)

	mcp.AddTool(server, &mcp.Tool{
		Meta:         nil,
		Annotations:  nil,
		OutputSchema: nil,
		Title:        "",
		Icons:        nil,

		Name: listSubscriptionsTool, Description: "List this session's subscriptions.",
		InputSchema: json.RawMessage(noArgumentsSchema),
	}, func(context.Context, *mcp.CallToolRequest, json.RawMessage) (*mcp.CallToolResult, any, error) {
		return nil, map[string][]filter.Subscription{subsKey: subs.list()}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Meta:         nil,
		Annotations:  nil,
		OutputSchema: nil,
		Title:        "",
		Icons:        nil,

		Name: checkDeliveryTool,
		Description: "Send a self-test event and confirm it reaches this session. " +
			"This proves delivery to the client over stdio but does not prove the gateway connection.",
		InputSchema: json.RawMessage(noArgumentsSchema),
	}, notifier.checkDelivery)

	return server
}

// notifyingTransport retains the SDK connection so it can write custom
// notifications. The SDK has no public API for sending them, but the
// connection can write a JSON-RPC request with no ID.
type notifyingTransport struct {
	inner mcp.Transport
	conn  mcp.Connection
	mu    sync.Mutex
}

func (t *notifyingTransport) Connect(ctx context.Context) (mcp.Connection, error) {
	conn, err := t.inner.Connect(ctx)
	if err != nil {
		return nil, fmt.Errorf("connect MCP transport: %w", err)
	}

	t.mu.Lock()
	t.conn = conn
	t.mu.Unlock()

	return conn, nil
}

func (t *notifyingTransport) Notify(ctx context.Context, method string, params json.RawMessage) error {
	t.mu.Lock()
	conn := t.conn
	t.mu.Unlock()

	if conn == nil {
		return errNoConnection
	}

	return conn.Write(ctx, &jsonrpc.Request{ID: jsonrpc.ID{}, Method: method, Params: params, Extra: nil})
}

type channelEvent struct {
	repository *string
	provider   string
	event      string
	delivery   string
	payload    json.RawMessage
}

type channelParameters struct {
	Meta    map[string]string `json:"meta"`
	Content string            `json:"content"`
}

func channelParams(event channelEvent) channelParameters {
	meta := map[string]string{
		channelProviderKey: channelAttribute(event.provider),
		channelEventKey:    channelAttribute(event.event),
		"delivery":         channelAttribute(event.delivery),
	}
	if event.repository != nil {
		meta[channelRepositoryKey] = channelAttribute(*event.repository)
	}

	object := sessionJSON[map[string]json.RawMessage](event.payload)

	var action *string

	if json.Unmarshal(object["action"], &action) == nil && action != nil {
		meta["action"] = channelAttribute(*action)
	}

	if len(event.payload) > maxChannelContent {
		meta["truncated"] = channelTruncated
		content := fmt.Sprintf("%s %s (action '%s'): payload of %d bytes omitted.",
			meta[channelProviderKey], meta[channelEventKey], meta["action"], len(event.payload))

		return channelParameters{Meta: meta, Content: content}
	}

	return channelParameters{Meta: meta, Content: channelFraming.Replace(string(event.payload))}
}

func channelAttribute(value string) string {
	return channelFraming.Replace(strings.ToValidUTF8(value[:min(len(value), maxChannelAttribute)], ""))
}

func (t *notifyingTransport) deliverChannel(ctx context.Context, session *mcp.ServerSession, event channelEvent) error {
	params := session.InitializeParams()
	if params == nil || params.ClientInfo == nil || params.ClientInfo.Name != claudeCodeClient {
		return errChannelClient
	}

	data, _ := json.Marshal(channelParams(event))

	return t.Notify(ctx, channelMethod, data)
}

func (t *notifyingTransport) checkDelivery(
	ctx context.Context, req *mcp.CallToolRequest, _ json.RawMessage,
) (*mcp.CallToolResult, any, error) {
	for _, event := range []string{"self_test", "echo"} {
		err := t.deliverChannel(ctx, req.Session, channelEvent{
			provider: mcpServerName, event: event, delivery: "", repository: nil,
			payload: json.RawMessage(`{"message":"Local stdio delivery test"}`),
		})
		if err != nil {
			return nil, nil, err
		}
	}

	return nil, map[string]string{"status": "Sent self_test and echo over stdio."}, nil
}

type channelNotice struct {
	Level string `json:"level"`
	Text  string `json:"text"`
}

func gatewayConnectionLost() channelNotice {
	return channelNotice{Level: channelWarning, Text: "Gateway connection lost."}
}

func gatewayReconnected() channelNotice {
	return channelNotice{Level: "info", Text: "Gateway reconnected."}
}

func gatewayGap(from, to uint64) channelNotice {
	return channelNotice{
		Level: channelWarning,
		Text:  fmt.Sprintf("Gateway events %d through %d are no longer available.", from, to),
	}
}

func (notice channelNotice) channelEvent() channelEvent {
	payload, _ := json.Marshal(notice)

	return channelEvent{
		provider: mcpServerName, event: "notice", delivery: "", repository: nil, payload: payload,
	}
}

func subscriptionSchema(required string) json.RawMessage {
	return json.RawMessage(fmt.Sprintf(`{
		"type":"object",
		"additionalProperties":false,
		"required":%s,
		"properties":{
			"provider":{"type":"string","pattern":"^[a-z0-9-]{1,32}$"},
			"event":{"type":"string","pattern":"^[a-z0-9_.-]{1,64}$"},
			"repository":{"type":"string","pattern":"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$"},
			"action":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"string","pattern":"^[a-z0-9_.-]{1,64}$"}},
			"conclusion":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"string","pattern":"^[a-z0-9_.-]{1,64}$"}},
			"name":{"type":"string","pattern":"^[\\x20-\\x7e]{1,128}$"},
			"workflow_path":{"type":"string","pattern":"^[A-Za-z0-9_./-]{1,256}$"},
			"number":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"integer","minimum":1}},
			"label":{"type":"array","minItems":1,"maxItems":8,"items":{"type":"string","pattern":"^[\\x20-\\x7e]{1,128}$"}}
		}
	}`, required))
}

type mcpSubscriptions struct {
	cursor *uint64
	state  mcpState
	subs   []filter.Subscription
	mu     sync.Mutex
	// Set during startup and immutable once callers can access the subscriptions.
	imported bool
}

func (s *mcpSubscriptions) list() []filter.Subscription {
	subs, _ := s.snapshot()

	return subs
}

// snapshot returns the subscriptions and cursor together. Their values are immutable.
func (s *mcpSubscriptions) snapshot() ([]filter.Subscription, *uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	return append([]filter.Subscription{}, s.subs...), s.cursor
}

func (s *mcpSubscriptions) change(
	_ context.Context, req *mcp.CallToolRequest, _ json.RawMessage,
) (*mcp.CallToolResult, any, error) {
	// The SDK checks the schema first. Parse the original arguments so number
	// filters use the same integer decoding as gateway sessions.
	sub, err := filter.Parse(req.Params.Arguments)
	if err != nil {
		return nil, nil, fmt.Errorf("parse subscription: %w", err)
	}

	return nil, map[string]bool{"changed": s.set(sub, req.Params.Name == subscribeTool)}, nil
}

func (s *mcpSubscriptions) set(sub filter.Subscription, add bool) bool {
	s.mu.Lock()
	defer s.mu.Unlock()

	index := slices.IndexFunc(s.subs, func(existing filter.Subscription) bool {
		return reflect.DeepEqual(existing, sub)
	})

	switch {
	case add && index < 0:
		s.subs = append(s.subs, sub)
	case !add && index >= 0:
		s.subs = slices.Delete(s.subs, index, index+1)
	default:
		return false
	}

	s.saveLocked()

	return true
}
