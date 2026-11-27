// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words eventfields temporaltarget

package temporaltarget

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/eventfields"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/routing"
)

const (
	workflowID = `"workflow_id":"pr-{{.number}}"`
	start      = `"task_queue":"queue","workflow_type":"workflow","id_reuse":`
	signal     = `"signal":"update"`
)

func rule(action string) routing.Rule {
	return routing.Rule{Name: "review-pr-head", Action: json.RawMessage(action)}
}

func TestNew(t *testing.T) {
	t.Parallel()

	const (
		startAction  = `{"kind":"start",` + workflowID + ","
		signalAction = `{"kind":"signal",` + workflowID + ","
		reject       = `"id_reuse":"reject_duplicate"`
	)
	for _, raw := range []string{
		`null`, `[]`, `{}`, `{"kind":"private"}`, signalAction + `"private":true}`,
		`{"kind":"signal",` + signal + `}`, `{"kind":"signal",` + signal + `,"workflow_id":"{{.private}}"}`,
		signalAction + "}", signalAction + `"signal":"private\n"}`,
		signalAction + `"signal":"` + strings.Repeat("a", 256) + `"}`,
		signalAction + `"id_reuse":null,` + signal + "}", signalAction + `"task_queue":null,` + signal + "}",
		startAction + `"workflow_type":"workflow",` + reject + "}", startAction + `"task_queue":"queue",` + reject + "}",
		startAction + `"task_queue":"private\u0085","workflow_type":"workflow",` + reject + "}",
		startAction + `"task_queue":"queue","workflow_type":"",` + reject + "}",
		startAction + `"task_queue":"queue","workflow_type":"workflow"}`, startAction + start + `"private"}`,
		startAction + start + `"reject_duplicate","signal":null}`,
		`{"kind":"signal_with_start",` + workflowID + "," + start + `"reject_duplicate"}`,
	} {
		r := rule(raw)

		_, err := New(testConfig(r), nil)
		if err == nil || !strings.Contains(err.Error(), r.Name) || strings.Contains(err.Error(), "private") {
			t.Errorf("invalid action %s returned %v", raw, err)
		}
	}

	config := testConfig()

	config.Type = "private"

	_, err := New(config, nil)
	if err == nil || strings.Contains(err.Error(), "private") {
		t.Fatalf("invalid target type returned %v", err)
	}
}

type client func(any) error

func (c client) Start(_ context.Context, req StartRequest) error   { return c(req) }
func (c client) Signal(_ context.Context, req SignalRequest) error { return c(req) }
func (c client) SignalWithStart(_ context.Context, req SignalWithStartRequest) error {
	return c(req)
}

func testConfig(rules ...routing.Rule) routing.Target {
	var config routing.Target

	config.Name, config.Type, config.Rules = "temporal-main", "temporal", rules

	return config
}

func target(t *testing.T, c client, rules ...routing.Rule) *Target {
	t.Helper()

	got, err := New(testConfig(rules...), c)
	if err != nil {
		t.Fatal(err)
	}

	return got
}

func checkDelivery(t *testing.T, err error, step, class string) {
	t.Helper()

	if class == "" && err == nil {
		return
	}

	classified, ok := err.(interface{ Permanent() bool })
	if !ok || class == "" || classified.Permanent() != (class == permanent) ||
		err.Error() != "temporal "+step+": "+class+" failure" {
		t.Fatalf("error = %v, want %s %s failure", err, step, class)
	}
}

