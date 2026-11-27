// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package recorder_test

import (
	"errors"
	"log/slog"
	"slices"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
)

const (
	nodeID      = "PR_1"
	mergedState = "merged"
)

var (
	errClaim = errors.New("claim failed")
	errFloor = errors.New("floor read failed")
)

func storeCreated() time.Time { return time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC) }

func clockNow() time.Time { return time.Date(2026, 11, 15, 14, 0, 0, 0, time.UTC) }

type fakeLedger struct {
	created  time.Time
	floor    time.Time
	floorErr error
	claimErr error
	onClaim  func()
	claimed  map[string]bool
	claims   []string
}

func newLedger(adjust func(*fakeLedger)) *fakeLedger {
	var ledger fakeLedger

	ledger.created, ledger.claimed = storeCreated(), map[string]bool{}
	ledger.floor = clockNow().Add(-3 * time.Hour)

	if adjust != nil {
		adjust(&ledger)
	}

	return &ledger
}

func (f *fakeLedger) Created() time.Time { return f.created }

func (f *fakeLedger) RepairFloor() (time.Time, error) { return f.floor, f.floorErr }

func (f *fakeLedger) ClaimMilestone(key string) (bool, error) {
	if f.onClaim != nil {
		f.onClaim()
	}

	if f.claimErr != nil {
		return false, f.claimErr
	}

	f.claims = append(f.claims, key)

	if f.claimed[key] {
		return false, nil
	}

	f.claimed[key] = true

	return true, nil
}

// closedPR is merged an hour before the fake clock, after an hour open.
func closedPR(adjust func(*recorder.ClosedPR)) recorder.ClosedPR {
	end := clockNow().Add(-time.Hour)

	var closed recorder.ClosedPR

	closed.Repository, closed.NodeID, closed.CreatedAt = repositoryName, nodeID, end.Add(-time.Hour)
	closed.MergedAt, closed.EndedAt, closed.EndedState, closed.Merged = end, end, mergedState, true

	if adjust != nil {
		adjust(&closed)
	}

	return closed
}

// observations returns the merge count and the cycle time states recorded.
func observations(t *testing.T, reader *sdkmetric.ManualReader) (int64, []string) {
	t.Helper()

	var (
		data   metricdata.ResourceMetrics
		merges int64
		states []string
	)

	err := reader.Collect(t.Context(), &data)
	if err != nil {
		t.Fatal(err)
	}

	for _, scope := range data.ScopeMetrics {
		for _, measurement := range scope.Metrics {
			if sum, ok := measurement.Data.(metricdata.Sum[int64]); ok &&
				measurement.Name == "github.pr.merges" {
				for _, point := range sum.DataPoints {
					merges += point.Value
				}
			}

			if hist, ok := measurement.Data.(metricdata.Histogram[float64]); ok {
				states = append(states, cycleStates(hist)...)
			}
		}
	}

	return merges, states
}

func cycleStates(hist metricdata.Histogram[float64]) []string {
	var states []string

	for _, point := range hist.DataPoints {
		state, _ := point.Attributes.Value("state")
		for range point.Count {
			states = append(states, state.AsString())
		}
	}

	return states
}

func TestRecordClosed(t *testing.T) {
	t.Parallel()

	recorded := recorder.Outcome{Merged: recorder.ResultRecorded, Ended: recorder.ResultRecorded}
	tooOld := recorder.Outcome{Merged: recorder.ResultTooOld, Ended: recorder.ResultTooOld}

	for _, test := range []struct {
		name       string
		adjust     func(*recorder.ClosedPR)
		prepare    func(*fakeLedger)
		want       recorder.Outcome
		wantMerges int64
		wantCycles int
	}{
		{"claim then recorded", nil, nil, recorded, 1, 1},
		{"too old by age", func(closed *recorder.ClosedPR) {
			old := clockNow().Add(-25 * time.Hour)
			closed.CreatedAt, closed.MergedAt, closed.EndedAt = old.Add(-time.Hour), old, old
			closed.Repair = true
		}, func(l *fakeLedger) { l.floorErr = errFloor }, tooOld, 0, 0},
		{"too old by creation time", func(closed *recorder.ClosedPR) { closed.Repair = true }, func(l *fakeLedger) {
			l.created = clockNow().Add(-30 * time.Minute)
			l.floorErr = errFloor
		}, tooOld, 0, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			ledger := newLedger(test.prepare)
			consumer, reader := newRecorderWith(t, ledger)

			got, err := consumer.RecordClosed(t.Context(), closedPR(test.adjust))
			if err != nil {
				t.Fatal(err)
			}

			// Every case claims both milestones, with or without an observation.
			merges, cycles := observations(t, reader)
			if got != test.want || len(ledger.claims) != 2 || merges != test.wantMerges ||
				len(cycles) != test.wantCycles {
				t.Fatalf(
					"outcome %v, claims %d, merges %d, cycles %d; want %v, 2, %d, %d",
					got,
					len(ledger.claims),
					merges,
					len(cycles),
					test.want,
					test.wantMerges,
					test.wantCycles,
				)
			}
		})
	}
}

