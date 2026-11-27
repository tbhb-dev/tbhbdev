// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"slices"
	"testing"
	"testing/synctest"
	"time"

	"go.opentelemetry.io/otel/metric/noop"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
)

const (
	firstRepo  = "o/first"
	nextRepo   = "o/next"
	lastRepo   = "o/last"
	listedRepo = "o/r"

	closedState = "closed"
)

type runClock time.Time

func (clock runClock) Now() time.Time { return time.Time(clock) }

type runFake struct {
	readErr, writeErr, listErr error
	end                        func(reconcile.ClosedPR) (reconcile.End, error)
	walk                       func(string, reconcile.WalkOptions) (reconcile.Walked, error)
	record                     func(recorder.ClosedPR) (recorder.Outcome, error)
	watermarks                 map[string]time.Time
	repos, walked, wrote       []string
	cleared                    []string
	listTruncated              bool
}

// ended is the first end the fake events reads give: a plain close.
func ended(reconcile.ClosedPR) reconcile.End {
	return reconcile.End{
		At:    time.Date(2026, time.January, 2, 8, 0, 0, 0, time.UTC),
		State: closedState,
		Pages: 1,
	}
}

func (fake *runFake) ClearUpdated(repo string) { fake.cleared = append(fake.cleared, repo) }

func (fake *runFake) FirstEnd(
	_ context.Context,
	repo string,
	number int,
	_ budget.Priority,
) (reconcile.End, error) {
	item := reconcile.ClosedPR{
		CreatedAt:  time.Time{},
		MergedAt:   time.Time{},
		ClosedAt:   time.Time{},
		UpdatedAt:  time.Time{},
		NodeID:     "",
		Body:       "",
		Repository: repo,
		Number:     number,
		Merged:     false,
	}
	if fake.end == nil {
		return ended(item), nil
	}

	return fake.end(item)
}

func (fake *runFake) Repositories(context.Context) ([]reconcile.Repository, bool, error) {
	repos := make([]reconcile.Repository, 0, len(fake.repos))
	for _, name := range fake.repos {
		repos = append(repos, reconcile.Repository{FullName: name, ID: 0})
	}

	return repos, fake.listTruncated, fake.listErr
}

func (fake *runFake) Updated(
	_ context.Context,
	repo string,
	opts reconcile.WalkOptions,
) (reconcile.Walked, error) {
	fake.walked = append(fake.walked, repo)

	return fake.walk(repo, opts)
}

func (fake *runFake) RecordClosed(
	_ context.Context,
	closed recorder.ClosedPR,
) (recorder.Outcome, error) {
	return fake.record(closed)
}

func (fake *runFake) Watermark(repo string) (time.Time, error) {
	return fake.watermarks[repo], fake.readErr
}

func (fake *runFake) SetWatermark(repo string, updated time.Time) error {
	fake.wrote = append(fake.wrote, repo)
	if fake.writeErr == nil {
		fake.watermarks[repo] = updated
	}

	return fake.writeErr
}

func TestRunRepairsMerges(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.January, 2, 12, 0, 0, 0, time.UTC)
	created, merged, old := now.Add(-6*time.Hour), now.Add(-time.Hour), now.Add(-48*time.Hour)
	results := []recorder.Result{
		recorder.ResultRecorded,
		recorder.ResultAlreadyCounted,
		recorder.ResultTooOld,
		recorder.ResultPreFloor,
	}

	items := repairItems(created, merged, now, len(results))

	walks := map[string]reconcile.Walked{
		firstRepo: {Newest: now, Items: items, Pages: 2, Truncated: false, Unchanged: false},
		nextRepo:  {Newest: now, Items: nil, Pages: 1, Truncated: false, Unchanged: true},
		lastRepo:  {Newest: now, Items: nil, Pages: 50, Truncated: true, Unchanged: false},
	}

	var fake runFake

	fake.repos = []string{firstRepo, nextRepo, lastRepo}
	fake.watermarks = map[string]time.Time{firstRepo: merged, lastRepo: old}
	fake.walk = repairWalk(t, walks, now, merged)

	calls := 0
	fake.record = repairRecord(t, &fake, items, results, &calls)

	want := reconcile.RunResult{
		Result: reconcile.RunPartial, Repositories: 3, Pages: 57, NotModified: 1, Truncated: 1,
		Recorded: 1, AlreadyCounted: 1, TooOld: 1, PreFloor: 1, Skipped: 1,
		EndedRecorded: 1, EndedAlreadyCounted: 1, EndedTooOld: 1, EndedPreFloor: 1, Ambiguous: 0,
	}

	got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(now))
	if err != nil {
		t.Fatal(err)
	}

	if got != want || calls != len(results) {
		t.Fatalf(
			"result = %+v, err = %v, calls = %d, want %+v and %d calls",
			got,
			err,
			calls,
			want,
			len(results),
		)
	}

	checkWatermark(t, &fake, firstRepo, now)
}

