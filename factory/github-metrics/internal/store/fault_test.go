// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package store_test

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
	boltErrors "go.etcd.io/bbolt/errors"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
)

const (
	queueName       = "queue"
	queueIDsName    = "queue_deliveries"
	queueEventsName = "queue_events"
)

func rawDB(t *testing.T, path string) *bolt.DB {
	t.Helper()

	opts := *bolt.DefaultOptions
	opts.Timeout = 3 * time.Second

	database, err := bolt.Open(path, 0o600, &opts)
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { closeStore(t, database) })

	return database
}

func update(t *testing.T, database *bolt.DB, fn func(*bolt.Tx) error) {
	t.Helper()

	err := database.Update(fn)
	if err != nil {
		t.Fatal(err)
	}
}

func requireError(t *testing.T, err, want error, context string) {
	t.Helper()

	if !errors.Is(err, want) || !strings.Contains(err.Error(), context) {
		t.Fatalf("error = %v, want %v with context %q", err, want, context)
	}
}

func TestOpenCorruptCreationTime(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	database := rawDB(t, path)
	update(t, database, func(txn *bolt.Tx) error {
		meta, err := txn.CreateBucket([]byte("meta"))
		if err != nil {
			return setupError(err)
		}

		return setupError(meta.Put([]byte("created"), []byte("invalid")))
	})

	err := database.Close()
	if err != nil {
		t.Fatal(err)
	}

	repo, err := store.Open(path)
	if repo != nil {
		closeStore(t, repo)

		t.Fatal("Open returned a store for a corrupt creation time")
	}

	requireError(t, err, store.ErrCorrupt, "initialize store: creation time:")

	_, ok := errors.AsType[*time.ParseError](err)
	if !ok {
		t.Fatalf("error = %v, want time.ParseError", err)
	}

	// Opening the file directly proves that failed initialization released it.
	rawDB(t, path)
}

func TestInitializeReadOnly(t *testing.T) {
	t.Parallel()

	database := rawDB(t, filepath.Join(t.TempDir(), "store.db"))
	err := initializeIn(database, true)
	requireError(t, err, boltErrors.ErrTxNotWritable, "create bucket queue:")
}

func TestInitializeCreationKeyIsBucket(t *testing.T) {
	t.Parallel()

	database := rawDB(t, filepath.Join(t.TempDir(), "store.db"))
	update(t, database, func(txn *bolt.Tx) error {
		meta, err := txn.CreateBucket([]byte("meta"))
		if err != nil {
			return setupError(err)
		}

		_, err = meta.CreateBucket([]byte("created"))

		return setupError(err)
	})
	err := initializeIn(database, false)
	requireError(t, err, boltErrors.ErrIncompatibleValue, "record creation time:")
}

func TestCreatedUsesUTC(t *testing.T) {
	t.Parallel()

	first := time.Date(2026, time.November, 15, 12, 0, 0, 123, time.FixedZone("offset", 3600))

	repo, err := store.OpenAt(
		filepath.Join(t.TempDir(), "store.db"),
		func() time.Time { return first },
	)
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { closeStore(t, repo) })

	if repo.Created().Location() != time.UTC || !repo.Created().Equal(first) {
		t.Fatalf("Created = %v, want UTC %v", repo.Created(), first.UTC())
	}
}

func TestAppendConflictingBucket(t *testing.T) {
	t.Parallel()

	hash := sha256.Sum256([]byte("body"))
	sequence := binary.BigEndian.AppendUint64(nil, 1)

	for _, test := range []struct {
		bucket  string
		context string
		key     []byte
	}{
		{"dedupe_bodies", "record body:", hash[:]},
		{"dedupe_deliveries", "record delivery:", []byte("d1")},
		{queueName, "append body to queue:", sequence},
		{queueIDsName, "append delivery ID to queue:", sequence},
		{queueEventsName, "append event to queue:", sequence},
	} {
		t.Run(test.bucket, func(t *testing.T) {
			t.Parallel()

			repo, err := store.Open(filepath.Join(t.TempDir(), "store.db"))
			if err != nil {
				t.Fatal(err)
			}

			t.Cleanup(func() { closeStore(t, repo) })

			conflict(t, repo.Database(), test.bucket, test.key)

			outcome, err := repo.Enqueue("d1", "pull_request", []byte("body"))

			if outcome != 0 {
				t.Fatalf("outcome = %v, want 0", outcome)
			}

			requireError(t, err, boltErrors.ErrIncompatibleValue, test.context)

			// Remove the conflict. All prior writes must have rolled back.
			update(t, repo.Database(), func(txn *bolt.Tx) error {
				return setupError(txn.Bucket([]byte(test.bucket)).DeleteBucket(test.key))
			})

			outcome, err = repo.Enqueue("d1", "pull_request", []byte("body"))

			if err != nil || outcome != store.Enqueued {
				t.Fatalf("retry = %v, %v", outcome, err)
			}

			entries, err := repo.Pending(1)
			if err != nil || len(entries) != 1 || entries[0].Seq != 1 {
				t.Fatalf("Pending after retry = %+v, %v", entries, err)
			}
		})
	}
}