func TestAct(t *testing.T) {
	t.Parallel()

	envelope := []byte(
		`{"provider":"github","event":"pull_request","delivery":"private-delivery","repository":"owner/name",
		"received_at":"2026-11-21T18:04:05Z","payload":{"action":"synchronize","pull_request":{"number":12,"head":{"sha":""}},
		"sender":{"login":"private-sender"}}}`,
	)

	fields, err := eventfields.FromEnvelope(48213, envelope)
	if err != nil {
		t.Fatal(err)
	}

	input := []byte(`{"delivery":"private-delivery","event":"pull_request","fields":` +
		`{"action":"synchronize","head_sha":"","label":null,"number":12,"sender":"private-sender"},` +
		`"provider":"github","received_at":"2026-11-21T18:04:05Z","repository":"owner/name",` +
		`"rule":"review-pr-head","seq":48213,"target":"temporal-main"}`)
	for policy, reuse := range map[string]IDReuse{
		"allow_duplicate":             AllowDuplicate,
		"allow_duplicate_failed_only": AllowDuplicateFailedOnly,
		"reject_duplicate":            RejectDuplicate,
	} {
		req := StartRequest{
			WorkflowID:   "pr-12",
			TaskQueue:    "queue",
			WorkflowType: "workflow",
			IDReuse:      reuse,
			Input:        input,
		}

		withStart := SignalWithStartRequest{StartRequest: req, SignalName: "update"}
		for _, action := range []struct {
			request     any
			kind, extra string
		}{
			{req, startKind, start + strconv.Quote(policy)},
			{withStart, "signal_with_start", start + strconv.Quote(policy) + "," + signal},
			{SignalRequest{WorkflowID: "pr-12", SignalName: "update", Input: input}, signalKind, signal},
		} {
			checkTemporalAction(t, fields, envelope, reuse, action.request, action.kind, action.extra)
		}
	}
}

func checkTemporalAction(
	t *testing.T, fields eventfields.Fields, envelope []byte, reuse IDReuse, expected any, kind, extra string,
) {
	t.Helper()

	match := rule(`{"kind":"` + kind + `",` + workflowID + "," + extra + "}")

	var response error

	got := target(t, func(request any) error {
		if !reflect.DeepEqual(request, expected) {
			t.Fatalf("%s request = %#v, want %#v", kind, request, expected)
		}

		return response
	}, match)

	const transient = "transient"
	for _, test := range []struct {
		err   error
		class string
	}{
		{nil, ""},
		{ErrTransient, transient},
		{context.DeadlineExceeded, transient},
		{context.Canceled, transient},
		{ErrAlreadyStarted, permanent},
		{ErrNotFound, permanent},
		{io.EOF, permanent},
	} {
		response = nil
		if test.err != nil {
			response = fmt.Errorf("%s %s: %w", fields.Delivery, fields.Sender, test.err)
		}

		class := test.class
		if errors.Is(test.err, ErrAlreadyStarted) && kind != signalKind && reuse == RejectDuplicate {
			class = ""
		}

		checkDelivery(t, got.Act(t.Context(), match, fields, envelope), kind, class)
	}
}

func TestActValidation(t *testing.T) {
	t.Parallel()

	match := rule(`{"kind":"signal","workflow_id":"fixed",` + signal + `}`)
	missing := rule(`{"kind":"signal",` + workflowID + "," + signal + "}")
	missing.Name = "missing-number"

	var fields eventfields.Fields

	base, err := reference(&fields, nil, match.Name, "temporal-main")
	sdkCheck(t, err)
	got := target(t, func(request any) error {
		signal, ok := request.(SignalRequest)
		if !ok || len(signal.Input) != 4096 {
			t.Fatalf("expected a 4096-byte signal, got %T with %d bytes", request, len(signal.Input))
		}

		return nil
	}, match, missing)
	unknown := routing.Rule{Name: "unknown", Action: nil}
	checkDelivery(t, got.Act(t.Context(), unknown, fields, nil), "rule lookup", permanent)
	checkDelivery(t, got.Act(t.Context(), missing, fields, nil), "workflow ID render", permanent)

	if !strings.Contains(string(base), `"received_at":"","repository":null`) {
		t.Fatalf("missing metadata in reference %s", base)
	}

	fields.Delivery = strings.Repeat("a", 4096-len(base))
	checkDelivery(t, got.Act(t.Context(), match, fields, nil), "signal", "")
	fields.Delivery += "a"
	checkDelivery(t, got.Act(t.Context(), match, fields, nil), "event reference", permanent)
	fields.Delivery = "\xff"
	checkDelivery(t, got.Act(t.Context(), match, fields, nil), "event reference", permanent)
}
