// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package routing_test

import (
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/routing"
)

const (
	grafanaProvider = "grafana"
	githubProvider  = "github"
	grafana         = `"provider":"` + grafanaProvider + `","event":"firing"`
	github          = `"provider":"` + githubProvider + `","repository":["Org/Repo","Team/*"]`
	match           = `"match":{` + grafana + `}`
	action          = `,"action":{}`
	rule            = `{"name":"send",` + match + action + `}`
	target          = `{"name":"sink","type":"webhook","rules":[` + rule + `]}`
	targetErr       = `target 0 ("sink"):`
	ruleErr         = targetErr + ` rule 0 ("send"):`
)

func config(targets string) string { return `{"targets":[` + targets + `]}` }

func rules(entries string) string {
	return config(strings.Replace(target, rule, entries, 1))
}

func matchConfig(scope string) string {
	return rules(strings.Replace(rule, grafana, scope, 1))
}

func load(t *testing.T, raw string) routing.Config {
	t.Helper()

	got, err := routing.Load([]byte(raw))
	if err != nil {
		t.Fatalf("Load(%s): %v", raw, err)
	}

	return got
}

func TestLoad(t *testing.T) {
	t.Parallel()

	const connection = `{ "extra": 1e999 }`

	for _, test := range []struct {
		at    time.Time
		start string
		kind  routing.StartKind
	}{
		{time.Time{}, "", routing.New},
		{time.Time{}, `,"start":"n\u0065w"`, routing.New},
		{time.Time{}, `,"start":"all"`, routing.All},
		{time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC), `,"start":"2026-01-02T03:04:05Z"`, routing.Time},
	} {
		got := load(t, config(strings.TrimSuffix(target, "}")+test.start+"}")).Targets[0]
		if got.Start.Kind != test.kind || !got.Start.Time.Equal(test.at) {
			t.Fatalf("start %q parsed as %+v", test.start, got.Start)
		}
	}

	withObjects := strings.NewReplacer("sink", "other", `"rules"`, `"connection":`+connection+`,"rules"`,
		action, `,"action":`+connection).Replace(target)

	got := load(t, config(target+","+withObjects)).Targets
	if len(got) != 2 || string(got[1].Connection) != connection || string(got[1].Rules[0].Action) != connection {
		t.Fatalf("opaque objects or second target lost: %+v", got)
	}
}

func TestLoadErrors(t *testing.T) {
	t.Parallel()

	const pushError = `event "push"`

	for _, test := range []struct{ raw, want string }{
		{`{"targets":[], "Targets":[]}`, `unknown key "Targets"`},
		{config(""), "targets"},
		{config(strings.Replace(target, "sink", "-sink", 1)), `target 0 ("-sink"): invalid name`},
		{config(target + "," + target), `target 1 ("sink"): duplicate name`},
		{config(strings.Replace(target, "webhook", "0bad", 1)), targetErr + " invalid type"},
		{config(strings.TrimSuffix(target, "}") + `,"extra":1}`), targetErr + " unknown key"},
		{config(strings.TrimSuffix(target, "}") + `,"start":"soon"}`), targetErr + " start"},
		{config(strings.TrimSuffix(target, "}") + `,"connection":[]}`), targetErr + " connection"},
		{rules(strings.Replace(rule, "send", "SEND", 1)), targetErr + ` rule 0 ("SEND"): invalid name`},
		{rules(rule + "," + rule), targetErr + ` rule 1 ("send"): duplicate name`},
		{rules(strings.TrimSuffix(rule, "}") + `,"extra":1}`), ruleErr + " unknown key"},
		{rules(strings.Replace(rule, action, `,"action":null`, 1)), ruleErr + " action"},
		{rules(strings.Replace(rule, action, "", 1)), ruleErr + " action"},
		{rules(strings.Replace(rule, match, `"match":[]`, 1)), ruleErr + " match"},
		{matchConfig(grafana + `,"extra":1`), ruleErr + " match: unknown key"},
		{matchConfig(`"provider":"grafana"`), "event: expected 1 to 16 entries"},
		{matchConfig(`"provider":"grafana","event":` + events(17, 0)), "event: expected 1 to 16 entries"},
		{config(strings.TrimSuffix(target, "}") + `,"start":null}`), targetErr + " start"},
		{config(strings.TrimSuffix(target, "}") + `,"connection":null}`), targetErr + " connection"},
		{matchConfig(`"provider":"grafana","event":"firing.alert"`), "event: invalid entry"},
		{matchConfig(`"provider":"github","event":"push"`), pushError},
		{matchConfig(grafana + `,"repository":["org"]`), "repository: invalid entry"},
		{matchConfig(grafana + `,"repository":["org/re*"]`), "repository: invalid entry"},
		{matchConfig(github + `,"event":"push","action":["opened"]`), pushError},
		{matchConfig(github + `,"event":["issues","push"],"number":[7]`), ruleErr + " " + pushError},
		{matchConfig(github + `,"event":"workflow_run","workflow_path":"ci.yml"`), `event "workflow_run"`},
	} {
		_, err := routing.Load([]byte(test.raw))
		if err == nil || !strings.Contains(err.Error(), test.want) {
			t.Fatalf("Load(%s) = %v, want error containing %q", test.raw, err, test.want)
		}
	}
}