func repairRecord(
	t *testing.T, fake *runFake, items []reconcile.ClosedPR, results []recorder.Result, calls *int,
) func(recorder.ClosedPR) (recorder.Outcome, error) {
	t.Helper()

	return func(got recorder.ClosedPR) (recorder.Outcome, error) {
		if *calls >= len(results) || len(fake.wrote) != 0 {
			t.Fatalf("unexpected record %d after watermark writes %v", *calls, fake.wrote)
		}

		want := recorder.ClosedPR{
			CreatedAt:  items[0].CreatedAt,
			MergedAt:   items[0].MergedAt,
			EndedAt:    ended(items[0]).At,
			Repository: firstRepo,
			NodeID:     items[*calls].NodeID,
			Body:       "",
			EndedState: closedState,
			Merged:     true,
			Repair:     true,
		}
		if got != want {
			t.Errorf("record = %+v, want %+v", got, want)
		}

		outcome := recorder.Outcome{
			Merged: results[*calls],
			Ended:  results[(*calls+1)%len(results)],
		}
		*calls++

		return outcome, nil
	}
}

func repairWalk(
	t *testing.T, walks map[string]reconcile.Walked, now, merged time.Time,
) func(string, reconcile.WalkOptions) (reconcile.Walked, error) {
	t.Helper()

	return func(repo string, got reconcile.WalkOptions) (reconcile.Walked, error) {
		stop := now.Add(-24 * time.Hour)
		if repo == firstRepo {
			stop = merged.Add(-5 * time.Minute)
		}

		want := reconcile.WalkOptions{
			Stop:     stop,
			MaxPages: reconcile.DefaultMaxPages,
			Priority: budget.Incremental,
		}
		if got != want {
			t.Errorf("walk %s = %+v, want %+v", repo, got, want)
		}

		return walks[repo], nil
	}
}

func repairItems(created, merged, now time.Time, count int) []reconcile.ClosedPR {
	items := make([]reconcile.ClosedPR, 0, count+1)
	for i := range count {
		items = append(items, reconcile.ClosedPR{
			CreatedAt: created, MergedAt: merged, ClosedAt: merged, UpdatedAt: now,
			NodeID: fmt.Sprintf("N%d", i), Body: "", Repository: firstRepo, Number: 0, Merged: true,
		})
	}

	// An item with a merge time and no close time has nothing to record.
	var missingClose reconcile.ClosedPR

	missingClose.MergedAt = merged
	items = append(items, missingClose)

	return items
}

func checkWatermark(t *testing.T, fake *runFake, repo string, now time.Time) {
	t.Helper()

	if !slices.Equal(fake.wrote, []string{repo}) || !fake.watermarks[repo].Equal(now) {
		t.Errorf("writes = %v, watermarks = %v", fake.wrote, fake.watermarks)
	}
}

type getFunc func(ctx context.Context, priority budget.Priority, target string) (*ghclient.Response, error)

func (get getFunc) Get(
	ctx context.Context,
	priority budget.Priority,
	target string,
) (*ghclient.Response, error) {
	return get(ctx, priority, target)
}

