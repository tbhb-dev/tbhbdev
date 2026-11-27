// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package recorder_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"reflect"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

const (
	workflowJobEvent = "workflow_job"
	ciFailuresName   = "ci_job_failures_total"
	completedAction  = "completed"
	failureResult    = "failure"
	workflowKey      = "workflow"
	jobKey           = "job"
	ciJobKey         = "ci_job"
	completedAtKey   = "completed_at"
	headSHAKey       = "head_sha"
	queueMetric      = "github.ci.job.queue_seconds"
	runMetric        = "github.ci.job.run_seconds"
	finishedMessage  = "ci job finished"
	eventClassKey    = "event_class"
	runnerKey        = "runner"
	queueSecondsKey  = "queue_seconds"
	runSecondsKey    = "run_seconds"
	createdAtKey     = "created_at"
	pushEvent        = "push"
	noRunner         = "none"
	jobIDKey         = "job_id"
	runIDKey         = "run_id"
	jobSHA           = "123abc"
	queueStart       = "11:59:45"
	jobStart         = "12:00:00"
	jobEnd           = "12:01:00"
)

func TestRecordFailedWorkflowJob(t *testing.T) {
	t.Parallel()

	const body = `{"action":"completed","repository":{"full_name":"example/widgets"},"workflow_job":{
		"id":42,"created_at":"2026-11-15T11:59:45Z","workflow_name":"CI","name":"build",
		"head_branch":"main","head_sha":"123abc","run_id":493,
		"started_at":"2026-11-15T12:00:00Z","completed_at":"2026-11-15T12:01:00Z","conclusion":"failure",
		"html_url":"omit job URL","steps":[{"name":"checkout","conclusion":"success"},
		{"name":"compile","conclusion":"failure"},{"name":"test","conclusion":"failure"},
		{"name":"three","conclusion":"failure"},{"name":"four","conclusion":"failure"},
		{"name":"five","conclusion":"failure"},{"name":"six","conclusion":"failure"},
		{"name":"seven","conclusion":"failure"},{"name":"eight","conclusion":"failure"},
		{"name":"nine","conclusion":"failure"}]}}`

	var logs bytes.Buffer

	consumer, reader := newRecorderWithLogger(
		t,
		newLedger(nil),
		slog.New(slog.NewJSONHandler(&logs, nil)),
	)

	err := consumer.Record(t.Context(), workflowJobEvent, []byte(body))
	if err != nil {
		t.Fatal(err)
	}

	checkFailedJobObservations(t, &logs, reader)

	before, counters := logs.String(), collectCounters(t, reader)

	err = consumer.Record(t.Context(), workflowJobEvent, []byte(body))
	if err != nil {
		t.Fatal(err)
	}

	checkJobHistograms(
		t,
		reader,
		pullRequestEvent,
		noRunner,
		failureResult,
		map[string]float64{queueMetric: 15, runMetric: 60},
	)

	if logs.String() != before || !reflect.DeepEqual(collectCounters(t, reader), counters) ||
		len(strings.Split(strings.TrimSpace(before), "\n")) != 2 {
		t.Fatal("redelivery changed logs or counters, or first delivery did not log twice")
	}
}

func checkFailedJobObservations(t *testing.T, logs *bytes.Buffer, reader *sdkmetric.ManualReader) {
	t.Helper()

	assertFailedJobLog(t, logs, map[string]any{
		repositoryKey: repositoryName, workflowKey: "CI", jobKey: buildName,
		"failed_steps": "compile, test, three, four, five, six, seven, eight",
		headBranchKey:  mainBranch, headSHAKey: jobSHA, runIDKey: float64(493),
		startedAtKey: "2026-11-15T12:00:00Z", completedAtKey: "2026-11-15T12:01:00Z",
	})
	checkJobHistograms(
		t,
		reader,
		pullRequestEvent,
		noRunner,
		failureResult,
		map[string]float64{queueMetric: 15, runMetric: 60},
	)
	assertJobLog(t, logs, finishedMessage, map[string]any{
		repositoryKey: repositoryName,
		workflowKey:   "CI",
		jobKey:        buildName,
		conclusionKey: failureResult,
		eventClassKey: pullRequestEvent,
		runnerKey:     noRunner,
		headBranchKey: mainBranch,
		headSHAKey:    jobSHA,
		runIDKey: float64(
			493,
		),
		jobIDKey:        float64(42),
		queueSecondsKey: float64(15),
		runSecondsKey:   float64(60),
	})

	if got := collectCounters(t, reader)[ciFailuresName]; !reflect.DeepEqual(
		got,
		map[string]int64{"ci_job=build,repository=example/widgets,workflow=CI": 1},
	) {
		t.Fatalf("failure counter = %v", got)
	}
}

