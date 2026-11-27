// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"encoding/json/v2"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	gatewayURLEnv        = "EVENT_GATEWAY_URL"
	gatewayTokenEnv      = "EVENT_GATEWAY_TOKEN"
	gatewayRetryMin      = time.Second
	gatewayRetryMax      = 30 * time.Second
	gatewayRetryFactor   = 2
	gatewayDeliveryLimit = 1000
)

var (
	errGatewayToken         = errors.New("gateway token rejected")
	errGatewayHello         = errors.New("invalid gateway hello subscription")
	errGatewaySubscriptions = errors.New("gateway subscriptions changed")
)

func startMCPGateway(
	ctx context.Context,
	subscriptions *mcpSubscriptions,
	notifier *mcpNotifier,
	logger *slog.Logger,
	getenv func(string) string,
) (func(context.Context, *mcp.InitializedRequest), func()) {
	endpoint, token := getenv(gatewayURLEnv), getenv(gatewayTokenEnv)
	if endpoint == "" || token == "" {
		return nil, func() {}
	}

	ctx, cancel := context.WithCancel(ctx)

	var worker sync.WaitGroup

	return func(_ context.Context, req *mcp.InitializedRequest) {
			worker.Go(func() {
				err := runMCPGateway(ctx, logger, endpoint, token, subscriptions, func(event channelEvent) bool {
					deliveryErr := notifier.deliverChannel(ctx, req.Session, event)
					logger.DebugContext(ctx, "gateway notification", "delivered", deliveryErr == nil)

					return deliveryErr == nil
				}, waitGatewayRetry)
				logger.DebugContext(ctx, "gateway client stopped", "failed", err != nil)
			})
		}, func() {
			cancel()
			worker.Wait()
		}
}

// runMCPGateway forwards events and reconnects with capped backoff, using
// the current subscriptions and cursor on each connection.
func runMCPGateway(
	ctx context.Context,
	logger *slog.Logger,
	endpoint, token string,
	subscriptions *mcpSubscriptions,
	notify func(channelEvent) bool,
	wait func(context.Context, time.Duration) bool,
) error {
	warnInsecureGateway(ctx, logger, endpoint)

	options := gatewayDialOptions(token)

	delay := gatewayRetryMin

	var state gatewayConnectionState

	state.deliveries.ids = make(map[string]struct{}, gatewayDeliveryLimit)
	receive := func(kind websocket.MessageType, data []byte) {
		state.receive(kind, data, subscriptions, notify)
	}

	for ctx.Err() == nil {
		err := runGatewayConnection(ctx, logger, endpoint, options, subscriptions, receive)

		select {
		case <-ctx.Done():
			return nil
		default:
		}

		switch {
		case errors.Is(err, errGatewaySubscriptions):
			continue
		case errors.Is(err, errGatewayToken):
			return rejectGatewayToken(notify)
		case err != nil:
			return err
		}

		if state.ready {
			notify(gatewayConnectionLost().channelEvent())

			delay = gatewayRetryMin
			state.ready, state.recovering = false, true
		}

		logger.DebugContext(ctx, "gateway retry scheduled", "delay_seconds", int(delay/time.Second))

		if !wait(ctx, delay) {
			break
		}

		delay = min(gatewayRetryFactor*delay, gatewayRetryMax)
	}

	return nil
}

type gatewayConnectionState struct {
	deliveries        gatewayDeliveries
	ready, recovering bool
}

func (state *gatewayConnectionState) receive(
	kind websocket.MessageType, data []byte, subscriptions *mcpSubscriptions, notify func(channelEvent) bool,
) {
	state.ready = true
	if state.recovering {
		notify(gatewayReconnected().channelEvent())

		state.recovering = false
	}

	if kind == websocket.MessageText {
		forwardGatewayFrame(*sessionJSON[sessionObject](data), subscriptions, &state.deliveries, notify)
	}
}

func gatewayDialOptions(token string) *websocket.DialOptions {
	var options websocket.DialOptions

	options.HTTPHeader = http.Header{"Authorization": {"Bearer " + token}}
	options.Subprotocols = []string{sessionProtocol}
	options.HTTPClient = &http.Client{
		Transport: nil, Jar: nil, Timeout: 0, CheckRedirect: rejectRedirect,
	}

	return &options
}

func warnInsecureGateway(ctx context.Context, logger *slog.Logger, endpoint string) {
	parsed, err := url.Parse(endpoint)
	if err != nil {
		return
	}

	switch parsed.Scheme {
	case httpScheme, "ws":
		host := parsed.Hostname()
		if !loopbackHost(host) && !net.ParseIP(host).IsPrivate() {
			logger.WarnContext(ctx, "Gateway connection uses an unencrypted WebSocket.")
		}
	}
}

