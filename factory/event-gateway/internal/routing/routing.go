// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package routing loads routing rules for stored events.
package routing

import (
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

// Config lists the configured target instances.
type Config struct {
	Targets []Target
}

// Target holds a target instance's settings and routing rules.
type Target struct {
	Name       string
	Type       string
	Start      Start
	Connection json.RawMessage
	Rules      []Rule
}

// StartKind selects the initial position in the event stream.
type StartKind uint8

// Start positions for a target.
const (
	New  StartKind = iota // Start with new events.
	All                   // Start at the beginning of the stream.
	Time                  // Start at the specified time.
)

// Start describes where a target begins reading events.
type Start struct {
	Time time.Time
	Kind StartKind
}

// Rule holds a compiled event match and an opaque target action.
type Rule struct {
	events       map[string]filter.Subscription
	Name         string
	Action       json.RawMessage
	repositories []string
}

const (
	maxEvents       = 16
	maxRepositories = 32
)

var (
	namePattern       = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	typePattern       = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	eventPattern      = regexp.MustCompile(`^[a-z0-9_-]{1,64}$`)
	repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,39}/([A-Za-z0-9_.-]{1,100}|\*)$`)
)

type configError string

func (e configError) Error() string { return string(e) }

// TargetError identifies the validated target name when its configuration fails.
type TargetError struct {
	err  error
	Name string
}

func (e TargetError) Error() string { return e.err.Error() }

func (e TargetError) Unwrap() error { return e.err }

// Load decodes and validates a JSON routing configuration.
func Load(data []byte) (Config, error) {
	object, err := readObject(data, "targets")
	if err != nil {
		return Config{}, fmt.Errorf("routing config: %w", err)
	}

	targets, err := loadEntries(object["targets"], "target", loadTarget, "name", "type", "start", "connection", "rules")

	return Config{Targets: targets}, err
}

func loadEntries[T any](
	raw json.RawMessage, kind string, load func(string, map[string]json.RawMessage) (T, error), keys ...string,
) ([]T, error) {
	entries := decode[[]json.RawMessage](raw)
	if len(entries) == 0 {
		return nil, fmt.Errorf("%ss: %w", kind, configError("expected one or more entries"))
	}

	result := make([]T, len(entries))
	names := make(map[string]bool)

	for index, entry := range entries {
		object, err := readObject(entry, keys...)
		name := decode[string](object["name"])

		switch {
		case err != nil:
		case !namePattern.MatchString(name):
			err = configError("invalid name")
		case names[name]:
			err = configError("duplicate name")
		default:
			names[name] = true
			result[index], err = load(name, object)
		}

		if err != nil {
			return nil, fmt.Errorf("%s %d (%q): %w", kind, index, name, err)
		}
	}

	return result, nil
}

func loadTarget(name string, object map[string]json.RawMessage) (Target, error) {
	kind := decode[string](object["type"])
	if !typePattern.MatchString(kind) {
		return Target{}, configError("invalid type")
	}

	start, err := parseStart(object["start"])
	if err != nil {
		return Target{}, TargetError{err: err, Name: name}
	}

	connection := object["connection"]
	if connection != nil && !bytes.HasPrefix(connection, []byte("{")) {
		return Target{}, TargetError{err: configError("connection must be a JSON object"), Name: name}
	}

	rules, err := loadEntries(object["rules"], "rule", loadRule, "name", "match", "action")
	if err != nil {
		return Target{}, TargetError{err: err, Name: name}
	}

	return Target{Name: name, Type: kind, Start: start, Connection: connection, Rules: rules}, nil
}

func loadRule(name string, object map[string]json.RawMessage) (Rule, error) {
	rule := Rule{Name: name, Action: object["action"], repositories: nil, events: nil}
	if !bytes.HasPrefix(rule.Action, []byte("{")) {
		return Rule{}, configError("action must be a JSON object")
	}

	err := compileMatch(&rule, object["match"])

	return rule, err
}

func parseStart(raw json.RawMessage) (Start, error) {
	value := decode[string](raw)
	switch {
	case raw == nil, value == "new":
		return Start{Kind: New, Time: time.Time{}}, nil
	case value == "all":
		return Start{Kind: All, Time: time.Time{}}, nil
	}

	start, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return Start{}, fmt.Errorf("start: %w", err)
	}

	return Start{Kind: Time, Time: start}, nil
}

func compileMatch(rule *Rule, raw json.RawMessage) error {
	object, err := readObject(raw, "provider", "event", "repository",
		"action", "conclusion", "name", "workflow_path", "number", "label")
	if err != nil {
		return fmt.Errorf("match: %w", err)
	}

	eventJSON := object["event"]

	events := decode[[]string](eventJSON)
	if bytes.HasPrefix(eventJSON, []byte(`"`)) {
		events = []string{decode[string](eventJSON)}
	}

	slices.Sort(events)

	events = slices.Compact(events)

	err = validateTokens(events, maxEvents, eventPattern)
	if err != nil {
		return fmt.Errorf("event: %w", err)
	}

	if repos := object["repository"]; repos != nil {
		rule.repositories = decode[[]string](repos)

		err = validateTokens(rule.repositories, maxRepositories, repositoryPattern)
		if err != nil {
			return fmt.Errorf("repository: %w", err)
		}

		for index, repository := range rule.repositories {
			rule.repositories[index] = strings.ToLower(repository)
		}

		object["repository"] = json.RawMessage(`"o/r"`)
	}

	rule.events = make(map[string]filter.Subscription, len(events))

	for _, event := range events {
		object["event"] = json.RawMessage(`"` + event + `"`)
		//nolint:errcheck,errchkjson // Every value is valid raw JSON.
		data, _ := json.Marshal(object)

		subscription, parseErr := filter.Parse(data)
		if parseErr != nil {
			return fmt.Errorf("event %q: %w", event, parseErr)
		}

		subscription.Repository = ""
		rule.events[event] = subscription
	}

	return nil
}