func TestRemoveConflictingBucket(t *testing.T) {
	t.Parallel()

	for _, bucket := range []string{queueName, queueIDsName, queueEventsName} {
		t.Run(bucket, func(t *testing.T) {
			t.Parallel()

			repo, err := store.Open(filepath.Join(t.TempDir(), "store.db"))
			if err != nil {
				t.Fatal(err)
			}

			t.Cleanup(func() { closeStore(t, repo) })

			key := binary.BigEndian.AppendUint64(nil, 1)

			conflict(t, repo.Database(), bucket, key)

			context := "delete body:"

			switch bucket {
			case queueIDsName:
				context = "delete delivery ID:"
			case queueEventsName:
				context = "delete event:"
			}

			requireError(t, repo.Remove(1), boltErrors.ErrIncompatibleValue, context)
		})
	}
}

func TestRemoveDeletesDeliveryID(t *testing.T) {
	t.Parallel()

	repo, err := store.Open(filepath.Join(t.TempDir(), "store.db"))
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { closeStore(t, repo) })

	_, err = repo.Enqueue("d1", "pull_request", []byte("body"))
	if err != nil {
		t.Fatal(err)
	}

	err = repo.Remove(1)
	if err != nil {
		t.Fatal(err)
	}

	err = repo.Database().View(func(txn *bolt.Tx) error {
		for _, name := range []string{queueIDsName, queueEventsName} {
			if txn.Bucket([]byte(name)).Get(binary.BigEndian.AppendUint64(nil, 1)) != nil {
				t.Fatalf("Remove kept queue metadata in %s", name)
			}
		}

		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestPendingMissingDeliveryID(t *testing.T) {
	t.Parallel()

	repo, err := store.Open(filepath.Join(t.TempDir(), "store.db"))
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { closeStore(t, repo) })

	_, err = repo.Enqueue("d1", "pull_request", []byte("body"))
	if err != nil {
		t.Fatal(err)
	}

	update(t, repo.Database(), func(txn *bolt.Tx) error {
		return setupError(
			txn.Bucket([]byte(queueIDsName)).Delete(binary.BigEndian.AppendUint64(nil, 1)),
		)
	})
	entries, err := repo.Pending(1)

	if entries != nil {
		t.Fatalf("Pending = %+v, want nil", entries)
	}

	requireError(t, err, store.ErrCorrupt, "read queue: queue entry without a delivery ID:")
}

func TestCloseFileError(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "store.db")
	database := rawDB(t, path)

	err := database.Close()
	if err != nil {
		t.Fatal(err)
	}

	var file *os.File

	opts := *bolt.DefaultOptions
	opts.ReadOnly = true
	opts.OpenFile = func(path string, flags int, mode os.FileMode) (*os.File, error) {
		var openErr error

		file, openErr = os.OpenFile(
			filepath.Clean(path),
			flags,
			mode,
		)

		return file, setupError(openErr)
	}

	database, err = bolt.Open(path, 0o600, &opts)
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() { closeStore(t, database) })

	err = file.Close()
	if err != nil {
		t.Fatal(err)
	}

	repo := store.FromDatabase(database)
	requireError(t, repo.Close(), os.ErrClosed, "close store:")
}

func TestInitializeBucketNameConflict(t *testing.T) {
	t.Parallel()

	for _, name := range []string{queueName, queueIDsName, queueEventsName, "dedupe_bodies", "dedupe_deliveries", "meta"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			database := rawDB(t, filepath.Join(t.TempDir(), "store.db"))
			err := database.Update(func(txn *bolt.Tx) error {
				// A root value with a bucket's name models an incompatible file.
				putErr := txn.Cursor().Bucket().Put([]byte(name), []byte("value"))
				if putErr != nil {
					return fmt.Errorf("write conflicting value: %w", putErr)
				}

				_, err := store.Initialize(txn, time.Now())

				return setupError(err)
			})
			requireError(t, err, boltErrors.ErrIncompatibleValue, "create bucket "+name+":")
		})
	}
}

func setupError(err error) error {
	if err != nil {
		return fmt.Errorf("test setup: %w", err)
	}

	return nil
}

func conflict(t *testing.T, database *bolt.DB, bucket string, key []byte) {
	t.Helper()

	update(t, database, func(txn *bolt.Tx) error {
		_, err := txn.Bucket([]byte(bucket)).CreateBucket(key)

		return setupError(err)
	})
}

func initializeIn(database *bolt.DB, readOnly bool) error {
	run := database.Update
	if readOnly {
		run = database.View
	}

	return setupError(run(func(txn *bolt.Tx) error {
		_, err := store.Initialize(txn, time.Now())

		return setupError(err)
	}))
}
