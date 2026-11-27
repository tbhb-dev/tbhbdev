// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
)

const (
	watermarkOverlap = 5 * time.Minute
	repairWindow     = 24 * time.Hour
)

// RepositoryLister supplies the installation's repositories and updated pull requests.
type RepositoryLister interface {
	Repositories(ctx context.Context) ([]Repository, bool, error)
	// Updated clears its first-page tag after an incomplete walk.
	Updated(ctx context.Context, repo string, opts WalkOptions) (Walked, error)
	// ClearUpdated drops the repository's first-page tag after a failure the
	// walk couldn't see.
	ClearUpdated(repo string)
}

// EndReader reads the first end of a pull request from its issue events.
type EndReader interface {
	FirstEnd(ctx context.Context, repo string, number int, priority budget.Priority) (End, error)
}

// ClosedRecorder claims and records pull request milestones.
type ClosedRecorder interface {
	RecordClosed(ctx context.Context, closed recorder.ClosedPR) (recorder.Outcome, error)
}

// Watermarks stores the latest update seen in each repository's completed walk.
type Watermarks interface {
	Watermark(repository string) (time.Time, error)
	SetWatermark(repository string, updated time.Time) error
}

// Clock supplies the start time of the pass.
type Clock interface {
	Now() time.Time
}

// RunStatus describes how much of the installation the pass covered.
type RunStatus string

// The results of one reconciliation pass.
const (
	RunOK         RunStatus = "ok"
	RunPartial    RunStatus = "partial"
	RunAuthFailed RunStatus = "auth_failed"
)

// RunResult counts attempted repository walks, their fetched pages (pull
// request and event pages) and milestone outcomes. The first four outcome
// counts are for the merge milestone, the next four for the ended milestone.
// Ambiguous counts items whose first end couldn't be read, so only the merge
// was recorded.
type RunResult struct {
	Result                                              RunStatus
	Repositories, Pages, NotModified, Truncated         int
	Recorded, AlreadyCounted, TooOld, PreFloor, Skipped int
	EndedRecorded, EndedAlreadyCounted                  int
	EndedTooOld, EndedPreFloor, Ambiguous               int
}

// Run walks the installation once and repairs merge and ended counts. A sweep
// from Loop walks the full repair window. A watermark moves forward only when
// the walk reaches the stop time and processes every item.
func Run(
	ctx context.Context,
	lister RepositoryLister,
	events EndReader,
	record ClosedRecorder,
	store Watermarks,
	clock Clock,
) (RunResult, error) {
	var result RunResult

	opts := WalkOptions{
		Stop:     clock.Now().Add(-repairWindow),
		MaxPages: DefaultMaxPages,
		Priority: budget.Incremental,
	}
	if ctx.Value(sweepPassKey{}) != nil {
		opts.MaxPages, opts.Priority = SweepMaxPages, budget.Sweep
	}

	repos, truncated, err := lister.Repositories(ctx)
	if err != nil {
		handleReadError(&result, err)

		return result, nil
	}

	result.Result = RunOK
	if truncated {
		result.Result = RunPartial
	}

	for _, repository := range repos {
		if ctx.Err() != nil {
			result.Result = RunPartial

			break
		}

		stop, repairErr := repairRepository(
			ctx,
			lister,
			events,
			record,
			store,
			repository.FullName,
			opts,
			&result,
		)
		if repairErr != nil {
			result.Result = RunPartial

			return result, repairErr
		}

		if stop {
			break
		}
	}

	return result, nil
}

