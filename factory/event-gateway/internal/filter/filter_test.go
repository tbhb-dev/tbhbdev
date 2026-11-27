// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package filter_test

import (
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	scope       = `"provider":"github","repository":"Org/Repo"`
	issues      = "issues"
	pullRequest = "pull_request"
	review      = "pull_request_review"
	numberSeven = `,"number":[7]`
	bugLabel    = `,"label":["Bug"]`
	workflowRun = "workflow_run"
	success     = `,"conclusion":["success"]`
)

func githubSub(event, filters string) string {
	return `{` + scope + `,"event":"` + event + `"` + filters + `}`
}

func TestParse(t *testing.T) {
	t.Parallel()

	const path = `,"name":"CI","workflow_path":".github/workflows/ci.yml"`

	for _, test := range []struct{ event, filters, want string }{
		{"push", `,"unknown":1e999`, ""},
		{"check_run", `,"name":"CI Run"`, `,"name":"CI Run"`},
		{workflowRun, `,"action":["z","a","z"]` + path, `,"action":["a","z"]` + path},
		{"workflow_job", `,"conclusion":["success","failure","success"]`, `,"conclusion":["failure","success"]`},
		{"check_suite", success, success},
		{
			issues, `,"number":[9007199254740991,7,1,7],"label":["team: Ops","Bug","Bug"]`,
			`,"number":[1,7,9007199254740991],"label":["Bug","team: Ops"]`,
		},
	} {
		sub := mustParse(t, githubSub(test.event, test.filters))
		want := `{"provider":"github","event":"` + test.event + `","repository":"org/repo"` + test.want + "}"

		got, err := json.Marshal(sub)
		if err != nil || string(got) != want {
			t.Fatalf("marshal %s = %s, %v, want %s", test.event, got, err, want)
		}
	}
}

func mustParse(t *testing.T, raw string) filter.Subscription {
	t.Helper()

	sub, err := filter.Parse(json.RawMessage(raw))
	if err != nil {
		t.Fatalf("Parse(%s): %v", raw, err)
	}

	return sub
}

func expectCode(t *testing.T, raw, code string) {
	t.Helper()

	_, err := filter.Parse(json.RawMessage(raw))

	got, ok := errors.AsType[*filter.Error](err)
	if !ok || got.Code != code || got.Error() == "" {
		t.Fatalf("Parse(%s) = %v, want %s", raw, err, code)
	}
}

func TestParseScopeErrors(t *testing.T) {
	t.Parallel()

	for _, raw := range []string{
		`[]`, `null`, `{`, `{}`, `{"provider":7}`, `{"provider":null}`,
		`{"provider":"github","event":null}`, `{"provider":"github","repository":[]}`,
	} {
		expectCode(t, raw, "bad_message")
	}

	for _, raw := range []string{
		`{"provider":"GitHub"}`, `{"provider":"github\n"}`, `{"provider":"github","event":"Push"}`,
		`{"provider":"github","event":"push\n"}`, `{"provider":"github","repository":"org/repo\n"}`,
		`{"provider":"github","repository":"org"}`, `{"provider":"github","event":"push"}`, `{` + scope + `}`,
		`{"provider":"github","event":"issues","number":[7]}`, `{` + scope + `,"action":["opened"]}`,
		githubSub("check_run", ""), githubSub("check_suite", ""), githubSub("workflow_job", ""),
		githubSub(workflowRun, `,"workflow_path":"ci.yml"`),
	} {
		expectCode(t, raw, "bad_subscription")
	}
}

func TestParseFilterErrors(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		field, event, valid string
		invalid             []string
	}{
		{"action", issues, `["opened"]`, []string{`[]`, `[0]`, `["opened\n"]`, `["a","a","a","a","a","a","a","a","a"]`}},
		{"conclusion", "check_suite", `["success"]`, []string{`["Success"]`}},
		{"name", "check_run", `"test"`, []string{`"test\t"`, `"test\n"`}},
		{"workflow_path", workflowRun, `"ci.yml"`, []string{`"ci.txt\n"`, `"a b"`}},
		{"number", issues, `[7]`, []string{`[0]`, `[-1]`, `[1.5]`, `["7"]`, `[9007199254740992]`, `[7,7,7,7,7,7,7,7,7]`}},
		{"label", pullRequest, `["team: Ops"]`, []string{`[null]`, `[7]`, `["Bug\n"]`}},
	} {
		prefix := `,"` + test.field + `":`
		expectCode(t, githubSub("push", prefix+test.valid), "bad_subscription")
		grafana := `{"provider":"grafana","repository":"org/repo","event":"` + test.event + `"`
		expectCode(t, grafana+prefix+test.valid+`}`, "bad_subscription")
		expectCode(t, githubSub(test.event, prefix+"null"), "bad_subscription")

		for _, value := range test.invalid {
			expectCode(t, githubSub(test.event, prefix+value), "bad_subscription")
		}
	}
}

