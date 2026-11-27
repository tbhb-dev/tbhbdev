// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package store

import (
	"time"

	bolt "go.etcd.io/bbolt"
)

// OpenAt opens the store with a test clock.
func OpenAt(path string, now func() time.Time) (*Bolt, error) {
	return open(path, now)
}

// Initialize creates the store records in a test transaction.
func Initialize(txn *bolt.Tx, first time.Time) (time.Time, error) {
	return initialize(txn, first)
}

// Database exposes the backing database.
func (b *Bolt) Database() *bolt.DB { return b.db }

// FromDatabase wraps a database opened with custom file options so tests can
// close its file handle before calling Close and check that the error is kept.
func FromDatabase(database *bolt.DB) *Bolt {
	return &Bolt{db: database, now: time.Now, created: time.Time{}}
}