func TestSweepRepairsPastTheWatermark(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		time.Sleep(sweepStart)

		now := time.Now()
		missed := closedItem(now.Add(-time.Hour), true)
		lister := newSweepLister(t, now.Add(-24*time.Hour), missed)

		var fake runFake

		fake.watermarks = map[string]time.Time{listedRepo: now}
		fake.record = func(got recorder.ClosedPR) (recorder.Outcome, error) {
			if !got.MergedAt.Equal(missed.MergedAt) || !got.EndedAt.Equal(missed.ClosedAt) {
				t.Errorf("record = %+v, want the missed pull request", got)
			}

			return recorder.Outcome{
				Merged: recorder.ResultRecorded,
				Ended:  recorder.ResultRecorded,
			}, nil
		}

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		pass := func(ctx context.Context) (reconcile.RunResult, error) {
			defer cancel()

			got, runErr := reconcile.Run(ctx, lister, lister, &fake, &fake, runClock(now))
			checkSweepResult(t, got, runErr, fake.wrote)

			return got, nil
		}

		loop, err := reconcile.NewLoop(
			pass,
			noop.NewMeterProvider().Meter("test"),
			slog.New(slog.DiscardHandler),
			nil,
		)
		if err != nil {
			t.Fatal(err)
		}

		err = loop.Start(ctx)
		if err != nil {
			t.Fatal(err)
		}
	})
}

func checkSweepResult(t *testing.T, got reconcile.RunResult, err error, wrote []string) {
	t.Helper()

	if err != nil || got.Result != reconcile.RunOK || got.Pages != 2 ||
		got.Recorded != 1 || got.EndedRecorded != 1 || len(wrote) != 0 {
		t.Errorf("result = %+v, err = %v, watermark writes = %v", got, err, wrote)
	}
}

func newSweepLister(t *testing.T, floor time.Time, missed reconcile.ClosedPR) *reconcile.Lister {
	t.Helper()

	older := closedItem(floor.Add(-time.Second), true)

	pulls, err := json.Marshal([]reconcile.ClosedPR{missed, older})
	if err != nil {
		t.Fatal(err)
	}

	tags := ghclient.NewETags()
	ready := false
	get := sweepGetter(t, tags, &ready, pulls, missed.ClosedAt)

	var base url.URL

	base.Path = "/"
	lister := reconcile.NewLister(get, tags, &base)

	// Prime the tag with a clean walk at the same floor.
	_, err = lister.Updated(t.Context(), listedRepo, reconcile.WalkOptions{
		Stop: floor, MaxPages: reconcile.DefaultMaxPages, Priority: budget.Incremental,
	})
	if err != nil || tags.Get(firstPage) == "" {
		t.Fatalf("prime first-page tag: err = %v, tag = %q", err, tags.Get(firstPage))
	}

	ready = true

	return lister
}

func sweepGetter(
	t *testing.T,
	tags *ghclient.ETags,
	ready *bool,
	pulls []byte,
	closedAt time.Time,
) getFunc {
	t.Helper()

	return getFunc(
		func(_ context.Context, priority budget.Priority, target string) (*ghclient.Response, error) {
			resp := ghclient.Response{ETag: "", Next: "", Body: nil, Status: http.StatusOK}

			switch target {
			case repoPage[1:]:
				resp.Body = []byte(`{"repositories":[{"full_name":"` + listedRepo + `"}]}`)
			case firstPage[1:]:
				if *ready && (priority != budget.Sweep || tags.Get(firstPage) != "") {
					t.Errorf(
						"pulls priority = %v, tag = %q, want sweep priority and no tag",
						priority,
						tags.Get(firstPage),
					)
				}

				tags.Set(firstPage, `"first"`)
				resp.Body, resp.Next = pulls, firstPage+"&page=2"
			case eventsPath[1:]:
				if priority != budget.Sweep {
					t.Errorf("events priority = %v, want sweep priority", priority)
				}

				resp.Body = []byte(
					"[" + event(closedEvent, closedAt.Format(time.RFC3339)) + "]",
				)
			default:
				t.Fatalf("unexpected request for %s", target)
			}

			return &resp, nil
		},
	)
}

type failure struct {
	err    error
	result reconcile.RunStatus
	stop   bool
}

// failures lists the read errors of design section 8 and the run result of each.
func failures() []failure {
	decodeErr := json.Unmarshal([]byte("["), new([]reconcile.ClosedPR))

	return []failure{
		{ghclient.ErrTokenSource, reconcile.RunAuthFailed, true},
		{
			&ghclient.StatusError{Path: "", Status: http.StatusUnauthorized},
			reconcile.RunAuthFailed,
			true,
		},
		{
			&ghclient.StatusError{Path: "", Status: http.StatusForbidden},
			reconcile.RunPartial,
			false,
		},
		{&ghclient.StatusError{Path: "", Status: http.StatusNotFound}, reconcile.RunPartial, false},
		{ghclient.ErrGaveUp, reconcile.RunPartial, true},
		{context.DeadlineExceeded, reconcile.RunPartial, true},
		{ghclient.ErrBodyTooLarge, reconcile.RunPartial, false},
		{decodeErr, reconcile.RunPartial, false},
	}
}

