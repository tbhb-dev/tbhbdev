// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

//go:build unix && !aix && (!solaris || illumos)

package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"syscall"
)

func (cfg *serveConfig) withParked(ctx context.Context, targets []runnerTarget, run func(parkedService) error) error {
	if len(targets) == 0 {
		return run(noParkedService)
	}

	err := os.MkdirAll(cfg.storeDir, 0o700)
	if err != nil {
		return fmt.Errorf("create JetStream directory: %w", err)
	}

	path := parkedSocketPath(cfg.storeDir, cfg.parkedPath)

	listener, err := listenParked(ctx, path)
	if err != nil {
		return err
	}

	// Serve closes and unlinks the listener. This also covers startup failures.
	//nolint:errcheck // The listener is closing during server shutdown.
	defer func() { _ = listener.Close() }()

	return run(func(store eventStore) []httpService {
		server := gatewayHTTP("", parkedAPI{store: store, targets: targets}.handler())
		server.serve = func() error { return server.Serve(listener) }

		return []httpService{server}
	})
}

func listenParked(ctx context.Context, path string) (net.Listener, error) {
	var raw syscall.RawSockaddrUnix

	const limit = len(raw.Path) - 1 // Leave room for the terminating NUL.

	address := parkedAddress(path)
	if length := len(address); length > limit {
		return nil, fmt.Errorf("parked socket path is %d bytes (limit %d): %w", length, limit, syscall.ENAMETOOLONG)
	}

	clearStaleSocket(ctx, address)

	listener, err := new(net.ListenConfig).Listen(ctx, "unix", address)
	if err != nil {
		return nil, fmt.Errorf("listen on parked socket: %w", err)
	}

	return secureParkedSocket(listener, address)
}

func secureParkedSocket(listener net.Listener, path string) (net.Listener, error) {
	err := os.Chmod(path, 0o600)
	if err != nil {
		return nil, errors.Join(fmt.Errorf("secure parked socket: %w", err), listener.Close())
	}

	return listener, nil
}

func clearStaleSocket(ctx context.Context, path string) {
	// Serialize stale cleanup across starts without leaving a lock file behind.
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return
	}

	//nolint:errcheck // The directory handle only holds the lock for stale socket cleanup.
	defer func() { _ = dir.Close() }()

	lockErr := syscall.Flock(int(dir.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	info, err := os.Lstat(path)

	if errors.Join(lockErr, err) != nil || info.Mode()&os.ModeSocket == 0 {
		return
	}

	conn, err := new(net.Dialer).DialContext(ctx, "unix", path)
	if err == nil {
		//nolint:errcheck // The successful dial already proved that the socket is active.
		_ = conn.Close()
	}

	if errors.Is(err, syscall.ECONNREFUSED) {
		//nolint:errcheck // The subsequent listen reports a stale socket that could not be removed.
		_ = os.Remove(path)
	}
}