func TestLoadEvents(t *testing.T) {
	t.Parallel()

	for _, list := range []string{events(16, 1), events(16, 16)} {
		load(t, matchConfig(`"provider":"grafana","event":`+list))
	}

	_, err := routing.Load([]byte(matchConfig(`"event":"firing"`)))

	var filterErr *filter.Error
	if !errors.As(err, &filterErr) || !strings.Contains(err.Error(), `event "firing"`) {
		t.Fatalf("Load without provider = %v, want *filter.Error naming the event", err)
	}
}

// events returns a JSON list of unique event names followed by repeats of the first.
func events(unique, repeats int) string {
	names := make([]string, 0, unique+repeats)
	for i := range unique {
		names = append(names, fmt.Sprintf("%q", "e"+strconv.Itoa(i)))
	}

	for range repeats {
		names = append(names, `"e0"`)
	}

	return "[" + strings.Join(names, ",") + "]"
}

func TestMatches(t *testing.T) {
	t.Parallel()

	const (
		workflowRun = "workflow_run"
		issues      = "issues"
		workflow    = `{"workflow_run":{"name":"CI","path":"ci.yml","conclusion":"success"}}`
		issue       = `{"action":"opened","issue":{"number":7,"labels":[{"name":"Bug"}]}}`
	)

	for _, test := range []struct{ event, fields, payload, value string }{
		{issues, `"action":["opened"]`, issue, `"opened"`},
		{workflowRun, `"conclusion":["success"]`, workflow, `"success"`},
		{workflowRun, `"name":"CI"`, workflow, `"CI"`},
		{workflowRun, `"name":"CI","workflow_path":"ci.yml"`, workflow, `"ci.yml"`},
		{issues, `"number":[7]`, issue, "7"},
		{issues, `"label":["Bug"]`, issue, `"Bug"`},
	} {
		scope := github + `,"event":"` + test.event + `",` + test.fields
		got := load(t, matchConfig(scope)).Targets[0].Rules[0]

		fields := filter.Extract(test.event, []byte(test.payload))
		miss := filter.Extract(test.event, []byte(strings.Replace(test.payload, test.value, "null", 1)))

		if !got.Matches(githubProvider, test.event, "oRG/rEPO", fields) ||
			got.Matches(githubProvider, test.event, "oRG/rEPO", miss) {
			t.Fatalf("unexpected match for %s with %s", test.fields, test.payload)
		}
	}
}

func TestScopesAndSubjects(t *testing.T) {
	t.Parallel()

	first := strings.Replace(rule, grafana, github+`,"event":["push","issues","push"]`, 1)
	second := strings.NewReplacer("send", "copy", grafana, github+`,"event":"push"`).Replace(rule)
	third := strings.Replace(rule, "send", "alerts", 1)
	got := load(t, rules(first+","+second+","+third)).Targets[0]

	want := []string{"events.github.issues", "events.github.push", "events.grafana.firing"}
	if subjects := got.Subjects(); !slices.Equal(subjects, want) {
		t.Fatalf("Subjects() = %v, want %v", subjects, want)
	}

	const push = "push"

	var empty filter.Fields

	for _, test := range []struct {
		provider, event, repository string
		index                       int
		want                        bool
	}{
		{githubProvider, push, "tEAm/repo", 0, true},
		{githubProvider, push, "tEAm/repo", 1, true},
		{githubProvider, push, "team-x/repo", 0, false},
		{githubProvider, push, "team/", 0, false},
		{githubProvider, push, "team/a/b", 0, false},
		{githubProvider, push, "", 0, false},
		{githubProvider, "release", "org/repo", 0, false},
		{grafanaProvider, push, "org/repo", 0, false},
		{grafanaProvider, "firing", "", 2, true},
	} {
		matched := got.Rules[test.index].Matches(test.provider, test.event, test.repository, empty)
		if matched != test.want {
			t.Fatalf("Matches(%+v) = %v", test, matched)
		}
	}
}
