// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package reconcile repairs pull request metrics that a missed webhook
// delivery left out. This file lists the installation's repositories and walks
// a repository's pull requests from the most recently updated backward.
package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"slices"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
)

const (
	// DefaultMaxPages caps an incremental walk of one repository.
	DefaultMaxPages = 50
	// SweepMaxPages caps the daily sweep of one repository.
	SweepMaxPages = 200
	// MaxRepoPages caps the repository list.
	MaxRepoPages = 10

	repositoriesPath = "installation/repositories?per_page=100"
)

// Getter is the part of ghclient.Client the lister uses.
type Getter interface {
	Get(ctx context.Context, priority budget.Priority, target string) (*ghclient.Response, error)
}

// Tags is the part of ghclient.ETags the lister uses. Set with an empty tag
// clears one, because the client sends no tag for an empty one.
type Tags interface {
	Set(target, etag string)
}

// Repository is one repository of the installation.
type Repository struct {
	FullName string `json:"full_name"` //nolint:tagliatelle // GitHub field name.
	ID       int64  `json:"id"`
}

// ClosedPR is the list item of a pull request that has a close time. It
// decodes straight from a list item.
type ClosedPR struct {
	CreatedAt time.Time `json:"created_at"` //nolint:tagliatelle // GitHub field name.
	MergedAt  time.Time `json:"merged_at"`  //nolint:tagliatelle // GitHub field name.
	ClosedAt  time.Time `json:"closed_at"`  //nolint:tagliatelle // GitHub field name.
	UpdatedAt time.Time `json:"updated_at"` //nolint:tagliatelle // GitHub field name.
	NodeID    string    `json:"node_id"`    //nolint:tagliatelle // GitHub field name.
	// Body is read for the start time and is never stored.
	Body string `json:"body"`
	// Repository is the full name the walk was asked for.
	Repository string `json:"-"`
	// Number is the pull request number, which the issue events URL needs.
	Number int `json:"number"`
	// Merged is true when the item has a merge time, since a list item has no
	// merged field.
	Merged bool `json:"-"`
}

// WalkOptions bounds one walk.
type WalkOptions struct {
	// Stop is the time the walk reaches back to. It stops at the first item
	// updated before it.
	Stop time.Time
	// MaxPages caps the pages read. It must be positive.
	MaxPages int
	// Priority is the budget priority of every request.
	Priority budget.Priority
}

// Walked is the result of one walk.
type Walked struct {
	// Newest is the latest `updated_at` seen. It is zero after an error, a
	// truncated walk, or a 304, so a caller can't advance a watermark from it.
	Newest time.Time
	// Items holds the pull requests with a close time, newest update first.
	Items []ClosedPR
	// Pages counts fetched pull request pages, including a 304 or a decode failure.
	Pages int
	// Truncated is true when the page cap ended the walk before the stop time.
	Truncated bool
	// Unchanged is true when the first page answered 304.
	Unchanged bool
}

// Lister reads the repository list and the updated-order walks. It isn't safe
// for concurrent use.
type Lister struct {
	client Getter
	tags   Tags
	base   *url.URL
	// stops holds the stop time of each repository's last clean walk.
	stops map[string]time.Time
	// cached holds the last repository list, valid only when the last full
	// read fit on one page and every page succeeded.
	cached []Repository
	valid  bool
}

// NewLister returns a lister. The base URL must be the one the client uses,
// because the client keys its tags by the full URL. A base path must end in a
// slash, such as /api/v3/, so the request paths stay under it. A page cap below
// one reads one page.
func NewLister(client Getter, tags Tags, base *url.URL) *Lister {
	return &Lister{
		client: client,
		tags:   tags,
		base:   base,
		cached: nil,
		valid:  false,
		stops:  map[string]time.Time{},
	}
}

type repositoryPage struct {
	Repositories []Repository `json:"repositories"`
}

// Repositories returns the installation's repositories. Only a list that fit
// on one page keeps its first-page tag, since a tag on page one can't see a
// change confined to page two. A 304 then returns the cached list. For a
// longer list, or after a failed page, every page is read in full and the
// tags are cleared. The bool is true when the page cap cut the list short.
func (l *Lister) Repositories(ctx context.Context) ([]Repository, bool, error) {
	if !l.valid {
		l.clear(repositoriesPath)
	}

	repos, truncated, single, err := l.readRepositories(ctx)
	l.valid = err == nil && single

	if l.valid {
		l.cached = slices.Clone(repos)

		return repos, truncated, nil
	}

	l.clear(repositoriesPath)

	return repos, truncated, err
}