func TestRunWalkFailure(t *testing.T) {
	t.Parallel()

	for _, test := range failures() {
		t.Run(test.err.Error(), func(t *testing.T) {
			t.Parallel()

			var fake runFake

			now := time.Now()
			previous := now.Add(-time.Hour)
			fake.repos = []string{firstRepo, nextRepo, lastRepo}
			fake.watermarks = map[string]time.Time{nextRepo: previous}
			fake.walk = func(repo string, _ reconcile.WalkOptions) (reconcile.Walked, error) {
				walked := reconcile.Walked{
					Newest:    now,
					Items:     nil,
					Pages:     1,
					Truncated: false,
					Unchanged: false,
				}
				if repo == nextRepo {
					return walked, fmt.Errorf("walk: %w", test.err)
				}

				return walked, nil
			}

			got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(now))

			checkRunReadFailure(t, &fake, test, previous, got, err, false)
		})
	}
}

func checkRunReadFailure(
	t *testing.T, fake *runFake, test failure, previous time.Time,
	got reconcile.RunResult, err error, events bool,
) {
	t.Helper()

	walked, wrote := fake.repos, []string{firstRepo, lastRepo}
	if test.stop {
		walked, wrote = walked[:2], wrote[:1]
	}

	pages, recorded := len(walked), 0
	if events {
		pages *= 2
		recorded = len(wrote)

		if !slices.Equal(fake.cleared, []string{nextRepo}) {
			t.Errorf("cleared = %v, want [%s]", fake.cleared, nextRepo)
		}
	}

	if err != nil || got.Result != test.result || got.Repositories != len(walked) ||
		got.Pages != pages || got.Recorded != recorded {
		t.Errorf("result = %+v, err = %v", got, err)
	}

	checkReadPaths(t, fake, walked, wrote, previous)
}

func checkReadPaths(t *testing.T, fake *runFake, walked, wrote []string, previous time.Time) {
	t.Helper()

	if !slices.Equal(fake.walked, walked) || !slices.Equal(fake.wrote, wrote) ||
		!fake.watermarks[nextRepo].Equal(previous) {
		t.Errorf(
			"walked = %v, wrote = %v, watermarks = %v",
			fake.walked,
			fake.wrote,
			fake.watermarks,
		)
	}
}

func TestRunStoreErrors(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		read, claim, write error
		name               string
	}{
		{io.ErrClosedPipe, nil, nil, "read"},
		{nil, io.ErrClosedPipe, nil, "claim"},
		{nil, nil, io.ErrClosedPipe, "write"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var fake runFake

			now := time.Now()
			fake.repos = []string{firstRepo, nextRepo}
			fake.watermarks = make(map[string]time.Time)
			fake.readErr, fake.writeErr = test.read, test.write
			fake.walk = func(string, reconcile.WalkOptions) (reconcile.Walked, error) {
				var item reconcile.ClosedPR

				item.ClosedAt, item.MergedAt = now, now
				walked := reconcile.Walked{
					Newest:    now,
					Items:     []reconcile.ClosedPR{item},
					Pages:     1,
					Truncated: false,
					Unchanged: false,
				}

				return walked, nil
			}
			fake.record = func(recorder.ClosedPR) (recorder.Outcome, error) {
				return recorder.Outcome{Merged: recorder.ResultRecorded, Ended: ""}, test.claim
			}

			got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(now))
			if !errors.Is(err, io.ErrClosedPipe) || got.Result != reconcile.RunPartial ||
				len(fake.walked) > 1 ||
				len(fake.watermarks) != 0 ||
				(test.write == nil && len(fake.wrote) != 0) ||
				(test.claim != nil && !slices.Equal(fake.cleared, []string{firstRepo})) {
				t.Errorf(
					"result = %+v, err = %v, walked = %v, wrote = %v",
					got,
					err,
					fake.walked,
					fake.wrote,
				)
			}
		})
	}
}

