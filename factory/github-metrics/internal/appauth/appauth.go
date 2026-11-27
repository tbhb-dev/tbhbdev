// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package appauth supplies tokens for one GitHub App installation.
package appauth

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
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	renewBefore       = 5 * time.Minute
	issuedBefore      = time.Minute
	assertionLifetime = 9 * time.Minute
	maxTokenLifetime  = 2 * time.Hour
	defaultTimeout    = 30 * time.Second
	maxBody           = 1 << 20
)

// ErrAuth reports a failure without exposing credentials or response text.
var ErrAuth = errors.New("github app authentication failed")

// Config identifies one installation. Nil HTTPClient, Now, and Logger fields
// use a client with a 30 second timeout, the system clock, and the default logger.
type Config struct {
	HTTPClient     *http.Client
	Now            func() time.Time
	Logger         *slog.Logger
	BaseURL        string
	PrivateKeyPEM  []byte
	AppID          int64
	InstallationID int64
}

// Source caches tokens. Make one with New.
type Source struct {
	expires    time.Time
	http       *http.Client
	now        func() time.Time
	log        *slog.Logger
	key        *rsa.PrivateKey
	endpoint   string
	token      string
	appID      int64
	generation atomic.Uint64
	mu         sync.Mutex
}

// New parses the private key without sending a request. It accepts an RSA key
// in PKCS1 or PKCS8 form and an absolute HTTPS API base URL. HTTP is allowed
// for localhost, 127.0.0.1, and ::1.
func New(cfg Config) (*Source, error) {
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{
			Transport: nil, CheckRedirect: nil, Jar: nil, Timeout: defaultTimeout,
		}
	}

	if cfg.Now == nil {
		cfg.Now = time.Now
	}

	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}

	base, err := url.Parse(cfg.BaseURL)
	if err != nil || !validBaseURL(base) || cfg.AppID <= 0 || cfg.InstallationID <= 0 {
		cfg.Logger.Error("github app authentication failed", "stage", "config", "status", 0)

		return nil, ErrAuth
	}

	key, err := parseKey(cfg.PrivateKeyPEM)
	if err != nil {
		cfg.Logger.Error("github app authentication failed", "stage", "key", "status", 0)

		return nil, ErrAuth
	}

	client := *cfg.HTTPClient
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

	return &Source{
		http: &client,
		now:  cfg.Now,
		log:  cfg.Logger,
		key:  key,
		endpoint: strings.TrimRight(base.String(), "/") + "/app/installations/" +
			strconv.FormatInt(cfg.InstallationID, 10) + "/access_tokens",
		token:      "",
		expires:    time.Time{},
		appID:      cfg.AppID,
		generation: atomic.Uint64{},
		mu:         sync.Mutex{},
	}, nil
}

func validBaseURL(base *url.URL) bool {
	return base.Host != "" && allowsScheme(base) && base.User == nil &&
		base.RawQuery == "" && !base.ForceQuery && base.Fragment == ""
}

func allowsScheme(base *url.URL) bool {
	switch base.Scheme {
	case "https":
		return true
	case "http":
		switch strings.ToLower(base.Hostname()) {
		case "localhost", "127.0.0.1", "::1":
			return true
		}
	}

	return false
}

func parseKey(data []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, ErrAuth
	}

	var (
		key *rsa.PrivateKey
		err error
	)

	switch block.Type {
	case "RSA PRIVATE KEY":
		key, err = x509.ParsePKCS1PrivateKey(block.Bytes)
	case "PRIVATE KEY":
		var parsed any

		parsed, err = x509.ParsePKCS8PrivateKey(block.Bytes)

		var ok bool

		key, ok = parsed.(*rsa.PrivateKey)
		if !ok {
			return nil, ErrAuth
		}
	default:
		return nil, ErrAuth
	}

	if err != nil || key == nil || key.Validate() != nil {
		return nil, ErrAuth
	}

	return key, nil
}

