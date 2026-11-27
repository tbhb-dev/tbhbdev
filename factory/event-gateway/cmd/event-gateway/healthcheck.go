// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"errors"
	"flag"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	healthcheckCommand = "healthcheck"
	healthcheckTimeout = 4 * time.Second
	// healthcheckBadHost lists characters that never belong in a bare host.
	healthcheckBadHost = " \t\r\n/?#@\\"
)

var (
	errHealthcheckUsage = errors.New("usage: event-gateway healthcheck [-addr HOST:PORT]")
	errHealthcheck      = errors.New("healthcheck failed")
)

// healthcheck probes GET /healthz on the address and succeeds only on HTTP 200.
// Errors never include the address or the response body.
func healthcheck(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("event-gateway healthcheck", flag.ContinueOnError)
	// Flag parsing errors can quote argument values.
	flags.SetOutput(io.Discard)

	addr := flags.String("addr", "127.0.0.1:8080", "server address to probe")

	if flags.Parse(args) != nil || flags.NArg() != 0 {
		return errHealthcheckUsage
	}

	err := validateHealthcheckAddr(*addr)
	if err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(ctx, healthcheckTimeout)
	defer cancel()

	// The address is already validated, so the URL is built without parsing.
	//nolint:exhaustruct_v5 // A GET request needs only the method, URL and headers.
	req := (&http.Request{
		Method: http.MethodGet,
		URL:    &url.URL{Scheme: "http", Host: *addr, Path: "/healthz"},
		Header: http.Header{},
	}).WithContext(ctx)

	// A redirect is returned as-is, so only a direct 200 counts as healthy.
	//nolint:exhaustruct_v5 // Only the redirect policy differs from the default client.
	client := &http.Client{
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}

	resp, err := client.Do(req)
	if err != nil {
		return errHealthcheck
	}

	//nolint:errcheck // The health check uses only the HTTP status.
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return errHealthcheck
	}

	return nil
}

// validateHealthcheckAddr requires HOST:PORT with a non-empty host without echoing the value.
func validateHealthcheckAddr(addr string) error {
	host, port, err := net.SplitHostPort(addr)
	if err != nil || host == "" || strings.ContainsAny(host, healthcheckBadHost) {
		return errHealthcheckUsage
	}

	number, err := strconv.ParseUint(port, 10, 16)
	if err != nil || number == 0 {
		return errHealthcheckUsage
	}

	return nil
}
