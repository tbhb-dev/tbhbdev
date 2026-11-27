// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:ignore nonamedreturns KeepAlives

import (
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

const (
	parkedStream     = "PARKED"
	parkedPrefix     = "parked."
	parkedMaxAge     = 30 * 24 * time.Hour
	parkTimeout      = 10 * time.Second
	parkedErrorCap   = 512
	parkedListSize   = 100
	parkedPayloadMin = 8
)

var errParkedLimit = errors.New("parked list limit must not be negative")

// RuleError identifies the rule that caused a delivery error.
// Err must be safe to persist and must omit the envelope and payload.
type RuleError struct {
	Err  error
	Rule string
}

func (e RuleError) Error() string    { return e.Err.Error() }
func (e RuleError) Unwrap() error    { return e.Err }
func (e RuleError) ruleName() string { return e.Rule }

type parkedRecord struct {
	Target   string `json:"target"`
	Rule     string `json:"rule"`
	Provider string `json:"provider"`
	Event    string `json:"event"`
	Delivery string `json:"delivery"`
	Error    string `json:"error"`
	ParkedAt string `json:"-"`
	Seq      uint64 `json:"-"`
}

func openParkedStream(ctx context.Context, jet jetstream.JetStream) error {
	var config jetstream.StreamConfig

	config.Name = parkedStream
	config.Subjects = []string{parkedPrefix + ">"}
	config.Storage = jetstream.FileStorage
	config.Replicas = 1
	config.MaxAge = parkedMaxAge

	_, err := jet.CreateOrUpdateStream(ctx, config)
	if err != nil {
		return fmt.Errorf("configure PARKED stream: %w", err)
	}

	return nil
}

// parkHook takes validated target names and delivery errors safe to persist.
func parkHook(
	jet jetstream.JetStream, signals telemetry, timeout time.Duration,
) func(context.Context, string, Event, error) {
	return func(ctx context.Context, target string, event Event, deliveryErr error) {
		record := newParkedRecord(target, event, deliveryErr)

		data, err := json.Marshal(record)
		if err != nil {
			signals.logger.ErrorContext(ctx, "encode parked record")

			return
		}

		// The runner has already acknowledged the event, even during shutdown.
		publish, cancel := context.WithTimeout(context.WithoutCancel(ctx), cmp.Or(timeout, parkTimeout))
		defer cancel()

		_, err = jet.Publish(publish, parkedPrefix+target, data, jetstream.WithExpectStream(parkedStream))

		counter, level, message := signals.parked, slog.LevelWarn, "event parked"
		if err != nil {
			counter, level, message = signals.parkFailed, slog.LevelError, "park publish failed"
		}

		counter.Add(ctx, 1, metric.WithAttributes(attribute.String("target", target)))
		signals.logger.LogAttrs(ctx, level, message, slog.String("target", target), slog.String("rule", record.Rule),
			slog.Uint64("seq", event.Seq), slog.String("provider", record.Provider), slog.String("event", record.Event))
	}
}

func newParkedRecord(target string, event Event, deliveryErr error) parkedRecord {
	var metadata struct {
		envelope

		Payload json.RawMessage `json:"payload"`
	}

	if json.Unmarshal(event.Envelope, &metadata) != nil {
		metadata.Provider, metadata.Event, metadata.Delivery = "", "", ""
	}

	details := parkedErrorDetails(deliveryErr)

	message := details.message
	if len(event.Envelope) > 0 {
		message = strings.ReplaceAll(message, string(event.Envelope), "[redacted]")
	}

	if len(metadata.Payload) >= parkedPayloadMin {
		message = strings.ReplaceAll(message, string(metadata.Payload), "[redacted]")
	}

	return parkedRecord{
		Target: target, Rule: details.name, Seq: event.Seq, Provider: metadata.Provider,
		Event: metadata.Event, Delivery: metadata.Delivery,
		Error:    strings.ToValidUTF8(message[:min(len(message), parkedErrorCap)], ""),
		ParkedAt: time.Now().UTC().Format(time.RFC3339),
	}
}

type parkedError struct{ name, message string }

func parkedErrorDetails(deliveryErr error) parkedError {
	var details parkedError

	func() {
		defer func() {
			if recover() != nil {
				details.message = ErrPermanent.Error()
			}
		}()

		var rule interface{ ruleName() string }
		if errors.As(deliveryErr, &rule) {
			details.name = rule.ruleName()
		}

		details.message = deliveryErr.Error()
	}()

	return details
}

func listParked(ctx context.Context, stream jetstream.Stream, target string, limit int) ([]parkedRecord, error) {
	if limit < 0 {
		return nil, errParkedLimit
	}

	var config jetstream.OrderedConsumerConfig

	config.FilterSubjects = []string{parkedPrefix + cmp.Or(target, ">")}
	config.MaxResetAttempts = 1
	config.InactiveThreshold = shutdownTimeout

	consumer, err := stream.OrderedConsumer(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("create parked consumer: %w", err)
	}

	defer func() {
		deleteOrderedConsumer(ctx, stream, consumer)
	}()

	return fetchParked(consumer.FetchNoWait, cmp.Or(limit, parkedListSize))
}

func fetchParked(fetch func(int) (jetstream.MessageBatch, error), limit int) ([]parkedRecord, error) {
	batch, err := fetch(limit)
	if err != nil {
		return nil, fmt.Errorf("fetch parked records: %w", err)
	}

	records := []parkedRecord{}

	for message := range batch.Messages() {
		var record parkedRecord

		decodeErr := json.Unmarshal(message.Data(), &record)
		if decodeErr != nil {
			return nil, fmt.Errorf("decode parked record: %w", decodeErr)
		}

		records = append(records, record)
	}

	return records, errors.Join(batch.Error())
}

func retryParked(ctx context.Context, store eventStore, target Target, seq uint64) error {
	message, err := store.stream.GetMsg(ctx, seq)
	if err != nil {
		return fmt.Errorf("read event sequence %d from EVENTS: %w", seq, err)
	}

	return deliverEvent(ctx, target, Event{Seq: message.Sequence, Envelope: message.Data})
}

const (
	parkedListPath       = "/parked"
	parkedRetryPath      = "/retry"
	parkedRetryBodyLimit = 1024
)

type parkedAPI struct {
	store   eventStore
	targets []runnerTarget
}

type parkedRetry struct {
	Target string `json:"target"`
	Seq    uint64 `json:"seq"`
}

func (api parkedAPI) handler() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET "+parkedListPath, api.list)
	mux.HandleFunc("POST "+parkedRetryPath, api.retry)

	return mux
}

