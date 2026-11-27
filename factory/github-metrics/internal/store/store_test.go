// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package store_test

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
	boltErrors "go.etcd.io/bbolt/errors"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
)

func open(t *testing.T, path string) *store.Bolt {
	t.Helper()

	repo, err := store.Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}

	t.Cleanup(func() { closeStore(t, repo) })

	return repo
}

func enqueue(t *testing.T, repo store.Store, delivery, body string) store.Outcome {
	t.Helper()

	outcome, err := repo.Enqueue(delivery, "pull_request", []byte(body))
	if err != nil {
		t.Fatalf("Enqueue(%q): %v", delivery, err)
	}

	return outcome
}

func pending(t *testing.T, repo store.Store) []store.Entry {
	t.Helper()

	entries, err := repo.Pending(100)
	if err != nil {
		t.Fatalf("Pending: %v", err)
	}

	return entries
}

func TestEnqueueThenPending(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	repo := open(t, path)

	if got := enqueue(t, repo, "d1", `{"a":1}`); got != store.Enqueued {
		t.Fatalf("outcome = %v, want Enqueued", got)
	}

	err := repo.Close()
	if err != nil {
		t.Fatal(err)
	}

	repo = open(t, path)
	entries := pending(t, repo)

	if len(entries) != 1 || entries[0].Delivery != "d1" || entries[0].Event != "pull_request" ||
		string(entries[0].Body) != `{"a":1}` {
		t.Fatalf("pending = %+v", entries)
	}
}

func TestDuplicateWritesNothing(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	enqueue(t, repo, "d1", "body")

	// The same body under a new ID is a duplicate, since the signature
	// covers only the body.
	if got := enqueue(t, repo, "d2", "body"); got != store.Duplicate {
		t.Fatalf("same body, new ID: outcome = %v, want Duplicate", got)
	}

	if got := enqueue(t, repo, "d1", "body"); got != store.Duplicate {
		t.Fatalf("same body, same ID: outcome = %v, want Duplicate", got)
	}

	if queued := pending(t, repo); len(queued) != 1 {
		t.Fatalf("queue holds %d entries, want 1", len(queued))
	}

	// d2 was never recorded, so it can still arrive with its own body.
	if got := enqueue(t, repo, "d2", "other"); got != store.Enqueued {
		t.Fatalf("d2 with a new body: outcome = %v, want Enqueued", got)
	}
}

func TestReusedIDWithDifferentBody(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	enqueue(t, repo, "d1", "first")

	outcome, err := repo.Enqueue("d1", "pull_request", []byte("second"))

	if outcome != 0 {
		t.Fatalf("outcome = %v, want 0", outcome)
	}

	if !errors.Is(err, store.ErrDeliveryReused) {
		t.Fatalf("err = %v, want ErrDeliveryReused", err)
	}

	entries := pending(t, repo)
	if len(entries) != 1 || string(entries[0].Body) != "first" {
		t.Fatalf("pending = %+v, want only the first body", entries)
	}

	// The rejected body has no dedupe row, so it can arrive under a new ID.
	if got := enqueue(t, repo, "d2", "second"); got != store.Enqueued {
		t.Fatalf("rejected body under a new ID: outcome = %v, want Enqueued", got)
	}
}

func TestReplayAfterReopenAndRemove(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	repo := open(t, path)
	enqueue(t, repo, "d1", "body")

	entries := pending(t, repo)

	err := repo.Remove(entries[0].Seq)
	if err != nil {
		t.Fatalf("Remove: %v", err)
	}

	err = repo.Close()
	if err != nil {
		t.Fatalf("Close: %v", err)
	}

	// GitHub redelivers a body the worker already took off the queue. The
	// dedupe table remembers it across a restart.
	repo = open(t, path)
	if got := enqueue(t, repo, "d1-redelivery", "body"); got != store.Duplicate {
		t.Fatalf("replay: outcome = %v, want Duplicate", got)
	}

	if requeued := pending(t, repo); len(requeued) != 0 {
		t.Fatalf("replay re-queued %+v", requeued)
	}
}