func TestRunRetriesWatermarkWrite(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.January, 2, 4, 0, 0, 0, time.UTC)
	servePulls := tagged(t, `"first"`, "["+item("open", now.Format(time.RFC3339), "", "")+"]")
	rig := newRig(t, func(resp http.ResponseWriter, req *http.Request, host string) {
		if req.URL.RequestURI() == repoPage {
			write(t, resp, `{"repositories":[{"full_name":"o/r"}]}`)

			return
		}

		servePulls(resp, req, host)
	})

	var fake runFake

	previous := now.Add(-time.Hour)
	fake.watermarks = map[string]time.Time{listedRepo: previous}
	fake.writeErr = io.ErrClosedPipe

	got, err := reconcile.Run(t.Context(), rig.lister, &fake, &fake, &fake, runClock(now))
	if !errors.Is(err, io.ErrClosedPipe) || got.Result != reconcile.RunPartial ||
		!fake.watermarks[listedRepo].Equal(previous) || rig.etags.Get(rig.base+firstPage) != "" {
		t.Fatalf("first pass = %+v, err = %v, watermarks = %v", got, err, fake.watermarks)
	}

	fake.writeErr = nil

	got, err = reconcile.Run(t.Context(), rig.lister, &fake, &fake, &fake, runClock(now))
	if err != nil || got.Result != reconcile.RunOK || got.NotModified != 0 ||
		!fake.watermarks[listedRepo].Equal(now) {
		t.Errorf("retry = %+v, err = %v, watermarks = %v", got, err, fake.watermarks)
	}
}

// closedItem is an item that closed, and merged when merged is true.
func closedItem(now time.Time, merged bool) reconcile.ClosedPR {
	item := reconcile.ClosedPR{
		CreatedAt: now.Add(-time.Hour), MergedAt: time.Time{}, ClosedAt: now, UpdatedAt: now,
		NodeID: "N", Body: "", Repository: firstRepo, Number: 7, Merged: false,
	}

	if merged {
		item.MergedAt, item.Merged = now, true
	}

	return item
}

// walkOf returns a walk that always finds the items, with the newest update at now.
func walkOf(
	now time.Time,
	items ...reconcile.ClosedPR,
) func(string, reconcile.WalkOptions) (reconcile.Walked, error) {
	return func(string, reconcile.WalkOptions) (reconcile.Walked, error) {
		return reconcile.Walked{
			Newest:    now,
			Items:     items,
			Pages:     1,
			Truncated: false,
			Unchanged: false,
		}, nil
	}
}

func TestRunRecordsUnmergedCloseAsEnded(t *testing.T) {
	t.Parallel()

	var fake runFake

	now := time.Now()
	item := closedItem(now, false)
	fake.repos = []string{firstRepo}
	fake.watermarks = make(map[string]time.Time)
	fake.walk = walkOf(now, item)
	fake.record = func(got recorder.ClosedPR) (recorder.Outcome, error) {
		want := recorder.ClosedPR{
			CreatedAt:  item.CreatedAt,
			MergedAt:   time.Time{},
			EndedAt:    ended(item).At,
			Repository: firstRepo,
			NodeID:     "N",
			Body:       "",
			EndedState: closedState,
			Merged:     false,
			Repair:     true,
		}
		if got != want {
			t.Errorf("record = %+v, want %+v", got, want)
		}

		return recorder.Outcome{Merged: "", Ended: recorder.ResultRecorded}, nil
	}

	got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(now))
	if err != nil || got.EndedRecorded != 1 || got.Recorded != 0 || got.Pages != 2 ||
		got.Result != reconcile.RunOK ||
		!slices.Equal(fake.wrote, []string{firstRepo}) {
		t.Errorf("result = %+v, err = %v, wrote = %v", got, err, fake.wrote)
	}
}