func (api parkedAPI) list(response http.ResponseWriter, request *http.Request) {
	query := request.URL.Query()
	target := query.Get("target")
	limit, err := strconv.Atoi(cmp.Or(query.Get("limit"), "0"))

	if err != nil || limit < 0 {
		http.Error(response, "invalid parked list query", http.StatusBadRequest)

		return
	}

	stream, err := api.store.jet.Stream(request.Context(), parkedStream)
	if err == nil {
		records, listErr := listParked(request.Context(), stream, target, limit)
		if listErr == nil {
			writeReadJSON(response, records)

			return
		}
	}

	http.Error(response, "parked list unavailable", http.StatusServiceUnavailable)
}

func (api parkedAPI) retry(response http.ResponseWriter, request *http.Request) {
	var input parkedRetry

	body, readErr := io.ReadAll(http.MaxBytesReader(response, request.Body, parkedRetryBodyLimit))
	if errors.Join(readErr, json.Unmarshal(body, &input)) != nil || input.Target == "" || input.Seq == 0 {
		http.Error(response, "invalid retry request", http.StatusBadRequest)

		return
	}

	for _, target := range api.targets {
		if target.Name() != input.Target {
			continue
		}

		if retryParked(request.Context(), api.store, target.Target, input.Seq) != nil {
			http.Error(response, "retry failed", http.StatusInternalServerError)
		} else {
			response.WriteHeader(http.StatusNoContent)
		}

		return
	}

	http.Error(response, "unknown target", http.StatusNotFound)
}
