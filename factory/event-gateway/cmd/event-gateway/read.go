// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"

	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel/attribute"
)

const (
	eventsPath = "/v1/events"
	pageLimit  = 10
	decimal    = 10
)

type readAPI struct {
	tokens    *readTokens
	store     eventStore
	telemetry telemetry
}

//nolint:tagliatelle // Use the same wire naming as received_at in the envelope.
type eventPage struct {
	Gap     map[string]uint64 `json:"gap,omitempty"`
	Events  []json.RawMessage `json:"events"`
	LastSeq uint64            `json:"last_seq"`
}

func (api readAPI) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET "+eventsPath, api.list)
	mux.HandleFunc("GET "+eventsPath+"/{seq}", api.get)

	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		name := api.tokens.authenticate(request.Header.Get("Authorization"))
		if name == "" {
			response.Header().Set("WWW-Authenticate", "Bearer")
			response.WriteHeader(http.StatusUnauthorized)

			return
		}

		ctx, span := api.telemetry.tracer.Start(request.Context(), "read events")
		defer span.End()

		span.SetAttributes(attribute.String("token.name", name))
		api.telemetry.logger.InfoContext(ctx, "authenticated read request", "token.name", name)
		mux.ServeHTTP(response, request.WithContext(ctx))
	})
}

func (api readAPI) list(response http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	after, err := strconv.ParseUint(cmp.Or(query.Get("after"), "0"), decimal, 64)
	provider, event := query.Get("provider"), query.Get("event")

	if err != nil || strings.Contains(provider, ".") || strings.ContainsAny(provider+event, "*> \t\r\n") ||
		event != "" && strings.Contains("."+event+".", "..") {
		response.WriteHeader(http.StatusBadRequest)

		return
	}

	subject := "events." + cmp.Or(provider, "*") + "." + cmp.Or(event, ">")
	stream := api.store.stream
	ctx := request.Context()

	info, err := stream.Info(ctx)
	if err != nil {
		response.WriteHeader(http.StatusServiceUnavailable)

		return
	}

	page := eventPage{Events: []json.RawMessage{}, LastSeq: after, Gap: nil}
	if after < info.State.LastSeq {
		if after+1 < info.State.FirstSeq {
			page.LastSeq = info.State.FirstSeq - 1
			page.Gap = map[string]uint64{"from": after + 1, "to": page.LastSeq}
		}

		err = page.fetch(ctx, stream, subject)
		if err != nil {
			response.WriteHeader(http.StatusServiceUnavailable)

			return
		}
	}

	writeReadJSON(response, page)
}

func (api readAPI) get(response http.ResponseWriter, request *http.Request) {
	seq, err := strconv.ParseUint(request.PathValue("seq"), decimal, 64)
	switch {
	case err != nil:
		response.WriteHeader(http.StatusBadRequest)
	case seq == 0:
		response.WriteHeader(http.StatusNotFound)
	default:
		api.getSequence(request.Context(), response, seq)
	}
}

func (api readAPI) getSequence(ctx context.Context, response http.ResponseWriter, seq uint64) {
	message, err := api.store.stream.GetMsg(ctx, seq)

	switch {
	case err == nil:
		writeReadJSON(response, sequenceEnvelope(message.Sequence, message.Data))
	case errors.Is(err, jetstream.ErrMsgNotFound):
		info, infoErr := api.store.stream.Info(ctx)
		switch {
		case infoErr != nil:
			response.WriteHeader(http.StatusServiceUnavailable)
		case seq < info.State.FirstSeq:
			response.WriteHeader(http.StatusGone)
		default:
			response.WriteHeader(http.StatusNotFound)
		}
	default:
		response.WriteHeader(http.StatusServiceUnavailable)
	}
}

func writeReadJSON(response http.ResponseWriter, body any) {
	response.Header().Set("Content-Type", "application/json")
	//nolint:errcheck,errchkjson // Both response types contain only JSON and fixed fields.
	_ = json.NewEncoder(response).Encode(body)
}

//nolint:wrapcheck // The handler only needs to know whether the store returned a page.
func (page *eventPage) fetch(ctx context.Context, stream jetstream.Stream, subject string) error {
	//nolint:exhaustruct_v5 // The ordered consumer starts after the page cursor.
	consumer, err := stream.OrderedConsumer(ctx, jetstream.OrderedConsumerConfig{
		DeliverPolicy:     jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:       page.LastSeq + 1,
		FilterSubjects:    []string{subject},
		MaxResetAttempts:  1,
		InactiveThreshold: shutdownTimeout,
	})
	if err != nil {
		return err
	}

	defer func() {
		//nolint:errcheck // JetStream expires the consumer if deletion fails.
		_ = stream.DeleteConsumer(ctx, consumer.CachedInfo().Name)
	}()

	batch, err := consumer.FetchNoWait(pageLimit)
	if err != nil {
		return err
	}

	for message := range batch.Messages() {
		//nolint:errcheck // Fetch validates message metadata before accepting delivery.
		meta, _ := message.Metadata()
		page.LastSeq = meta.Sequence.Stream
		page.Events = append(page.Events, sequenceEnvelope(page.LastSeq, message.Data()))
	}

	return batch.Error()
}

func sequenceEnvelope(seq uint64, data []byte) json.RawMessage {
	encoded := strconv.AppendUint([]byte(`{"seq":`), seq, decimal)

	return append(append(encoded, ','), data[1:]...)
}
