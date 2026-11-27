// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words eventfields

// Package eventfields extracts event values for workflow ID templates.
package eventfields

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

var errEnvelope = errors.New("invalid event envelope")

// Fields holds the values available to a workflow ID template.
type Fields struct {
	present    map[string]bool
	Provider   string
	Event      string
	Delivery   string
	Repository string
	Action     string
	HeadSHA    string
	Sender     string
	Label      string
	Seq        uint64
	Number     int64
}

// Has reports whether the named field was present, even if its value is empty.
func (f *Fields) Has(name string) bool { return f.present[name] }

// FromEnvelope extracts fields from a stored event and its sequence number.
func FromEnvelope(seq uint64, envelope []byte) (Fields, error) {
	object := readObject(envelope)
	if object == nil {
		return Fields{}, fmt.Errorf("%w: expected an object", errEnvelope)
	}

	var fields Fields

	fields.Seq = seq
	fields.present = map[string]bool{"seq": true}
	fields.setString("provider", &fields.Provider, object["provider"])
	fields.setString("event", &fields.Event, object["event"])
	fields.setString("delivery", &fields.Delivery, object["delivery"])

	for _, name := range []string{"provider", "event", "delivery"} {
		if !fields.Has(name) {
			return Fields{}, fmt.Errorf("%w: %s must be a string", errEnvelope, name)
		}
	}

	fields.setString("repository", &fields.Repository, object["repository"])
	rawPayload := object["payload"]
	payload := readObject(rawPayload)
	fields.setString("action", &fields.Action, payload["action"])
	fields.setString("sender", &fields.Sender, readObject(payload["sender"])["login"])
	fields.setString("head_sha", &fields.HeadSHA, headSHA(fields.Event, payload))

	filtered := filter.Extract(fields.Event, rawPayload)
	number := filtered.Number

	if number == nil && fields.Event == "pull_request_review_comment" {
		var value *int64

		if json.Unmarshal(readObject(payload["pull_request"])["number"], &value) == nil {
			number = value
		}
	}

	if number != nil {
		fields.Number = *number
		fields.present["number"] = true
	}

	switch filtered.Action {
	case "labeled", "unlabeled":
		fields.setString("label", &fields.Label, readObject(payload["label"])["name"])
	}

	return fields, nil
}

func (f *Fields) setString(name string, dest *string, raw json.RawMessage) {
	var value *string
	if json.Unmarshal(raw, &value) == nil && value != nil {
		*dest = *value
		f.present[name] = true
	}
}

func readObject(raw json.RawMessage) map[string]json.RawMessage {
	var object map[string]json.RawMessage
	if json.Unmarshal(raw, &object) != nil {
		return nil
	}

	return object
}

func headSHA(event string, payload map[string]json.RawMessage) json.RawMessage {
	switch event {
	case "pull_request", "pull_request_review", "pull_request_review_comment":
		return readObject(readObject(payload["pull_request"])["head"])["sha"]
	case "check_run", "check_suite", "workflow_run", "workflow_job":
		return readObject(payload[event])["head_sha"]
	case "push":
		return payload["after"]
	default:
		return nil
	}
}