func TestMatches(t *testing.T) {
	t.Parallel()

	const workflow = `{"action":"completed","workflow_run":{"name":"CI","conclusion":"success","path":"ci.yml"}}`

	for _, test := range []struct {
		event, filters, payload string
		want                    bool
	}{
		{workflowRun, `,"action":["completed"]`, workflow, true},
		{workflowRun, `,"action":["created"]`, workflow, false},
		{workflowRun, success, workflow, true},
		{workflowRun, `,"conclusion":["failure"]`, workflow, false},
		{workflowRun, `,"name":"CI"`, workflow, true},
		{workflowRun, `,"name":"Release"`, workflow, false},
		{workflowRun, `,"workflow_path":"ci.yml"`, workflow, true},
		{workflowRun, `,"workflow_path":"release.yml"`, workflow, false},
		{workflowRun, `,"action":["completed"],"name":"CI","workflow_path":"ci.yml"` + success, workflow, true},
		{issues, bugLabel, `{"issue":{"labels":[5,{"name":8},{"name":"docs"},{"name":"Bug"}]}}`, true},
		{issues, bugLabel, `{"action":"opened","label":{"name":"Bug"},"issue":{"labels":[{"name":"bug"}]}}`, false},
		{"issue_comment", numberSeven, `{"issue":{"number":7,"pull_request":{}},"pull_request":{"number":8}}`, true},
		{"issue_comment", numberSeven, `{"issue":{"number":8},"pull_request":{"number":7}}`, false},
		{pullRequest, numberSeven + bugLabel, `{"pull_request":{"number":7}}`, false},
		{pullRequest, bugLabel, `{"action":"labeled","pull_request":{"labels":[]},"label":{"name":"Bug"}}`, true},
		{review, bugLabel, `{"action":"unlabeled","label":{"name":"Bug"}}`, true},
		{review, numberSeven + bugLabel, `{"pull_request":{"number":7,"labels":[{"name":"Bug"}]}}`, true},
		{review, numberSeven, `{"pull_request_review":{"number":7},"pull_request":{"number":1e999}}`, false},
	} {
		var sub filter.Subscription

		err := json.Unmarshal([]byte(githubSub(test.event, test.filters)), &sub)
		if err != nil {
			t.Fatal(err)
		}

		fields := filter.Extract(test.event, []byte(test.payload))
		if got := sub.Matches("github", test.event, "ORG/Repo", fields); got != test.want {
			t.Fatalf("match %s with %s on %s = %t, want %t", test.event, test.filters, test.payload, got, test.want)
		}
	}
}

func TestExtractEmpty(t *testing.T) {
	t.Parallel()

	var zero filter.Fields

	scoped := mustParse(t, githubSub("push", ""))
	if scoped.Matches("other", "push", "org/repo", zero) ||
		scoped.Matches("github", "issues", "org/repo", zero) || scoped.Matches("github", "push", "", zero) {
		t.Fatal("matched an event outside the subscription scope")
	}

	sub := mustParse(t, `{"provider":"grafana"}`)

	for _, raw := range []string{
		`{`, `[]`, `null`, `1e999`, `{"action":false,"alerts.firing":{"name":[],"path":3,"conclusion":{}}}`,
	} {
		fields := filter.Extract("alerts.firing", []byte(raw))
		if !reflect.DeepEqual(fields, zero) || !sub.Matches("grafana", "alerts.firing", "", fields) {
			t.Fatalf("Extract and match %s with an empty repository yielded %+v", raw, fields)
		}
	}
}

func TestInvalidSubscriptionJSON(t *testing.T) {
	t.Parallel()

	valid := mustParse(t, githubSub(workflowRun, `,"name":"CI"`))
	invalidProvider := valid
	invalidProvider.Provider = "\xff"
	invalidPath := valid
	invalidPath.WorkflowPath = "\xff"

	for _, sub := range []filter.Subscription{invalidProvider, invalidPath} {
		_, err := json.Marshal(sub)
		if err == nil {
			t.Error("marshal accepted invalid UTF-8")
		}
	}

	for _, raw := range []string{`[]`, `{"workflow_path":42}`, `{"provider":42}`} {
		var sub filter.Subscription
		if json.Unmarshal([]byte(raw), &sub) == nil {
			t.Errorf("unmarshal accepted %s", raw)
		}
	}
}
