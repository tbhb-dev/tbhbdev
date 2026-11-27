// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

const (
	retention     = 7 * 24 * time.Hour
	maxPayload    = 6 << 20
	natsReadyWait = 5 * time.Second
	expiryRetry   = 100 * time.Millisecond
	streamName    = "EVENTS"
	bodyHashKey   = "Event-Body-SHA256"
)

var (
	errNATSNotReady = errors.New("embedded NATS did not become ready")
	errStoreDir     = errors.New("store directory must not be empty")
	errInvalidEvent = errors.New("event name must be a single subject token without wildcards or whitespace")
	errEmptyPayload = errors.New("event payload must not be empty")
	errBodyConflict = errors.New("delivery ID already has a different body")
)

type openOrderedConsumer func(context.Context, jetstream.OrderedConsumerConfig) (jetstream.Consumer, error)

type eventStore struct {
	openOrdered openOrderedConsumer
	jet         jetstream.JetStream
	stream      jetstream.Stream
}

func storeOptions(storeDir string) *server.Options {
	var options server.Options

	options.JetStream = true
	options.StoreDir = storeDir
	options.DontListen = true
	options.NoSigs = true
	options.SyncAlways = true
	options.MaxPayload = maxPayload

	return &options
}

func openStore(ctx context.Context, options *server.Options, maxAge time.Duration) (eventStore, func(), error) {
	if options.StoreDir == "" {
		return eventStore{}, nil, errStoreDir
	}

	embedded, err := server.NewServer(options)
	if err != nil {
		return eventStore{}, nil, fmt.Errorf("configure embedded NATS: %w", err)
	}

	return startStore(ctx, embedded, maxAge, natsReadyWait)
}

// cspell:words Noticef Warnf Debugf Tracef

// startupLog records the first fatal error without exiting the gateway.
type startupLog chan string

func (startupLog) Noticef(string, ...any) {}
func (startupLog) Warnf(string, ...any)   {}
func (startupLog) Errorf(string, ...any)  {}
func (startupLog) Debugf(string, ...any)  {}
func (startupLog) Tracef(string, ...any)  {}

func (log startupLog) Fatalf(format string, args ...any) {
	select {
	case log <- fmt.Sprintf(format, args...):
	default:
	}
}

func startStore(
	ctx context.Context,
	embedded *server.Server,
	maxAge, readyWait time.Duration,
) (eventStore, func(), error) {
	failures := make(startupLog, 1)
	embedded.SetLogger(failures, false, false)

	go embedded.Start()

	if !embedded.ReadyForConnections(readyWait) {
		embedded.Shutdown()

		select {
		case cause := <-failures:
			return eventStore{}, nil, fmt.Errorf("%w: %s", errNATSNotReady, cause)
		default:
			return eventStore{}, nil, errNATSNotReady
		}
	}

	return connectStore(ctx, embedded, maxAge)
}

func connectStore(ctx context.Context, embedded *server.Server, maxAge time.Duration) (eventStore, func(), error) {
	conn, err := nats.Connect(nats.DefaultURL, nats.InProcessServer(embedded))
	if err != nil {
		embedded.Shutdown()

		return eventStore{}, nil, fmt.Errorf("connect to embedded NATS: %w", err)
	}

	return openStream(ctx, embedded.Shutdown, conn, maxAge)
}

func openStream(
	ctx context.Context,
	shutdown func(),
	conn *nats.Conn,
	maxAge time.Duration,
	options ...jetstream.JetStreamOpt,
) (eventStore, func(), error) {
	closeStore := func() {
		conn.Close()
		shutdown()
	}

	jet, err := jetstream.New(conn, options...)
	if err != nil {
		closeStore()

		return eventStore{}, nil, fmt.Errorf("initialize JetStream: %w", err)
	}

	var config jetstream.StreamConfig

	config.Name = streamName
	config.Subjects = []string{"events.>"}
	config.Storage = jetstream.FileStorage
	config.Replicas = 1
	config.MaxAge = maxAge
	config.Duplicates = maxAge

	stream, err := jet.CreateOrUpdateStream(ctx, config)
	if err != nil {
		closeStore()

		return eventStore{}, nil, fmt.Errorf("configure EVENTS stream: %w", err)
	}

	return eventStore{jet: jet, stream: stream, openOrdered: nil}, closeStore, nil
}

func (s eventStore) orderedConsumer() openOrderedConsumer {
	if s.openOrdered != nil {
		return s.openOrdered
	}

	return s.stream.OrderedConsumer
}

func validateEvent(name string, payload []byte) error {
	if name == "" || strings.ContainsAny(name, ".*>") || strings.ContainsFunc(name, unicode.IsSpace) {
		return errInvalidEvent
	}

	if len(payload) == 0 {
		return errEmptyPayload
	}

	return nil
}

func (s eventStore) publish(ctx context.Context, event envelope) error {
	// Grafana events are alert.<status>, a fixed pair of subject tokens.
	name := event.Event
	if event.Provider == grafanaProvider {
		name = strings.ReplaceAll(name, ".", "_")
	}

	err := validateEvent(name, event.Payload)
	if err != nil {
		return err
	}

	sum := sha256.Sum256(event.Payload)
	hash := hex.EncodeToString(sum[:])
	msg := nats.NewMsg("events." + event.Provider + "." + event.Event)

	msg.Data, err = event.encode()
	if err != nil {
		return fmt.Errorf("encode event: %w", err)
	}

	msg.Header.Set(nats.MsgIdHdr, event.Provider+":"+event.Delivery)
	msg.Header.Set(bodyHashKey, hash)

	err = s.publishChecked(ctx, msg, hash)
	if !errors.Is(err, jetstream.ErrMsgNotFound) {
		return err
	}

	// Retention and duplicate eviction run on separate JetStream timers.
	wait := time.NewTimer(expiryRetry)
	defer wait.Stop()

	select {
	case <-ctx.Done():
		return fmt.Errorf("wait for duplicate expiry: %w", ctx.Err())
	case <-wait.C:
		return s.publishChecked(ctx, msg, hash)
	}
}

func (s eventStore) publishChecked(ctx context.Context, msg *nats.Msg, hash string) error {
	ack, err := s.jet.PublishMsg(ctx, msg, jetstream.WithExpectStream(streamName))
	if err != nil {
		return fmt.Errorf("publish event: %w", err)
	}

	if !ack.Duplicate {
		return nil
	}

	original, err := s.stream.GetMsg(ctx, ack.Sequence)
	if err != nil {
		return fmt.Errorf("read original event: %w", err)
	}

	if original.Header.Get(bodyHashKey) != hash {
		return errBodyConflict
	}

	return nil
}
