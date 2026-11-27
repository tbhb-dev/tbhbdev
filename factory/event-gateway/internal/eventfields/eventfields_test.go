// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words eventfields

package eventfields_test

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/eventfields"
)

const (
	pullRequest   = "pull_request"
	issues        = "issues"
	reviewComment = "pull_request_review_comment"
	push          = "push"
	labelField    = "label"
	senderField   = "sender"
	numberField   = "number"
	headPattern   = `{{.head_sha}}`
	eventPattern  = `{{.event}}`
)

func TestFromEnvelope(t *testing.T) {
	t.Parallel()

	for _, test := range append(pullRequestFields(), eventFieldEdgeCases()...) {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			envelope := `{"provider":"github","delivery":"example","repository":"org/repo","event":"` + test.event +
				`","payload":` + test.payload + `}`
			fields := extract(t, envelope)
			assertRender(t, fields, test.pattern, test.want)

			for _, name := range test.absent {
				if fields.Has(name) {
					t.Errorf("unexpected field %s", name)
				}
			}
		})
	}
}

type envelopeCase struct {
	name, event, payload, pattern, want string
	absent                              []string
}

func pullRequestFields() []envelopeCase {
	return []envelopeCase{
		{
			"opened",
			pullRequest,
			`{"action":"opened","pull_request":{"number":42,"head":{"sha":"abc"}},` +
				`"sender":{"login":"alice"}}`,
			`{{.provider}}/{{.event}}/{{.delivery}}/{{.seq}}:{{.repository}}#{{.number}}@{{.head_sha}}:{{.action}}:{{.sender}}`,
			"github/pull_request/example/7:org/repo#42@abc:opened:alice",
			[]string{labelField},
		},
		{"labeled", issues, `{"action":"labeled","issue":{"number":13,"labels":[{"name":"old"}]},` +
			`"label":{"name":"new"}}`, `{{.number}}/{{.label}}`, "13/new", []string{senderField}},
		{
			"unlabeled", issues, `{"action":"unlabeled","issue":{"number":13},"label":{"name":"old"}}`,
			`{{.number}}/{{.label}}`, "13/old", nil,
		},
		{
			"item labels only", issues, `{"action":"labeled","issue":{"number":13,"labels":[{"name":"old"}]}}`,
			`{{.number}}`, "13",
			[]string{labelField},
		},
		{
			"unrelated label", issues, `{"action":"opened","label":{"name":"old"}}`,
			`{{.action}}`, "opened",
			[]string{numberField, labelField},
		},
		{
			"review", "pull_request_review", `{"pull_request":{"number":42,"head":{"sha":"review"}}}`,
			`{{.head_sha}}:{{.number}}`, "review:42", nil,
		},
		{
			"review comment", reviewComment, `{"pull_request":{"head":{"sha":"comment"}}}`,
			headPattern, "comment", nil,
		},
		{
			"review comment number", reviewComment, `{"pull_request":{"number":5}}`,
			`{{.repository}}#{{.number}}`, "org/repo#5", nil,
		},
		{
			"review comment no pull request", reviewComment, `{}`,
			eventPattern, reviewComment,
			[]string{numberField},
		},
		{
			"review comment wrong number", reviewComment, `{"pull_request":{"number":"5"}}`,
			eventPattern, reviewComment,
			[]string{numberField},
		},
		{push, push, `{"after":"tip"}`, headPattern, "tip", nil},
	}
}

func eventFieldEdgeCases() []envelopeCase {
	return []envelopeCase{
		{"check run", "check_run", `{"check_run":{"head_sha":"run"}}`, headPattern, "run", nil},
		{"check suite", "check_suite", `{"check_suite":{"head_sha":"suite"}}`, headPattern, "suite", nil},
		{"workflow run", "workflow_run", `{"workflow_run":{"head_sha":"run"}}`, headPattern, "run", nil},
		{"workflow job", "workflow_job", `{"workflow_job":{"head_sha":"job"}}`, headPattern, "job", nil},
		{
			"wrong types", pullRequest, `{"action":0,"sender":{"login":[]},` +
				`"pull_request":{"number":"42","head":{"sha":true}}}`, eventPattern, pullRequest,
			[]string{"action", senderField, numberField, "head_sha"},
		},
		{
			"wrong label", issues, `{"action":"labeled","label":{"name":7}}`,
			`{{.action}}`, "labeled",
			[]string{labelField},
		},
		{
			"huge number", issues, `{"issue":{"number":9223372036854775808}}`,
			eventPattern, issues,
			[]string{numberField},
		},
		{"non-object payload", push, `["after"]`, eventPattern, push, []string{"head_sha", "action", senderField}},
		{
			"empty values", pullRequest, `{"action":"","sender":{"login":""},` +
				`"pull_request":{"number":0,"head":{"sha":""}}}`, `{{.action}}/{{.sender}}/{{.head_sha}}/{{.number}}`,
			"///0", nil,
		},
	}
}

