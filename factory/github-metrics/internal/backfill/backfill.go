// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package backfill lists a repository's pull requests in creation order.
package backfill

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
)

// ErrMaxRequests means the request cap isn't positive.
var ErrMaxRequests = errors.New("max requests must be positive")

// Options sets the page request cap for one run.
type Options struct {
	MaxRequests int
}

// Record contains only the pull request fields needed for backfill.
type Record struct {
	CreatedAt time.Time `json:"created_at"` //nolint:tagliatelle // GitHub field name.
	MergedAt  time.Time `json:"merged_at"`  //nolint:tagliatelle // GitHub field name.
	ClosedAt  time.Time `json:"closed_at"`  //nolint:tagliatelle // GitHub field name.
	State     string    `json:"state"`
	HeadSHA   string    `json:"-"`
	BaseRef   string    `json:"-"`
	Number    int       `json:"number"`
	Draft     bool      `json:"draft"`
}

// Result holds records in page order and the next URL when capped.
type Result struct {
	ResumeURL string
	Records   []Record
	Truncated bool
}

type pageRecord struct {
	Head struct {
		SHA string `json:"sha"`
	} `json:"head"`
	Base struct {
		Ref string `json:"ref"`
	} `json:"base"`

	Record //nolint:embeddedstructfieldcheck // Keep pointer fields together for field alignment.
}

// List reads all pages, or stops at the cap. The client manages ETags and
// retries. A 304 ends the run without adding records from that page. Reuse
// the client's tag store only when previously read records have been kept.
func List(ctx context.Context, client *ghclient.Client, repo string, opts Options) (Result, error) {
	var result Result

	if opts.MaxRequests <= 0 {
		return result, ErrMaxRequests
	}

	next := "/repos/" + repo + "/pulls?state=all&sort=created&direction=asc&per_page=100"

	for range opts.MaxRequests {
		response, err := client.Get(ctx, budget.Backfill, next)
		if err != nil {
			return result, fmt.Errorf("list pull requests: %w", err)
		}

		if response.NotModified() {
			return result, nil
		}

		var page []pageRecord

		err = json.Unmarshal(response.Body, &page)
		if err != nil {
			return result, fmt.Errorf("decode pull requests: %w", err)
		}

		if len(page) == 0 {
			return result, nil
		}

		for _, item := range page {
			item.HeadSHA = item.Head.SHA
			item.BaseRef = item.Base.Ref
			result.Records = append(result.Records, item.Record)
		}

		next = response.Next
		if next == "" {
			return result, nil
		}
	}

	result.Truncated = true
	result.ResumeURL = next

	return result, nil
}