// Updated walks a repository's pull requests in `updated_at` order, newest
// first, until the first item updated before the stop time. Only the first
// page sends a stored tag: a 304 there means no change since the last clean
// walk. Each later page clears its tag first, because page contents shift as
// pull requests update. Any walk that doesn't finish cleanly clears the
// first-page tag, so the next walk reads the page in full, and so does a walk
// whose stop time reaches back further than the last clean one.
func (l *Lister) Updated(ctx context.Context, repo string, opts WalkOptions) (Walked, error) {
	first := updatedPath(repo)

	// A tag only proves the first page is unchanged, so it can't stand in for a
	// deeper walk than the one that stored it.
	if prev, ok := l.stops[repo]; !ok || opts.Stop.Before(prev) {
		l.clear(first)
	}

	walked, err := l.walk(ctx, repo, first, opts)
	if err != nil || walked.Truncated {
		l.clear(first)
		delete(l.stops, repo)
	} else if !walked.Unchanged {
		l.stops[repo] = opts.Stop
	}

	return walked, err
}

// ClearUpdated drops the stored first-page tag of a repository's walk, so the
// next walk reads the page in full. A caller uses it when it couldn't finish
// the items of a walk that ended cleanly, since a 304 would hide them.
func (l *Lister) ClearUpdated(repo string) {
	l.clear(updatedPath(repo))
}

func updatedPath(repo string) string {
	return "repos/" + repo + "/pulls?state=all&sort=updated&direction=desc&per_page=100"
}

// clear drops the stored tag of a path or Next URL, keyed as the client keys it.
func (l *Lister) clear(target string) {
	// A target that doesn't parse never reached the client's tag store.
	ref, err := url.Parse(target)
	if err == nil {
		l.tags.Set(l.base.ResolveReference(ref).String(), "")
	}
}

// fetch reads one page. A later page clears its tag first, so it can't answer
// 304. If one did, the empty body would fail to decode.
func (l *Lister) fetch(ctx context.Context, priority budget.Priority, target string, page int) (
	*ghclient.Response, error,
) {
	if page > 1 {
		l.clear(target)
	}

	resp, err := l.client.Get(ctx, priority, target)
	if err != nil {
		return nil, fmt.Errorf("list pages: %w", err)
	}

	return resp, nil
}

// readRepositories returns the cached list after a 304 of the first page. The
// second bool is true when the list fit on one page.
func (l *Lister) readRepositories(ctx context.Context) ([]Repository, bool, bool, error) {
	repos := []Repository{}
	target := repositoriesPath

	for page := 1; ; page++ {
		resp, err := l.fetch(ctx, budget.Incremental, target, page)
		if err != nil {
			return nil, false, false, err
		}

		if resp.NotModified() && page == 1 && l.valid {
			return slices.Clone(l.cached), false, true, nil
		}

		var decoded repositoryPage

		err = json.Unmarshal(resp.Body, &decoded)
		if err != nil {
			return nil, false, false, fmt.Errorf("decode repositories: %w", err)
		}

		repos = append(repos, decoded.Repositories...)

		switch {
		case resp.Next == "":
			return repos, false, page == 1, nil
		case page >= MaxRepoPages:
			return repos, true, false, nil
		}

		target = resp.Next
	}
}

func (l *Lister) walk(ctx context.Context, repo, first string, opts WalkOptions) (Walked, error) {
	var walked Walked

	target := first

	for page := 1; ; page++ {
		resp, err := l.fetch(ctx, opts.Priority, target, page)
		if err != nil {
			return Walked{
				Newest:    time.Time{},
				Items:     nil,
				Pages:     walked.Pages,
				Truncated: false,
				Unchanged: false,
			}, err
		}

		walked.Pages++
		if resp.NotModified() && page == 1 {
			walked.Unchanged = true

			return walked, nil
		}

		var decoded []ClosedPR

		err = json.Unmarshal(resp.Body, &decoded)
		if err != nil {
			return Walked{
					Newest:    time.Time{},
					Items:     nil,
					Pages:     walked.Pages,
					Truncated: false,
					Unchanged: false,
				},
				fmt.Errorf(
					"decode pull requests: %w",
					err,
				)
		}

		if absorb(&walked, repo, decoded, opts.Stop) || resp.Next == "" {
			return walked, nil
		}

		if page >= opts.MaxPages {
			walked.Newest = time.Time{}
			walked.Truncated = true

			return walked, nil
		}

		target = resp.Next
	}
}

// absorb adds a page to the walk and reports whether it reached the stop time.
func absorb(walked *Walked, repo string, page []ClosedPR, stop time.Time) bool {
	for _, item := range page {
		if item.UpdatedAt.Before(stop) {
			return true
		}

		if item.UpdatedAt.After(walked.Newest) {
			walked.Newest = item.UpdatedAt
		}

		if !item.ClosedAt.IsZero() {
			item.Repository = repo
			item.Merged = !item.MergedAt.IsZero()
			walked.Items = append(walked.Items, item)
		}
	}

	return false
}
