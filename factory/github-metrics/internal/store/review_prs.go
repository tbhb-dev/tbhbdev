// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package store

import (
	"fmt"
	"time"

	bolt "go.etcd.io/bbolt"
)

const (
	reviewPRRetention = 30 * 24 * time.Hour
	reviewStampLayout = "2006-01-02T15:04:05.000000000Z"
	reviewStampSize   = len(reviewStampLayout)
	reviewRecordSize  = 2 * reviewStampSize
)

// ReviewPR holds the timestamps for one pull request.
type ReviewPR struct {
	// ReadyAt is zero when the pull request has no cached ready time.
	ReadyAt time.Time
	// UpdatedAt is the latest update time and determines retention.
	UpdatedAt time.Time
}

// ReviewPR reads the record keyed by the PR node ID. It returns false and no
// error when the record is absent.
func (b *Bolt) ReviewPR(nodeID string) (ReviewPR, bool, error) {
	var record ReviewPR

	var found bool

	err := b.db.View(func(txn *bolt.Tx) error {
		raw := txn.Bucket([]byte(reviewPRsBucket)).Get([]byte(nodeID))
		if raw == nil {
			return nil
		}

		found = true

		var parseErr error

		record, parseErr = parseReviewPR(raw)

		return parseErr
	})
	if err != nil {
		return ReviewPR{}, false, fmt.Errorf("read review PR: %w", err)
	}

	return record, found, nil
}

// PutReviewPR merges and syncs a record keyed by the PR node ID. Each field
// keeps the later time, except that a zero ReadyAt clears the stored time.
func (b *Bolt) PutReviewPR(nodeID string, record ReviewPR) error {
	err := b.db.Update(func(txn *bolt.Tx) error {
		bucket := txn.Bucket([]byte(reviewPRsBucket))
		key := []byte(nodeID)

		if raw := bucket.Get(key); raw != nil {
			previous, err := parseReviewPR(raw)
			if err != nil {
				return err
			}

			if !record.ReadyAt.IsZero() && previous.ReadyAt.After(record.ReadyAt) {
				record.ReadyAt = previous.ReadyAt
			}

			if previous.UpdatedAt.After(record.UpdatedAt) {
				record.UpdatedAt = previous.UpdatedAt
			}
		}

		// Two fixed-width UTC timestamps take 60 bytes, including a zero ready time.
		value := make([]byte, 0, reviewRecordSize)
		value = record.ReadyAt.UTC().AppendFormat(value, reviewStampLayout)
		value = record.UpdatedAt.UTC().AppendFormat(value, reviewStampLayout)

		return bucket.Put(key, value)
	})
	if err != nil {
		return fmt.Errorf("put review PR: %w", err)
	}

	return nil
}

// DeleteReviewPR removes a record by PR node ID. A missing record returns no
// error.
func (b *Bolt) DeleteReviewPR(nodeID string) error {
	err := b.db.Update(func(txn *bolt.Tx) error {
		return txn.Bucket([]byte(reviewPRsBucket)).Delete([]byte(nodeID))
	})
	if err != nil {
		return fmt.Errorf("delete review PR: %w", err)
	}

	return nil
}

// PruneReviewPRs deletes records last updated more than 30 days ago and
// returns the number removed in a single synced transaction.
func (b *Bolt) PruneReviewPRs() (int, error) {
	cutoff := b.now().Add(-reviewPRRetention)

	var expired []string

	err := b.db.Update(func(txn *bolt.Tx) error {
		bucket := txn.Bucket([]byte(reviewPRsBucket))

		err := bucket.ForEach(func(key, value []byte) error {
			record, err := parseReviewPR(value)
			if err != nil {
				return err
			}

			if record.UpdatedAt.Before(cutoff) {
				expired = append(expired, string(key))
			}

			return nil
		})
		if err != nil {
			return fmt.Errorf("scan records: %w", err)
		}

		// Deleting while iterating the bucket can skip the next entry.
		for _, key := range expired {
			err = bucket.Delete([]byte(key))
			if err != nil {
				return fmt.Errorf("delete expired record: %w", err)
			}
		}

		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("prune review PRs: %w", err)
	}

	return len(expired), nil
}

func parseReviewPR(raw []byte) (ReviewPR, error) {
	if len(raw) != reviewRecordSize {
		return ReviewPR{}, fmt.Errorf("review PR record size: %w", ErrCorrupt)
	}

	readyAt, err := parseStamp(raw[:reviewStampSize])
	if err != nil {
		return ReviewPR{}, err
	}

	updatedAt, err := parseStamp(raw[reviewStampSize:])
	if err != nil {
		return ReviewPR{}, err
	}

	return ReviewPR{ReadyAt: readyAt, UpdatedAt: updatedAt}, nil
}
