// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
)

const (
	// MaxEventPages caps the issue events read for one pull request.
	MaxEventPages = 3

	stateClosed = "closed"
	stateMerged = "merged"
)

// End is the first end of a pull request, read from its issue events.
type End struct {
	// At is the `created_at` of the earliest `closed` event. It is zero when
	// the events run past the page cap or hold no `closed` event, so the end
	// is ambiguous.
	At time.Time
	// State is "merged" when a `merged` event shares At, else "closed". It is
	// empty when At is zero.
	State string
	// Pages counts the fetched pages, including a failed one.
	Pages int
}

type issueEvent struct {
	CreatedAt time.Time `json:"created_at"` //nolint:tagliatelle // GitHub field name.
	Event     string    `json:"event"`
}

// FirstEnd reads a pull request's issue events and returns the first end.
// Each page clears its tag first, because the client
// would answer 304 with an empty body for a URL it saw before.
func (l *Lister) FirstEnd(
	ctx context.Context,
	repo string,
	number int,
	priority budget.Priority,
) (End, error) {
	events, pages, complete, err := l.events(ctx, repo, number, priority)
	end := End{At: time.Time{}, State: "", Pages: pages}

	if err != nil || !complete {
		return end, err
	}

	var merged []time.Time

	for _, event := range events {
		switch event.Event {
		case "closed":
			if end.At.IsZero() || event.CreatedAt.Before(end.At) {
				end.At = event.CreatedAt
			}
		case "merged":
			merged = append(merged, event.CreatedAt)
		}
	}

	if !end.At.IsZero() {
		end.State = stateClosed
		if slices.ContainsFunc(merged, end.At.Equal) {
			end.State = stateMerged
		}
	}

	return end, nil
}

// events returns the events of up to MaxEventPages pages. The bool is false
// when more pages remain.
func (l *Lister) events(ctx context.Context, repo string, number int, priority budget.Priority) (
	[]issueEvent, int, bool, error,
) {
	var events []issueEvent

	target := "repos/" + repo + "/issues/" + strconv.Itoa(number) + "/events?per_page=100"

	for page := 1; ; page++ {
		l.clear(target)

		resp, err := l.fetch(ctx, priority, target, page)
		if err != nil {
			return nil, page - 1, false, err
		}

		var decoded []issueEvent

		err = json.Unmarshal(resp.Body, &decoded)
		if err != nil {
			return nil, page, false, fmt.Errorf("decode issue events: %w", err)
		}

		events = append(events, decoded...)

		switch {
		case resp.Next == "":
			return events, page, true, nil
		case page >= MaxEventPages:
			return events, page, false, nil
		}

		target = resp.Next
	}
}
