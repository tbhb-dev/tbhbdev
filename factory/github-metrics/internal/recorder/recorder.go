// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package recorder records metrics and logs from queued GitHub deliveries.
package recorder

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

// maxAge is how old a milestone may be and still count, as appendix A step 5 says.
const maxAge = 24 * time.Hour

const (
	actionClosed     = "closed"
	actionCompleted  = "completed"
	failure          = "failure"
	milestoneMerged  = "merged"
	milestoneEnded   = "ended"
	logTextLimit     = 128
	metricNameLimit  = 64
	failedStepsLimit = 8
)

// ErrNoLedger means New got no ledger to claim milestones in.
var ErrNoLedger = errors.New("recorder needs a ledger")

// Ledger holds what the eligibility rule reads and the one claim per milestone.
// The store satisfies it.
type Ledger interface {
	// Created returns the time of the store's first open.
	Created() time.Time
	// RepairFloor returns the time of the claiming recorder's first start.
	RepairFloor() (time.Time, error)
	// ClaimMilestone returns true and keeps the key when it was absent.
	ClaimMilestone(key string) (bool, error)
}

// Result says what RecordClosed did with one milestone.
type Result string

// The results of one milestone.
const (
	ResultTooOld         Result = "too_old"
	ResultPreFloor       Result = "pre_floor"
	ResultAlreadyCounted Result = "already_counted"
	ResultRecorded       Result = "recorded"
)

// Outcome holds the result for each milestone of a closed PR. A milestone the
// PR doesn't have, or has no valid time for, keeps an empty result.
type Outcome struct{ Merged, Ended Result }

// ClosedPR is a closed pull request from a webhook body or a REST list item.
// The first end's time and state travel together, apart from the merge.
type ClosedPR struct {
	CreatedAt, MergedAt, EndedAt time.Time
	Repository, NodeID, Body     string
	// EndedState is "merged" or "closed" and picks the cycle time series.
	EndedState string
	Merged     bool
	Repair     bool
}

// Recorder records metrics and logs for GitHub events.
type Recorder struct {
	events, merges, conclusions, dropped metric.Int64Counter
	ciJobFailures                        metric.Int64Counter
	cycleTime, ciJobQueue, ciJobRun      metric.Float64Histogram
	mainRed                              metric.Int64Gauge
	ledger                               Ledger
	log                                  *slog.Logger
	now                                  func() time.Time
}

type checkRun struct {
	Name       string `json:"name"`
	Conclusion string `json:"conclusion"`
	CheckSuite struct {
		HeadBranch string `json:"head_branch"` //nolint:tagliatelle // GitHub uses this field name.
	} `json:"check_suite"` //nolint:tagliatelle // GitHub uses this field name.
}

type workflowJob struct {
	CreatedAt    string   `json:"created_at"` //nolint:tagliatelle // GitHub uses this field name.
	Labels       []string `json:"labels"`
	WorkflowName string   `json:"workflow_name"` //nolint:tagliatelle // GitHub uses this field name.
	Name         string   `json:"name"`
	Conclusion   string   `json:"conclusion"`
	HeadBranch   string   `json:"head_branch"`  //nolint:tagliatelle // GitHub uses this field name.
	HeadSHA      string   `json:"head_sha"`     //nolint:tagliatelle // GitHub uses this field name.
	StartedAt    string   `json:"started_at"`   //nolint:tagliatelle // GitHub uses this field name.
	CompletedAt  string   `json:"completed_at"` //nolint:tagliatelle // GitHub uses this field name.
	Steps        []struct {
		Name       string `json:"name"`
		Conclusion string `json:"conclusion"`
	} `json:"steps"`
	ID    int64 `json:"id"`
	RunID int64 `json:"run_id"` //nolint:tagliatelle // GitHub uses this field name.
}

// WorkflowJob is the shared GitHub job payload.
type WorkflowJob = workflowJob

// JobEventClass uses the live recorder's branch classification.
func JobEventClass(
	branch, defaultBranch string,
) string {
	return jobEventClass(branch, defaultBranch)
}

