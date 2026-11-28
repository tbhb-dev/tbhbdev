// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package store keeps the service's durable state in one `bbolt` file. It holds
// the webhook queue, the delivery dedupe table, milestone claims, repository
// watermarks, review PR timestamps, the store's creation time, and the repair
// floor.
//
// Later slices add the dedupe bound (entries last 7 days, at most 200,000,
// oldest evicted first), the dead-letter bucket, and the span ID and entity tag
// records. Until the bound is added, the dedupe table grows without limit.
package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"

	bolt "go.etcd.io/bbolt"
)

const (
	// fileMode is the mode of a new store file.
	fileMode = 0o600
	// lockTimeout bounds the wait for another process to release the file.
	lockTimeout = time.Second
	// stampLayout is the text form of a stored time.
	stampLayout = time.RFC3339Nano
)

// The bucket and key names of the file. Changing one orphans the data in it.
const (
	metaBucket        = "meta"
	queueBucket       = "queue"
	queueIDsBucket    = "queue_deliveries"
	queueEventsBucket = "queue_events"
	bodiesBucket      = "dedupe_bodies"
	deliveriesBucket  = "dedupe_deliveries"
	milestonesBucket  = "milestones"
	watermarksBucket  = "watermarks"
	reviewPRsBucket   = "review_prs"
	createdKey        = "created"
	repairFloorKey    = "repair_floor"
)

// ErrDeliveryReused is returned by Enqueue for a delivery ID that the dedupe
// table already has with a different body.
var ErrDeliveryReused = errors.New("delivery ID reused with a different body")

// ErrNoDelivery is returned by Enqueue for an empty delivery ID.
var ErrNoDelivery = errors.New("delivery ID is empty")

// ErrCorrupt is returned when a stored record can't be read back.
var ErrCorrupt = errors.New("store record is corrupt")

// Outcome says what Enqueue did with a delivery.
type Outcome int

const (
	// Enqueued means the body is new and now sits in the queue.
	Enqueued Outcome = iota + 1
	// Duplicate means the dedupe table already has the body, and the store
	// stays the same.
	Duplicate
)

// Entry is one queued webhook body.
type Entry struct {
	// Delivery is the `X-GitHub-Delivery` ID that came with the body.
	Delivery string
	// Event is the checked X-GitHub-Event header.
	Event string
	// Body is the raw signed body.
	Body []byte
	// Seq orders the queue by arrival and names the entry for Remove.
	Seq uint64
}

// Store is what the receiver and the worker use. The receiver calls Enqueue
// after a delivery's signature passes and answers 202 only when it returns
// without an error. The worker calls Pending and Remove.
type Store interface {
	// Enqueue runs in one transaction. It looks up the body's SHA-256 in the
	// dedupe table first. A body already there, under any delivery ID, is a
	// Duplicate and the store stays the same. A delivery ID already there
	// with another body returns ErrDeliveryReused, and the store stays the
	// same. Otherwise the body hash and the delivery ID go into the
	// table, the body goes onto the queue, and the data is synced to disk
	// before Enqueue returns. On any other error the transaction rolls back.
	Enqueue(delivery, event string, body []byte) (Outcome, error)
	// Pending returns up to limit queued entries in arrival order. A limit
	// under 1 returns none.
	Pending(limit int) ([]Entry, error)
	// Remove deletes one queued entry logically. A Seq that's already gone
	// returns no error. The dedupe table keeps the delivery.
	Remove(seq uint64) error
	// Created is the time of the store's first open. It never changes.
	Created() time.Time
	// Close releases the file.
	Close() error
}

// Bolt is the `bbolt` store, which satisfies Store.
type Bolt struct {
	now     func() time.Time
	db      *bolt.DB
	created time.Time

	//exhaustruct:optional
	floor struct {
		value   *time.Time
		resetAt *time.Time
		mu      sync.Mutex
	}
}

var _ Store = (*Bolt)(nil)

// Open opens the store file at path, creating it with mode 0600 when it
// doesn't exist. The creation time is written at the first open only.
func Open(path string) (*Bolt, error) {
	return open(path, time.Now)
}

func open(path string, now func() time.Time) (*Bolt, error) {
	resetFloor := os.Getenv("RECONCILE_FLOOR_RESET") == "1"
	opts := *bolt.DefaultOptions
	opts.Timeout = lockTimeout

	file, err := bolt.Open(path, fileMode, &opts)
	if err != nil {
		return nil, fmt.Errorf("open store: %w", err)
	}

	var created, started time.Time

	err = file.Update(func(txn *bolt.Tx) error {
		var initErr error

		started = now().UTC()
		created, initErr = initialize(txn, started)

		return initErr
	})
	if err != nil {
		return nil, errors.Join(fmt.Errorf("initialize store: %w", err), file.Close())
	}

	store := &Bolt{db: file, now: now, created: created}
	if resetFloor {
		store.floor.resetAt = &started
	}

	return store, nil
}

