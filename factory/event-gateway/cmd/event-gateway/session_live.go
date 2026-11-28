// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"math"

	"github.com/nats-io/nats.go/jetstream"
)

func (s *session) helloReply(count int) sessionReply {
	reply := sessionReply{typeKey: helloKind, subsKey: count}
	if s.liveSeq != nil {
		reply[seqKey] = *s.liveSeq
	}

	return reply
}

func configureSessionConsumer(
	config *jetstream.OrderedConsumerConfig, state jetstream.StreamState, resume *uint64, importing bool,
) (uint64, *sessionReplay, *uint64) {
	switch {
	case importing:
		config.DeliverPolicy = jetstream.DeliverAllPolicy

		return 0, &sessionReplay{last: state.LastSeq, gap: nil, pending: false}, nil
	case resume != nil:
		after := *resume
		// Saturate the start at MaxUint64 so it cannot wrap into retained history.
		config.OptStartSeq = min(after, math.MaxUint64-1) + 1
		replay := &sessionReplay{last: state.LastSeq, gap: nil, pending: false}

		if state.LastSeq != 0 && config.OptStartSeq < state.FirstSeq {
			replay.gap = sessionReply{typeKey: "gap", "from": config.OptStartSeq, "to": state.FirstSeq - 1}
			config.OptStartSeq = state.FirstSeq
		}

		return after, replay, nil
	default:
		after := state.LastSeq
		config.OptStartSeq = min(after, math.MaxUint64-1) + 1

		return after, nil, &after
	}
}
