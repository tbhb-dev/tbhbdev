// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package appauth_test

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/appauth"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
)

var _ ghclient.TokenSource = (*appauth.Source)(nil)

const privateKeyType = "PRIVATE KEY"

func newKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}

	return key
}

func encodeKey(t *testing.T, key *rsa.PrivateKey, pkcs8 bool) []byte {
	t.Helper()

	block := &pem.Block{
		Type:    "RSA PRIVATE KEY",
		Headers: nil,
		Bytes:   x509.MarshalPKCS1PrivateKey(key),
	}
	if pkcs8 {
		data, err := x509.MarshalPKCS8PrivateKey(key)
		if err != nil {
			t.Fatal(err)
		}

		block.Type = privateKeyType
		block.Bytes = data
	}

	return pem.EncodeToMemory(block)
}

func config(t *testing.T, server *httptest.Server, now func() time.Time) appauth.Config {
	t.Helper()

	return appauth.Config{
		HTTPClient:     server.Client(),
		Now:            now,
		Logger:         slog.New(slog.DiscardHandler),
		BaseURL:        server.URL + "/api/v3/",
		PrivateKeyPEM:  encodeKey(t, newKey(t), false),
		AppID:          123,
		InstallationID: 456,
	}
}

func newSource(t *testing.T, cfg appauth.Config) *appauth.Source {
	t.Helper()

	source, err := appauth.New(cfg)
	if err != nil {
		t.Fatal(err)
	}

	return source
}

func decodePart(t *testing.T, part string) []byte {
	t.Helper()

	data, err := base64.RawURLEncoding.DecodeString(part)
	if err != nil {
		t.Fatal("invalid assertion encoding")
	}

	return data
}

func verifyRequest(t *testing.T, req *http.Request) {
	t.Helper()

	if req.Method != http.MethodPost ||
		req.URL.Path != "/api/v3/app/installations/456/access_tokens" {
		t.Error("unexpected method or installation path")
	}

	if req.Header.Get("Accept") != "application/vnd.github+json" ||
		req.Header.Get("Content-Type") != "application/json" ||
		req.Header.Get("X-Github-Api-Version") != "2022-11-28" {
		t.Error("missing API headers")
	}
}

func verifyPermissions(t *testing.T, req *http.Request) {
	t.Helper()

	body, err := io.ReadAll(req.Body)
	if err != nil {
		t.Error("could not read the installation token request body")

		return
	}

	var payload map[string]map[string]string

	if json.Unmarshal(body, &payload) != nil {
		t.Error("invalid installation token request body")

		return
	}

	const readPermission = "read"

	permissions := payload["permissions"]
	if len(payload) != 1 || len(permissions) != 2 ||
		permissions["metadata"] != readPermission || permissions["pull_requests"] != readPermission {
		t.Error("expected only metadata and pull request read permissions")
	}
}

func verifyAssertion(t *testing.T, req *http.Request, key *rsa.PrivateKey, now time.Time) {
	t.Helper()

	verifyRequest(t, req)

	authorization := req.Header.Get("Authorization")
	if !strings.HasPrefix(authorization, "Bearer ") {
		t.Fatal("missing bearer assertion")
	}

	parts := strings.Split(strings.TrimPrefix(authorization, "Bearer "), ".")
	if len(parts) != 3 {
		t.Fatal("invalid assertion parts")
	}

	var header struct {
		Algorithm string `json:"alg"`
		Type      string `json:"typ"`
	}

	if json.Unmarshal(decodePart(t, parts[0]), &header) != nil || header.Algorithm != "RS256" ||
		header.Type != "JWT" {
		t.Error("invalid assertion header")
	}

	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if rsa.VerifyPKCS1v15(
		&key.PublicKey,
		crypto.SHA256,
		digest[:],
		decodePart(t, parts[2]),
	) != nil {
		t.Error("invalid assertion signature")
	}

	verifyClaims(t, parts[1], now)
}

func verifyClaims(t *testing.T, part string, now time.Time) {
	t.Helper()

	var claims map[string]int64

	if json.Unmarshal(decodePart(t, part), &claims) != nil {
		t.Fatal("invalid assertion claims")
	}

	if len(claims) != 3 || claims["iss"] != 123 || claims["iat"] != now.Add(-time.Minute).Unix() ||
		claims["exp"] != now.Add(9*time.Minute).Unix() {
		t.Error("incorrect assertion claims")
	}
}

func TestTokenLifecycle(t *testing.T) {
	t.Parallel()

	key := newKey(t)

	for _, pkcs8 := range []bool{false, true} {
		t.Run(fmt.Sprintf("PKCS8=%t", pkcs8), func(t *testing.T) {
			t.Parallel()

			start := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)
			now := start
			requests := make(chan *http.Request, 3)

			var calls atomic.Int64

			server := installationServer(t, &calls, requests, func() time.Time { return now })

			cfg := config(t, server, func() time.Time { return now })
			cfg.PrivateKeyPEM = encodeKey(t, key, pkcs8)
			source := newSource(t, cfg)

			if calls.Load() != 0 {
				t.Fatal("New sent a request")
			}

			checkToken(t, source.Token, "installation-1")
			verifyAssertion(t, <-requests, key, now)
			checkToken(t, source.Token, "installation-1")

			now = start.Add(55 * time.Minute)

			checkToken(t, source.Token, "installation-1")

			if calls.Load() != 1 {
				t.Fatal("cache missed with at least five minutes left")
			}

			now = now.Add(time.Second)

			checkToken(t, source.Token, "installation-2")
			verifyAssertion(t, <-requests, key, now)
			checkToken(t, source.Renew, "installation-3")
			verifyAssertion(t, <-requests, key, now)
			checkToken(t, source.Token, "installation-3")

			if calls.Load() != 3 {
				t.Error("unexpected request count")
			}
		})
	}
}

func installationServer(
	t *testing.T, calls *atomic.Int64, requests chan<- *http.Request, now func() time.Time,
) *httptest.Server {
	t.Helper()

	server := httptest.NewServer(
		http.HandlerFunc(func(resp http.ResponseWriter, req *http.Request) {
			verifyPermissions(t, req)

			count := calls.Add(1)

			requests <- req

			resp.WriteHeader(http.StatusCreated)

			// The response grants fewer permissions than the request asks for.
			writeResponsef(
				t, resp,
				`{"token":"installation-%d","expires_at":%q,"permissions":{"metadata":"read"}}`,
				count,
				now().Add(time.Hour).Format(time.RFC3339),
			)
		}),
	)
	t.Cleanup(server.Close)

	return server
}

func writeResponsef(t *testing.T, resp http.ResponseWriter, format string, args ...any) {
	t.Helper()

	_, err := fmt.Fprintf(resp, format, args...)
	if err != nil {
		t.Error("write response failed")
	}
}

func checkToken(t *testing.T, token func(context.Context) (string, error), want string) {
	t.Helper()

	got, err := token(t.Context())
	if err != nil || got != want {
		t.Fatal("token call didn't return the expected value")
	}
}