type pullRequest struct {
	Body      string `json:"body"`
	NodeID    string `json:"node_id"`    //nolint:tagliatelle // GitHub uses this field name.
	CreatedAt string `json:"created_at"` //nolint:tagliatelle // GitHub uses this field name.
	MergedAt  string `json:"merged_at"`  //nolint:tagliatelle // GitHub uses this field name.
	ClosedAt  string `json:"closed_at"`  //nolint:tagliatelle // GitHub uses this field name.
	Merged    bool   `json:"merged"`
}

// New creates the instruments used to record deliveries. The ledger claims
// each milestone before it counts.
func New(meter metric.Meter, ledger Ledger, log *slog.Logger) (*Recorder, error) {
	if ledger == nil {
		return nil, ErrNoLedger
	}

	var recorder Recorder

	recorder.ledger, recorder.log, recorder.now = ledger, log, time.Now

	for _, counter := range []struct {
		target *metric.Int64Counter
		name   string
	}{
		{&recorder.events, "github.events"},
		{&recorder.merges, "github.pr.merges"},
		{&recorder.conclusions, "github.check.conclusions"},
		{&recorder.dropped, "github.events.dropped"},
		{&recorder.ciJobFailures, "ci_job_failures_total"},
	} {
		instrument, err := meter.Int64Counter(counter.name)
		if err != nil {
			return nil, fmt.Errorf("create %s counter: %w", counter.name, err)
		}

		*counter.target = instrument
	}

	for _, reason := range []string{"empty_event", "bad_body", "no_node_id"} {
		recorder.dropped.Add(context.Background(), 0,
			metric.WithAttributes(attribute.String("reason", reason)))
	}

	err := recorder.setupDurations(meter)
	if err != nil {
		return nil, err
	}

	recorder.mainRed, err = meter.Int64Gauge("github.main.red")
	if err != nil {
		return nil, fmt.Errorf("create github.main.red gauge: %w", err)
	}

	return &recorder, nil
}

// Record counts a delivery or a drop before allowing its removal from the queue.
func (r *Recorder) Record(ctx context.Context, event string, body []byte) error {
	if event == "" {
		r.dropped.Add(ctx, 1, metric.WithAttributes(attribute.String("reason", "empty_event")))

		return nil
	}

	var delivery struct {
		Repository struct {
			FullName      string `json:"full_name"`      //nolint:tagliatelle // GitHub uses this field name.
			DefaultBranch string `json:"default_branch"` //nolint:tagliatelle // GitHub uses this field name.
		} `json:"repository"`
		Action      string          `json:"action"`
		CheckRun    checkRun        `json:"check_run"`    //nolint:tagliatelle // GitHub uses this field name.
		PullRequest pullRequest     `json:"pull_request"` //nolint:tagliatelle // GitHub uses this field name.
		WorkflowJob json.RawMessage `json:"workflow_job"` //nolint:tagliatelle // GitHub uses this field name.
	}

	err := json.Unmarshal(body, &delivery)
	if err != nil {
		r.dropped.Add(ctx, 1, metric.WithAttributes(attribute.String("reason", "bad_body")))

		return nil //nolint:nilerr // Bad data is counted as a drop and may leave the queue.
	}

	repository := delivery.Repository.FullName
	if repository == "" {
		repository = "unknown"
	}

	// Claim before any Add, so a claim error leaves the retried entry uncounted.
	if tracksPullRequest(event, delivery.Action) {
		if delivery.PullRequest.NodeID == "" {
			r.dropped.Add(ctx, 1, metric.WithAttributes(attribute.String("reason", "no_node_id")))

			return nil
		}

		err = r.recordPullRequest(ctx, repository, delivery.Action, delivery.PullRequest)
		if err != nil {
			return err
		}
	}

	if event == "workflow_job" && delivery.Action == actionCompleted {
		return r.recordJob(ctx, repository, delivery.Repository.DefaultBranch, delivery.WorkflowJob)
	}

	r.recordEvent(ctx, repository, event, delivery.Action)

	if event == "check_run" {
		r.recordCheckRun(
			ctx,
			attribute.String("repository", repository),
			delivery.Action,
			delivery.Repository.DefaultBranch,
			delivery.CheckRun,
		)
	}

	return nil
}

func tracksPullRequest(event, action string) bool {
	return event == "pull_request" && (action == actionClosed || action == "reopened")
}

