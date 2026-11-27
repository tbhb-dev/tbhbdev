// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:words synctest

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

const issuesEvent = "issues"

func TestStoreDeliveryDuplicates(t *testing.T) {
	t.Parallel()

	ctx := t.Context()
	store := testStore(t, retention)

	const (
		event    = "pull_request"
		delivery = "delivery-1"
		body     = "\t\n" + `{ "repository": {"full_name": "example/repository"}, "action": "opened" }` + "\r\n"
	)

	repository := "example/repository"
	received := time.Now().UTC()
	message := envelope{
		Type:       "event",
		Provider:   "github",
		Event:      event,
		Delivery:   delivery,
		Repository: &repository,
		ReceivedAt: received.Format(time.RFC3339Nano),
		Payload:    []byte(body),
		projected:  nil,
	}
	checkError(t, store.publish(ctx, message))
	checkError(t, store.publish(ctx, message))

	changed := message
	changed.Payload = []byte(body + " ")
	err := store.publish(ctx, changed)
	checkEqual(t, errors.Is(err, errBodyConflict), true)

	info, err := store.stream.Info(ctx)
	checkError(t, err)
	checkEqual(t, info.State.Msgs, uint64(1))
	checkEqual(t, info.Config.Storage, jetstream.FileStorage)
	checkEqual(t, info.Config.MaxAge, retention)
	checkEqual(t, info.Config.Duplicates, retention)

	msg, err := store.stream.GetMsg(ctx, info.State.FirstSeq)
	checkError(t, err)

	checkEqual(t, msg.Subject, "events.github.pull_request")
	checkEqual(t, msg.Header.Get(nats.MsgIdHdr), "github:"+delivery)

	hash := sha256.Sum256(message.Payload)
	checkEqual(t, msg.Header.Get(bodyHashKey), hex.EncodeToString(hash[:]))

	var stored map[string]json.RawMessage
	checkError(t, json.Unmarshal(msg.Data, &stored))
	checkEqual(t, len(stored), 7)
	checkEqual(t, string(stored["type"]), `"event"`)
	checkEqual(t, string(stored["provider"]), `"github"`)
	checkEqual(t, string(stored["event"]), `"pull_request"`)
	checkEqual(t, string(stored["delivery"]), `"delivery-1"`)
	checkEqual(t, string(stored["repository"]), `"example/repository"`)
	checkEqual(t, string(stored["payload"]), strings.TrimSpace(body))

	_, encodedPayload, found := strings.Cut(string(msg.Data), `,"payload":`)
	checkEqual(t, found, true)
	checkEqual(t, encodedPayload, body+"}")

	var receivedAt time.Time
	checkError(t, json.Unmarshal(stored["received_at"], &receivedAt))
	checkEqual(t, receivedAt.Location(), time.UTC)

	checkError(t, store.stream.DeleteMsg(ctx, msg.Sequence))

	err = store.publish(ctx, message)
	checkEqual(t, errors.Is(err, jetstream.ErrMsgNotFound), true)

	canceled, cancel := context.WithCancel(ctx)
	cancel()

	err = store.publish(canceled, message)
	checkEqual(t, errors.Is(err, context.Canceled), true)
}

func TestEventExpiry(t *testing.T) {
	t.Parallel()

	const (
		maxAge   = 150 * time.Millisecond
		delivery = "expiry"
	)

	ctx := t.Context()
	store := testStore(t, maxAge)
	message := envelope{
		Type:       "event",
		Provider:   "github",
		Event:      issuesEvent,
		Delivery:   delivery,
		Repository: nil,
		ReceivedAt: time.Now().UTC().Format(time.RFC3339Nano),
		Payload:    []byte("{}"),
		projected:  nil,
	}
	checkError(t, store.publish(ctx, message))

	deadline := time.Now().Add(5 * time.Second)

	for {
		info, err := store.stream.Info(ctx)
		checkError(t, err)

		if info.State.Msgs == 0 {
			break
		}

		if time.Now().After(deadline) {
			t.Fatal("event did not expire")
		}

		time.Sleep(10 * time.Millisecond)
	}

	_, err := store.stream.GetMsg(ctx, 1)
	checkEqual(t, errors.Is(err, jetstream.ErrMsgNotFound), true)

	message.Payload = []byte(`{"action":"opened"}`)
	checkError(t, store.publish(ctx, message))

	msg, err := store.stream.GetMsg(ctx, 2)
	checkError(t, err)
	checkEqual(t, msg.Sequence, uint64(2))
}