// initialize creates the buckets and returns the stored creation time. When
// the file has none yet, it stores first.
func initialize(txn *bolt.Tx, first time.Time) (time.Time, error) {
	for _, name := range []string{
		queueBucket, queueIDsBucket, queueEventsBucket, bodiesBucket, deliveriesBucket, milestonesBucket, watermarksBucket,
		reviewPRsBucket,
	} {
		_, err := txn.CreateBucketIfNotExists([]byte(name))
		if err != nil {
			return time.Time{}, fmt.Errorf("create bucket %s: %w", name, err)
		}
	}

	meta, err := txn.CreateBucketIfNotExists([]byte(metaBucket))
	if err != nil {
		return time.Time{}, fmt.Errorf("create bucket %s: %w", metaBucket, err)
	}

	raw := meta.Get([]byte(createdKey))
	if raw == nil {
		first = first.UTC()

		err = meta.Put([]byte(createdKey), []byte(first.Format(stampLayout)))
		if err != nil {
			return time.Time{}, fmt.Errorf("record creation time: %w", err)
		}

		return first, nil
	}

	created, err := parseStamp(raw)
	if err != nil {
		return time.Time{}, fmt.Errorf("creation time: %w", err)
	}

	return created, nil
}

// Created returns the time of the store's first open.
func (b *Bolt) Created() time.Time { return b.created }

// RepairFloor returns the time from which repairs can count milestones.
// The first call records the current time if the store has no floor. If
// RECONCILE_FLOOR_RESET was 1 at open, the first call records the open time.
func (b *Bolt) RepairFloor() (time.Time, error) {
	state := &b.floor
	state.mu.Lock()
	defer state.mu.Unlock()

	if state.value != nil {
		return *state.value, nil
	}

	var floor time.Time

	err := b.db.Update(func(txn *bolt.Tx) error {
		meta := txn.Bucket([]byte(metaBucket))
		key := []byte(repairFloorKey)

		raw := meta.Get(key)
		if raw != nil && state.resetAt == nil {
			var parseErr error

			floor, parseErr = parseStamp(raw)

			return parseErr
		}

		if state.resetAt == nil {
			floor = b.now().UTC()
		} else {
			floor = *state.resetAt
		}

		return meta.Put(key, []byte(floor.Format(stampLayout)))
	})
	if err != nil {
		return time.Time{}, fmt.Errorf("repair floor: %w", err)
	}

	state.value = &floor

	return floor, nil
}

// ClaimMilestone records a new milestone with a disk sync. The key is the PR
// node ID followed by a zero byte and the milestone name.
func (b *Bolt) ClaimMilestone(key string) (bool, error) {
	var claimed bool

	err := b.db.Update(func(txn *bolt.Tx) error {
		milestones := txn.Bucket([]byte(milestonesBucket))
		if milestones.Get([]byte(key)) != nil {
			return nil
		}

		claimed = true

		return milestones.Put([]byte(key), []byte(b.now().UTC().Format(stampLayout)))
	})
	if err != nil {
		return false, fmt.Errorf("claim milestone: %w", err)
	}

	return claimed, nil
}

// Watermark returns the latest update time stored for a repository full name.
// A repository with no watermark returns the zero time.
func (b *Bolt) Watermark(repository string) (time.Time, error) {
	var watermark time.Time

	err := b.db.View(func(txn *bolt.Tx) error {
		raw := txn.Bucket([]byte(watermarksBucket)).Get([]byte(repository))
		if raw == nil {
			return nil
		}

		var parseErr error

		watermark, parseErr = parseStamp(raw)

		return parseErr
	})
	if err != nil {
		return time.Time{}, fmt.Errorf("read watermark: %w", err)
	}

	return watermark, nil
}

// SetWatermark stores an update time for a repository full name.
func (b *Bolt) SetWatermark(repository string, updated time.Time) error {
	err := b.db.Update(func(txn *bolt.Tx) error {
		return txn.Bucket([]byte(watermarksBucket)).
			Put([]byte(repository), []byte(updated.UTC().Format(stampLayout)))
	})
	if err != nil {
		return fmt.Errorf("set watermark: %w", err)
	}

	return nil
}

