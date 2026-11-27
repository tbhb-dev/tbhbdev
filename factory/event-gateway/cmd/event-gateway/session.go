// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
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

		state := session{
			conn:          conn,
			stream:        api.store.stream,
			openOrdered:   api.store.orderedConsumer(),
			events:        nil,
			subscriptions: nil,
			replay:        nil,
		}
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
	openOrdered   openOrderedConsumer
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
			if api.revoked(hash) {
				return tokenRevoked, "token revoked"
			}
		case frame := <-frames:
			if frame.err != nil {
				return websocket.CloseStatus(frame.err), ""
			}

			var ready bool

			ready, err = peer.handleFrame(ctx, frame, live == nil)
			if ready {
				deadline = nil
				live = peer.events
			}
		case event := <-live:
			err = api.deliverAuthorized(ctx, peer, hash, event)
		}

		switch {
		case errors.Is(err, errSessionRevoked):
			return tokenRevoked, "token revoked"
		case err != nil:
			return websocket.StatusInternalError, "delivery failed"
		}
	}
}

var errSessionRevoked = errors.New("session token revoked")

func (api *sessionAPI) deliverAuthorized(
	ctx context.Context,
	peer *session,
	hash [sha256.Size]byte,
	event jetstream.Msg,
) error {
	if api.revoked(hash) {
		return errSessionRevoked
	}

	return peer.deliver(ctx, event)
}

func (api *sessionAPI) revoked(hash [sha256.Size]byte) bool {
	return (*api.tokens.set.Load())[hash] == ""
}

func (s *session) handleFrame(ctx context.Context, frame sessionFrame, waiting bool) (bool, error) {
	reply := s.command(ctx, frame, waiting)
	switch {
	case reply[typeKey] == helloKind:
		return true, s.finishHello(ctx, reply)
	case reply != nil:
		return false, s.write(ctx, reply)
	default:
		return false, nil
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

	object := *sessionJSON[sessionObject](frame.data)
	kind := *sessionJSON[string](object[typeKey])

	switch {
	case kind == "ping":
		reply = sessionReply{typeKey: "pong"}
	case kind == helloKind && waiting:
		reply = s.hello(ctx, object)
	}

	id := *sessionJSON[string](object["id"])
	if reply != nil && id != "" && len([]rune(id)) <= idLimit {
		reply["id"] = id
	}

	return reply
}

func sessionJSON[T any](raw json.RawMessage) *T {
	var value T

	err := json.Unmarshal(raw, &value)
	if err != nil {
		if _, mismatch := errors.AsType[*json.UnmarshalTypeError](err); !mismatch {
			return new(T)
		}
	}

	return &value
}

func errorReply(code, msg string) sessionReply {
	return sessionReply{typeKey: "error", codeKey: code, "message": msg}
}

func (s *session) hello(ctx context.Context, object sessionObject) sessionReply {
	if reply := s.importError(object); reply != nil {
		return reply
	}

	more := string(object["more"])
	raw := *sessionJSON[[]json.RawMessage](object[subsKey])

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
