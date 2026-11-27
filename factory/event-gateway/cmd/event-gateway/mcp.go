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
	"sync"

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
	eventKey              = "event"
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

func runMCP(ctx context.Context, logHandler slog.Handler, stdin io.ReadCloser, stdout io.Writer, args []string) error {
	if len(args) != 0 {
		return errMCPUsage
	}

	return serveMCP(ctx, logHandler, stdin, stdout)
}

func serveMCP(ctx context.Context, logHandler slog.Handler, stdin io.ReadCloser, stdout io.Writer) error {
	return serveMCPWithEnv(ctx, logHandler, stdin, stdout, os.Getenv)
}

func serveMCPWithEnv(
	ctx context.Context, logHandler slog.Handler, stdin io.ReadCloser, stdout io.Writer, getenv func(string) string,
) error {
	defer stdin.Close()

	logger := slog.New(logHandler)
	subs := restoreMCPSubscriptions(ctx, logger, getenv)

	runCtx, cancel := context.WithCancel(ctx)
	output := &mcpStdout{writer: stdout, done: runCtx.Done(), mu: sync.Mutex{}}
	transport := &mcp.IOTransport{
		Reader:        mcpStdin{Reader: stdin, cancel: cancel},
		Writer:        output,
		MaxLineLength: 0,
	}
	notifier := &mcpNotifier{output: output}
	initialized, stopGateway := startMCPGateway(runCtx, subs, notifier, logger, getenv)

	defer stopGateway()

	server := newMCPServer(logger, subs, notifier, initialized)

	// Let the SDK handle EOF and report any transport error.
	err := server.Run(ctx, transport)

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
	count, err := in.Reader.Read(p)
	switch {
	case errors.Is(err, io.EOF):
		in.cancel()

		return count, io.EOF
	case err != nil:
		return count, fmt.Errorf("read MCP stdin: %w", err)
	default:
		return count, nil
	}
}

type mcpStdout struct {
	writer io.Writer
	done   <-chan struct{}
	mu     sync.Mutex
}

func (*mcpStdout) Close() error { return nil }

func (out *mcpStdout) Write(data []byte) (int, error) {
	out.mu.Lock()
	defer out.mu.Unlock()

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
	notifier *mcpNotifier,
	initialized func(context.Context, *mcp.InitializedRequest),
) *mcp.Server {
	// The supported protocol versions still advertise the legacy logging capability.
	capabilities := mcpCapabilities([]byte(`{"logging":{}}`))

	capabilities.Experimental = map[string]any{channelCapability: map[string]any{}}
	capabilities.Tools = &mcp.ToolCapabilities{ListChanged: false}

	var options mcp.ServerOptions

	options.Logger = logger
	options.InitializedHandler = initialized
	options.Instructions = "After a restart or reconnect, call check_delivery and confirm both events arrive: " +
		"self_test and echo."
	options.Capabilities = capabilities
	// These versions use initialize, which supplies the client's name.
	options.SupportedProtocolVersions = []string{"2025-11-25", "2025-06-18", "2025-03-26"}

	server := mcp.NewServer(mcpImplementation(mcpServerName), &options)
	addMCPTools(server, subs, notifier)

	return server
}

func mcpCapabilities(legacy []byte) *mcp.ServerCapabilities {
	var capabilities mcp.ServerCapabilities

	err := json.Unmarshal(legacy, &capabilities)
	if err != nil {
		panic(err)
	}

	return &capabilities
}

func mcpImplementation(name string) *mcp.Implementation {
	return &mcp.Implementation{Title: "", Description: "", WebsiteURL: "", Icons: nil, Name: name, Version: version}
}

func addMCPTools(server *mcp.Server, subs *mcpSubscriptions, notifier *mcpNotifier) {
	mcp.AddTool(server, mcpTool(subscribeTool, "Start receiving matching webhook events in this session.",
		subscriptionSchema(`["provider","event","repository"]`)), subs.change)
	mcp.AddTool(server, mcpTool(unsubscribeTool, "Stop receiving the events a subscription matched.",
		subscriptionSchema(`["provider"]`)), subs.change)
	mcp.AddTool(server, mcpTool(listSubscriptionsTool, "List this session's subscriptions.",
		json.RawMessage(noArgumentsSchema)),
		func(context.Context, *mcp.CallToolRequest, json.RawMessage) (*mcp.CallToolResult, any, error) {
			return nil, map[string][]filter.Subscription{subsKey: subs.list()}, nil
		},
	)
	mcp.AddTool(server, mcpTool(checkDeliveryTool,
		"Send a self-test event and confirm it reaches this session. "+
			"This proves delivery to the client over stdio but does not prove the gateway connection.",
		json.RawMessage(noArgumentsSchema)), notifier.checkDelivery)
}

func mcpTool(name, description string, schema json.RawMessage) *mcp.Tool {
	return &mcp.Tool{
		Meta: nil, Annotations: nil, OutputSchema: nil, Title: "", Icons: nil,
		Name: name, Description: description, InputSchema: schema,
	}
}