func TestOutOfOrderPair(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))

	// A review arrives before the PR event that came first on GitHub's side.
	// The queue keeps arrival order, and the worker reconciles from REST.
	enqueue(t, repo, "review-delivery", "review")
	enqueue(t, repo, "opened-delivery", "opened")

	entries := pending(t, repo)
	if len(entries) != 2 || entries[0].Delivery != "review-delivery" ||
		entries[1].Delivery != "opened-delivery" {
		t.Fatalf("pending = %+v, want arrival order", entries)
	}

	err := repo.Remove(entries[0].Seq)
	if err != nil {
		t.Fatalf("Remove: %v", err)
	}

	err = repo.Remove(entries[0].Seq)
	if err != nil {
		t.Fatalf("second Remove of the same entry: %v", err)
	}

	left := pending(t, repo)
	if len(left) != 1 || left[0].Delivery != "opened-delivery" {
		t.Fatalf("after Remove, pending = %+v", left)
	}

	none, err := repo.Pending(0)
	if err != nil || len(none) != 0 {
		t.Fatalf("Pending(0) = %+v, %v", none, err)
	}
}

func TestFailedWriteLeavesStoreUnchanged(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	enqueue(t, repo, "d1", "kept")

	// `bbolt` rejects a key over 32768 bytes after the body hash row is
	// already written in the same transaction, so this fails mid-write.
	huge := strings.Repeat("x", 40000)

	_, err := repo.Enqueue(huge, "pull_request", []byte("lost"))
	if !errors.Is(err, boltErrors.ErrKeyTooLarge) {
		t.Fatalf("Enqueue error = %v, want ErrKeyTooLarge", err)
	}

	entries := pending(t, repo)
	if len(entries) != 1 || string(entries[0].Body) != "kept" {
		t.Fatalf("queue changed: %+v", entries)
	}

	// The rollback removed the body row too, so the body is new again.
	if got := enqueue(t, repo, "d2", "lost"); got != store.Enqueued {
		t.Fatalf("body of the failed write: outcome = %v, want Enqueued", got)
	}
}

func TestEmptyDeliveryID(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))

	outcome, err := repo.Enqueue("", "pull_request", []byte("body"))

	if outcome != 0 {
		t.Fatalf("outcome = %v, want 0", outcome)
	}

	if !errors.Is(err, store.ErrNoDelivery) {
		t.Fatalf("err = %v, want ErrNoDelivery", err)
	}
}

func TestCreatedIsRecordedOnce(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	first := time.Date(2026, time.November, 15, 12, 0, 0, 123456789, time.UTC)
	later := first.Add(time.Hour)

	repo, err := store.OpenAt(path, func() time.Time { return first })
	if err != nil {
		t.Fatalf("OpenAt: %v", err)
	}

	if !repo.Created().Equal(first) {
		t.Fatalf("Created = %v, want %v", repo.Created(), first)
	}

	err = repo.Close()
	if err != nil {
		t.Fatalf("Close: %v", err)
	}

	// The second open runs on a later clock and must keep the first time.
	again, err := store.OpenAt(path, func() time.Time { return later })
	if err != nil {
		t.Fatalf("OpenAt again: %v", err)
	}

	if !again.Created().Equal(first) {
		t.Fatalf("Created after reopen = %v, want %v", again.Created(), first)
	}

	err = again.Close()
	if err != nil {
		t.Fatalf("Close again: %v", err)
	}

	raw := storedCreated(t, path)

	parsed, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		t.Fatalf("parse stored creation time %q: %v", raw, err)
	}

	if !parsed.Equal(repo.Created()) {
		t.Fatalf("stored creation time = %v, want %v", parsed, repo.Created())
	}
}

// storedCreated reads the creation-time text straight from the file.
func storedCreated(t *testing.T, path string) string {
	t.Helper()

	opts := *bolt.DefaultOptions
	opts.ReadOnly = true
	opts.Timeout = time.Second

	file, err := bolt.Open(path, 0o600, &opts)
	if err != nil {
		t.Fatalf("open the file directly: %v", err)
	}

	defer closeStore(t, file)

	var text string

	err = file.View(func(txn *bolt.Tx) error {
		text = string(txn.Bucket([]byte("meta")).Get([]byte("created")))

		return nil
	})
	if err != nil {
		t.Fatalf("read the creation time: %v", err)
	}

	return text
}