// RecordClosed counts the merge and the first end of a closed PR once each.
// A milestone at least 24 hours old or at or before the store's creation time
// is claimed without recording and returns ResultTooOld.
// For a repair, an otherwise eligible milestone at or before the repair floor
// returns ResultPreFloor without recording. The repair claims ended to guard
// against a second cycle time after a reopen, and leaves merged unclaimed so
// a queued webhook can still count the merge. Webhook closes ignore the floor.
// A floor read or claim error returns before recording the milestone, so the
// caller can retry.
func (r *Recorder) RecordClosed(ctx context.Context, closed ClosedPR) (Outcome, error) {
	var (
		out Outcome
		err error
	)

	repo := attribute.String("repository", closed.Repository)

	if closed.Merged && !closed.MergedAt.IsZero() {
		out.Merged, err = r.milestone(closed, milestoneMerged, closed.MergedAt, func() {
			r.merges.Add(ctx, 0, metric.WithAttributes(repo))
			r.merges.Add(ctx, 1, metric.WithAttributes(repo))
		})
		if err != nil {
			return out, err
		}
	}

	if !closed.EndedAt.IsZero() && !closed.CreatedAt.IsZero() {
		out.Ended, err = r.milestone(
			closed,
			milestoneEnded,
			closed.EndedAt,
			func() { r.recordCycleTime(ctx, repo, closed) },
		)
	}

	return out, err
}

//nolint:mnd // The histograms have fixed duration buckets in seconds.
func (r *Recorder) setupDurations(meter metric.Meter) error {
	var err error

	r.cycleTime, err = meter.Float64Histogram("github.pr.cycle_time", metric.WithUnit("s"),
		metric.WithExplicitBucketBoundaries(
			60, 300, 600, 900, 1800, 3600, 7200, 14400, 28800,
			86400, 172800, 259200, 345600, 432000, 518400, 604800, 1209600, 2592000,
		))
	if err != nil {
		return fmt.Errorf("create github.pr.cycle_time histogram: %w", err)
	}

	for _, histogram := range []struct {
		target *metric.Float64Histogram
		name   string
	}{
		{&r.ciJobQueue, "github.ci.job.queue_seconds"},
		{&r.ciJobRun, "github.ci.job.run_seconds"},
	} {
		instrument, histErr := meter.Float64Histogram(
			histogram.name,
			metric.WithUnit("s"),
			metric.WithExplicitBucketBoundaries(
				5,
				15,
				30,
				60,
				120,
				300,
				600,
				900,
				1200,
				1800,
				2700,
				3600,
				5400,
				7200,
			),
		)
		if histErr != nil {
			return fmt.Errorf("create %s histogram: %w", histogram.name, histErr)
		}

		*histogram.target = instrument
	}

	return nil
}

func (r *Recorder) recordEvent(ctx context.Context, repository, event, action string) {
	repo := attribute.String("repository", repository)
	r.merges.Add(ctx, 0, metric.WithAttributes(repo))

	eventAttrs := metric.WithAttributes(repo,
		attribute.String("event", event), attribute.String("action", action))
	r.events.Add(ctx, 0, eventAttrs)
	r.events.Add(ctx, 1, eventAttrs)
}

// recordPullRequest handles a closed or reopened delivery. For a PR created
// at or before the repair floor, a reopen claims ended to guard against
// counting a first close the old recorder may already have counted.
func (r *Recorder) recordPullRequest(
	ctx context.Context,
	repository, action string,
	request pullRequest,
) error {
	if action == actionClosed {
		_, err := r.RecordClosed(ctx, closedFromWebhook(repository, request))

		return err
	}

	floor, err := r.ledger.RepairFloor()
	if err != nil {
		return fmt.Errorf("read repair floor: %w", err)
	}

	if parseTime(request.CreatedAt).After(floor) {
		return nil
	}

	_, err = r.ledger.ClaimMilestone(request.NodeID + "\x00" + milestoneEnded)
	if err != nil {
		return fmt.Errorf("claim ended milestone: %w", err)
	}

	return nil
}

