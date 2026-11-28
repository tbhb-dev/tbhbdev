// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package review_test

import (
	"encoding/json"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/review"
)

type expectedExample struct {
	Counts     map[string]int `json:"counts"`
	Reason     review.Reason  `json:"reason"`
	Harness    string         `json:"harness"`
	Model      string         `json:"model"`
	Effort     string         `json:"effort"`
	Parsed     bool           `json:"parsed"`
	Superseded bool           `json:"superseded"`
	CRLF       bool           `json:"crlf"`
}

func TestParseExamples(t *testing.T) {
	t.Parallel()

	dir := filepath.Join("..", "..", "..", "cli", "schemas", "testdata", "review-bodies")
	dirFS := os.DirFS(dir)

	matches, err := fs.Glob(dirFS, "*.txt")
	if err != nil || len(matches) == 0 {
		t.Fatalf("glob examples in %s: count=%d err=%v", dir, len(matches), err)
	}

	for _, file := range matches {
		t.Run(strings.TrimSuffix(file, ".txt"), func(t *testing.T) {
			t.Parallel()

			checkExample(t, dirFS, file)
		})
	}
}

func checkExample(t *testing.T, dirFS fs.FS, file string) {
	t.Helper()

	rawBody, bodyErr := fs.ReadFile(dirFS, file)
	rawJSON, jsonErr := fs.ReadFile(dirFS, strings.TrimSuffix(file, ".txt")+".json")

	var want expectedExample

	unmarshalErr := json.Unmarshal(rawJSON, &want)
	if bodyErr != nil || jsonErr != nil || unmarshalErr != nil {
		t.Fatalf("load fixture %s: body=%v json=%v parse=%v", file, bodyErr, jsonErr, unmarshalErr)
	}

	body := string(rawBody)
	if want.CRLF {
		body = strings.ReplaceAll(body, "\n", "\r\n")
	}

	wantCounts := make(map[review.Key]int, len(want.Counts))
	for pair, count := range want.Counts {
		prio, kind, _ := strings.Cut(pair, "/")
		wantCounts[review.Key{Priority: review.Priority(prio), Kind: review.Kind(kind)}] = count
	}

	got := review.Parse(body)
	if got.Parsed != want.Parsed || got.Reason != want.Reason || got.Superseded != want.Superseded ||
		got.Harness != want.Harness || got.Model != want.Model || got.Effort != want.Effort ||
		!maps.Equal(got.Counts, wantCounts) {
		t.Fatalf("Parse() = %+v, want %+v (counts=%v)", got, want, wantCounts)
	}
}

func TestParseNeverLeaksBodyText(t *testing.T) {
	t.Parallel()

	const sentinel = "zxqv-secret-body-token-9182"

	body := "SUPERSEDED: " + sentinel + "\n## Summary\n\nMentions " + sentinel + ".\n\n" +
		"## Findings\n\n- F1: P1 correctness `" + sentinel + ".go:42` fails. Occurs when: " + sentinel + ".\n\n" +
		"## Suggestions\n\n- S1: Remove " + sentinel + ".\n\n## Verification\n\n- Checked " + sentinel + ".\n\n" +
		"Generated-by: LLM effort=high;harness=codex;model=gpt-5.4\n"

	got := review.Parse(body)
	if !got.Parsed || strings.Contains(string(got.Reason)+got.Harness+got.Model+got.Effort, sentinel) {
		t.Fatalf("unexpected parse result or leaked sentinel: %+v", got)
	}
}

func FuzzParse(f *testing.F) {
	for _, seed := range []string{
		"", "free text review",
		"SUPERSEDED: old\n## Summary\n\nOk.\n\n## Verification\n\n- Ok.\n\nGenerated-by: LLM harness=c;model=m\n",
		"## Summary\r\n\r\nOk.\r\n\r\n## Findings\r\n\r\nInline findings: 1 (P1 correctness 1).\r\n" +
			"F1.\r\n\r\n## Verification\r\n\r\n- Ok.\r\n",
		"## Summary\n\nOk.\n\n## Findings\n\n- F1: P4 bug Bad.\n\n## Verification\n\n- Ok.\n",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, body string) {
		res := review.Parse(body)
		checkFuzzEnums(t, res)
		checkFuzzState(t, strings.ReplaceAll(body, "\r\n", "\n"), res)
	})
}

func checkFuzzEnums(t *testing.T, res review.Result) {
	t.Helper()

	const expectedFields = 7
	if reflect.TypeOf(res).NumField() != expectedFields {
		t.Fatalf("Result field count changed; update fuzz leak checks")
	}

	reasons := []review.Reason{
		review.ReasonNone, review.ReasonNoSections, review.ReasonBadFinding, review.ReasonNoTrailer,
	}
	priorities := []review.Priority{review.PriorityP0, review.PriorityP1, review.PriorityP2, review.PriorityP3}
	kinds := []review.Kind{
		review.KindCorrectness, review.KindSecurity, review.KindDesign, review.KindTest, review.KindDocs,
	}

	if !slices.Contains(reasons, res.Reason) {
		t.Fatalf("unknown reason %q", res.Reason)
	}

	for key, count := range res.Counts {
		if !slices.Contains(priorities, key.Priority) || !slices.Contains(kinds, key.Kind) || count <= 0 {
			t.Fatalf("invalid count entry %+v=%d", key, count)
		}
	}
}

func checkFuzzState(t *testing.T, body string, res review.Result) {
	t.Helper()

	empty := res.Harness == "" && res.Model == "" && res.Effort == ""
	if !res.Parsed {
		if len(res.Counts) != 0 || !empty ||
			(res.Reason != review.ReasonNoSections && res.Reason != review.ReasonBadFinding) {
			t.Fatalf("invalid unparsed result: %+v", res)
		}

		return
	}

	if (res.Reason != review.ReasonNone && res.Reason != review.ReasonNoTrailer) ||
		(res.Reason == review.ReasonNoTrailer && !empty) {
		t.Fatalf("invalid parsed result: %+v", res)
	}

	checkTrailerValue(t, body, "harness", res.Harness)
	checkTrailerValue(t, body, "model", res.Model)
	checkTrailerValue(t, body, "effort", res.Effort)
}

func checkTrailerValue(t *testing.T, body, key, val string) {
	t.Helper()

	if val != "" && (strings.ContainsAny(val, " \t\r\n;") ||
		!strings.Contains(body, "Generated-by: LLM ") || !strings.Contains(body, key+"="+val)) {
		t.Fatalf("trailer %s = %q did not come from Generated-by: LLM line", key, val)
	}
}
