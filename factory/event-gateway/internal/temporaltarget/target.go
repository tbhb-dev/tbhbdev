// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words eventfields temporaltarget

// Package temporaltarget delivers event references to Temporal workflows.
package temporaltarget

import (
	"context"
	"encoding/json"
	"encoding/json/jsontext"
	"errors"
	"fmt"
	"regexp"
	"slices"

	jsonv2 "encoding/json/v2"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/eventfields"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/routing"
)

// The SDK adapter maps server errors to these sentinels.
var (
	ErrTransient      = errors.New("transient")
	ErrAlreadyStarted = errors.New("already started")
	ErrNotFound       = errors.New("not found")
)

// IDReuse selects how a closed workflow ID may be reused.
type IDReuse uint8

// Workflow ID reuse policies.
const (
	AllowDuplicate IDReuse = iota + 1
	AllowDuplicateFailedOnly
	RejectDuplicate
)

// StartRequest starts a workflow with an event reference.
type StartRequest struct {
	WorkflowID   string
	TaskQueue    string
	WorkflowType string
	Input        []byte
	IDReuse      IDReuse
}

// SignalRequest signals the current run with an event reference.
type SignalRequest struct {
	WorkflowID, SignalName string
	Input                  []byte
}

// SignalWithStartRequest starts a workflow if needed and signals it with the same input.
type SignalWithStartRequest struct {
	SignalName   string
	StartRequest StartRequest
}

// Client is the Temporal adapter. Start uses the use-existing conflict policy.
// Signal uses an empty run ID.
type Client interface {
	Start(ctx context.Context, req StartRequest) error
	Signal(ctx context.Context, req SignalRequest) error
	SignalWithStart(ctx context.Context, req SignalWithStartRequest) error
}

// Target runs the actions of one Temporal target instance.
type Target struct {
	client  Client
	actions map[string]action
	name    string
}

type deliveryError struct{ step, class string }

func (e deliveryError) Error() string { return "temporal " + e.step + ": " + e.class + " failure" }

func (e deliveryError) Permanent() bool { return e.class == permanent }

const (
	maxReferenceBytes = 4096
	startKind         = "start"
	signalKind        = "signal"
	permanent         = "permanent"
)

// Act delivers an event to the workflow selected by the rule.
func (t *Target) Act(ctx context.Context, rule routing.Rule, fields eventfields.Fields, envelope []byte) error {
	action, ok := t.actions[rule.Name]
	if !ok {
		return deliveryError{"rule lookup", permanent}
	}

	workflow, err := action.id.Render(fields)
	if err != nil {
		return deliveryError{"workflow ID render", permanent}
	}

	input, err := reference(&fields, envelope, rule.Name, t.name)
	if err != nil || len(input) > maxReferenceBytes {
		return deliveryError{"event reference", permanent}
	}

	start := StartRequest{
		WorkflowID:   workflow,
		TaskQueue:    action.queue,
		WorkflowType: action.workflow,
		IDReuse:      action.reuse,
		Input:        input,
	}
	switch action.kind {
	case startKind:
		err = t.client.Start(ctx, start)
	case signalKind:
		err = t.client.Signal(ctx, SignalRequest{WorkflowID: workflow, SignalName: action.signal, Input: input})
	default:
		err = t.client.SignalWithStart(ctx, SignalWithStartRequest{StartRequest: start, SignalName: action.signal})
	}

	switch {
	case err == nil, action.reuse == RejectDuplicate && errors.Is(err, ErrAlreadyStarted):
		return nil
	case errors.Is(err, ErrTransient), errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
		return deliveryError{action.kind, "transient"}
	default:
		return deliveryError{action.kind, permanent}
	}
}

func reference(fields *eventfields.Fields, envelope []byte, rule, target string) ([]byte, error) {
	var metadata map[string]json.RawMessage
	if json.Unmarshal(envelope, &metadata) != nil {
		metadata = nil
	}

	receivedAt := readString(metadata["received_at"])
	value := func(name string, field any) any {
		if fields.Has(name) {
			return field
		}

		return nil
	}
	repository := value("repository", fields.Repository)

	input, err := jsonv2.Marshal(map[string]any{
		"seq": fields.Seq, "provider": fields.Provider, "event": fields.Event, "delivery": fields.Delivery,
		"repository": repository, "received_at": receivedAt, "rule": rule, "target": target,
		"fields": map[string]any{
			"action": value("action", fields.Action), "number": value("number", fields.Number),
			"head_sha": value("head_sha", fields.HeadSHA), "label": value("label", fields.Label),
			"sender": value("sender", fields.Sender),
		},
	}, jsonv2.Deterministic(true), jsontext.EscapeForHTML(true), jsontext.EscapeForJS(true))
	if err != nil {
		return nil, fmt.Errorf("encode Temporal event reference: %w", err)
	}

	return input, nil
}

type configError string

func (e configError) Error() string { return string(e) }

var namePattern = regexp.MustCompile(`^[^\p{Cc}]{1,255}$`)

func readObject(raw json.RawMessage, keys ...string) map[string]json.RawMessage {
	var object map[string]json.RawMessage
	if json.Unmarshal(raw, &object) != nil {
		return nil
	}

	for key := range object {
		if !slices.Contains(keys, key) {
			return nil
		}
	}

	return object
}

func readString(raw json.RawMessage) string {
	var value string

	if json.Unmarshal(raw, &value) != nil {
		return ""
	}

	return value
}

type action struct {
	id                    *eventfields.Template
	kind, queue, workflow string
	signal                string
	reuse                 IDReuse
}

// New prepares the actions for a Temporal target.
func New(config routing.Target, client Client) (*Target, error) {
	if config.Type != "temporal" {
		return nil, configError("invalid temporal target type")
	}

	target := &Target{name: config.Name, client: client, actions: make(map[string]action, len(config.Rules))}
	for _, rule := range config.Rules {
		parsed, err := parseAction(rule.Action)
		if err != nil {
			return nil, fmt.Errorf("temporal rule %q: %w", rule.Name, err)
		}

		target.actions[rule.Name] = parsed
	}

	return target, nil
}

func parseAction(raw json.RawMessage) (action, error) {
	object := readObject(raw, "kind", "task_queue", "workflow_type", "workflow_id", "id_reuse", "signal")
	kind := readString(object["kind"])
	queue, workflow := readString(object["task_queue"]), readString(object["workflow_type"])
	signal := readString(object["signal"])

	reuse := map[string]IDReuse{
		"allow_duplicate": AllowDuplicate, "allow_duplicate_failed_only": AllowDuplicateFailedOnly,
		"reject_duplicate": RejectDuplicate,
	}[readString(object["id_reuse"])]
	switch {
	case kind != signalKind && kind != startKind && kind != "signal_with_start",
		kind == signalKind && object["id_reuse"] != nil,
		kind != signalKind && (object["task_queue"] == nil || object["workflow_type"] == nil || reuse == 0),
		kind == startKind && object["signal"] != nil,
		kind != startKind && !namePattern.MatchString(signal):
		return action{}, configError("invalid action settings")
	}

	for _, key := range []string{"task_queue", "workflow_type"} {
		if rawName := object[key]; rawName != nil && !namePattern.MatchString(readString(rawName)) {
			return action{}, configError("invalid start name")
		}
	}

	id, err := eventfields.ParseTemplate(readString(object["workflow_id"]))
	if err != nil {
		return action{}, configError("invalid workflow ID template")
	}

	return action{id: id, kind: kind, queue: queue, workflow: workflow, signal: signal, reuse: reuse}, nil
}