func TestRecordRepairFloor(t *testing.T) {
	t.Parallel()

	preFloor := recorder.Outcome{Merged: recorder.ResultPreFloor, Ended: recorder.ResultPreFloor}
	recorded := recorder.Outcome{Merged: recorder.ResultRecorded, Ended: recorder.ResultRecorded}
	preFloorEnd := recorder.Outcome{Merged: recorder.ResultRecorded, Ended: recorder.ResultPreFloor}

	for _, test := range []struct {
		name                   string
		want                   recorder.Outcome
		mergeOffset, endOffset time.Duration
		wantMerges             int64
		wantCycles             int
	}{
		{"at the floor", preFloor, 0, 0, 0, 0},
		{"before the floor", preFloor, -time.Minute, -time.Minute, 0, 0},
		{"after the floor", recorded, time.Minute, time.Minute, 1, 1},
		{"first close before the floor", preFloorEnd, time.Minute, -time.Minute, 1, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			ledger := newLedger(nil)
			consumer, reader := newRecorderWith(t, ledger)

			closed := closedPR(func(pr *recorder.ClosedPR) {
				pr.Repair = true
				pr.CreatedAt = ledger.floor.Add(-time.Hour)
				pr.MergedAt = ledger.floor.Add(test.mergeOffset)
				pr.EndedAt = ledger.floor.Add(test.endOffset)
			})

			got, err := consumer.RecordClosed(t.Context(), closed)
			if err != nil || got != test.want {
				t.Fatalf("RecordClosed = %v, %v; want %v, nil", got, err, test.want)
			}

			merges, cycles := observations(t, reader)
			if merges != test.wantMerges || len(cycles) != test.wantCycles {
				t.Fatalf("merges %d, cycle states %v; want %d merges and %d cycle times",
					merges, cycles, test.wantMerges, test.wantCycles)
			}

			if !ledger.claimed[nodeID+"\x00ended"] ||
				ledger.claimed[nodeID+"\x00merged"] != (test.wantMerges == 1) {
				t.Fatalf("claims %q, want ended and a claim for each recorded merge", ledger.claims)
			}
		})
	}
}

func TestRepairFloorReadError(t *testing.T) {
	t.Parallel()

	ledger := newLedger(func(l *fakeLedger) { l.floorErr = errFloor })
	consumer, reader := newRecorderWith(t, ledger)

	_, err := consumer.RecordClosed(
		t.Context(),
		closedPR(func(pr *recorder.ClosedPR) { pr.Repair = true }),
	)
	if !errors.Is(err, errFloor) {
		t.Fatalf("RecordClosed error = %v, want %v", err, errFloor)
	}

	body := []byte(
		`{"action":"reopened","pull_request":{"node_id":"PR_1","created_at":"2026-11-15T12:00:00Z"}}`,
	)

	err = consumer.Record(t.Context(), pullRequestEvent, body)
	if !errors.Is(err, errFloor) {
		t.Fatalf("Record error = %v, want %v", err, errFloor)
	}

	if counters := collectCounters(
		t,
		reader,
	); len(ledger.claims) != 0 ||
		len(counters["github.events"]) != 0 {
		t.Fatalf(
			"floor read errors produced claims %q or counted events %v",
			ledger.claims,
			counters["github.events"],
		)
	}
}

func TestRecordClosedClaimError(t *testing.T) {
	t.Parallel()

	consumer, reader := newRecorderWith(t, newLedger(func(l *fakeLedger) { l.claimErr = errClaim }))

	_, err := consumer.RecordClosed(t.Context(), closedPR(nil))
	if !errors.Is(err, errClaim) {
		t.Fatalf("error = %v, want %v", err, errClaim)
	}

	merges, cycles := observations(t, reader)
	if merges != 0 || len(cycles) != 0 {
		t.Fatalf("merges %d, cycles %d after a claim error; want none", merges, len(cycles))
	}
}

