// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package filter validates subscriptions and matches them against events.
package filter

import (
	"cmp"
	"encoding/json"
	"regexp"
	"slices"
	"strings"
)

// Subscription selects events and optionally filters their payload fields.
type Subscription struct {
	Provider   string   `json:"provider,omitempty"`
	Event      string   `json:"event,omitempty"`
	Repository string   `json:"repository,omitempty"`
	Action     []string `json:"action,omitempty"`
	Conclusion []string `json:"conclusion,omitempty"`
	Name       string   `json:"name,omitempty"`
	//nolint:tagliatelle // The protocol uses workflow_path on the wire.
	WorkflowPath string   `json:"workflow_path,omitempty"`
	Number       []int64  `json:"number,omitempty"`
	Label        []string `json:"label,omitempty"`
}

// Error reports a subscription validation failure with its protocol code.
type Error struct {
	Code    string
	Message string
}

func (e *Error) Error() string { return e.Message }

const (
	badMessage      = "bad_message"
	badSubscription = "bad_subscription"
	github          = "github"
	maxList         = 8
	maxNumber       = 1<<53 - 1

	// Spaces delimit whole event names, including the first and last.
	actionEvents = " branch_protection_configuration branch_protection_rule check_run check_suite code_scanning_alert " +
		"commit_comment custom_property custom_property_values dependabot_alert deploy_key deployment " +
		"deployment_protection_rule deployment_review deployment_status discussion discussion_comment " +
		"github_app_authorization installation installation_repositories installation_target issue_comment " +
		"issue_dependencies issue_relates_to issues label marketplace_purchase member membership merge_group " +
		"meta milestone org_block organization package personal_access_token_request project project_card " +
		"project_column projects_v2 projects_v2_item projects_v2_status_update pull_request pull_request_review " +
		"pull_request_review_comment pull_request_review_thread registry_package release repository " +
		"repository_advisory repository_dispatch repository_ruleset repository_vulnerability_alert " +
		"secret_scanning_alert secret_scanning_alert_location secret_scanning_scan security_advisory " +
		"sponsorship star sub_issues team watch workflow_job workflow_run "
	conclusionEvents = " check_run check_suite workflow_run workflow_job "
	nameEvents       = " check_run workflow_run workflow_job "
	pathEvents       = " workflow_run "
	itemEvents       = " issues issue_comment pull_request pull_request_review "
)