// Token refreshes the cached token when less than five minutes remain.
// If the refresh fails, Token returns the cached token while it is still valid.
func (s *Source) Token(ctx context.Context) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.token != "" && s.expires.Sub(s.now()) >= renewBefore {
		return s.token, nil
	}

	token, err := s.fetch(ctx)
	if err == nil {
		return token, nil
	}

	if s.token != "" && s.expires.After(s.now()) {
		return s.token, nil
	}

	s.token = ""
	s.expires = time.Time{}

	return "", err
}

// Renew fetches a new token or reuses a valid token fetched while it waited.
func (s *Source) Renew(ctx context.Context) (string, error) {
	generation := s.generation.Load()

	s.mu.Lock()
	defer s.mu.Unlock()

	if generation != s.generation.Load() && s.token != "" && s.expires.After(s.now()) {
		return s.token, nil
	}

	s.token = ""
	s.expires = time.Time{}

	return s.fetch(ctx)
}

func (s *Source) assertion() (string, error) {
	now := s.now()
	claims := struct {
		Issuer   int64 `json:"iss"`
		IssuedAt int64 `json:"iat"`
		Expires  int64 `json:"exp"`
	}{Issuer: s.appID, IssuedAt: now.Add(-issuedBefore).Unix(), Expires: now.Add(assertionLifetime).Unix()}

	body, err := json.Marshal(claims)
	if err != nil {
		return "", ErrAuth
	}

	unsigned := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`)) + "." +
		base64.RawURLEncoding.EncodeToString(body)
	digest := sha256.Sum256([]byte(unsigned))

	signature, err := rsa.SignPKCS1v15(rand.Reader, s.key, crypto.SHA256, digest[:])
	if err != nil {
		return "", ErrAuth
	}

	return unsigned + "." + base64.RawURLEncoding.EncodeToString(signature), nil
}

func (s *Source) failure(ctx context.Context, stage string, status int) (string, error) {
	s.log.ErrorContext(ctx, "github app authentication failed", "stage", stage, "status", status)

	return "", ErrAuth
}

func (s *Source) fetch(ctx context.Context) (string, error) {
	assertion, err := s.assertion()
	if err != nil {
		return s.failure(ctx, "sign", 0)
	}

	const requestBody = `{"permissions":{"metadata":"read","pull_requests":"read"}}`

	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		s.endpoint,
		strings.NewReader(requestBody),
	)
	if err != nil {
		return s.failure(ctx, "request", 0)
	}

	req.Header.Set("Authorization", "Bearer "+assertion)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Github-Api-Version", "2022-11-28")

	res, err := s.http.Do(req)
	if err != nil {
		return s.failure(ctx, "send", 0)
	}

	if res.StatusCode != http.StatusCreated {
		_, err = io.Copy(io.Discard, io.LimitReader(res.Body, maxBody))
		if errors.Join(err, res.Body.Close()) != nil {
			return s.failure(ctx, "read", res.StatusCode)
		}

		return s.failure(ctx, "status", res.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(res.Body, maxBody+1))
	if errors.Join(err, res.Body.Close()) != nil || len(body) > maxBody {
		return s.failure(ctx, "read", res.StatusCode)
	}

	var reply struct {
		ExpiresAt time.Time `json:"expires_at"` //nolint:tagliatelle // GitHub defines this field with an underscore.
		Token     string    `json:"token"`
	}

	now := s.now()
	if json.Unmarshal(body, &reply) != nil || reply.Token == "" ||
		!validExpiry(now, reply.ExpiresAt) {
		return s.failure(ctx, "decode", res.StatusCode)
	}

	s.token = reply.Token
	s.expires = reply.ExpiresAt
	s.generation.Add(1)

	return s.token, nil
}

func validExpiry(now, expiry time.Time) bool {
	return expiry.After(now) && !expiry.After(now.Add(maxTokenLifetime))
}