func (r *Recorder) recordCheckRun(
	ctx context.Context, repo attribute.KeyValue, action, defaultBranch string, run checkRun,
) {
	const conclusion = "conclusion"

	checkName := attribute.String("check_name", run.Name)

	for _, known := range []string{
		"success", failure, "cancelled", "skipped", "neutral",
		"timed_out", "action_required", "stale", "startup_failure",
	} {
		r.conclusions.Add(ctx, 0, metric.WithAttributes(repo, checkName,
			attribute.String(conclusion, known)))
	}

	if action != actionCompleted {
		return
	}

	r.conclusions.Add(ctx, 1, metric.WithAttributes(repo, checkName,
		attribute.String(conclusion, run.Conclusion)))

	if run.Name != conclusion || run.Conclusion == "" ||
		defaultBranch == "" || run.CheckSuite.HeadBranch != defaultBranch {
		return
	}

	var red int64
	if run.Conclusion == failure || run.Conclusion == "timed_out" {
		red = 1
	}

	r.mainRed.Record(ctx, red, metric.WithAttributes(repo))
}

func (r *Recorder) recordJob(
	ctx context.Context,
	repository, defaultBranch string,
	body json.RawMessage,
) error {
	var job workflowJob

	err := json.Unmarshal(body, &job)
	if err != nil || job.ID == 0 {
		return nil //nolint:nilerr // Invalid job data may leave the queue.
	}

	claimed, err := r.ledger.ClaimMilestone(strconv.FormatInt(job.ID, 10) + "\x00ci_job")
	if err != nil {
		return fmt.Errorf("claim ci job milestone: %w", err)
	}

	if !claimed {
		return nil
	}

	r.recordJobTiming(ctx, repository, defaultBranch, job)

	if job.Conclusion == failure {
		r.recordFailedJob(ctx, repository, job)
	}

	r.recordEvent(ctx, repository, "workflow_job", actionCompleted)

	return nil
}

func (r *Recorder) recordJobTiming(
	ctx context.Context,
	repository, defaultBranch string,
	job workflowJob,
) {
	eventClass, runner := jobEventClass(job.HeadBranch, defaultBranch), "none"
	if len(job.Labels) > 0 {
		runner = job.Labels[0]
	}

	values := []struct{ key, value string }{
		{"repository", repository},
		{"workflow", job.WorkflowName},
		{"job", job.Name},
		{"conclusion", job.Conclusion},
		{"event_class", eventClass},
		{"runner", runner},
	}
	attrs := make([]attribute.KeyValue, 0, len(values))
	fields := []any{}

	for _, field := range values {
		// The scrape job label overwrites a job attribute, so the metric uses ci_job. Logs keep job.
		attrKey := field.key
		if attrKey == "job" {
			attrKey = "ci_job"
		}

		attrs = append(attrs, attribute.String(attrKey, cleanText(field.value, metricNameLimit)))
		fields = append(fields, field.key, cleanText(field.value, logTextLimit))
	}

	fields = append(fields,
		"head_branch", cleanText(job.HeadBranch, logTextLimit),
		"head_sha", cleanText(job.HeadSHA, logTextLimit), "run_id", job.RunID, "job_id", job.ID)

	for _, duration := range []struct {
		histogram        metric.Float64Histogram
		name, start, end string
	}{
		{r.ciJobQueue, "queue_seconds", job.CreatedAt, job.StartedAt},
		{r.ciJobRun, "run_seconds", job.StartedAt, job.CompletedAt},
	} {
		start, end := parseTime(duration.start), parseTime(duration.end)
		if start.IsZero() || end.IsZero() || end.Before(start) {
			continue
		}

		seconds := end.Sub(start).Seconds()
		duration.histogram.Record(ctx, seconds, metric.WithAttributes(attrs...))
		fields = append(fields, duration.name, seconds)
	}

	r.log.InfoContext(ctx, "ci job finished", fields...)
}

// jobEventClass approximates the trigger: scheduled and manual runs on the default
// branch count as push, and other triggers on non-default branches count as pull_request.
func jobEventClass(branch, defaultBranch string) string {
	switch {
	case strings.HasPrefix(branch, "gh-readonly-queue/"):
		return "merge_group"
	case branch == defaultBranch:
		return "push"
	default:
		return "pull_request"
	}
}

