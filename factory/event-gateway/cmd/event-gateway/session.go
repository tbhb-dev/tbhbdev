// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	sessionProtocol   = "gateway.v1"
	sessionPath       = "/connect"
	upgradeHeader     = "Upgrade"
	sessionFrameLimit = 4096
	codeKey           = "code"
	subscriptionLimit = 256
	helloTimeout      = 10 * time.Second
	tokenRevoked      = websocket.StatusCode(4401)
	badFrame          = "bad_message"
	helloKind         = "hello"
	typeKey           = "type"
	subsKey           = "subscriptions"
	resumeKey         = "resume_after"
	jsonTrue          = "true"
	idLimit           = 64
)

type sessionAPI struct {
	readAPI

	stop      chan struct{}
	active    sync.WaitGroup
	helloWait time.Duration
}

func newSessionAPI(api readAPI, wait time.Duration) *sessionAPI {
	return &sessionAPI{active: sync.WaitGroup{}, readAPI: api, stop: make(chan struct{}), helloWait: wait}
}

func (api *sessionAPI) connect(writer http.ResponseWriter, req *http.Request) {
	header := req.Header.Get("Authorization")

	name := api.tokens.authenticate(header)
	switch {
	case req.URL.Path != sessionPath:
		writer.WriteHeader(http.StatusNotFound)
	case req.Method != http.MethodGet:
		writer.Header().Set("Allow", http.MethodGet)
		writer.WriteHeader(http.StatusMethodNotAllowed)
	case name == "":
		writer.Header().Set("WWW-Authenticate", "Bearer")
		writer.WriteHeader(http.StatusUnauthorized)
	case !offers(req, "Connection", upgradeHeader), !offers(req, upgradeHeader, "websocket"):
		writer.Header().Set(upgradeHeader, "websocket")
		writer.WriteHeader(http.StatusUpgradeRequired)
	case !offers(req, "Sec-WebSocket-Protocol", sessionProtocol):
		writer.WriteHeader(http.StatusBadRequest)
	default:
		api.active.Add(1)
		defer api.active.Done()

		conn, err := websocket.Accept(
			writer,
			req,
			&websocket.AcceptOptions{
				InsecureSkipVerify:   false,
				OriginPatterns:       nil,
				CompressionMode:      0,
				CompressionThreshold: 0,
				OnPingReceived:       nil,
				OnPongReceived:       nil,
				Subprotocols:         []string{sessionProtocol},
			},
		)
		if err != nil {
			return
		}

		conn.SetReadLimit(sessionFrameLimit)

		api.telemetry.logger.InfoContext(req.Context(), "session connected", "token.name", name)

		state := session{conn: conn, stream: api.store.stream, events: nil, subscriptions: nil, replay: nil}
		code, reason := api.run(req.Context(), &state, sha256.Sum256([]byte(strings.Fields(header)[1])))
		_ = conn.Close(code, reason)
	}
}

func offers(req *http.Request, key, value string) bool {
	return slices.ContainsFunc(strings.Split(strings.Join(req.Header.Values(key), ","), ","), func(item string) bool {
		item = strings.TrimSpace(item)

		return item == value || key != "Sec-WebSocket-Protocol" && strings.EqualFold(item, value)
	})
}

type session struct {
	stream        jetstream.Stream
	conn          *websocket.Conn
	events        chan jetstream.Msg
	replay        *sessionReplay
	subscriptions []filter.Subscription
}

type sessionReplay struct {
	gap     sessionReply
	last    uint64
	pending bool
}

type sessionFrame struct {
	err  error
	data []byte
	kind websocket.MessageType
}

func readSession(ctx context.Context, conn *websocket.Conn, frames chan<- sessionFrame) {
	for {
		kind, data, err := conn.Read(context.WithoutCancel(ctx))
		select {
		case frames <- sessionFrame{kind: kind, data: data, err: err}:
		case <-ctx.Done():
			return
		}
	}
}

