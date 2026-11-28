// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package store_test

import (
	"path/filepath"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
	boltErrors "go.etcd.io/bbolt/errors"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
)

const reviewPRsName = "review_prs"

func TestReviewPRRoundTrip(t *testing.T) {
	t.Parallel()

	const key = "PR_node"

	var missing store.ReviewPR

	path := filepath.Join(t.TempDir(), "store.db")
	repo := open(t, path)
	checkReviewPR(t, repo, key, missing, false)

	ready := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.FixedZone("offset", 3600))
	record := store.ReviewPR{ReadyAt: ready, UpdatedAt: ready.Add(time.Minute)}
	putReviewPR(t, repo, key, record)
	closeStore(t, repo)

	repo = open(t, path)
	checkReviewPR(t, repo, key, record, true)

	for range 2 {
		err := repo.DeleteReviewPR(key)
		if err != nil {
			t.Fatalf("DeleteReviewPR: %v", err)
		}
	}

	checkReviewPR(t, repo, key, missing, false)
}

func TestPutReviewPRTimestamps(t *testing.T) {
	t.Parallel()

	const key = "PR_node"

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	first := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	later := first.Add(time.Hour)
	latest := later.Add(time.Hour)

	putReviewPR(t, repo, key, store.ReviewPR{ReadyAt: later, UpdatedAt: later})
	putReviewPR(t, repo, key, store.ReviewPR{ReadyAt: first, UpdatedAt: latest})
	checkReviewPR(t, repo, key, store.ReviewPR{ReadyAt: later, UpdatedAt: latest}, true)

	putReviewPR(t, repo, key, store.ReviewPR{ReadyAt: latest, UpdatedAt: first})
	checkReviewPR(t, repo, key, store.ReviewPR{ReadyAt: latest, UpdatedAt: latest}, true)

	cleared := store.ReviewPR{ReadyAt: time.Time{}, UpdatedAt: latest}
	putReviewPR(t, repo, key, store.ReviewPR{ReadyAt: time.Time{}, UpdatedAt: first})
	checkReviewPR(t, repo, key, cleared, true)

	putReviewPR(t, repo, key, store.ReviewPR{ReadyAt: later, UpdatedAt: latest})
	checkReviewPR(t, repo, key, store.ReviewPR{ReadyAt: later, UpdatedAt: latest}, true)
}

func TestPruneReviewPRs(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	cutoff := now.AddDate(0, 0, -30)
	repo := openAt(t, filepath.Join(t.TempDir(), "store.db"), func() time.Time { return now })

	for _, key := range []string{"old-1", "old-2"} {
		putReviewPR(t, repo, key, store.ReviewPR{ReadyAt: time.Time{}, UpdatedAt: cutoff.Add(-time.Nanosecond)})
	}

	boundary := store.ReviewPR{ReadyAt: time.Time{}, UpdatedAt: cutoff}
	recent := store.ReviewPR{ReadyAt: cutoff.Add(-time.Hour), UpdatedAt: now}

	putReviewPR(t, repo, "boundary", boundary)
	putReviewPR(t, repo, "recent", recent)
	checkPrunedReviewPRs(t, repo, 2)

	var missing store.ReviewPR

	checkReviewPR(t, repo, "old-1", missing, false)
	checkReviewPR(t, repo, "old-2", missing, false)
	checkReviewPR(t, repo, "boundary", boundary, true)
	checkReviewPR(t, repo, "recent", recent, true)
	checkPrunedReviewPRs(t, repo, 0)
}

func TestReviewPRClosedStore(t *testing.T) {
	t.Parallel()

	const key = "PR_node"

	var empty store.ReviewPR

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	closeStore(t, repo)

	record, found, err := repo.ReviewPR(key)
	if record != empty || found {
		t.Fatalf("ReviewPR = %+v, %t, want an empty result", record, found)
	}

	requireError(t, err, boltErrors.ErrDatabaseNotOpen, "read review PR:")
	requireError(t, repo.PutReviewPR(key, empty), boltErrors.ErrDatabaseNotOpen, "put review PR:")
	requireError(t, repo.DeleteReviewPR(key), boltErrors.ErrDatabaseNotOpen, "delete review PR:")

	removed, err := repo.PruneReviewPRs()
	if removed != 0 {
		t.Fatalf("PruneReviewPRs removed %d, want 0", removed)
	}

	requireError(t, err, boltErrors.ErrDatabaseNotOpen, "prune review PRs:")
}

func TestReviewPRConflictingBucket(t *testing.T) {
	t.Parallel()

	const key = "PR_node"

	var empty store.ReviewPR

	repo := open(t, filepath.Join(t.TempDir(), "store.db"))
	conflict(t, repo.Database(), reviewPRsName, []byte(key))

	requireError(t, repo.PutReviewPR(key, empty), boltErrors.ErrIncompatibleValue, "put review PR:")
	requireError(t, repo.DeleteReviewPR(key), boltErrors.ErrIncompatibleValue, "delete review PR:")
}

func TestReviewPRCorruptRecord(t *testing.T) {
	t.Parallel()

	const key = "z-corrupt"

	now := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.UTC)
	repo := openAt(t, filepath.Join(t.TempDir(), "store.db"), func() time.Time { return now })
	old := store.ReviewPR{ReadyAt: time.Time{}, UpdatedAt: now.AddDate(0, -2, 0)}
	putReviewPR(t, repo, "a-old", old)
	update(t, repo.Database(), func(txn *bolt.Tx) error {
		return setupError(txn.Bucket([]byte(reviewPRsName)).Put([]byte(key), []byte("invalid")))
	})

	_, found, err := repo.ReviewPR(key)
	if found {
		t.Fatal("ReviewPR found a corrupt record")
	}

	requireError(t, err, store.ErrCorrupt, "read review PR:")
	requireError(t, repo.PutReviewPR(key, old), store.ErrCorrupt, "put review PR:")

	removed, err := repo.PruneReviewPRs()
	if removed != 0 {
		t.Fatalf("PruneReviewPRs removed %d, want 0", removed)
	}

	requireError(t, err, store.ErrCorrupt, "prune review PRs:")
	checkReviewPR(t, repo, "a-old", old, true)
}

func putReviewPR(t *testing.T, repo *store.Bolt, key string, record store.ReviewPR) {
	t.Helper()

	err := repo.PutReviewPR(key, record)
	if err != nil {
		t.Fatalf("PutReviewPR(%q): %v", key, err)
	}
}

func checkReviewPR(t *testing.T, repo *store.Bolt, key string, want store.ReviewPR, wantFound bool) {
	t.Helper()

	got, found, err := repo.ReviewPR(key)
	if err != nil || found != wantFound || !got.ReadyAt.Equal(want.ReadyAt) || !got.UpdatedAt.Equal(want.UpdatedAt) {
		t.Fatalf("ReviewPR(%q) = %+v, %t, %v, want %+v, %t, nil", key, got, found, err, want, wantFound)
	}
}

func checkPrunedReviewPRs(t *testing.T, repo *store.Bolt, want int) {
	t.Helper()

	removed, err := repo.PruneReviewPRs()
	if err != nil || removed != want {
		t.Fatalf("PruneReviewPRs = %d, %v, want %d, nil", removed, err, want)
	}
}
