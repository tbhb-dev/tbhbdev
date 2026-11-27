// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:ignore nonamedreturns KeepAlives

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
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

//nolint:tagliatelle // The record uses the gateway protocol's timestamp naming.
type parkedRecord struct {
	Target   string `json:"target"`
	Rule     string `json:"rule"`
	Provider string `json:"provider"`
	Event    string `json:"event"`
	Delivery string `json:"delivery"`
	Error    string `json:"error"`
	ParkedAt string `json:"parked_at"`
	Seq      uint64 `json:"seq"`
}

//nolint:ireturn // JetStream exposes streams through its Stream interface.
func openParkedStream(ctx context.Context, jet jetstream.JetStream) (jetstream.Stream, error) {
	//nolint:exhaustruct_v5 // The stream uses JetStream defaults for the other options.
	stream, err := jet.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:     parkedStream,
		Subjects: []string{parkedPrefix + ">"},
		Storage:  jetstream.FileStorage,
		Replicas: 1,
		MaxAge:   parkedMaxAge,
	})
	if err != nil {
		return nil, fmt.Errorf("configure PARKED stream: %w", err)
	}

	return stream, nil
}

// parkHook takes validated target names and delivery errors safe to persist.
func parkHook(
	jet jetstream.JetStream, signals telemetry, timeout time.Duration,
) func(context.Context, string, Event, error) {
	return func(ctx context.Context, target string, event Event, deliveryErr error) {
		record := newParkedRecord(target, event, deliveryErr)
		//nolint:errcheck // The record contains only strings and a sequence.
		data, _ := json.Marshal(record)

		// The runner has already acknowledged the event, even during shutdown.
		publish, cancel := context.WithTimeout(context.WithoutCancel(ctx), cmp.Or(timeout, parkTimeout))
		defer cancel()

		_, err := jet.Publish(publish, parkedPrefix+target, data, jetstream.WithExpectStream(parkedStream))

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

	name, message := parkedErrorDetails(deliveryErr)
	if len(event.Envelope) > 0 {
		message = strings.ReplaceAll(message, string(event.Envelope), "[redacted]")
	}

	if len(metadata.Payload) >= parkedPayloadMin {
		message = strings.ReplaceAll(message, string(metadata.Payload), "[redacted]")
	}

	return parkedRecord{
		Target: target, Rule: name, Seq: event.Seq, Provider: metadata.Provider,
		Event: metadata.Event, Delivery: metadata.Delivery,
		Error:    strings.ToValidUTF8(message[:min(len(message), parkedErrorCap)], ""),
		ParkedAt: time.Now().UTC().Format(time.RFC3339),
	}
}

//nolint:nonamedreturns // Recovery sets the message if a delivery error method panics.
func parkedErrorDetails(deliveryErr error) (name, message string) {
	defer func() {
		if recover() != nil {
			message = ErrPermanent.Error()
		}
	}()

	var rule interface{ ruleName() string }
	if errors.As(deliveryErr, &rule) {
		name = rule.ruleName()
	}

	return name, deliveryErr.Error()
}

//nolint:wrapcheck // Report pull batch errors directly to the caller.
func listParked(ctx context.Context, stream jetstream.Stream, target string, limit int) ([]parkedRecord, error) {
	if limit < 0 {
		return nil, errParkedLimit
	}

	//nolint:exhaustruct_v5 // The ordered consumer reads from the oldest retained record.
	consumer, err := stream.OrderedConsumer(ctx, jetstream.OrderedConsumerConfig{
		FilterSubjects:    []string{parkedPrefix + cmp.Or(target, ">")},
		MaxResetAttempts:  1,
		InactiveThreshold: shutdownTimeout,
	})
	if err != nil {
		return nil, fmt.Errorf("create parked consumer: %w", err)
	}

	defer func() {
		//nolint:errcheck // JetStream expires the consumer if deletion fails.
		_ = stream.DeleteConsumer(ctx, consumer.CachedInfo().Name)
	}()

	batch, err := consumer.FetchNoWait(cmp.Or(limit, parkedListSize))
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

	return records, batch.Error()
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

const (
	parkedCommand      = "parked"
	parkedListCommand  = "list"
	parkedRetryCommand = "retry"
	parkedSocketEnv    = "EVENT_GATEWAY_PARKED_SOCKET"
	httpScheme         = "http"
)

var (
	errParkedUsage      = errors.New("usage: event-gateway parked list [flags] or parked retry [flags] TARGET SEQ")
	errParkedRequest    = errors.New("parked request failed")
	errParkedNotRunning = errors.New("event-gateway is not running")
)

func parkedSocketPath(storeDir, path string) string {
	return cmp.Or(path, filepath.Join(storeDir, "parked.sock"))
}

// Keep relative paths that start with @ in the filesystem namespace.
func parkedAddress(path string) string { return filepath.Dir(path) + "/" + filepath.Base(path) }

type parkedSend func(string, *http.Request) (*http.Response, error)

func runParked(ctx context.Context, out io.Writer, args []string, send parkedSend) error {
	if len(args) == 0 {
		return errParkedUsage
	}

	flags := flag.NewFlagSet(parkedCommand, flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	path := flags.String("socket", os.Getenv(parkedSocketEnv), "parked API Unix socket path")
	//nolint:exhaustruct_v5 // The client needs only the method, URL and optional body.
	request := &http.Request{URL: &url.URL{Scheme: httpScheme, Host: "unix"}, Header: http.Header{}}

	switch args[0] {
	case parkedListCommand:
		target := flags.String("target", "", "target name")
		limit := flags.Int("limit", 0, "maximum records")

		if flags.Parse(args[1:]) != nil || flags.NArg() != 0 {
			return errParkedUsage
		}

		request.Method, request.URL.Path = http.MethodGet, parkedListPath
		request.URL.RawQuery = url.Values{"target": {*target}, "limit": {strconv.Itoa(*limit)}}.Encode()
	case parkedRetryCommand:
		if flags.Parse(args[1:]) != nil || flags.NArg() != 2 {
			return errParkedUsage
		}

		seq, err := strconv.ParseUint(flags.Arg(1), decimal, 64)
		if err != nil {
			return errParkedUsage
		}

		//nolint:errcheck,errchkjson // The body contains only a string and a sequence.
		body, _ := json.Marshal(parkedRetry{Target: flags.Arg(0), Seq: seq})
		request.Method, request.URL.Path = http.MethodPost, parkedRetryPath
		request.Body = io.NopCloser(bytes.NewReader(body))
	default:
		return errParkedUsage
	}

	socket := parkedSocketPath(defaultStoreDir, *path)

	ctx, cancel := context.WithTimeout(ctx, writeTimeout)
	defer cancel()

	//nolint:bodyclose // parkedResponse closes the response body.
	response, err := send(socket, request.WithContext(ctx))

	return parkedResponse(out, socket, response, err)
}

func parkedResponse(out io.Writer, socket string, response *http.Response, err error) error {
	switch {
	case errors.Is(err, os.ErrNotExist), errors.Is(err, syscall.ECONNREFUSED):
		return fmt.Errorf("%w (cannot reach %s)", errParkedNotRunning, socket)
	case err != nil:
		return errParkedRequest
	}

	//nolint:errcheck // Closing the body is cleanup after receiving the response.
	defer func() { _ = response.Body.Close() }()

	switch response.StatusCode {
	case http.StatusOK:
		return printParked(out, response.Body)
	case http.StatusNoContent:
		return nil
	default:
		return fmt.Errorf("%w (HTTP %d)", errParkedRequest, response.StatusCode)
	}
}

//nolint:wrapcheck // Caller maps transport errors to fixed messages.
func requestParked(path string, request *http.Request) (*http.Response, error) {
	//nolint:exhaustruct_v5 // One request per invocation, using only the local socket.
	transport := http.Transport{
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return new(net.Dialer).DialContext(ctx, "unix", parkedAddress(path))
		},
	}

	return transport.RoundTrip(request)
}

func printParked(out io.Writer, body io.Reader) error {
	var records []parkedRecord
	if json.NewDecoder(body).Decode(&records) != nil {
		return errParkedRequest
	}

	encoder := json.NewEncoder(out)
	for _, record := range records {
		err := encoder.Encode(record)
		if err != nil {
			return fmt.Errorf("write parked record: %w", err)
		}
	}

	return nil
}
