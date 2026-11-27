// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
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

type eventPage struct {
	Gap     map[string]uint64 `json:"gap,omitempty"`
	Events  []json.RawMessage `json:"events"`
	LastSeq uint64            `json:"-"`
}

func (page *eventPage) MarshalJSON() ([]byte, error) {
	type fields eventPage

	data, err := json.Marshal((*fields)(page))
	if err != nil {
		return nil, fmt.Errorf("encode event page: %w", err)
	}

	data = strconv.AppendUint(append(data[:len(data)-1], `,"last_seq":`...), page.LastSeq, decimal)

	return append(data, '}'), nil
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

		err = page.fetch(ctx, stream, subject, api.store.orderedConsumer())
		if err != nil {
			response.WriteHeader(http.StatusServiceUnavailable)

			return
		}
	}

	writeReadJSON(response, &page)
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

	data, err := json.Marshal(body)
	if err != nil {
		response.WriteHeader(http.StatusInternalServerError)

		return
	}

	_, err = response.Write(append(data, '\n'))
	if err != nil {
		slog.Default().Debug("JSON response write failed", "error", err)
	}
}

func (page *eventPage) fetch(
	ctx context.Context, stream jetstream.Stream, subject string, open openOrderedConsumer,
) error {
	consumer, err := open(ctx, jetstream.OrderedConsumerConfig{
		OptStartTime: nil,
		ReplayPolicy: 0,
		HeadersOnly:  false,
		Metadata:     nil,
		NamePrefix:   "",

		DeliverPolicy:     jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:       page.LastSeq + 1,
		FilterSubjects:    []string{subject},
		MaxResetAttempts:  1,
		InactiveThreshold: shutdownTimeout,
	})
	if err != nil {
		return fmt.Errorf("open event page consumer: %w", err)
	}

	defer func() {
		deleteOrderedConsumer(ctx, stream, consumer)
	}()

	batch, err := consumer.FetchNoWait(pageLimit)
	if err != nil {
		return fmt.Errorf("fetch event page: %w", err)
	}

	for message := range batch.Messages() {
		meta, metadataErr := message.Metadata()
		if metadataErr != nil {
			return fmt.Errorf("read event metadata: %w", metadataErr)
		}

		page.LastSeq = meta.Sequence.Stream
		page.Events = append(page.Events, sequenceEnvelope(page.LastSeq, message.Data()))
	}

	err = batch.Error()
	if err != nil {
		return fmt.Errorf("read event page: %w", err)
	}

	return nil
}

func sequenceEnvelope(seq uint64, data []byte) json.RawMessage {
	encoded := strconv.AppendUint([]byte(`{"seq":`), seq, decimal)

	return append(append(encoded, ','), data[1:]...)
}

func deleteOrderedConsumer(ctx context.Context, stream jetstream.Stream, consumer jetstream.Consumer) {
	err := stream.DeleteConsumer(ctx, consumer.CachedInfo().Name)
	if err != nil {
		slog.Default().DebugContext(ctx, "ordered consumer cleanup failed", "error", err)
	}
}
