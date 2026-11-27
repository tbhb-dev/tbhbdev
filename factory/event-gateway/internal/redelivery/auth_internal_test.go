// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package redelivery

import (
	"bytes"
	"crypto"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/iotest"
	"time"
)

const (
	installationPath = "/app/installations/42/access_tokens"
	secretToken      = "test-installation-secret"
	secretKey        = "test-key-material"
	testAppID        = "12345"
)

func TestTokenCacheAndJWT(t *testing.T) {
	t.Parallel()

	key := testKey(t)
	start := time.Unix(0, 0).UTC()
	now := start

	var calls atomic.Int32

	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodPost || req.URL.RequestURI() != installationPath || req.ContentLength != 0 ||
			req.Header.Get("Accept") != "application/vnd.github+json" || req.UserAgent() != "event-gateway" ||
			req.Header.Get("X-Github-Api-Version") != "2022-11-28" {
			t.Error("unexpected installation token request")
		}

		call := calls.Add(1)
		if call == 1 {
			verifyJWT(t, req.Header.Get("Authorization"), &key.PublicKey, start)
		}

		writer.WriteHeader(http.StatusCreated)

		_, err := io.Copy(
			writer,
			tokenResponse(fmt.Sprint(secretToken, call), start.Add(time.Duration(call)*time.Hour)),
		)
		if err != nil {
			t.Error(err)
		}
	}))
	t.Cleanup(server.Close)

	file := keyFile(t, keyPEM("RSA PRIVATE KEY", x509.MarshalPKCS1PrivateKey(key)))
	clock := func() time.Time { return now }
	client, err := NewClient(Options{server.Client(), clock, testAppID, file, server.URL + "/", 42})
	noError(t, err)

	check := func(want int32) {
		t.Helper()

		token, tokenErr := client.Token(t.Context())
		if tokenErr != nil || token != fmt.Sprint(secretToken, want) || calls.Load() != want {
			t.Fatalf("token exchange %d failed: %v", want, tokenErr)
		}
	}
	check(1)
	check(1)

	now = start.Add(58 * time.Minute)

	check(2)

	client.Invalidate()
	check(3)

	checkCredentialFormatting(t, client)

	checkConcurrentTokens(t, client)
}

func checkCredentialFormatting(t *testing.T, client *Client) {
	t.Helper()

	for _, value := range []any{client, *client, client.cache.token} {
		if fmt.Sprintf("%v %+v %#v", value, value, value) != strings.Repeat(redacted+" ", 2)+redacted ||
			slog.AnyValue(value).Resolve().String() != redacted {
			t.Fatal("formatter or logger exposed credentials")
		}
	}
}

func checkConcurrentTokens(t *testing.T, client *Client) {
	t.Helper()

	var group sync.WaitGroup
	for range 16 {
		group.Go(func() {
			client.Invalidate()

			token, err := client.Token(t.Context())
			if token == "" || err != nil {
				t.Error("expected a token and no error")
			}
		})
	}

	group.Wait()
}

func verifyJWT(t *testing.T, auth string, key *rsa.PublicKey, now time.Time) {
	t.Helper()

	encoding := base64.RawURLEncoding
	claims := fmt.Sprintf(`{"iss":%q,"iat":%d,"exp":%d}`, testAppID, now.Unix()-60, now.Unix()+540)
	header := encoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	unsigned := header + "." + encoding.EncodeToString([]byte(claims))
	sig, ok := strings.CutPrefix(auth, "Bearer "+unsigned+".")
	decoded, err := encoding.DecodeString(sig)
	digest := sha256.Sum256([]byte(unsigned))

	if !ok || err != nil || rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], decoded) != nil {
		t.Error("JWT header, claims, or signature did not match")
	}
}

func TestKeyErrors(t *testing.T) {
	t.Parallel()

	der := pkcs8(t, testKey(t))
	key := keyPEM("PRIVATE KEY", der)
	nonRSA := ed25519.NewKeyFromSeed(make([]byte, ed25519.SeedSize))
	encrypted := &pem.Block{Type: "PRIVATE KEY", Bytes: der, Headers: map[string]string{"Proc-Type": "4,ENCRYPTED"}}

	for name, data := range map[string][]byte{
		"invalid RSA":       keyPEM("RSA PRIVATE KEY", []byte(secretKey)),
		"invalid PKCS8":     keyPEM("PRIVATE KEY", []byte(secretKey)),
		"non-RSA":           keyPEM("PRIVATE KEY", pkcs8(t, nonRSA)),
		"encrypted header":  pem.EncodeToMemory(encrypted),
		"extra key":         bytes.Repeat(key, 2),
		"extra certificate": append(bytes.Clone(key), keyPEM("CERTIFICATE", der)...),
		"trailing data":     append(bytes.Clone(key), secretKey...),
		"wrong type":        keyPEM("PUBLIC KEY", der),
		"malformed":         []byte("invalid\n" + secretKey),
		"empty":             nil,
	} {
		path := keyFile(t, data)

		_, err := NewClient(Options{nil, nil, "", path, "", 0})
		if err == nil || err.Error() != "parse GitHub App RSA key "+path {
			t.Errorf("%s: expected a path-only parse error", name)
		}
	}

	dir := t.TempDir()
	missing := filepath.Join(dir, "missing.pem")

	for _, path := range []string{dir, missing} {
		_, err := NewClient(Options{nil, nil, "", path, "", 0})
		if err == nil || !strings.Contains(err.Error(), path) || errors.Is(err, fs.ErrNotExist) != (path == missing) {
			t.Fatal("expected a path-only read error with the correct cause")
		}
	}
}

