// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:ignore nonamedreturns KeepAlives

import (
	"bytes"
	"cmp"
	"context"
	"encoding/json"
	"encoding/json/jsontext"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"syscall"

	jsonv2 "encoding/json/v2"
)

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

	var endpoint url.URL

	endpoint.Scheme, endpoint.Host = httpScheme, "unix"

	var request http.Request

	request.URL, request.Header = &endpoint, make(http.Header)

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

		body, err := jsonv2.Marshal(
			parkedRetry{Target: flags.Arg(0), Seq: seq},
			jsontext.EscapeForHTML(true),
			jsontext.EscapeForJS(true),
		)
		if err != nil {
			return errParkedRequest
		}

		request.Method, request.URL.Path = http.MethodPost, parkedRetryPath
		request.Body = io.NopCloser(bytes.NewReader(body))
	default:
		return errParkedUsage
	}

	socket := parkedSocketPath(defaultStoreDir, *path)

	return sendParked(ctx, out, socket, &request, send)
}

func sendParked(ctx context.Context, out io.Writer, socket string, request *http.Request, send parkedSend) error {
	ctx, cancel := context.WithTimeout(ctx, writeTimeout)
	defer cancel()

	response, err := send(socket, request.WithContext(ctx))
	if err != nil {
		return parkedRequestError(socket, err)
	}
	defer response.Body.Close()

	return parkedResponse(out, response)
}

func parkedRequestError(socket string, err error) error {
	if errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ECONNREFUSED) {
		return fmt.Errorf("%w (cannot reach %s)", errParkedNotRunning, socket)
	}

	return errParkedRequest
}

func parkedResponse(out io.Writer, response *http.Response) error {
	switch response.StatusCode {
	case http.StatusOK:
		return printParked(out, response.Body)
	case http.StatusNoContent:
		return nil
	default:
		return fmt.Errorf("%w (HTTP %d)", errParkedRequest, response.StatusCode)
	}
}

func requestParked(path string, request *http.Request) (*http.Response, error) {
	var transport http.Transport

	transport.DisableKeepAlives = true
	transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
		return new(net.Dialer).DialContext(ctx, "unix", parkedAddress(path))
	}

	response, err := transport.RoundTrip(request)
	if err != nil {
		return nil, fmt.Errorf("parked socket request: %w", err)
	}

	return response, nil
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

// MarshalJSON preserves the parked stream and CLI field names.
func (record parkedRecord) MarshalJSON() ([]byte, error) {
	type fields parkedRecord

	data, recordErr := jsonv2.Marshal(fields(record), jsontext.EscapeForHTML(true), jsontext.EscapeForJS(true))
	parkedAt, timeErr := json.Marshal(record.ParkedAt)

	data = append(bytes.TrimSuffix(data, []byte("}")), `,"parked_at":`...)
	data = append(data, parkedAt...)
	data = strconv.AppendUint(append(data, `,"seq":`...), record.Seq, decimal)

	return append(data, '}'), errors.Join(recordErr, timeErr)
}

func (record *parkedRecord) UnmarshalJSON(data []byte) error {
	var object map[string]json.RawMessage

	err := json.Unmarshal(data, &object)
	if err != nil {
		return fmt.Errorf("decode parked record fields: %w", err)
	}

	for name, value := range map[string]any{
		"target": &record.Target, "rule": &record.Rule, "provider": &record.Provider,
		eventKey: &record.Event, "delivery": &record.Delivery, "error": &record.Error,
		"seq": &record.Seq, "parked_at": &record.ParkedAt,
	} {
		if raw := object[name]; raw != nil {
			err = json.Unmarshal(raw, value)
			if err != nil {
				return fmt.Errorf("decode parked %s: %w", name, err)
			}
		}
	}

	return nil
}
