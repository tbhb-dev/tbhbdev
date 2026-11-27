// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package receiver

import (
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// A busy response must close the connection without waiting for the rest of the body.
func TestRejectBusyClosesConnection(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(resp http.ResponseWriter, req *http.Request) {
		rejectBusy(req.Context(), resp, slog.New(slog.DiscardHandler))
	}))
	t.Cleanup(srv.Close)

	for _, framing := range []string{"Content-Length: 1000", "Transfer-Encoding: chunked"} {
		t.Run(framing, func(t *testing.T) {
			t.Parallel()

			assertBusyClosesConnection(t, srv.Listener.Addr().String(), framing)
		})
	}
}

func assertBusyClosesConnection(t *testing.T, addr, framing string) {
	t.Helper()

	conn, err := new(net.Dialer).DialContext(t.Context(), "tcp", addr)
	if err != nil {
		t.Fatal(err)
	}

	t.Cleanup(func() {
		closeErr := conn.Close()
		if closeErr != nil {
			t.Error(closeErr)
		}
	})

	_, err = io.WriteString(conn, "POST /webhook HTTP/1.1\r\nHost: x\r\n"+framing+"\r\n\r\na")
	if err != nil {
		t.Fatal(err)
	}

	err = conn.SetReadDeadline(time.Now().Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}

	answer, err := io.ReadAll(conn)
	if err != nil {
		t.Errorf("connection stayed open after the 503: %v", err)
	}

	if !strings.HasPrefix(string(answer), "HTTP/1.1 503 ") {
		t.Errorf("answer %q, want a 503", answer)
	}
}