func (api *sessionAPI) run(ctx context.Context, peer *session, hash [sha256.Size]byte) (websocket.StatusCode, string) {
	ctx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	defer cancel()

	frames := make(chan sessionFrame)
	go readSession(ctx, peer.conn, frames)

	tick := time.NewTicker(time.Second)
	defer tick.Stop()

	deadline := time.After(api.helloWait)

	var live <-chan jetstream.Msg

	for {
		var err error

		select {
		case <-api.stop:
			return websocket.StatusGoingAway, "server shutdown"
		case <-deadline:
			return websocket.StatusPolicyViolation, "hello timeout"
		case <-tick.C:
			if (*api.tokens.set.Load())[hash] == "" {
				return tokenRevoked, "token revoked"
			}
		case frame := <-frames:
			if frame.err != nil {
				return websocket.CloseStatus(frame.err), ""
			}

			reply := peer.command(ctx, frame, live == nil)
			switch {
			case reply[typeKey] == helloKind:
				deadline = nil
				live = peer.events
				err = peer.finishHello(ctx, reply)
			case reply != nil:
				err = peer.write(ctx, reply)
			}
		case event := <-live:
			if (*api.tokens.set.Load())[hash] == "" {
				return tokenRevoked, "token revoked"
			}

			err = peer.deliver(ctx, event)
		}

		if err != nil {
			return websocket.StatusInternalError, "delivery failed"
		}
	}
}

// sessionReply sends the subscription count for hello and echoes valid request IDs.
type sessionReply map[string]any

type sessionObject map[string]json.RawMessage

func (s *session) command(ctx context.Context, frame sessionFrame, waiting bool) sessionReply {
	reply := errorReply(badFrame, "unexpected message")
	if frame.kind != websocket.MessageText {
		return reply
	}

	object := sessionJSON[sessionObject](frame.data)
	kind := sessionJSON[string](object[typeKey])

	switch {
	case kind == "ping":
		reply = sessionReply{typeKey: "pong"}
	case kind == helloKind && waiting:
		reply = s.hello(ctx, object)
	}

	id := sessionJSON[string](object["id"])
	if reply != nil && id != "" && len([]rune(id)) <= idLimit {
		reply["id"] = id
	}

	return reply
}

func sessionJSON[T any](raw json.RawMessage) T {
	var value T

	_ = json.Unmarshal(raw, &value)

	return value
}

func errorReply(code, msg string) sessionReply {
	return sessionReply{typeKey: "error", codeKey: code, "message": msg}
}

func (s *session) hello(ctx context.Context, object sessionObject) sessionReply {
	if reply := s.importError(object); reply != nil {
		return reply
	}

	more := string(object["more"])
	raw := sessionJSON[[]json.RawMessage](object[subsKey])

	switch {
	case more != "" && more != jsonTrue && more != "false", object[subsKey] != nil && raw == nil:
		return errorReply(badFrame, "invalid hello fields")
	case len(s.subscriptions)+len(raw) > subscriptionLimit:
		return errorReply("limit", "too many subscriptions")
	}

	var resume *uint64

	if cursor := object[resumeKey]; cursor != nil {
		seq, err := strconv.ParseUint(string(cursor), decimal, 64)
		if s.events != nil || err != nil {
			return errorReply(badFrame, "invalid resume_after")
		}

		resume = &seq
	}

	subs := s.subscriptions

	for _, item := range raw {
		sub, err := filter.Parse(item)
		if err != nil {
			failure := func() *filter.Error {
				target := &filter.Error{Code: "", Message: ""}
				_ = errors.As(err, &target)
				return target
			}()

			return errorReply(failure.Code, failure.Message)
		}

		subs = append(subs, sub)
	}

	// Creating the consumer sets the live boundary. The run loop starts
	// receiving its events after the final hello, once all filters are known.
	if s.events == nil && !s.consume(ctx, resume, object["import"] != nil) {
		return errorReply("internal", "stream unavailable")
	}

	s.subscriptions = subs

	if more == jsonTrue {
		return nil
	}

	return sessionReply{typeKey: helloKind, subsKey: len(subs)}
}

func (s *session) importError(object sessionObject) sessionReply {
	switch value := string(object["import"]); {
	case value == "":
		return nil
	case value != jsonTrue:
		return errorReply(badFrame, "import must be true")
	case object[resumeKey] != nil:
		return errorReply(badFrame, "import and resume_after are mutually exclusive")
	case s.events != nil:
		return errorReply(badFrame, "import is allowed only in the first hello frame")
	default:
		return nil
	}
}

func (s *session) consume(ctx context.Context, resume *uint64, importing bool) bool {
	config := jetstream.OrderedConsumerConfig{
		DeliverPolicy: jetstream.DeliverNewPolicy, InactiveThreshold: shutdownTimeout, MaxResetAttempts: 1,
	}

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

	consumer, err := s.stream.OrderedConsumer(ctx, config)
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
		return err
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
	event := sessionJSON[struct {
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
		data, _ := json.Marshal(reply)

		err := s.conn.Write(ctx, websocket.MessageText, data)
		if err != nil {
			return err
		}
	}

	return nil
}
