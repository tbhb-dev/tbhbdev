// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"sync/atomic"
)

var (
	errTokenFormat = errors.New("read token file requires at least one name:SHA-256 hex entry")
	tokenEntry     = regexp.MustCompile(`^([^:]+):([a-fA-F0-9]{64})$`)
)

type readTokens struct {
	set  atomic.Pointer[map[[sha256.Size]byte]string]
	path string
}

func (tokens *readTokens) load() error {
	data, err := os.ReadFile(tokens.path)
	if err != nil {
		return fmt.Errorf("read token file: %w", err)
	}

	set := make(map[[sha256.Size]byte]string)

	for line := range strings.SplitSeq(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}

		entry := tokenEntry.FindStringSubmatch(line)
		if entry == nil {
			return errTokenFormat
		}

		hash, decodeErr := hex.DecodeString(entry[2])
		if decodeErr != nil {
			return errTokenFormat
		}

		set[[sha256.Size]byte(hash)] = entry[1]
	}

	if len(set) == 0 {
		return errTokenFormat
	}

	tokens.set.Store(&set)

	return nil
}

func (tokens *readTokens) reload(ctx context.Context, logger *slog.Logger) {
	err := tokens.load()
	if err != nil {
		logger.ErrorContext(ctx, "reload read token file", "error", err)
	}
}

func (tokens *readTokens) authenticate(header string) string {
	fields := strings.Fields(header)
	if len(fields) != 2 || !strings.EqualFold(fields[0], "Bearer") {
		return ""
	}

	digest := sha256.Sum256([]byte(fields[1]))
	name := ""

	for hash, tokenName := range *tokens.set.Load() {
		if subtle.ConstantTimeCompare(digest[:], hash[:]) == 1 {
			name = tokenName
		}
	}

	return name
}