func TestNonFailureWorkflowJobs(t *testing.T) {
	t.Parallel()

	for _, test := range []struct{ event, action, conclusion string }{
		{workflowJobEvent, completedAction, successResult},
		{workflowJobEvent, completedAction, "cancelled"},
		{workflowJobEvent, "in_progress", failureResult},
		{pushEvent, completedAction, failureResult},
	} {
		t.Run(test.event+"/"+test.action+"/"+test.conclusion, func(t *testing.T) {
			t.Parallel()

			var logs bytes.Buffer

			consumer, reader := newRecorderWithLogger(
				t,
				newLedger(nil),
				slog.New(slog.NewJSONHandler(&logs, nil)),
			)
			body := `{"action":"` + test.action + `","workflow_job":{"id":42,"conclusion":"` + test.conclusion + `"}}`

			err := consumer.Record(t.Context(), test.event, []byte(body))
			if err != nil {
				t.Fatal(err)
			}

			if test.event == workflowJobEvent && test.action == completedAction {
				assertJobLog(t, &logs, finishedMessage, map[string]any{
					repositoryKey: unknownRepository,
					workflowKey:   "",
					jobKey:        "",
					conclusionKey: test.conclusion,
					eventClassKey: pushEvent,
					runnerKey:     noRunner,
					headBranchKey: "",
					headSHAKey:    "",
					runIDKey:      float64(0),
					jobIDKey:      float64(42),
				})
			} else if logs.Len() != 0 {
				t.Fatalf("unexpected job log: %s", &logs)
			}

			if got := collectCounters(t, reader)[ciFailuresName]; got != nil {
				t.Fatalf("unexpected failure series: %v", got)
			}

			checkMetrics(t, reader, map[string]map[string]string{
				eventsName: {
					repositoryKey: unknownRepository,
					eventKey:      test.event,
					actionKey:     test.action,
				},
			})
		})
	}
}