func TestExchangeErrors(t *testing.T) {
	t.Parallel()

	now := time.Unix(0, 0).UTC()
	key := keyPEM("PRIVATE KEY", pkcs8(t, testKey(t)))
	client, err := NewClient(Options{nil, nil, testAppID, keyFile(t, append(key, " \t\n"...)), ":", 42})
	noError(t, err)

	client.options.Now = func() time.Time { return now }
	assertTokenError(t, client, "invalid URL", "create token request")

	client.options.BaseURL = "https://example.test"

	badExpiry := `{"token":"` + secretToken + `","expires_at":"` + secretKey + `"}`
	for _, test := range []struct {
		body   io.Reader
		name   string
		status int
	}{
		{strings.NewReader(secretToken), "unauthorized", http.StatusUnauthorized},
		{strings.NewReader(secretToken), "forbidden", http.StatusForbidden},
		{strings.NewReader(secretToken), "redirect", http.StatusFound},
		{strings.NewReader(secretKey), "invalid JSON", http.StatusCreated},
		{tokenResponse("", now.Add(time.Hour)), "missing token", http.StatusCreated},
		{strings.NewReader(`{"token":"` + secretToken + `"}`), "missing expiry", http.StatusCreated},
		{strings.NewReader(badExpiry), "unparsable expiry", http.StatusCreated},
		{tokenResponse(secretToken, now.Add(-time.Hour)), "expired", http.StatusCreated},
		{tokenResponse(secretToken, now.Add(time.Minute)), "inside margin", http.StatusCreated},
		{tokenResponse(secretToken, now.Add(2*time.Minute)), "at margin", http.StatusCreated},
		{strings.NewReader(strings.Repeat(" ", responseLimit+1)), "oversized", http.StatusCreated},
		{iotest.ErrReader(io.ErrUnexpectedEOF), "read error", http.StatusCreated},
	} {
		client.options.HTTPClient.Transport = roundTripFunc(func(req *http.Request) (*http.Response, error) {
			if req.URL.Path != installationPath {
				t.Error("token request followed a redirect")
			}

			response := httptest.NewRecorder()
			response.Header().Set("Location", "/other")
			response.WriteHeader(test.status)

			result := response.Result()
			result.Body = io.NopCloser(test.body)

			return result, nil
		})

		assertTokenError(t, client, test.name, fmt.Sprintf("HTTP %d", test.status))
	}

	client.options.HTTPClient.Transport = roundTripFunc(func(req *http.Request) (*http.Response, error) {
		return nil, fmt.Errorf("%w %s", authError(secretToken), req.Header.Get("Authorization"))
	})
	assertTokenError(t, client, "transport", "send token request")
	client.key = new(rsa.PrivateKey)
	assertTokenError(t, client, "sign", "sign GitHub App JWT")
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func tokenResponse(token string, expiry time.Time) *strings.Reader {
	return strings.NewReader(fmt.Sprintf(`{"token":%q,"expires_at":%q}`, token, expiry.Format(time.RFC3339)))
}

func assertTokenError(t *testing.T, client *Client, name, detail string) {
	t.Helper()

	var empty installationToken

	token, err := client.Token(t.Context())
	if token != "" || client.cache.token != empty || err == nil ||
		strings.TrimSuffix(err.Error(), " invalid token response") != "POST "+installationPath+": "+detail {
		t.Errorf("%s: expected a redacted exchange error without a returned or cached token", name)
	}
}

func pkcs8(t *testing.T, key any) []byte {
	t.Helper()

	der, err := x509.MarshalPKCS8PrivateKey(key)
	noError(t, err)

	return der
}

func testKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	noError(t, err)

	return key
}

func keyPEM(kind string, der []byte) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: kind, Bytes: der, Headers: nil})
}

func keyFile(t *testing.T, data []byte) string {
	t.Helper()

	path := filepath.Join(t.TempDir(), "app.pem")
	noError(t, os.WriteFile(path, data, 0o600))

	return path
}

func noError(t *testing.T, err error) {
	t.Helper()

	if err != nil {
		t.Fatal(err)
	}
}