func TestRecordClosedFirstEndKeepsItsState(t *testing.T) {
	t.Parallel()

	consumer, reader := newRecorderWith(t, newLedger(nil))
	closedAt := clockNow().Add(-2 * time.Hour)
	mergedAt := clockNow().Add(-time.Hour)

	first, err := consumer.RecordClosed(t.Context(), closedPR(func(pr *recorder.ClosedPR) {
		pr.Merged, pr.MergedAt, pr.EndedAt, pr.EndedState = false, time.Time{}, closedAt, closedState
	}))
	if err != nil || first.Ended != recorder.ResultRecorded || first.Merged != "" {
		t.Fatalf("first close = %v, %v", first, err)
	}

	// The merge delivery after a reopen carries the merge time and its own end.
	second, err := consumer.RecordClosed(
		t.Context(),
		closedPR(func(pr *recorder.ClosedPR) { pr.EndedAt = mergedAt }),
	)
	if err != nil || second.Merged != recorder.ResultRecorded ||
		second.Ended != recorder.ResultAlreadyCounted {
		t.Fatalf("merge after reopen = %v, %v", second, err)
	}

	merges, cycles := observations(t, reader)
	if merges != 1 || len(cycles) != 1 || cycles[0] != closedState {
		t.Fatalf("merges %d, cycle states %v; want 1 and [closed]", merges, cycles)
	}
}

func TestRecordReopened(t *testing.T) {
	t.Parallel()

	const (
		fresh = "2026-11-15T12:00:00Z"
		first = `,"closed_at":"2026-11-15T12:30:00Z"`
		merge = `,"merged":true,"merged_at":"2026-11-15T13:00:00Z","closed_at":"2026-11-15T13:00:00Z"`
	)

	reopen := [2]string{"reopened", ""}
	firstClose := [2]string{closedState, first}
	mergeClose := [2]string{closedState, merge}
	firstEnd := []string{closedState}

	for _, test := range []struct {
		name       string
		created    string
		steps      [][2]string // action and the rest of the pull_request body
		wantCycles []string
	}{
		// An earlier version may have counted this first close, so the reopen blocks a second count.
		{"created before the floor", "2026-11-15T10:30:00Z", [][2]string{reopen, mergeClose}, nil},
		{"created at the floor", "2026-11-15T11:00:00Z", [][2]string{reopen, mergeClose}, nil},
		// This version counts the first close in any delivery order.
		{"reopen before the first close", fresh, [][2]string{reopen, firstClose, mergeClose}, firstEnd},
		{"first close before the reopen", fresh, [][2]string{firstClose, reopen, mergeClose}, firstEnd},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			consumer, reader := newRecorderWith(t, newLedger(nil))

			for _, step := range test.steps {
				body := `{"repository":{"full_name":"example/widgets"},"action":"` + step[0] +
					`","pull_request":{"node_id":"PR_1","created_at":"` + test.created + `"` + step[1] + `}}`

				err := consumer.Record(t.Context(), pullRequestEvent, []byte(body))
				if err != nil {
					t.Fatal(err)
				}
			}

			merges, cycles := observations(t, reader)
			if merges != 1 || !slices.Equal(cycles, test.wantCycles) {
				t.Fatalf(
					"merges %d, cycle states %v; want 1 merge and cycle states %v",
					merges,
					cycles,
					test.wantCycles,
				)
			}
		})
	}
}