func (r *Recorder) recordFailedJob(ctx context.Context, repository string, job workflowJob) {
	var failedSteps []string

	for _, step := range job.Steps {
		if step.Conclusion == failure {
			failedSteps = append(failedSteps, step.Name)
			if len(failedSteps) == failedStepsLimit {
				break
			}
		}
	}

	const repositoryKey, workflowKey, jobKey = "repository", "workflow", "job"

	repository = cleanText(repository, logTextLimit)
	r.log.InfoContext(ctx, "ci job failed",
		repositoryKey, repository,
		workflowKey, cleanText(job.WorkflowName, logTextLimit),
		jobKey, cleanText(job.Name, logTextLimit),
		"failed_steps", cleanText(strings.Join(failedSteps, ", "), logTextLimit),
		"head_branch", cleanText(job.HeadBranch, logTextLimit),
		"head_sha", cleanText(job.HeadSHA, logTextLimit),
		"run_id", job.RunID,
		"started_at", cleanText(job.StartedAt, logTextLimit),
		"completed_at", cleanText(job.CompletedAt, logTextLimit))

	r.ciJobFailures.Add(ctx, 1, metric.WithAttributes(
		attribute.String(repositoryKey, repository),
		attribute.String(workflowKey, cleanText(job.WorkflowName, metricNameLimit)),
		attribute.String("ci_job", cleanText(job.Name, metricNameLimit)),
	))
}

func cleanText(text string, limit int) string {
	text = strings.ReplaceAll(strings.ReplaceAll(text, "\n", ""), "\r", "")
	for i := range text {
		if limit == 0 {
			return text[:i]
		}

		limit--
	}

	return text
}

func closedFromWebhook(repository string, request pullRequest) ClosedPR {
	var closed ClosedPR

	closed.Repository, closed.NodeID, closed.Body = repository, request.NodeID, request.Body
	closed.CreatedAt, closed.MergedAt = parseTime(request.CreatedAt), parseTime(request.MergedAt)
	closed.EndedAt, closed.EndedState, closed.Merged = parseTime(
		request.ClosedAt,
	), actionClosed, request.Merged

	if request.Merged {
		closed.EndedState = "merged"
	}

	return closed
}

func parseTime(text string) time.Time {
	parsed, err := time.Parse(time.RFC3339, text)
	if err != nil {
		return time.Time{}
	}

	return parsed
}

// milestone applies the eligibility rule to one milestone time, claims it, and
// calls record only after a true claim.
func (r *Recorder) milestone(
	closed ClosedPR,
	name string,
	when time.Time,
	record func(),
) (Result, error) {
	var skip Result

	switch {
	case r.now().Sub(when) >= maxAge || !when.After(r.ledger.Created()):
		skip = ResultTooOld
	case closed.Repair:
		floor, err := r.ledger.RepairFloor()
		if err != nil {
			return "", fmt.Errorf("read repair floor: %w", err)
		}

		if !when.After(floor) {
			if name == milestoneMerged {
				return ResultPreFloor, nil
			}

			skip = ResultPreFloor
		}
	}

	claimed, err := r.ledger.ClaimMilestone(closed.NodeID + "\x00" + name)
	if err != nil {
		return "", fmt.Errorf("claim %s milestone: %w", name, err)
	}

	switch {
	case skip != "":
		return skip, nil
	case !claimed:
		return ResultAlreadyCounted, nil
	}

	record()

	return ResultRecorded, nil
}

func (r *Recorder) recordCycleTime(ctx context.Context, repo attribute.KeyValue, closed ClosedPR) {
	start, source := cycleStart(closed.CreatedAt, closed.Body)
	if start.IsZero() || closed.EndedAt.Before(start) {
		return
	}

	r.cycleTime.Record(ctx, closed.EndedAt.Sub(start).Seconds(), metric.WithAttributes(repo,
		attribute.String("state", closed.EndedState), attribute.String("start_source", source)))
}

func cycleStart(start time.Time, body string) (time.Time, string) {
	// pr_created is interim: the owner's rule starts at the earliest commit time.
	// That needs the REST commits call in a later change, so interim values read low.
	source := "pr_created"
	startedAt := regexp.MustCompile(`\bstarted_at[:=]\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)`)

	for line := range strings.Lines(body) {
		if !strings.HasPrefix(line, "Generated-by:") && !strings.HasPrefix(line, "Assisted-by:") {
			continue
		}

		for _, match := range startedAt.FindAllStringSubmatch(line, -1) {
			candidate, parseErr := time.Parse("2006-01-02T15:04:05Z", match[1])
			if parseErr == nil && candidate.Before(start) {
				start, source = candidate, "started_at"
			}
		}
	}

	return start, source
}