func validateTokens(tokens []string, limit int, pattern *regexp.Regexp) error {
	if len(tokens) == 0 || len(tokens) > limit {
		return configError(fmt.Sprintf("expected 1 to %d entries", limit))
	}

	for _, token := range tokens {
		if !pattern.MatchString(token) {
			return fmt.Errorf("%w %q", configError("invalid entry"), token)
		}
	}

	return nil
}

func readObject(raw []byte, keys ...string) (map[string]json.RawMessage, error) {
	object := decode[map[string]json.RawMessage](raw)
	if object == nil {
		return nil, configError("expected a JSON object")
	}

	for key := range object {
		if !slices.Contains(keys, key) {
			return object, fmt.Errorf("%w %q", configError("unknown key"), key)
		}
	}

	return object, nil
}

//nolint:ireturn // Callers select the concrete JSON type.
func decode[T any](raw json.RawMessage) T {
	var value, zero T

	err := json.Unmarshal(raw, &value)
	if err != nil {
		return zero
	}

	return value
}

// Subjects returns the target's sorted, distinct event subjects.
func (t Target) Subjects() []string {
	subjects := make(map[string]struct{})

	for _, rule := range t.Rules {
		for _, subscription := range rule.events {
			subjects["events."+subscription.Provider+"."+subscription.Event] = struct{}{}
		}
	}

	return slices.Sorted(maps.Keys(subjects))
}

// Matches reports whether an event satisfies the rule.
func (r Rule) Matches(provider, event, repository string, fields filter.Fields) bool {
	subscription, ok := r.events[event]

	return ok && r.matchesRepository(repository) && subscription.Matches(provider, event, repository, fields)
}

func (r Rule) matchesRepository(repository string) bool {
	owner, name, _ := strings.Cut(repository, "/")

	return len(r.repositories) == 0 || slices.ContainsFunc(r.repositories, func(entry string) bool {
		if prefix, wildcard := strings.CutSuffix(entry, "/*"); wildcard {
			return name != "" && !strings.Contains(name, "/") && strings.EqualFold(owner, prefix)
		}

		return strings.EqualFold(entry, repository)
	})
}