var (
	providerPattern   = regexp.MustCompile(`^[a-z0-9-]{1,32}$`)
	tokenPattern      = regexp.MustCompile(`^[a-z0-9_.-]{1,64}$`)
	repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$`)
	namePattern       = regexp.MustCompile(`^[\x20-\x7e]{1,128}$`)
	pathPattern       = regexp.MustCompile(`^[A-Za-z0-9_./-]{1,256}$`)
)

// Parse validates and normalizes a subscription received as JSON.
func Parse(raw json.RawMessage) (Subscription, error) {
	object := decode[map[string]json.RawMessage](raw)
	if object == nil {
		return Subscription{}, &Error{Code: badMessage, Message: "subscription must be an object"}
	}

	var sub Subscription

	err := parseScope(&sub, object)
	if err == nil {
		err = parseFilters(&sub, object)
	}

	if err != nil {
		return Subscription{}, err
	}

	if sub.Provider == github {
		switch {
		// check_suite allows conclusion only. The other three also allow name.
		case strings.Contains(conclusionEvents, " "+sub.Event+" ") && sub.Name == "" && len(sub.Conclusion) == 0:
			return Subscription{}, &Error{Code: badSubscription, Message: "missing required filter for " + sub.Event}
		case sub.Event == "" || sub.Repository == "":
			return Subscription{}, &Error{
				Code:    badSubscription,
				Message: "github subscription is missing event and repository",
			}
		}
	}

	return sub, nil
}

func parseScope(sub *Subscription, object map[string]json.RawMessage) *Error {
	for _, field := range []struct {
		target  *string
		pattern *regexp.Regexp
		key     string
	}{
		{&sub.Provider, providerPattern, "provider"},
		{&sub.Event, tokenPattern, "event"},
		{&sub.Repository, repositoryPattern, "repository"},
	} {
		raw, present := object[field.key]
		if !present && field.key != "provider" {
			continue
		}

		value := decode[*string](raw)
		if value == nil {
			return &Error{Code: badMessage, Message: field.key + " must be a string"}
		}

		if !field.pattern.MatchString(*value) {
			return &Error{Code: badSubscription, Message: "invalid " + field.key}
		}

		*field.target = *value
	}

	sub.Repository = strings.ToLower(sub.Repository)

	return nil
}

func parseFilters(sub *Subscription, object map[string]json.RawMessage) *Error {
	for _, field := range []struct {
		read   func(json.RawMessage) bool
		key    string
		events string
	}{
		{listReader(&sub.Action, tokenPattern.MatchString), "action", actionEvents},
		{listReader(&sub.Conclusion, tokenPattern.MatchString), "conclusion", conclusionEvents},
		{textReader(&sub.Name, namePattern), "name", nameEvents},
		{textReader(&sub.WorkflowPath, pathPattern), "workflow_path", pathEvents},
		{listReader(&sub.Number, func(n int64) bool { return n >= 1 && n <= maxNumber }), "number", itemEvents},
		{listReader(&sub.Label, namePattern.MatchString), "label", itemEvents},
	} {
		raw, present := object[field.key]
		switch {
		case !present:
			continue
		case sub.Provider != github:
			return &Error{Code: badSubscription, Message: field.key + " is supported only for github"}
		case sub.Event == "" || sub.Repository == "":
			return &Error{Code: badSubscription, Message: "filtered subscriptions require event and repository"}
		case !strings.Contains(field.events, " "+sub.Event+" "):
			return &Error{Code: badSubscription, Message: field.key + " is unsupported for " + sub.Event}
		case !field.read(raw):
			return &Error{Code: badSubscription, Message: "invalid " + field.key}
		}
	}

	return nil
}

func textReader(target *string, pattern *regexp.Regexp) func(json.RawMessage) bool {
	return func(raw json.RawMessage) bool {
		*target = decode[string](raw)

		return pattern.MatchString(*target)
	}
}

func listReader[T cmp.Ordered](target *[]T, valid func(T) bool) func(json.RawMessage) bool {
	return func(raw json.RawMessage) bool {
		values := decode[[]T](raw)
		if len(values) == 0 || len(values) > maxList ||
			slices.ContainsFunc(values, func(value T) bool { return !valid(value) }) {
			return false
		}

		slices.Sort(values)
		*target = slices.Compact(values)

		return true
	}
}

//nolint:ireturn // Callers choose the concrete JSON value type.
func decode[T any](raw json.RawMessage) T {
	var value, zero T

	err := json.Unmarshal(raw, &value)
	if err != nil {
		return zero
	}

	return value
}

// Fields holds the payload values used by subscription filters.
type Fields struct {
	Action       string
	Conclusion   string
	Name         string
	WorkflowPath string
	Number       *int64
	Labels       []string
}

// Extract reads filter values from the payload of an event.
func Extract(event string, payload []byte) Fields {
	object := decode[map[string]json.RawMessage](payload)
	eventObject := decode[map[string]json.RawMessage](object[event])
	item := eventObject

	switch event {
	case "issues", "issue_comment":
		item = decode[map[string]json.RawMessage](object["issue"])
	case "pull_request_review":
		item = decode[map[string]json.RawMessage](object["pull_request"])
	}

	fields := Fields{
		Action:       decode[string](object["action"]),
		Conclusion:   decode[string](eventObject["conclusion"]),
		Name:         decode[string](eventObject["name"]),
		WorkflowPath: decode[string](eventObject["path"]),
		Number:       decode[*int64](item["number"]),
		Labels:       nil,
	}

	for _, label := range decode[[]json.RawMessage](item["labels"]) {
		fields.Labels = appendLabel(fields.Labels, label)
	}

	switch fields.Action {
	case "labeled", "unlabeled":
		fields.Labels = appendLabel(fields.Labels, object["label"])
	}

	return fields
}

func appendLabel(labels []string, raw json.RawMessage) []string {
	object := decode[map[string]json.RawMessage](raw)
	if name := decode[*string](object["name"]); name != nil {
		return append(labels, *name)
	}

	return labels
}

// Matches reports whether the event matches the subscription and all its filters.
func (s Subscription) Matches(provider, event, repository string, fields Fields) bool {
	return s.Provider == provider &&
		(s.Event == "" || s.Event == event) &&
		(s.Repository == "" || strings.EqualFold(s.Repository, repository)) &&
		s.matchesPayload(fields)
}

func (s Subscription) matchesPayload(fields Fields) bool {
	return (len(s.Action) == 0 || slices.Contains(s.Action, fields.Action)) &&
		(len(s.Conclusion) == 0 || slices.Contains(s.Conclusion, fields.Conclusion)) &&
		(s.Name == "" || s.Name == fields.Name) &&
		(s.WorkflowPath == "" || s.WorkflowPath == fields.WorkflowPath) &&
		(len(s.Number) == 0 || (fields.Number != nil && slices.Contains(s.Number, *fields.Number))) &&
		(len(s.Label) == 0 || slices.ContainsFunc(fields.Labels, func(label string) bool {
			return slices.Contains(s.Label, label)
		}))
}
