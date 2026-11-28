// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package review parses pull request review bodies into enums and counts.
package review

import (
	"regexp"
	"slices"
	"strings"
)

// Reason explains why a review body did not fully parse.
type Reason string

// Priority is a finding severity from review.cue.
type Priority string

// Kind is a finding category from review.cue.
type Kind string

// Fixed parse reasons, finding priorities, and finding kinds from review.cue.
const (
	ReasonNone       Reason = ""
	ReasonNoSections Reason = "no_sections"
	ReasonBadFinding Reason = "bad_finding"
	ReasonNoTrailer  Reason = "no_trailer"

	PriorityP0 Priority = "p0"
	PriorityP1 Priority = "p1"
	PriorityP2 Priority = "p2"
	PriorityP3 Priority = "p3"

	KindCorrectness Kind = "correctness"
	KindSecurity    Kind = "security"
	KindDesign      Kind = "design"
	KindTest        Kind = "test"
	KindDocs        Kind = "docs"

	maxFindings     = 8
	minSectionCount = 2
)

// Key identifies one priority and kind cell in Result.Counts.
type Key struct {
	Priority Priority
	Kind     Kind
}

// Result holds structured counts and trailer fields parsed from a review body.
type Result struct {
	Counts                 map[Key]int
	Reason                 Reason
	Harness, Model, Effort string
	Parsed, Superseded     bool
}

var (
	findingPattern     = regexp.MustCompile(`^F[1-8]: P([0-3]) (correctness|security|design|test|docs)\s+\S`)
	inlineCountPattern = regexp.MustCompile(
		`^Inline findings: ([1-8]) \((P[0-3] (?:correctness|security|design|test|docs) [1-8]` +
			`(?:, P[0-3] (?:correctness|security|design|test|docs) [1-8])*)\)\.$`,
	)
	inlineRangePattern = regexp.MustCompile(`^F[1-8](?: to F[1-8])?\.$`)
	trailerBlobPattern = regexp.MustCompile(`^[a-z_]+=[^\s;]+(;[a-z_]+=[^\s;]+)*$`)
)

// Parse turns a review body into enums, finding counts, and trailer fields.
func Parse(body string) Result {
	var res Result

	body = strings.ReplaceAll(body, "\r\n", "\n")
	first, after, _ := strings.Cut(body, "\n")

	res.Superseded = strings.HasPrefix(first, "SUPERSEDED:")
	if res.Superseded {
		body = after
	}

	sections, hasFindings, sectionsOK := splitSections(body)
	if !sectionsOK {
		res.Reason = ReasonNoSections

		return res
	}

	if hasFindings {
		counts, findingsOK := parseFindings(sections["Findings"])
		if !findingsOK {
			res.Reason = ReasonBadFinding

			return res
		}

		res.Counts = counts
	}

	harness, model, effort, hasTrailer := parseTrailer(sections["Verification"])
	res.Harness, res.Model, res.Effort, res.Parsed = harness, model, effort, true

	if !hasTrailer {
		res.Reason = ReasonNoTrailer
	}

	return res
}

func splitSections(body string) (map[string][]string, bool, bool) {
	sections, order, current := make(map[string][]string), []string(nil), ""

	for line := range strings.SplitSeq(body, "\n") {
		if title, isHeading := strings.CutPrefix(line, "## "); isHeading {
			current = strings.TrimSpace(title)
			order = append(order, current)

			continue
		}

		if current == "" && strings.TrimSpace(line) != "" {
			return nil, false, false
		}

		if current != "" {
			sections[current] = append(sections[current], line)
		}
	}

	valid := len(order) >= minSectionCount && order[0] == "Summary" && order[len(order)-1] == "Verification"
	if valid {
		mid := strings.Join(order[1:len(order)-1], ",")
		valid = mid == "" || mid == "Findings" || mid == "Suggestions" || mid == "Findings,Suggestions"
	}

	nonBlank := func(line string) bool { return strings.TrimSpace(line) != "" }
	if !valid || !slices.ContainsFunc(sections["Summary"], nonBlank) ||
		!slices.ContainsFunc(sections["Verification"], nonBlank) {
		return nil, false, false
	}

	return sections, slices.Contains(order, "Findings"), true
}

func parseFindings(lines []string) (map[Key]int, bool) {
	for len(lines) > 0 && strings.TrimSpace(lines[0]) == "" {
		lines = lines[1:]
	}

	if len(lines) == 0 {
		return nil, false
	}

	counts, total := make(map[Key]int), 0

	if strings.HasPrefix(lines[0], "Inline findings:") {
		inlineTotal, rest, ok := parseInline(lines, counts)
		if !ok {
			return nil, false
		}

		total, lines = inlineTotal, rest
	}

	for _, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}

		bullet, hasBullet := strings.CutPrefix(line, "- ")

		match := findingPattern.FindStringSubmatch(bullet)
		if !hasBullet || match == nil {
			return nil, false
		}

		counts[toKey(match[1][0], match[2])]++

		total++
		if total > maxFindings {
			return nil, false
		}
	}

	return counts, total > 0
}

func parseInline(lines []string, counts map[Key]int) (int, []string, bool) {
	match := inlineCountPattern.FindStringSubmatch(lines[0])
	if match == nil {
		return 0, nil, false
	}

	declared, sum := int(match[1][0]-'0'), 0

	for pair := range strings.SplitSeq(match[2], ", ") {
		parts := strings.Split(pair, " ")
		count := int(parts[2][0] - '0')
		counts[toKey(parts[0][1], parts[1])] += count
		sum += count
	}

	rest := lines[1:]
	if len(rest) > 0 && inlineRangePattern.MatchString(rest[0]) {
		rest = rest[1:]
	}

	if sum != declared || (len(rest) > 0 && strings.TrimSpace(rest[0]) != "") {
		return 0, nil, false
	}

	return declared, rest, true
}

func toKey(digit byte, word string) Key {
	priorities := [...]Priority{PriorityP0, PriorityP1, PriorityP2, PriorityP3}
	kinds := [...]Kind{KindCorrectness, KindSecurity, KindDesign, KindTest, KindDocs}
	idx := slices.IndexFunc(kinds[:], func(kind Kind) bool { return word == string(kind) })

	return Key{Priority: priorities[digit-'0'], Kind: kinds[idx]}
}

func parseTrailer(lines []string) (string, string, string, bool) {
	var blob, harness, model, effort string

	for _, line := range lines {
		if after, ok := strings.CutPrefix(line, "Generated-by: LLM "); ok {
			if candidate := strings.TrimSpace(after); trailerBlobPattern.MatchString(candidate) {
				blob = candidate
			}
		}
	}

	if blob == "" {
		return "", "", "", false
	}

	attrs := map[string]*string{"harness": &harness, "model": &model, "effort": &effort}
	for pair := range strings.SplitSeq(blob, ";") {
		if key, val, _ := strings.Cut(pair, "="); attrs[key] != nil {
			*attrs[key] = val
		}
	}

	return harness, model, effort, true
}
