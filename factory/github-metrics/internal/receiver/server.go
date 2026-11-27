// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package receiver

import (
	"fmt"
	"net"
	"net/http"
	"time"
)

// The write timeout covers the body read, so it must exceed the read timeout.
const (
	headerTimeout = 5 * time.Second
	readTimeout   = 30 * time.Second
	writeTimeout  = 40 * time.Second
	idleTimeout   = 60 * time.Second
)

// NewServer returns the HTTP server for a listen address with the service's
// timeouts. It turns off net/http's own answer to a star-form OPTIONS request.
// The handler answers it with the same 404 as for any other path.
func NewServer(addr string, handler http.Handler) *http.Server {
	srv := new(http.Server)
	srv.Addr = addr
	srv.Handler = handler
	srv.DisableGeneralOptionsHandler = true
	srv.ReadHeaderTimeout = headerTimeout
	srv.ReadTimeout = readTimeout
	srv.WriteTimeout = writeTimeout
	srv.IdleTimeout = idleTimeout

	return srv
}

// HealthURL returns the URL that probes the health endpoint of a server
// listening on a given address. A wildcard or empty host probes the loopback address of
// the same family, and any other host is probed as given.
func HealthURL(addr string) (string, error) {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return "", fmt.Errorf("parse listen address: %w", err)
	}

	switch host {
	case "", "0.0.0.0":
		host = "127.0.0.1"
	case "::":
		host = "::1"
	}

	return "http://" + net.JoinHostPort(host, port) + "/healthz", nil
}