func parseStamp(raw []byte) (time.Time, error) {
	stamp, err := time.Parse(stampLayout, string(raw))
	if err != nil {
		return time.Time{}, fmt.Errorf("%w: %w", ErrCorrupt, err)
	}

	return stamp, nil
}

// Close releases the file.
func (b *Bolt) Close() error {
	err := b.db.Close()
	if err != nil {
		return fmt.Errorf("close store: %w", err)
	}

	return nil
}

// Enqueue is Store.Enqueue. A `bbolt` update commits with a disk sync, and the
// store leaves NoSync off, so the commit is durable when Update returns.
func (b *Bolt) Enqueue(delivery, event string, body []byte) (Outcome, error) {
	if delivery == "" {
		return 0, ErrNoDelivery
	}

	hash := sha256.Sum256(body)
	stamp := []byte(b.now().UTC().Format(stampLayout))
	outcome := Enqueued

	err := b.db.Update(func(txn *bolt.Tx) error {
		bodies := txn.Bucket([]byte(bodiesBucket))
		if bodies.Get(hash[:]) != nil {
			outcome = Duplicate

			return nil
		}

		return appendDelivery(txn, delivery, event, body, hash[:], stamp)
	})
	if err != nil {
		return 0, fmt.Errorf("enqueue: %w", err)
	}

	return outcome, nil
}

// appendDelivery records a delivery with a new body and queues the body.
func appendDelivery(txn *bolt.Tx, delivery, event string, body, hash, stamp []byte) error {
	deliveries := txn.Bucket([]byte(deliveriesBucket))
	if deliveries.Get([]byte(delivery)) != nil {
		return ErrDeliveryReused
	}

	err := txn.Bucket([]byte(bodiesBucket)).Put(hash, stamp)
	if err != nil {
		return fmt.Errorf("record body: %w", err)
	}

	err = deliveries.Put([]byte(delivery), hash)
	if err != nil {
		return fmt.Errorf("record delivery: %w", err)
	}

	queue := txn.Bucket([]byte(queueBucket))

	seq, err := queue.NextSequence()
	if err != nil {
		return fmt.Errorf("next sequence: %w", err)
	}

	key := binary.BigEndian.AppendUint64(nil, seq)

	err = queue.Put(key, body)
	if err != nil {
		return fmt.Errorf("append body to queue: %w", err)
	}

	err = txn.Bucket([]byte(queueIDsBucket)).Put(key, []byte(delivery))
	if err != nil {
		return fmt.Errorf("append delivery ID to queue: %w", err)
	}

	err = txn.Bucket([]byte(queueEventsBucket)).Put(key, []byte(event))
	if err != nil {
		return fmt.Errorf("append event to queue: %w", err)
	}

	return nil
}

// Pending is Store.Pending.
func (b *Bolt) Pending(limit int) ([]Entry, error) {
	var entries []Entry

	if limit < 1 {
		return entries, nil
	}

	err := b.db.View(func(txn *bolt.Tx) error {
		ids := txn.Bucket([]byte(queueIDsBucket))
		cursor := txn.Bucket([]byte(queueBucket)).Cursor()

		for key, body := cursor.First(); key != nil && len(entries) < limit; key, body = cursor.Next() {
			delivery := ids.Get(key)
			if delivery == nil {
				return fmt.Errorf("queue entry without a delivery ID: %w", ErrCorrupt)
			}

			// The slices from `bbolt` are valid only inside the transaction,
			// so copy.
			entries = append(entries, Entry{
				Seq:      binary.BigEndian.Uint64(key),
				Delivery: string(delivery),
				Event:    string(txn.Bucket([]byte(queueEventsBucket)).Get(key)),
				Body:     bytes.Clone(body),
			})
		}

		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("read queue: %w", err)
	}

	return entries, nil
}

// Remove is Store.Remove.
func (b *Bolt) Remove(seq uint64) error {
	key := binary.BigEndian.AppendUint64(nil, seq)

	err := b.db.Update(func(txn *bolt.Tx) error {
		err := txn.Bucket([]byte(queueBucket)).Delete(key)
		if err != nil {
			return fmt.Errorf("delete body: %w", err)
		}

		err = txn.Bucket([]byte(queueIDsBucket)).Delete(key)
		if err != nil {
			return fmt.Errorf("delete delivery ID: %w", err)
		}

		err = txn.Bucket([]byte(queueEventsBucket)).Delete(key)
		if err != nil {
			return fmt.Errorf("delete event: %w", err)
		}

		return nil
	})
	if err != nil {
		return fmt.Errorf("remove from queue: %w", err)
	}

	return nil
}