func TestRecordReopenedWithRepair(t *testing.T) {
	t.Parallel()

	for _, test := range []struct {
		name                       string
		wantCycles                 []string
		createdOffset, endedOffset time.Duration
		repairFirst                bool
	}{
		{"before floor, reopen first", nil, -time.Hour, -time.Minute, false},
		{"before floor, repair first", nil, -time.Hour, -time.Minute, true},
		{"after floor, reopen first", []string{closedState}, time.Minute, time.Hour, false},
		{"after floor, repair first", []string{closedState}, time.Minute, time.Hour, true},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			ledger := newLedger(nil)
			consumer, reader := newRecorderWith(t, ledger)
			repair := closedPR(func(pr *recorder.ClosedPR) {
				pr.Repair = true
				pr.CreatedAt = ledger.floor.Add(test.createdOffset)
				pr.EndedAt = ledger.floor.Add(test.endedOffset)
				pr.EndedState = closedState
			})

			const reopened = "reopened"

			created := repair.CreatedAt.Format(time.RFC3339)
			if !test.repairFirst {
				deliverTestPullRequest(t, consumer, created, reopened, "")
			}

			outcome, err := consumer.RecordClosed(t.Context(), repair)
			if err != nil || outcome.Merged != recorder.ResultRecorded {
				t.Fatalf("repair = %v, %v, want a recorded merge", outcome, err)
			}

			if test.repairFirst {
				deliverTestPullRequest(t, consumer, created, reopened, "")
			}

			mergeTime := repair.MergedAt.Format(time.RFC3339)
			merge := `,"merged":true,"merged_at":"` + mergeTime + `","closed_at":"` + mergeTime + `"`
			deliverTestPullRequest(t, consumer, created, closedState, merge)

			merges, cycles := observations(t, reader)
			if merges != 1 || !slices.Equal(cycles, test.wantCycles) {
				t.Fatalf(
					"merges = %d, cycle states = %v, want 1 merge and %v",
					merges,
					cycles,
					test.wantCycles,
				)
			}
		})
	}
}

func deliverTestPullRequest(
	t *testing.T,
	consumer *recorder.Recorder,
	created, action, extra string,
) {
	t.Helper()

	body := `{"repository":{"full_name":"` + repositoryName + `"},"action":"` + action +
		`","pull_request":{"node_id":"` + nodeID + `","created_at":"` + created + `"` + extra + `}}`

	err := consumer.Record(t.Context(), pullRequestEvent, []byte(body))
	if err != nil {
		t.Fatal(err)
	}
}

func TestRecordClaimsBeforeAnyAdd(t *testing.T) {
	t.Parallel()

	ledger := newLedger(func(l *fakeLedger) { l.floor = clockNow().Add(-30 * time.Minute) })
	consumer, reader := newRecorderWith(t, ledger)

	ledger.onClaim = checkCountersBeforeClaim(t, reader)

	body := `{"repository":{"full_name":"example/widgets"},"action":"closed","pull_request":{` +
		`"node_id":"PR_1","merged":true,"created_at":"2026-11-15T12:00:00Z",` +
		`"merged_at":"2026-11-15T13:00:00Z","closed_at":"2026-11-15T13:00:00Z"}}`

	err := consumer.Record(t.Context(), pullRequestEvent, []byte(body))
	if err != nil || len(ledger.claims) != 2 {
		t.Fatalf("Record error %v, claims %v; want two claims", err, ledger.claims)
	}

	merges, cycles := observations(t, reader)
	if merges != 1 || len(cycles) != 1 || cycles[0] != mergedState {
		t.Fatalf(
			"webhook before the floor recorded %d merges and cycle states %v; want 1 and [merged]",
			merges,
			cycles,
		)
	}

	ledger.claimErr = errClaim

	err = consumer.Record(t.Context(), pullRequestEvent, []byte(body))
	if !errors.Is(err, errClaim) {
		t.Fatalf("Record error = %v, want %v", err, errClaim)
	}

	if counters := collectCounters(
		t,
		reader,
	); counters["github.events"]["action=closed,event=pull_request,"+
		"repository=example/widgets"] != 1 {
		t.Fatalf("a failed claim counted the delivery: %v", counters["github.events"])
	}
}

func checkCountersBeforeClaim(t *testing.T, reader *sdkmetric.ManualReader) func() {
	t.Helper()

	first := true

	return func() {
		counters := collectCounters(t, reader)
		if _, seen := counters["github.events"]; seen && first {
			t.Error("github.events counted before the claim")
		}

		if _, seen := counters["github.pr.merges"]; seen && first {
			t.Error("github.pr.merges counted before the claim")
		}

		first = false
	}
}

func TestNewWithoutLedger(t *testing.T) {
	t.Parallel()

	_, err := recorder.New(
		sdkmetric.NewMeterProvider().Meter("test"),
		nil,
		slog.New(slog.DiscardHandler),
	)
	if !errors.Is(err, recorder.ErrNoLedger) {
		t.Fatalf("New error = %v, want %v", err, recorder.ErrNoLedger)
	}
}