// TestOpenWhileLockedTimesOut allows ten seconds for the one-second lock wait.
func TestOpenWhileLockedTimesOut(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	held := open(t, path)

	result := make(chan error, 1)

	go func() {
		other, err := store.Open(path)
		if other != nil {
			err = errors.Join(err, other.Close())
		}

		result <- err
	}()

	select {
	case err := <-result:
		if !errors.Is(err, boltErrors.ErrTimeout) {
			t.Fatalf("second Open err = %v, want bbolt's ErrTimeout", err)
		}
	case <-time.After(10 * time.Second):
		// Release the waiter before the temporary directory is removed.
		closeErr := held.Close()
		waitErr := <-result

		t.Fatalf("second Open exceeded the ten-second bound: %v", errors.Join(waitErr, closeErr))
	}

	err := held.Close()
	if err != nil {
		t.Fatalf("Close: %v", err)
	}

	// Once the holder closes, the file opens again.
	open(t, path)
}

func TestNewFileMode(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	open(t, path)

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("Stat: %v", err)
	}

	if got := info.Mode().Perm(); got != 0o600 {
		t.Fatalf("mode = %o, want 600", got)
	}
}

func TestPendingLimits(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	enqueue(t, repo, "d1", "first")
	enqueue(t, repo, "d2", "second")

	for _, limit := range []int{-1, 0, 1, 2, 3} {
		entries, err := repo.Pending(limit)
		want := max(0, min(limit, 2))

		if err != nil || len(entries) != want {
			t.Fatalf("Pending(%d) = %+v, %v, want %d entries", limit, entries, err, want)
		}
	}
}

func TestClosedStore(t *testing.T) {
	t.Parallel()

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))

	err := repo.Close()
	if err != nil {
		t.Fatal(err)
	}

	outcome, err := repo.Enqueue("d1", "pull_request", []byte("body"))
	if outcome != 0 || !errors.Is(err, boltErrors.ErrDatabaseNotOpen) {
		t.Fatalf("Enqueue = %v, %v", outcome, err)
	}

	entries, err := repo.Pending(1)
	if entries != nil || !errors.Is(err, boltErrors.ErrDatabaseNotOpen) {
		t.Fatalf("Pending = %+v, %v", entries, err)
	}

	for _, limit := range []int{-1, 0} {
		entries, err = repo.Pending(limit)
		if entries != nil || err != nil {
			t.Fatalf("Pending(%d) = %+v, %v", limit, entries, err)
		}
	}

	err = repo.Remove(1)
	if !errors.Is(err, boltErrors.ErrDatabaseNotOpen) {
		t.Fatalf("Remove = %v", err)
	}
}

func TestOutcomeValues(t *testing.T) {
	t.Parallel()

	if store.Enqueued != 1 || store.Duplicate != 2 {
		t.Fatalf("outcomes = %d, %d, want 1, 2", store.Enqueued, store.Duplicate)
	}
}

func TestClaimMilestone(t *testing.T) {
	t.Parallel()

	const key = "PR_node\x00merged"

	path := filepath.Join(t.TempDir(), "store.db")
	claimedAt := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	now := claimedAt
	repo := openAt(t, path, func() time.Time { return now })

	checkClaim(t, repo, key, true)

	now = now.Add(time.Hour)

	checkClaim(t, repo, key, false)
	checkClaim(t, repo, "PR_node\x00ended", true)
	closeStore(t, repo)

	repo = open(t, path)
	checkClaim(t, repo, key, false)

	var stamp string

	err := repo.Database().View(func(txn *bolt.Tx) error {
		stamp = string(txn.Bucket([]byte("milestones")).Get([]byte(key)))

		return nil
	})
	if want := claimedAt.Format(time.RFC3339Nano); err != nil || stamp != want {
		t.Fatalf("milestone timestamp = %q, %v, want %q, nil", stamp, err, want)
	}
}