func TestFailedWorkflowJobCleansText(t *testing.T) {
	t.Parallel()

	longText := "\r\n" + strings.Repeat("é", 129)
	logText, labelText := strings.Repeat("é", 128), strings.Repeat("é", 64)

	body, err := json.Marshal(map[string]any{
		actionKey: completedAction,
		workflowJobEvent: map[string]any{
			"id":            42,
			"workflow_name": longText,
			nameKey:         longText,
			conclusionKey:   failureResult,
			headBranchKey:   longText,
			headSHAKey:      longText,
			startedAtKey:    longText,
			completedAtKey:  longText,
			"steps":         []map[string]string{{nameKey: longText, conclusionKey: failureResult}},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	var logs bytes.Buffer

	consumer, reader := newRecorderWithLogger(
		t,
		newLedger(nil),
		slog.New(slog.NewJSONHandler(&logs, nil)),
	)

	err = consumer.Record(t.Context(), workflowJobEvent, body)
	if err != nil {
		t.Fatal(err)
	}

	assertFailedJobLog(t, &logs, map[string]any{
		repositoryKey:  unknownRepository,
		workflowKey:    logText,
		jobKey:         logText,
		"failed_steps": logText,
		headBranchKey:  logText,
		headSHAKey:     logText,
		runIDKey:       float64(0),
		startedAtKey:   logText,
		completedAtKey: logText,
	})
	checkMetrics(t, reader, map[string]map[string]string{
		eventsName: {
			repositoryKey: unknownRepository,
			eventKey:      workflowJobEvent,
			actionKey:     completedAction,
		},
		ciFailuresName: {
			repositoryKey: unknownRepository,
			workflowKey:   labelText,
			ciJobKey:      labelText,
		},
	})
}

func assertFailedJobLog(t *testing.T, logs *bytes.Buffer, want map[string]any) {
	t.Helper()

	assertJobLog(t, logs, "ci job failed", want)
}

func assertJobLog(t *testing.T, logs *bytes.Buffer, message string, want map[string]any) {
	t.Helper()

	for line := range strings.SplitSeq(strings.TrimSpace(logs.String()), "\n") {
		var got map[string]any

		err := json.Unmarshal([]byte(line), &got)
		if err != nil {
			t.Fatal(err)
		}

		if got["msg"] != message {
			continue
		}

		delete(got, "time")

		want["level"], want["msg"] = "INFO", message

		if !reflect.DeepEqual(got, want) {
			t.Fatalf("log = %v, want %v", got, want)
		}

		return
	}

	t.Fatalf("missing %q log: %s", message, logs)
}

type workflowTimingTest struct {
	want                                               map[string]float64
	branch, class, created, started, completed, runner string
}

func TestWorkflowJobTimings(t *testing.T) {
	t.Parallel()

	both := map[string]float64{queueMetric: 15, runMetric: 60}

	for _, test := range []workflowTimingTest{
		{both, "gh-readonly-queue/main/pr-1", "merge_group", queueStart, jobStart, jobEnd, "ubuntu-latest"},
		{both, mainBranch, pushEvent, queueStart, jobStart, jobEnd, noRunner},
		{both, "feature", pullRequestEvent, queueStart, jobStart, jobEnd, noRunner},
		{map[string]float64{runMetric: 60}, "missing created", pullRequestEvent, "", jobStart, jobEnd, noRunner},
		{map[string]float64{queueMetric: 15}, "invalid end", pullRequestEvent, queueStart, jobStart, "invalid", noRunner},
		{map[string]float64{}, "missing started", pullRequestEvent, queueStart, "", jobEnd, noRunner},
		{map[string]float64{runMetric: 60}, "negative queue", pullRequestEvent, "12:00:01", jobStart, jobEnd, noRunner},
		{map[string]float64{queueMetric: 15}, "negative run", pullRequestEvent, queueStart, jobStart, "11:59:59", noRunner},
	} {
		t.Run(test.branch, func(t *testing.T) {
			t.Parallel()

			reader, logs, ledger := recordJobTimingFixture(t, test)

			checkJobHistograms(t, reader, test.class, test.runner, successResult, test.want)
			wantLog := map[string]any{
				repositoryKey: repositoryName,
				workflowKey:   "CI",
				jobKey:        buildName,
				conclusionKey: successResult,
				eventClassKey: test.class,
				runnerKey:     test.runner,
				headBranchKey: test.branch,
				headSHAKey:    jobSHA,
				runIDKey:      float64(493),
				jobIDKey:      float64(42),
			}

			for metricName, seconds := range test.want {
				wantLog[strings.TrimPrefix(metricName, "github.ci.job.")] = seconds
			}

			assertJobLog(t, logs, finishedMessage, wantLog)

			if !ledger.claimed["42\x00ci_job"] ||
				len(strings.Split(strings.TrimSpace(logs.String()), "\n")) != 1 {
				t.Fatal("expected job claim and one finished log")
			}
		})
	}
}

func recordJobTimingFixture(
	t *testing.T,
	test workflowTimingTest,
) (*sdkmetric.ManualReader, *bytes.Buffer, *fakeLedger) {
	t.Helper()

	job := map[string]any{
		"id": 42, "workflow_name": "CI", nameKey: buildName, conclusionKey: successResult,
		headBranchKey: test.branch, headSHAKey: jobSHA, runIDKey: 493,
	}

	for key, value := range map[string]string{
		createdAtKey: test.created, startedAtKey: test.started, completedAtKey: test.completed,
	} {
		if value != "" {
			job[key] = "2026-11-15T" + value + "Z"
		}
	}

	if test.runner != noRunner {
		job["labels"] = []string{test.runner, "ignored"}
	} else if test.branch == mainBranch {
		job["labels"] = []string{}
	}

	body, err := json.Marshal(map[string]any{
		actionKey: completedAction, workflowJobEvent: job,
		repositoryKey: map[string]string{fullNameKey: repositoryName, "default_branch": mainBranch},
	})
	if err != nil {
		t.Fatal(err)
	}

	var logs bytes.Buffer

	ledger := newLedger(nil)
	consumer, reader := newRecorderWithLogger(t, ledger, slog.New(slog.NewJSONHandler(&logs, nil)))

	err = consumer.Record(t.Context(), workflowJobEvent, body)
	if err != nil {
		t.Fatal(err)
	}

	return reader, &logs, ledger
}

func checkJobHistograms(
	t *testing.T,
	reader *sdkmetric.ManualReader,
	class, runner, conclusion string,
	want map[string]float64,
) {
	t.Helper()

	var data metricdata.ResourceMetrics

	err := reader.Collect(t.Context(), &data)
	if err != nil {
		t.Fatal(err)
	}

	got := map[string]float64{}

	for _, scope := range data.ScopeMetrics {
		for _, measurement := range scope.Metrics {
			if measurement.Name != queueMetric && measurement.Name != runMetric {
				continue
			}

			got[measurement.Name] = checkJobHistogramPoint(
				t,
				measurement,
				class,
				runner,
				conclusion,
			)
		}
	}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("histograms = %v, want %v", got, want)
	}
}

func checkJobHistogramPoint(
	t *testing.T,
	measurement metricdata.Metrics,
	class, runner, conclusion string,
) float64 {
	t.Helper()

	histogram, ok := measurement.Data.(metricdata.Histogram[float64])
	if !ok || measurement.Unit != "s" || len(histogram.DataPoints) != 1 {
		t.Fatalf("unexpected histogram: %v", measurement)
	}

	point := histogram.DataPoints[0]
	attrs := map[string]string{}

	for _, attr := range point.Attributes.ToSlice() {
		attrs[string(attr.Key)] = attr.Value.AsString()
	}

	wantAttrs := map[string]string{
		repositoryKey: repositoryName, workflowKey: "CI", ciJobKey: buildName,
		conclusionKey: conclusion, eventClassKey: class, runnerKey: runner,
	}
	bounds := []float64{5, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200}

	if point.Count != 1 || !reflect.DeepEqual(attrs, wantAttrs) ||
		!reflect.DeepEqual(point.Bounds, bounds) {
		t.Fatalf("unexpected histogram point: %v", point)
	}

	return point.Sum
}

func TestWorkflowJobClaimSkips(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		claimErr error
		name, id string
	}{
		{errClaim, "claim error", `"id":42,`}, {nil, "zero id", `"id":0,`}, {nil, "missing id", ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var logs bytes.Buffer

			ledger := newLedger(func(l *fakeLedger) { l.claimErr = test.claimErr })
			consumer, reader := newRecorderWithLogger(
				t,
				ledger,
				slog.New(slog.NewJSONHandler(&logs, nil)),
			)
			body := `{"action":"completed","workflow_job":{` + test.id + `"conclusion":"failure",` +
				`"created_at":"2026-11-15T11:59:45Z","started_at":"2026-11-15T12:00:00Z","completed_at":"2026-11-15T12:01:00Z"}}`

			err := consumer.Record(t.Context(), workflowJobEvent, []byte(body))
			if !errors.Is(err, test.claimErr) || logs.Len() != 0 || len(ledger.claims) != 0 {
				t.Fatalf("error = %v, logs = %s, claims = %v", err, &logs, ledger.claims)
			}

			checkMetrics(t, reader, map[string]map[string]string{})
		})
	}
}
