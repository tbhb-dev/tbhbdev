// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"time"

	"github.com/coder/websocket"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

func (s *session) consume(ctx context.Context, resume *uint64, importing bool) bool {
	var config jetstream.OrderedConsumerConfig

	config.DeliverPolicy = jetstream.DeliverNewPolicy
	config.InactiveThreshold = shutdownTimeout
	config.MaxResetAttempts = 1

	var after uint64

	switch {
	case resume != nil:
		after = *resume
		config.DeliverPolicy = jetstream.DeliverByStartSequencePolicy
		// Saturate the start at MaxUint64 so it cannot wrap into retained history.
		config.OptStartSeq = min(after, math.MaxUint64-1) + 1
	case importing:
		config.DeliverPolicy = jetstream.DeliverAllPolicy
	}

	var replay *sessionReplay

	if importing || resume != nil {
		info, err := s.stream.Info(ctx)
		if err != nil {
			return false
		}

		replay = &sessionReplay{last: info.State.LastSeq, gap: nil, pending: false}
		if resume != nil && info.State.LastSeq != 0 && config.OptStartSeq < info.State.FirstSeq {
			replay.gap = sessionReply{typeKey: "gap", "from": config.OptStartSeq, "to": info.State.FirstSeq - 1}
			config.OptStartSeq = info.State.FirstSeq
		}
	}

	consumer, err := s.openOrdered(ctx, config)
	if err != nil {
		return false
	}

	if replay != nil {
		replay.pending = after < replay.last && consumer.CachedInfo().NumPending != 0
		s.replay = replay
	}

	s.events = make(chan jetstream.Msg)
	send := func(msg jetstream.Msg) {
		select {
		case s.events <- msg:
		case <-ctx.Done():
		}
	}

	pull, err := consumer.Consume(send, consumeErrorHandler(send))
	if err != nil {
		close(s.events)
	} else {
		context.AfterFunc(ctx, pull.Stop)
	}

	return true
}

func consumeErrorHandler(send func(jetstream.Msg)) jetstream.ConsumeErrHandler {
	return func(_ jetstream.ConsumeContext, err error) {
		if !errors.Is(err, jetstream.ErrNoHeartbeat) {
			send(nil)
		}
	}
}

func (s *session) deliver(ctx context.Context, message jetstream.Msg) error {
	if message == nil {
		return io.EOF
	}

	matches := s.matches(message.Data())
	if s.replay == nil && !matches {
		return nil
	}

	meta, err := message.Metadata()
	if err != nil {
		return fmt.Errorf("read session event metadata: %w", err)
	}

	var replies []any

	// Pending counts can include new events. Use the snapshot to place the
	// marker before the first live event, including when the boundary was deleted.
	if s.replay != nil && meta.Sequence.Stream > s.replay.last {
		replies = append(replies, s.caughtUp())
	}

	if matches {
		replies = append(replies, sequenceEnvelope(meta.Sequence.Stream, message.Data()))
	}

	// A zero pending count also finishes replay when its tail is missing.
	if s.replay != nil && (meta.Sequence.Stream == s.replay.last || meta.NumPending == 0) {
		replies = append(replies, s.caughtUp())
	}

	return s.write(ctx, replies...)
}

func (s *session) matches(data []byte) bool {
	event := *sessionJSON[struct {
		Provider, Event, Repository string
		Payload                     json.RawMessage
	}](data)

	fields := filter.Extract(event.Event, event.Payload)
	for _, sub := range s.subscriptions {
		if sub.Matches(event.Provider, event.Event, event.Repository, fields) {
			return true
		}
	}

	return false
}

func (s *session) finishHello(ctx context.Context, reply sessionReply) error {
	replies := []any{reply}

	if s.replay != nil {
		if s.replay.gap != nil {
			replies = append(replies, s.replay.gap)
		}

		if !s.replay.pending {
			replies = append(replies, s.caughtUp())
		}
	}

	return s.write(ctx, replies...)
}

func (s *session) caughtUp() sessionReply {
	last := s.replay.last
	s.replay = nil

	return sessionReply{typeKey: "caught_up", "seq": last}
}

func (s *session) write(ctx context.Context, replies ...any) error {
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()

	for _, reply := range replies {
		data, err := json.Marshal(reply)
		if err != nil {
			return fmt.Errorf("encode session reply: %w", err)
		}

		err = s.conn.Write(ctx, websocket.MessageText, data)
		if err != nil {
			return fmt.Errorf("write session reply: %w", err)
		}
	}

	return nil
}