func TestStoreStartupErrors(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()

	options := storeOptions(dir)
	checkEqual(t, options.NoSigs, true)
	checkEqual(t, options.SyncAlways, true)
	options.MaxPending = 1

	_, _, err := openStore(t.Context(), options, retention)
	if err == nil {
		t.Fatal("NATS accepted invalid startup options")
	}

	srv, err := server.NewServer(storeOptions(dir))
	checkError(t, err)
	srv.Shutdown()

	_, _, err = connectStore(t.Context(), srv, retention)
	if err == nil {
		t.Fatal("connected to a stopped server")
	}
}

func TestStoreRejectsInvalidDirectory(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	file := filepath.Join(dir, "storage")
	checkError(t, os.WriteFile(file, []byte("data"), 0o600))

	_, _, err := openStore(t.Context(), storeOptions(""), retention)
	checkEqual(t, errors.Is(err, errStoreDir), true)

	embedded, err := server.NewServer(storeOptions(file))
	checkError(t, err)
	t.Cleanup(embedded.Shutdown)

	_, stop, err := startStore(t.Context(), embedded, retention, time.Second)
	checkEqual(t, errors.Is(err, errNATSNotReady), true)
	// The server's cause is attached; its wording varies by OS.
	checkEqual(t, len(err.Error()) > len(errNATSNotReady.Error())+2, true)
	checkEqual(t, stop == nil, true)
	checkEqual(t, embedded.Running(), false)
}

func TestStoreStopsOnStreamError(t *testing.T) {
	t.Parallel()

	embedded, err := server.NewServer(storeOptions(t.TempDir()))
	checkError(t, err)
	t.Cleanup(embedded.Shutdown)
	embedded.Start()

	conn, err := nats.Connect(nats.DefaultURL, nats.InProcessServer(embedded))
	checkError(t, err)
	t.Cleanup(conn.Close)

	_, stop, err := openStream(t.Context(), embedded, conn, time.Nanosecond)
	if err == nil || !strings.Contains(err.Error(), "configure EVENTS stream") {
		t.Fatalf("expected a stream configuration error, got %v", err)
	}

	checkEqual(t, stop == nil, true)
	checkEqual(t, conn.IsClosed(), true)
	checkEqual(t, embedded.Running(), false)
}

func TestStoreRejectsInvalidEvent(t *testing.T) {
	t.Parallel()

	var store eventStore

	var event envelope

	event.Payload = []byte("{}")
	for _, name := range []string{
		"", "issue.comment", "issue*", "issue>", "issue comment", "issue\tcomment", "issue\u00a0comment",
	} {
		event.Event = name
		err := store.publish(t.Context(), event)
		checkEqual(t, errors.Is(err, errInvalidEvent), true)
	}

	event.Event = issuesEvent
	event.Payload = nil
	err := store.publish(t.Context(), event)
	checkEqual(t, errors.Is(err, errEmptyPayload), true)
}

type retryJet struct {
	jetstream.JetStream

	calls int
}

func (jet *retryJet) PublishMsg(context.Context, *nats.Msg, ...jetstream.PublishOpt) (*jetstream.PubAck, error) {
	jet.calls++

	var ack jetstream.PubAck

	ack.Duplicate = jet.calls == 1

	return &ack, nil
}

type expiredStream struct {
	jetstream.Stream
}

func (expiredStream) GetMsg(context.Context, uint64, ...jetstream.GetMsgOpt) (*jetstream.RawStreamMsg, error) {
	return nil, jetstream.ErrMsgNotFound
}

func retryFixture(jet *retryJet) (eventStore, envelope) {
	var stream expiredStream

	var event envelope

	event.Event = issuesEvent
	event.Payload = []byte("{}")

	return eventStore{jet: jet, stream: stream}, event
}

func TestStoreRetryAfterExpiry(t *testing.T) {
	t.Parallel()

	jet := new(retryJet)
	store, event := retryFixture(jet)
	checkError(t, store.publish(t.Context(), event))
	checkEqual(t, jet.calls, 2)
}

func TestStoreCancelDuringRetryWait(t *testing.T) {
	t.Parallel()

	synctest.Test(t, func(t *testing.T) {
		jet := new(retryJet)
		store, event := retryFixture(jet)

		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()

		result := make(chan error, 1)
		go func() {
			result <- store.publish(ctx, event)
		}()

		synctest.Wait()
		cancel()
		checkEqual(t, errors.Is(<-result, context.Canceled), true)
		checkEqual(t, jet.calls, 1)
	})
}
