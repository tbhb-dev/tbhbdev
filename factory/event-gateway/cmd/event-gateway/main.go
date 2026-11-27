// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Command event-gateway receives and stores GitHub webhooks.
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
)

const (
	version      = "dev"
	serveCommand = "serve"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	err := run(ctx, os.Stdout, logger.Handler(), os.Args[1:])

	stop()

	os.Exit(exitCode(ctx, logger, err))
}

func exitCode(ctx context.Context, logger *slog.Logger, err error) int {
	if err != nil {
		logger.ErrorContext(ctx, "event gateway stopped", "error", err)

		return 1
	}

	return 0
}

func run(ctx context.Context, out io.Writer, logHandler slog.Handler, args []string) error {
	if len(args) == 0 {
		_, err := fmt.Fprintf(out, "event-gateway %s\n", version)
		if err != nil {
			return fmt.Errorf("write version: %w", err)
		}

		return nil
	}

	switch args[0] {
	case serveCommand:
		return serve(ctx, out, logHandler, args[1:])
	case healthcheckCommand:
		return healthcheck(ctx, args[1:])
	case parkedCommand:
		return runParked(ctx, out, args[1:], requestParked)
	case mcpCommand:
		return runMCP(ctx, logHandler, args[1:])
	default:
		return errUsage
	}
}