func TestRepository(t *testing.T) {
	t.Parallel()

	for _, repository := range []string{"", `,"repository":null`, `,"repository":false`} {
		fields := extract(t, `{"provider":"grafana","event":"alert.firing","delivery":"example"`+repository+`}`)
		assertRender(t, fields, `{{.provider}}/{{.event}}`, "grafana/alert.firing")

		if fields.Has("repository") {
			t.Errorf("repository present for %q", repository)
		}
	}
}

func TestInvalidEnvelope(t *testing.T) {
	t.Parallel()

	for _, envelope := range []string{
		`[]`, `null`, `{`, `{"provider":"github","event":"push"}`,
		`{"provider":null,"event":"push","delivery":"example"}`,
		`{"provider":"github","event":7,"delivery":"example"}`,
	} {
		_, err := eventfields.FromEnvelope(7, []byte(envelope))
		if err == nil {
			t.Errorf("accepted invalid envelope %q", envelope)
		}
	}
}

func TestInvalidTemplate(t *testing.T) {
	t.Parallel()

	for _, pattern := range []string{
		"", strings.Repeat("x", 513), "{{", `{{.unknown}}`, `{{$.unknown}}`, `{{(.).unknown}}`, `{{.event.unknown}}`,
		`{{if .event}}{{.event}}{{else}}{{.unknown}}{{end}}`, `{{range .unknown}}{{end}}`,
		`{{define "workflow ID"}}{{end}}`, `{{template "child" .}}`, `{{block "child" .}}x{{end}}`,
		`{{printf "%s" .event}}`, `{{index . "absent"}}`, `{{unknown .event}}`,
	} {
		_, err := eventfields.ParseTemplate(pattern)
		if err == nil {
			t.Errorf("accepted invalid template %q", pattern)
		}
	}
}

func TestTemplateRender(t *testing.T) {
	t.Parallel()

	fields := extract(t, `{"provider":"github","event":"push","delivery":"example","repository":"",`+
		`"payload":{"after":"`+strings.Repeat("x", 1000)+`"}}`)

	for _, test := range []struct{ pattern, want string }{
		{"literal ID", "literal ID"},
		{strings.Repeat("x", 512), strings.Repeat("x", 512)},
		{headPattern, strings.Repeat("x", 1000)},
		{`repo:{{.repository}}`, "repo:"},
		{`{{if .event}}{{with $.event}}{{.}}{{end}}:{{(.).delivery}}{{end}}`, "push:example"},
		{`{{range $key, $value := .}}{{if $key}}{{break}}{{end}}{{end}}fixed`, "fixed"},
	} {
		assertRender(t, fields, test.pattern, test.want)
	}

	for _, pattern := range []string{`{{.sender}}`, `{{$.number}}`, `{{.repository}}`, `x{{.head_sha}}`} {
		got, err := compile(t, pattern).Render(fields)
		if !errors.Is(err, eventfields.ErrRender) || got != "" {
			t.Errorf("Render(%q) = %q, %v, want empty result and ErrRender", pattern, got, err)
		}
	}
}

func TestRenderBound(t *testing.T) {
	t.Parallel()

	start := time.Now()

	for _, number := range []string{"2000", "9223372036854775807"} {
		fields := extract(t, `{"provider":"github","event":"issues","delivery":"d","payload":{"issue":{"number":`+
			number+`}}}`)

		_, err := compile(t, `{{range .number}}x{{end}}`).Render(fields)
		if !errors.Is(err, eventfields.ErrRender) {
			t.Errorf("number %s: err = %v, want ErrRender", number, err)
		}
	}

	if time.Since(start) > time.Second {
		t.Error("render was not bounded")
	}

	for size, ok := range map[int]bool{1000: true, 1001: false} {
		fields := extract(t, `{"provider":"github","event":"push","delivery":"d","payload":{"after":"`+
			strings.Repeat("x", size)+`"}}`)

		_, err := compile(t, headPattern).Render(fields)
		if (err == nil) != ok {
			t.Errorf("size %d: err = %v", size, err)
		}
	}
}

func extract(t *testing.T, envelope string) eventfields.Fields {
	t.Helper()

	fields, err := eventfields.FromEnvelope(7, []byte(envelope))
	if err != nil {
		t.Fatal(err)
	}

	return fields
}

func compile(t *testing.T, pattern string) *eventfields.Template {
	t.Helper()

	parsed, err := eventfields.ParseTemplate(pattern)
	if err != nil {
		t.Fatal(err)
	}

	return parsed
}

func assertRender(t *testing.T, fields eventfields.Fields, pattern, want string) {
	t.Helper()

	got, err := compile(t, pattern).Render(fields)
	if err != nil || got != want {
		t.Errorf("Render(%q) = %q, %v, want %q", pattern, got, err, want)
	}
}
