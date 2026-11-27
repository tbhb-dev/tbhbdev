// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package redelivery supports GitHub webhook redelivery.
package redelivery

import (
	"bytes"
	"cmp"
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	redacted      = "[redacted]"
	responseLimit = 1 << 20
	refreshBefore = 2 * time.Minute
	jwtLifetime   = 9 * time.Minute
)

// Options configures GitHub App authentication. An empty BaseURL uses
// https://api.github.com. A nil HTTPClient uses http.DefaultClient, and a nil
// Now uses time.Now.
type Options struct {
	HTTPClient     *http.Client
	Now            func() time.Time
	AppID          string
	KeyFile        string
	BaseURL        string
	InstallationID int64
}

// Client exchanges GitHub App credentials for installation tokens.
type Client struct {
	key     *rsa.PrivateKey
	cache   *tokenCache
	path    string
	options Options
}

// authError leaves the underlying error out of messages that could carry credentials.
type authError string

func (e authError) Error() string { return string(e) }

type tokenCache struct {
	token installationToken
	mu    sync.Mutex
}

type installationToken struct {
	ExpiresAt time.Time
	Token     string
}

func decodeToken(data []byte, token *installationToken) bool {
	var object map[string]json.RawMessage
	if json.Unmarshal(data, &object) != nil {
		return false
	}

	for name, field := range map[string]any{"token": &token.Token, "expires_at": &token.ExpiresAt} {
		if raw := object[name]; raw != nil && json.Unmarshal(raw, field) != nil {
			return false
		}
	}

	return true
}

func (installationToken) String() string       { return redacted }
func (installationToken) GoString() string     { return redacted }
func (installationToken) LogValue() slog.Value { return slog.StringValue(redacted) }

// NewClient loads the RSA private key from disk once.
func NewClient(options Options) (*Client, error) {
	data, err := os.ReadFile(options.KeyFile)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, fmt.Errorf("read GitHub App key %s: %w", options.KeyFile, fs.ErrNotExist)
	}

	if err != nil {
		return nil, fmt.Errorf("%w %s", authError("read GitHub App key"), options.KeyFile)
	}

	key := parseKey(data)
	if key == nil {
		return nil, fmt.Errorf("%w %s", authError("parse GitHub App RSA key"), options.KeyFile)
	}

	if options.Now == nil {
		options.Now = time.Now
	}

	client := *cmp.Or(options.HTTPClient, http.DefaultClient)
	// An installation JWT is only sent to the configured token endpoint.
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

	options.HTTPClient = &client
	options.BaseURL = strings.TrimRight(cmp.Or(options.BaseURL, "https://api.github.com"), "/")

	return &Client{
		key: key, cache: new(tokenCache), options: options,
		path: fmt.Sprintf("/app/installations/%d/access_tokens", options.InstallationID),
	}, nil
}

// String redacts credentials in formatted output.
func (Client) String() string { return redacted }

// GoString redacts credentials in Go-syntax output.
func (Client) GoString() string { return redacted }

// LogValue redacts credentials in structured logs.
func (Client) LogValue() slog.Value { return slog.StringValue(redacted) }

func parseKey(data []byte) *rsa.PrivateKey {
	block, rest := pem.Decode(data)
	if block == nil || len(bytes.TrimSpace(rest)) != 0 || len(block.Headers) != 0 {
		return nil
	}

	switch block.Type {
	case "RSA PRIVATE KEY":
		key, err := x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return nil
		}

		return key
	case "PRIVATE KEY":
		key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil
		}

		rsaKey, ok := key.(*rsa.PrivateKey)
		if !ok {
			return nil
		}

		return rsaKey
	}

	return nil
}

// Invalidate drops the cached installation token.
func (c Client) Invalidate() {
	c.cache.mu.Lock()
	defer c.cache.mu.Unlock()

	c.cache.token = installationToken{ExpiresAt: time.Time{}, Token: ""}
}

// Token returns an installation token, refreshing it two minutes before expiry.
func (c Client) Token(ctx context.Context) (string, error) {
	cache := c.cache
	cache.mu.Lock()
	defer cache.mu.Unlock()

	now := c.options.Now()
	if now.Before(cache.token.ExpiresAt.Add(-refreshBefore)) {
		return cache.token.Token, nil
	}

	jwt, ok := c.sign(now)
	if !ok {
		return "", fmt.Errorf("POST %s: %w", c.path, authError("sign GitHub App JWT"))
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.options.BaseURL+c.path, nil)
	if err != nil {
		return "", fmt.Errorf("POST %s: %w", c.path, authError("create token request"))
	}

	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-Github-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "event-gateway")
	req.Header.Set("Authorization", "Bearer "+jwt)

	res, err := c.options.HTTPClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("POST %s: %w", c.path, authError("send token request"))
	}

	defer res.Body.Close()

	if res.StatusCode != http.StatusCreated {
		return "", fmt.Errorf("POST %s: %w %d", c.path, authError("HTTP"), res.StatusCode)
	}

	data, err := io.ReadAll(io.LimitReader(res.Body, responseLimit+1))

	var token installationToken
	if err != nil || len(data) > responseLimit || !decodeToken(data, &token) ||
		token.Token == "" || token.ExpiresAt.IsZero() || !token.ExpiresAt.After(now.Add(refreshBefore)) {
		return "", fmt.Errorf("POST %s: HTTP %d %w", c.path, res.StatusCode, authError("invalid token response"))
	}

	cache.token = token

	return token.Token, nil
}

func (c Client) sign(now time.Time) (string, bool) {
	const header = `{"alg":"RS256","typ":"JWT"}`

	issuer, encodeErr := json.Marshal(c.options.AppID)

	claims := fmt.Sprintf(
		`{"iss":%s,"iat":%d,"exp":%d}`, issuer, now.Add(-time.Minute).Unix(), now.Add(jwtLifetime).Unix(),
	)
	encoding := base64.RawURLEncoding
	unsigned := encoding.EncodeToString([]byte(header)) + "." + encoding.EncodeToString([]byte(claims))
	digest := sha256.Sum256([]byte(unsigned))
	sig, err := rsa.SignPKCS1v15(nil, c.key, crypto.SHA256, digest[:])

	return unsigned + "." + encoding.EncodeToString(sig), errors.Join(encodeErr, err) == nil
}
