// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:words awrel

import (
	"cmp"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	mcpSessionKeyEnv     = "EVENT_GATEWAY_SESSION_KEY"
	awrelSessionEnv      = "AWREL_SESSION_ID"
	claudeCodeSessionEnv = "CLAUDE_CODE_SESSION_ID"
	mcpStateHomeEnv      = "XDG_STATE_HOME"
	mcpStateHomeWarning  = "MCP subscription state directory is unavailable."
	mcpStateReadWarning  = "MCP subscription state could not be fully loaded."
	mcpStateWriteWarning = "MCP subscription state could not be saved."
	awrelStateWarning    = "awrel subscriptions could not be fully imported."
	awrelStateLimit      = 1 << 20
	mcpStateVersion      = 1
	mcpStateDirMode      = 0o700
	mcpStateFileMode     = 0o600
)

type mcpState struct {
	logger    *slog.Logger
	writeFile func(*os.File, []byte) (int, error)
	dir       string
	file      string
}

// loadMCPSubscriptions restores the saved set before the gateway sends hello.
// An unset session key leaves subscriptions in memory only.
func loadMCPSubscriptions(ctx context.Context, logger *slog.Logger) *mcpSubscriptions {
	subs := new(mcpSubscriptions)

	key := cmp.Or(os.Getenv(mcpSessionKeyEnv), os.Getenv(awrelSessionEnv), os.Getenv(claudeCodeSessionEnv))
	if key == "" {
		return subs
	}

	dir := os.Getenv(mcpStateHomeEnv)
	if !filepath.IsAbs(dir) {
		dir = filepath.Join(os.Getenv("HOME"), ".local", "state")
	}

	if !filepath.IsAbs(dir) {
		logger.WarnContext(ctx, mcpStateHomeWarning)

		return subs
	}

	file := filepath.Join("sessions", fmt.Sprintf("%x.json", sha256.Sum256([]byte(key))))
	state := mcpState{
		logger:    logger,
		dir:       dir,
		file:      filepath.Join(mcpServerName, file),
		writeFile: (*os.File).Write,
	}

	valid, missing := state.load(subs)
	if missing {
		valid = loadAwrelSubscriptions(ctx, logger, filepath.Join(dir, "awrel"), file, subs)
	}

	if !valid {
		logger.WarnContext(ctx, mcpStateReadWarning)
	}

	subs.state = state
	if subs.imported {
		// Startup owns the subscriptions until they are returned to the caller.
		subs.saveLocked()
	}

	return subs
}

// load reports whether the state is valid and whether its file is missing.
func (state mcpState) load(subs *mcpSubscriptions) (bool, bool) {
	root, err := os.OpenRoot(state.dir)
	if err != nil {
		return false, false
	}
	//nolint:errcheck // Closing the directory handle cannot change the completed file operation.
	defer root.Close()

	data, err := root.ReadFile(state.file)
	if err != nil {
		_, statErr := root.Lstat(state.file)

		return false, errors.Is(statErr, fs.ErrNotExist)
	}

	var saved struct {
		Cursor        *uint64           `json:"cursor"`
		Subscriptions []json.RawMessage `json:"subscriptions"`
		Version       int               `json:"version"`
	}

	if json.Unmarshal(data, &saved) != nil || saved.Version != mcpStateVersion || saved.Subscriptions == nil {
		return false, false
	}

	valid := subs.restore(saved.Subscriptions, len(saved.Subscriptions))
	if valid {
		subs.cursor = saved.Cursor
	}

	return valid, false
}

// loadAwrelSubscriptions reports whether there was state to import or warn about.
func loadAwrelSubscriptions(ctx context.Context, logger *slog.Logger, dir, path string, subs *mcpSubscriptions) bool {
	data, exists := readAwrelState(dir, path)
	if !exists {
		return false
	}

	var saved struct {
		Subscriptions []json.RawMessage `json:"subscriptions"`
		Version       int               `json:"version"`
	}

	valid := false
	if len(data) <= awrelStateLimit && json.Unmarshal(data, &saved) == nil &&
		saved.Version == mcpStateVersion && len(saved.Subscriptions) > 0 {
		valid = subs.restore(saved.Subscriptions, subscriptionLimit)
		subs.imported = len(subs.subs) > 0
	}

	if !valid {
		logger.WarnContext(ctx, awrelStateWarning)
	}

	return true
}

func readAwrelState(dir, path string) ([]byte, bool) {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, !errors.Is(err, fs.ErrNotExist)
	}
	//nolint:errcheck // Closing the directory handle cannot change the completed file operation.
	defer root.Close()

	file, err := root.Open(path)
	if err != nil {
		_, statErr := root.Lstat(path)

		return nil, !errors.Is(statErr, fs.ErrNotExist)
	}
	//nolint:errcheck // The file is open for reading only.
	defer file.Close()

	data, err := io.ReadAll(io.LimitReader(file, awrelStateLimit+1))
	if err != nil {
		return nil, true
	}

	return data, true
}

func (s *mcpSubscriptions) restore(rawSubscriptions []json.RawMessage, limit int) bool {
	valid := true

	for _, raw := range rawSubscriptions {
		if len(s.subs) == limit {
			return false
		}

		sub, parseErr := filter.Parse(raw)
		if parseErr != nil {
			valid = false

			continue
		}

		s.set(sub, true)
	}

	return valid
}

func (s *mcpSubscriptions) advanceCursor(seq uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.cursor == nil || seq > *s.cursor {
		s.cursor = &seq
		s.saveLocked()
	}
}

// saveLocked keeps subscription and cursor writes in the order of their changes.
func (s *mcpSubscriptions) saveLocked() {
	if s.state.file != "" && !s.state.write(s.subs, s.cursor) {
		s.state.logger.Warn(mcpStateWriteWarning)
	}
}

func (state mcpState) write(subs []filter.Subscription, cursor *uint64) bool {
	if subs == nil {
		subs = []filter.Subscription{}
	}

	saved := struct {
		Cursor        *uint64               `json:"cursor,omitempty"`
		Subscriptions []filter.Subscription `json:"subscriptions"`
		Version       int                   `json:"version"`
	}{Version: mcpStateVersion, Subscriptions: subs, Cursor: cursor}
	//nolint:errcheck,errchkjson // The state contains only integers, strings and slices of those types.
	data, _ := json.Marshal(saved)

	//nolint:errcheck // Opening the root checks whether directory creation succeeded.
	_ = os.MkdirAll(state.dir, mcpStateDirMode)

	root, err := os.OpenRoot(state.dir)
	if err != nil {
		return false
	}
	//nolint:errcheck // Closing the directory handle cannot change the completed file operation.
	defer root.Close()

	dir := filepath.Dir(state.file)
	//nolint:errcheck // Creating the temp file checks whether directory creation succeeded.
	_ = root.MkdirAll(dir, mcpStateDirMode)
	temp := filepath.Join(dir, "subscriptions-"+rand.Text())

	file, err := root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mcpStateFileMode)
	if err != nil {
		return false
	}

	return replaceMCPState(root, file, temp, state.file, data, func(data []byte) (int, error) {
		return state.writeFile(file, data)
	})
}

func replaceMCPState(
	root *os.Root,
	file *os.File,
	temp, target string,
	data []byte,
	write func([]byte) (int, error),
) bool {
	_, err := write(data)
	if errors.Join(err, file.Sync(), file.Close()) == nil && root.Rename(temp, target) == nil {
		return true
	}

	//nolint:errcheck // Cleanup is best effort after a failed write.
	_ = root.Remove(temp)

	return false
}