func TestRunAmbiguousEndRecordsMergeOnly(t *testing.T) {
	t.Parallel()

	var fake runFake

	now := time.Now()
	item := closedItem(now, true)
	fake.repos = []string{firstRepo}
	fake.watermarks = make(map[string]time.Time)
	fake.end = func(reconcile.ClosedPR) (reconcile.End, error) {
		return reconcile.End{At: time.Time{}, State: "", Pages: 3}, nil
	}
	fake.walk = walkOf(now, item)
	fake.record = func(got recorder.ClosedPR) (recorder.Outcome, error) {
		if !got.EndedAt.IsZero() || got.EndedState != "" || !got.Merged ||
			!got.MergedAt.Equal(now) {
			t.Errorf("record = %+v, want the merge only", got)
		}

		return recorder.Outcome{Merged: recorder.ResultRecorded, Ended: ""}, nil
	}

	got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(now))
	checkAmbiguousResult(t, got, err, fake.wrote)
}

func checkAmbiguousResult(t *testing.T, got reconcile.RunResult, err error, wrote []string) {
	t.Helper()

	if err != nil || got.Ambiguous != 1 || got.Recorded != 1 || got.EndedRecorded != 0 ||
		got.Pages != 4 || got.Result != reconcile.RunOK || !slices.Equal(wrote, []string{firstRepo}) {
		t.Errorf("result = %+v, err = %v, wrote = %v", got, err, wrote)
	}
}

func TestRunEventsFailure(t *testing.T) {
	t.Parallel()

	for _, test := range failures() {
		t.Run(test.err.Error(), func(t *testing.T) {
			t.Parallel()

			var fake runFake

			now := time.Now()
			previous := now.Add(-time.Hour)
			fake.repos = []string{firstRepo, nextRepo, lastRepo}
			fake.watermarks = map[string]time.Time{nextRepo: previous}
			fake.walk = func(repo string, _ reconcile.WalkOptions) (reconcile.Walked, error) {
				item := closedItem(now, true)
				item.Repository = repo

				return reconcile.Walked{
					Newest:    now,
					Items:     []reconcile.ClosedPR{item},
					Pages:     1,
					Truncated: false,
					Unchanged: false,
				}, nil
			}
			fake.end = func(item reconcile.ClosedPR) (reconcile.End, error) {
				end := ended(item)
				if item.Repository != nextRepo {
					return end, nil
				}

				return reconcile.End{
					At:    time.Time{},
					State: "",
					Pages: 1,
				}, fmt.Errorf(
					"read events: %w",
					test.err,
				)
			}
			fake.record = func(recorder.ClosedPR) (recorder.Outcome, error) {
				return recorder.Outcome{
					Merged: recorder.ResultRecorded,
					Ended:  recorder.ResultRecorded,
				}, nil
			}

			got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(now))

			checkRunReadFailure(t, &fake, test, previous, got, err, true)
		})
	}
}

func TestRunDeadlineBetweenRepositories(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(t.Context(), time.Second)
		defer cancel()

		var fake runFake

		fake.repos = []string{firstRepo, nextRepo}
		fake.walk = func(repo string, opts reconcile.WalkOptions) (reconcile.Walked, error) {
			<-ctx.Done()

			return walkOf(time.Time{})(repo, opts)
		}

		got, err := reconcile.Run(ctx, &fake, &fake, &fake, &fake, runClock(time.Now()))
		if err != nil || got.Result != reconcile.RunPartial || got.Repositories != 1 ||
			!slices.Equal(fake.walked, []string{firstRepo}) {
			t.Errorf("result = %+v, err = %v, walked = %v", got, err, fake.walked)
		}
	})
}

func TestRunRepositoryList(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		err       error
		name      string
		result    reconcile.RunStatus
		walks     int
		truncated bool
	}{
		{io.ErrUnexpectedEOF, "failed list", reconcile.RunPartial, 0, false},
		{ghclient.ErrTokenSource, "failed authentication", reconcile.RunAuthFailed, 0, false},
		{nil, "truncated list", reconcile.RunPartial, 1, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var fake runFake

			fake.repos = []string{firstRepo}
			fake.walk = walkOf(time.Time{})
			fake.listErr, fake.listTruncated = test.err, test.truncated

			got, err := reconcile.Run(t.Context(), &fake, &fake, &fake, &fake, runClock(time.Now()))
			if err != nil || got.Result != test.result || got.Repositories != test.walks ||
				got.Pages != test.walks ||
				len(fake.walked) != test.walks {
				t.Errorf("result = %+v, err = %v, walked = %v", got, err, fake.walked)
			}
		})
	}
}
