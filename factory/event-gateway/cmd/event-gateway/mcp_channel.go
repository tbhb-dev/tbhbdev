// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"slices"
	"strings"
	"sync"

	"github.com/modelcontextprotocol/go-sdk/jsonrpc"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	jsonv2 "encoding/json/v2"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

// mcpNotifier writes JSON-RPC notifications the SDK session doesn't expose.
// It shares the serialized output writer with the SDK's stdio transport.
type mcpNotifier struct{ output io.Writer }

func (t *mcpNotifier) notify(ctx context.Context, params json.RawMessage) error {
	if t.output == nil {
		return errNoConnection
	}

	if ctx.Err() != nil {
		return fmt.Errorf("write MCP notification: %w", ctx.Err())
	}

	data, err := jsonrpc.EncodeMessage(&jsonrpc.Request{
		ID: jsonrpc.ID{}, Method: channelMethod, Params: params, Extra: nil,
	})
	if err != nil {
		return fmt.Errorf("encode MCP notification: %w", err)
	}

	_, err = t.output.Write(append(data, '\n'))
	if err != nil {
		return fmt.Errorf("write MCP notification: %w", err)
	}

	return nil
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
		eventKey:           channelAttribute(event.event),
		"delivery":         channelAttribute(event.delivery),
	}
	if event.repository != nil {
		meta[channelRepositoryKey] = channelAttribute(*event.repository)
	}

	object := *sessionJSON[map[string]json.RawMessage](event.payload)

	var action *string

	if json.Unmarshal(object["action"], &action) == nil && action != nil {
		meta["action"] = channelAttribute(*action)
	}

	if len(event.payload) > maxChannelContent {
		meta["truncated"] = channelTruncated
		content := fmt.Sprintf("%s %s (action '%s'): payload of %d bytes omitted.",
			meta[channelProviderKey], meta[eventKey], meta["action"], len(event.payload))

		return channelParameters{Meta: meta, Content: content}
	}

	return channelParameters{Meta: meta, Content: escapeChannelFraming(string(event.payload))}
}

func escapeChannelFraming(value string) string {
	return strings.NewReplacer("<", `\u003c`, ">", `\u003e`, "&", `\u0026`).Replace(value)
}

func channelAttribute(value string) string {
	return escapeChannelFraming(strings.ToValidUTF8(value[:min(len(value), maxChannelAttribute)], ""))
}

func (t *mcpNotifier) deliverChannel(ctx context.Context, session *mcp.ServerSession, event channelEvent) error {
	params := session.InitializeParams()
	if params == nil || params.ClientInfo == nil || params.ClientInfo.Name != claudeCodeClient {
		return errChannelClient
	}

	data, err := jsonv2.Marshal(channelParams(event))
	if err != nil {
		return fmt.Errorf("encode MCP channel event: %w", err)
	}

	return t.notify(ctx, data)
}

func (t *mcpNotifier) checkDelivery(
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
	payload, err := jsonv2.Marshal(notice)
	if err != nil {
		payload = []byte(`{"level":"warning","text":"Gateway notice unavailable."}`)
	}

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