func checkClaim(t *testing.T, repo *store.Bolt, key string, want bool) {
	t.Helper()

	claimed, err := repo.ClaimMilestone(key)
	if err != nil || claimed != want {
		t.Fatalf("ClaimMilestone(%q) = %t, %v, want %t, nil", key, claimed, err, want)
	}
}

func TestConcurrentMilestoneClaims(t *testing.T) {
	t.Parallel()

	const callers = 16

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	start := make(chan struct{})
	claims := make(chan bool, callers)

	for range callers {
		go func() {
			<-start

			claimed, err := repo.ClaimMilestone("PR_node\x00merged")
			if err != nil {
				t.Errorf("ClaimMilestone: %v", err)
			}

			claims <- claimed
		}()
	}

	close(start)

	var count int

	for range callers {
		if <-claims {
			count++
		}
	}

	if count != 1 {
		t.Fatalf("successful claims = %d, want 1", count)
	}
}

func TestRepairFloorIsStampedOnFirstCall(t *testing.T) {
	t.Setenv("RECONCILE_FLOOR_RESET", "0")

	path := filepath.Join(t.TempDir(), "store.db")
	now := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	clock := func() time.Time { return now }
	repo := openAt(t, path, clock)

	now = now.Add(time.Minute)
	firstFloor := now
	checkRepairFloor(t, repo, firstFloor)

	now = now.Add(time.Hour)

	checkRepairFloor(t, repo, firstFloor)
	closeStore(t, repo)

	repo = openAt(t, path, clock)
	checkRepairFloor(t, repo, firstFloor)
}

func TestRepairFloorResetOnce(t *testing.T) {
	t.Setenv("RECONCILE_FLOOR_RESET", "0")

	path := filepath.Join(t.TempDir(), "store.db")
	now := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	clock := func() time.Time { return now }
	repo := openAt(t, path, clock)
	checkRepairFloor(t, repo, now)
	closeStore(t, repo)

	t.Setenv("RECONCILE_FLOOR_RESET", "1")

	now = now.Add(time.Hour)
	started := now
	repo = openAt(t, path, clock)

	now = now.Add(time.Minute)

	checkRepairFloor(t, repo, started)

	now = now.Add(time.Hour)

	checkRepairFloor(t, repo, started)
	closeStore(t, repo)

	t.Setenv("RECONCILE_FLOOR_RESET", "0")
	repo = openAt(t, path, clock)
	checkRepairFloor(t, repo, started)
}

func openAt(t *testing.T, path string, now func() time.Time) *store.Bolt {
	t.Helper()

	repo, err := store.OpenAt(path, now)
	if err != nil {
		t.Fatalf("OpenAt: %v", err)
	}

	t.Cleanup(func() { closeStore(t, repo) })

	return repo
}

func closeStore(t *testing.T, repo io.Closer) {
	t.Helper()

	err := repo.Close()
	if err != nil {
		t.Fatalf("Close: %v", err)
	}
}

func checkRepairFloor(t *testing.T, repo *store.Bolt, want time.Time) {
	t.Helper()

	floor, err := repo.RepairFloor()
	if err != nil || !floor.Equal(want) {
		t.Fatalf("RepairFloor = %v, %v, want %v, nil", floor, err, want)
	}
}

func TestWatermarkRoundTrip(t *testing.T) {
	t.Parallel()

	const name = "owner/repo"

	path := filepath.Join(t.TempDir(), "store.db")
	repo := open(t, path)
	checkWatermark(t, repo, name, time.Time{})

	first := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	later := first.Add(time.Hour)

	for _, updated := range []time.Time{first, later} {
		err := repo.SetWatermark(name, updated)
		if err != nil {
			t.Fatalf("SetWatermark: %v", err)
		}

		checkWatermark(t, repo, name, updated)
	}

	closeStore(t, repo)

	repo = open(t, path)
	checkWatermark(t, repo, name, later)
	checkWatermark(t, repo, "other/repo", time.Time{})
}

func checkWatermark(t *testing.T, repo *store.Bolt, name string, want time.Time) {
	t.Helper()

	watermark, err := repo.Watermark(name)
	if err != nil || !watermark.Equal(want) {
		t.Fatalf("Watermark(%q) = %v, %v, want %v, nil", name, watermark, err, want)
	}
}
