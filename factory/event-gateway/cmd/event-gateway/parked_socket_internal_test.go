// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

//go:build unix && !aix && (!solaris || illumos)

//nolint:exhaustruct_v5 // The fixtures use only their socket and target fields.
package main

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"strings"
	"syscall"
	"testing"
)

func TestParkedSocket(t *testing.T) {
	ctx := t.Context()
	dir := shortSocketDir(t)
	t.Chdir(dir)

	const path = "s"

	args := []string{parkedCommand, parkedListCommand, "-socket=" + path}
	notRunning := "event-gateway is not running (cannot reach " + path + ")"
	checkEqual(t, run(ctx, io.Discard, slog.DiscardHandler, args).Error(), notRunning)

	stale, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	checkError(t, err)
	stale.SetUnlinkOnClose(false)
	checkError(t, stale.Close())
	checkEqual(t, run(ctx, io.Discard, slog.DiscardHandler, args).Error(), notRunning)

	targets := []runnerTarget{{Target: fakeTarget{name: "replay"}}}
	cfg := serveConfig{storeDir: dir, parkedPath: path}
	checkError(t, cfg.withParked(ctx, targets, func(parkedService) error {
		info, statErr := os.Stat(path)
		checkError(t, statErr)
		checkEqual(t, info.Mode().Perm(), os.FileMode(0o600))

		busy := cfg.withParked(ctx, targets, nil)
		checkEqual(t, errors.Is(busy, syscall.EADDRINUSE), true)

		return nil
	}))

	_, err = os.Lstat(path)
	checkEqual(t, errors.Is(err, os.ErrNotExist), true)
	checkError(t, os.WriteFile(path, nil, 0o600))
	_, err = listenParked(ctx, path)
	checkEqual(t, errors.Is(err, syscall.EADDRINUSE), true)

	cfg.storeDir = path
	checkEqual(t, errors.Is(cfg.withParked(ctx, targets, nil), syscall.ENOTDIR), true)

	closed, err := listenParked(ctx, "b")
	checkError(t, err)
	_, err = secureParkedSocket(closed, "missing")
	checkEqual(t, errors.Is(err, os.ErrNotExist), true)
	clearStaleSocket(ctx, "missing/s")
}

func TestParkedSocketPathLimit(t *testing.T) {
	t.Parallel()

	const limit = len(syscall.RawSockaddrUnix{}.Path) - 1
	// The listener adds "./" to the relative name before checking its length.
	_, err := listenParked(t.Context(), strings.Repeat("s", limit-1))
	checkEqual(t, errors.Is(err, syscall.ENAMETOOLONG), true)

	want := fmt.Sprintf("parked socket path is %d bytes (limit %d):", limit+1, limit)
	checkEqual(t, strings.HasPrefix(err.Error(), want), true)
}
