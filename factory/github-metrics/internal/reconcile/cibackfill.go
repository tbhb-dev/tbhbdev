// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strconv"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
)

const (
	completed    = "completed"
	runPageLimit = 30
	jobPageLimit = 3
)

// CIConfig selects one repository and a window of up to seven days.
type CIConfig struct {
	Repository string
	Days       int
}

// Ledger shares the live recorder's durable job claims.
type Ledger interface {
	ClaimMilestone(key string) (bool, error)
}

type ciPass struct {
	client                       *ghclient.Client
	ledger                       Ledger
	log                          *slog.Logger
	start, end                   time.Time
	repository, defaultBranch    string
	runs, logged, pages, skipped int
}

// BackfillCI logs exactly one terminal summary, including on cancellation or a claim error.
func BackfillCI(
	ctx context.Context,
	client *ghclient.Client,
	ledger Ledger,
	log *slog.Logger,
	cfg CIConfig,
	now time.Time,
) {
	state := ciPass{
		client: client, ledger: ledger, log: log, repository: cfg.Repository,
		defaultBranch: "", start: now.Add(-time.Duration(cfg.Days) * 24 * time.Hour), end: now,
		runs: 0, logged: 0, skipped: 0, pages: 0,
	}

	log.InfoContext(ctx, "ci backfill started", "repository", cfg.Repository, "days", cfg.Days)

	err := state.walk(ctx, "/repos/"+cfg.Repository+"/actions/runs?per_page=100&created="+
		url.QueryEscape(">="+state.start.Format(time.RFC3339)), runPageLimit, state.recordRuns)
	if err == nil {
		err = ctx.Err()
	}

	fields := []any{
		"repository", cfg.Repository, "runs", state.runs, "jobs_logged", state.logged,
		"jobs_skipped", state.skipped, "pages", state.pages, "duration", time.Since(now),
	}

	if err != nil {
		status := 0

		if response, ok := errors.AsType[*ghclient.StatusError](err); ok {
			status = response.Status
		}

		// Error text from dependencies may contain operator configuration; keep only status.
		log.ErrorContext(
			ctx,
			"ci backfill failed",
			append(fields, "status", status, "errors", 1)...,
		)

		return
	}

	log.InfoContext(ctx, "ci backfill finished", fields...)
}

func (p *ciPass) walk(
	ctx context.Context,
	target string,
	limit int,
	visit func(context.Context, []byte) error,
) error {
	for range limit {
		response, err := p.client.Get(ctx, budget.Backfill, target)

		p.pages++

		if err != nil {
			return fmt.Errorf("read CI history: %w", err)
		}

		if response.NotModified() {
			return nil
		}

		err = visit(ctx, response.Body)
		if err != nil {
			return err
		}

		if response.Next == "" {
			return nil
		}

		target = response.Next
	}

	return nil
}

func (p *ciPass) recordRuns(ctx context.Context, body []byte) error {
	var page struct {
		Runs []struct {
			Status string `json:"status"`
			Event  string `json:"event"`
			ID     int64  `json:"id"`
		} `json:"workflow_runs"` //nolint:tagliatelle // GitHub field name.
	}

	err := json.Unmarshal(body, &page)
	if err != nil {
		return fmt.Errorf("decode runs: %w", err)
	}

	for _, run := range page.Runs {
		p.runs++

		if run.Status != completed {
			continue
		}

		if p.defaultBranch == "" {
			response, readErr := p.client.Get(ctx, budget.Backfill, "/repos/"+p.repository)

			p.pages++

			if readErr != nil {
				return fmt.Errorf("read repository: %w", readErr)
			}

			var repo struct {
				DefaultBranch string `json:"default_branch"` //nolint:tagliatelle // GitHub field name.
			}

			err = json.Unmarshal(response.Body, &repo)
			if err != nil {
				return fmt.Errorf("decode repository: %w", err)
			}

			p.defaultBranch = repo.DefaultBranch
		}

		err = p.walk(ctx, "/repos/"+p.repository+"/actions/runs/"+strconv.FormatInt(run.ID, 10)+
			"/jobs?per_page=100&filter=all", jobPageLimit, func(ctx context.Context, body []byte) error {
			return p.recordJobs(ctx, body, run.Event)
		})
		if err != nil {
			return err
		}
	}

	return nil
}

func (p *ciPass) recordJobs(ctx context.Context, body []byte, event string) error {
	var page struct {
		Jobs []recorder.WorkflowJob `json:"jobs"`
	}

	err := json.Unmarshal(body, &page)
	if err != nil {
		return fmt.Errorf("decode jobs: %w", err)
	}

	for _, job := range page.Jobs {
		if ctx.Err() != nil {
			return fmt.Errorf("cancel backfill: %w", ctx.Err())
		}

		end, parseErr := time.Parse(time.RFC3339, job.CompletedAt)

		if parseErr != nil || !p.inWindow(end) {
			p.skipped++

			continue
		}

		claimed, claimErr := p.ledger.ClaimMilestone(strconv.FormatInt(job.ID, 10) + "\x00ci_job")
		if claimErr != nil {
			return fmt.Errorf("claim CI job: %w", claimErr)
		}

		if !claimed {
			p.skipped++

			continue
		}

		record := p.jobRecord(end, job, event)

		err = p.log.Handler().Handle(ctx, record)
		if err != nil {
			return fmt.Errorf("log CI job: %w", err)
		}

		p.logged++
	}

	return nil
}

func (p *ciPass) jobRecord(end time.Time, job recorder.WorkflowJob, event string) slog.Record {
	record := slog.NewRecord(end, slog.LevelInfo, "ci job finished", 0)
	runner := "none"

	if len(job.Labels) > 0 {
		runner = job.Labels[0]
	}

	record.Add(
		"repository",
		p.repository,
		"workflow",
		job.WorkflowName,
		"job",
		job.Name,
		"conclusion",
		job.Conclusion,
		"event_class",
		recorder.JobEventClass(job.HeadBranch, p.defaultBranch),
		"runner",
		runner,
		"head_branch",
		job.HeadBranch,
		"head_sha",
		job.HeadSHA,
		"run_id",
		job.RunID,
		"job_id",
		job.ID,
		"event",
		event,
		"source",
		"backfill",
	)

	for _, duration := range []struct{ name, start, end string }{
		{"queue_seconds", job.CreatedAt, job.StartedAt}, {"run_seconds", job.StartedAt, job.CompletedAt},
	} {
		start, startErr := time.Parse(time.RFC3339, duration.start)
		finish, endErr := time.Parse(time.RFC3339, duration.end)

		if startErr == nil && endErr == nil && !start.IsZero() && !finish.IsZero() &&
			!finish.Before(start) {
			record.Add(duration.name, finish.Sub(start).Seconds())
		}
	}

	return record
}

func (p *ciPass) inWindow(end time.Time) bool {
	return !end.IsZero() && !end.Before(p.start) && !end.After(p.end)
}