func repairRepository(
	ctx context.Context,
	lister RepositoryLister,
	events EndReader,
	record ClosedRecorder,
	store Watermarks,
	repo string,
	opts WalkOptions,
	result *RunResult,
) (bool, error) {
	watermark, err := store.Watermark(repo)
	if err != nil {
		return false, fmt.Errorf("read reconcile watermark: %w", err)
	}

	if opts.Priority == budget.Sweep {
		lister.ClearUpdated(repo)
	} else if overlap := watermark.Add(-watermarkOverlap); overlap.After(opts.Stop) {
		opts.Stop = overlap
	}

	result.Repositories++
	walked, err := lister.Updated(ctx, repo, opts)
	result.Pages += walked.Pages

	if err != nil {
		return handleReadError(result, err) == stopPass, nil
	}

	if walked.Unchanged {
		result.NotModified++

		return false, nil
	}

	if walked.Truncated {
		result.Truncated++
		result.Result = RunPartial
	}

	read, err := recordItems(ctx, events, record, walked.Items, opts.Priority, result)
	if read != readsComplete || err != nil {
		// A 304 would hide items this pass couldn't finish.
		lister.ClearUpdated(repo)

		return read == stopPass, err
	}

	return false, advanceWatermark(lister, store, repo, walked, watermark)
}

func advanceWatermark(
	lister RepositoryLister, store Watermarks, repo string, walked Walked, watermark time.Time,
) error {
	if walked.Truncated || !walked.Newest.After(watermark) {
		return nil
	}

	err := store.SetWatermark(repo, walked.Newest)
	if err != nil {
		lister.ClearUpdated(repo)

		return fmt.Errorf("write reconcile watermark: %w", err)
	}

	return nil
}

type readOutcome uint8

const (
	readsComplete readOutcome = iota
	skipRepository
	stopPass
)

// handleReadError sets the result and reports whether the pass can continue.
func handleReadError(result *RunResult, err error) readOutcome {
	status, ok := errors.AsType[*ghclient.StatusError](err)
	if errors.Is(err, ghclient.ErrTokenSource) || ok && status.Status == http.StatusUnauthorized {
		result.Result = RunAuthFailed

		return stopPass
	}

	result.Result = RunPartial

	if errors.Is(err, ghclient.ErrGaveUp) || errors.Is(err, context.DeadlineExceeded) {
		return stopPass
	}

	return skipRepository
}

// recordItems records each item's merge and first end. An events read failure
// reports whether to skip the repository or stop the pass.
func recordItems(
	ctx context.Context,
	events EndReader,
	record ClosedRecorder,
	items []ClosedPR,
	priority budget.Priority,
	result *RunResult,
) (readOutcome, error) {
	for _, item := range items {
		if item.ClosedAt.IsZero() {
			result.Skipped++

			continue
		}

		end, err := events.FirstEnd(ctx, item.Repository, item.Number, priority)
		result.Pages += end.Pages

		if err != nil {
			return handleReadError(result, err), nil
		}

		if end.At.IsZero() {
			result.Ambiguous++
		}

		outcome, err := record.RecordClosed(ctx, recorder.ClosedPR{
			CreatedAt:  item.CreatedAt,
			MergedAt:   item.MergedAt,
			EndedAt:    end.At,
			Repository: item.Repository,
			NodeID:     item.NodeID,
			Body:       item.Body,
			EndedState: end.State,
			Merged:     item.Merged,
			Repair:     true,
		})
		if err != nil {
			return readsComplete, fmt.Errorf("record reconciled milestones: %w", err)
		}

		tally(
			outcome.Merged,
			&result.Recorded,
			&result.AlreadyCounted,
			&result.TooOld,
			&result.PreFloor,
		)
		tally(
			outcome.Ended,
			&result.EndedRecorded,
			&result.EndedAlreadyCounted,
			&result.EndedTooOld,
			&result.EndedPreFloor,
		)
	}

	return readsComplete, nil
}

func tally(outcome recorder.Result, recorded, alreadyCounted, tooOld, preFloor *int) {
	switch outcome {
	case recorder.ResultRecorded:
		*recorded++
	case recorder.ResultAlreadyCounted:
		*alreadyCounted++
	case recorder.ResultTooOld:
		*tooOld++
	case recorder.ResultPreFloor:
		*preFloor++
	}
}