func waitGatewayRetry(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// runGatewayConnection returns errGatewaySubscriptions for an immediate redial,
// nil for a retry with backoff, or a fatal error.
func runGatewayConnection(
	ctx context.Context,
	logger *slog.Logger,
	endpoint string,
	options *websocket.DialOptions,
	subscriptions *mcpSubscriptions,
	receive func(websocket.MessageType, []byte),
) error {
	conn, response, err := websocket.Dial(ctx, endpoint, options)
	if err != nil {
		// The dial error may contain the URL, headers or an untrusted reply.
		if response != nil {
			defer response.Body.Close()

			if response.StatusCode == http.StatusUnauthorized {
				return errGatewayToken
			}
		}

		return nil
	}

	defer func() {
		logger.DebugContext(ctx, "gateway socket cleanup", "closed", conn.CloseNow() == nil)
	}()

	// The server may expand each payload byte to a JSON escape.
	conn.SetReadLimit(int64(len(`\u003c`)*maxPayload + sessionFrameLimit))

	socketCtx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)

	subs, cursor := subscriptions.snapshotForConnection(func() { cancel(errGatewaySubscriptions) })

	frames, err := gatewayHelloFrames(subs, cursor, subscriptions.imported)
	if err != nil {
		return err
	}

	for err == nil {
		if len(frames) > 0 {
			err = conn.Write(socketCtx, websocket.MessageText, frames[0])
			frames = frames[1:]

			continue
		}

		kind, data, readErr := conn.Read(socketCtx)
		if readErr == nil {
			receive(kind, data)
		}

		err = readErr
	}

	if websocket.CloseStatus(err) == tokenRevoked {
		return errGatewayToken
	}

	return context.Cause(socketCtx)
}

func rejectGatewayToken(notify func(channelEvent) bool) error {
	notify((channelNotice{Level: channelWarning, Text: "Gateway token was rejected."}).channelEvent())

	return errGatewayToken
}

func gatewayHelloFrames(subscriptions []filter.Subscription, cursor *uint64, imported bool) ([][]byte, error) {
	const (
		prefix = `{"type":"hello","subscriptions":[`
		final  = `]}`
		more   = `],"more":true}`
	)

	var frames [][]byte

	frame := []byte(prefix)

	switch {
	case cursor != nil:
		frame = strconv.AppendUint([]byte(`{"type":"hello","resume_after":`), *cursor, decimal)
		frame = append(frame, `,"subscriptions":[`...)
	case imported:
		frame = []byte(`{"type":"hello","import":true,"subscriptions":[`)
	}

	for index, sub := range subscriptions {
		suffix := final
		if index+1 < len(subscriptions) {
			suffix = more
		}

		// Encode without HTML escapes so a valid label list fits in one frame.
		item, err := json.Marshal(sub)
		if err != nil || len(prefix)+len(item)+len(suffix) > sessionFrameLimit {
			return nil, errGatewayHello
		}

		if len(frame)+len(item)+len(suffix) > sessionFrameLimit {
			frames = append(frames, append(bytes.TrimSuffix(frame, []byte(",")), more...))
			frame = []byte(prefix)
		}

		frame = append(append(frame, item...), ',')
	}

	return append(frames, append(bytes.TrimSuffix(frame, []byte(",")), final...)), nil
}

type gatewayDeliveries struct {
	ids   map[string]struct{}
	order [gatewayDeliveryLimit]string
	next  int
}

func (d *gatewayDeliveries) remember(id string) {
	delete(d.ids, d.order[d.next])
	d.ids[id] = struct{}{}
	d.order[d.next] = id
	d.next = (d.next + 1) % len(d.order)
}

func forwardGatewayFrame(
	frame sessionObject, subscriptions *mcpSubscriptions, deliveries *gatewayDeliveries, notify func(channelEvent) bool,
) {
	switch *sessionJSON[string](frame[typeKey]) {
	case eventKey:
		event := channelEvent{
			provider:   *sessionJSON[string](frame[channelProviderKey]),
			event:      *sessionJSON[string](frame[eventKey]),
			delivery:   *sessionJSON[string](frame["delivery"]),
			repository: *sessionJSON[*string](frame[channelRepositoryKey]),
			payload:    frame["payload"],
		}
		// Delivery IDs are scoped to their provider, as in the event store.
		deliveryID := event.provider + ":" + event.delivery
		if _, seen := deliveries.ids[deliveryID]; !seen {
			if !notify(event) {
				return
			}

			if event.delivery != "" {
				deliveries.remember(deliveryID)
			}
		}
	case "caught_up":
	case "gap":
		notify(gatewayGap(*sessionJSON[uint64](frame["from"]), *sessionJSON[uint64](frame["to"])).channelEvent())

		return
	default:
		return
	}

	seq, err := strconv.ParseUint(string(frame["seq"]), decimal, 64)
	if err == nil {
		subscriptions.advanceCursor(seq)
	}
}
