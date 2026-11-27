// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Command github-metrics runs the webhook receiver, queue worker and optional reconciler.
//
// WEBHOOK_SECRET_FILE names a file that contains the App's webhook secret.
//
// STORE_FILE names the store file for the webhook queue and dedupe table.
//
// OTEL_EXPORTER_OTLP_ENDPOINT enables metric export and is required for reconciliation.
// GITHUB_APP_ID and GITHUB_INSTALLATION_ID are positive integers that identify
// the GitHub App and its installation. GITHUB_APP_KEY_FILE names the App's
// private key file. Set all three to configure reconciliation.
// GITHUB_API_URL is the GitHub API base and defaults to https://api.github.com.
//
// LISTEN_ADDR is the listen address and defaults to :8080. The health check
// probes that address, or loopback when its host is a wildcard.
//
// With -healthcheck the command probes the local health endpoint and exits
// 0 or 1, so a container health check works in an image that has no shell.
package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.opentelemetry.io/otel/metric"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/receiver"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/telemetry"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/worker"
)

const (
	version         = "0.0.0-dev"
	shutdownTimeout = 10 * time.Second
	probeTimeout    = 3 * time.Second
	pollInterval    = time.Second
)

var (
	errNoSecretFile = errors.New("WEBHOOK_SECRET_FILE is not set")
	errEmptySecret  = errors.New("webhook secret file is empty")
	errNoStoreFile  = errors.New("STORE_FILE is not set")
	errNoRecorder   = errors.New("no recorder is configured")
)

func main() {
	healthcheck := flag.Bool("healthcheck", false, "probe the health endpoint and exit")

	flag.Parse()

	addr := os.Getenv("LISTEN_ADDR")
	if addr == "" {
		addr = ":8080"
	}

	if *healthcheck {
		os.Exit(probe(addr))
	}

	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	err := run(ctx, addr, log)

	stop()

	if err != nil {
		log.Error("exit", "error", err)
		os.Exit(1)
	}
}

//nolint:funlen // Startup owns service shutdown ordering.
func run(ctx context.Context, addr string, log *slog.Logger) error {
	cfg, err := reconcilerConfig(os.Getenv)
	if err != nil {
		return err
	}

	backfillCfg, err := ciBackfillConfig(os.Getenv)
	if err != nil {
		return err
	}

	secret, err := webhookSecret()
	if err != nil {
		return err
	}

	repo, err := openStore()
	if err != nil {
		return err
	}

	err = logRepairFloor(log, repo)
	if err != nil {
		return errors.Join(err, repo.Close())
	}

	srv := receiver.NewServer(addr, receiver.Handler(secret, repo, log))

	ctx, stop := context.WithCancel(ctx)
	defer stop()

	shutdownTelemetry, meter, err := telemetry.Setup(ctx, "github-metrics", version)
	if err != nil {
		return errors.Join(err, repo.Close())
	}

	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

	consumer, loop, client, err := newConsumers(ctx, endpoint, cfg, meter, repo, log)
	if err != nil {
		shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
		defer cancel()

		return errors.Join(err, shutdownTelemetry(shutdownCtx), repo.Close())
	}

	workerErr := make(chan error, 1)

	go func() {
		workerErr <- worker.New(repo, consumer, log).Run(ctx, pollInterval)

		stop()
	}()

	reconcileErr := startReconciler(ctx, loop, stop)

	serveErr := make(chan error, 1)

	go func() { serveErr <- srv.ListenAndServe() }()

	log.InfoContext(ctx, "listening", "addr", addr)

	backfillDone := startBackfill(ctx, client, repo, log, backfillCfg)

	select {
	case err = <-serveErr:
		// Shut down the worker and reconciler before closing their store.
	case <-ctx.Done():
	}

	stop()

	shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
	defer cancel()

	<-backfillDone

	return errors.Join(
		err,
		srv.Shutdown(shutdownCtx),
		<-workerErr,
		<-reconcileErr,
		shutdownTelemetry(shutdownCtx),
		repo.Close(),
	)
}

func startBackfill(
	ctx context.Context,
	client *ghclient.Client,
	repo reconcile.Ledger,
	log *slog.Logger,
	cfg reconcile.CIConfig,
) <-chan struct{} {
	done := make(chan struct{})
	if client == nil || cfg.Days <= 0 {
		close(done)

		return done
	}

	go func() {
		defer close(done)

		reconcile.BackfillCI(ctx, client, repo, log, cfg, time.Now())
	}()

	return done
}

func webhookSecret() ([]byte, error) {
	file := os.Getenv("WEBHOOK_SECRET_FILE")
	if file == "" {
		return nil, errNoSecretFile
	}

	raw, err := os.ReadFile(file) //nolint:gosec // The operator names this path.
	if err != nil {
		return nil, fmt.Errorf("read webhook secret: %w", err)
	}

	secret := bytes.TrimRight(raw, "\r\n")
	if len(secret) == 0 {
		return nil, errEmptySecret
	}

	return secret, nil
}

func openStore() (*store.Bolt, error) {
	file := os.Getenv("STORE_FILE")
	if file == "" {
		return nil, errNoStoreFile
	}

	repo, err := store.Open(file)
	if err != nil {
		return nil, fmt.Errorf("open store: %w", err)
	}

	return repo, nil
}

func logRepairFloor(log *slog.Logger, ledger recorder.Ledger) error {
	floor, err := ledger.RepairFloor()
	if err != nil {
		return fmt.Errorf("read repair floor: %w", err)
	}

	log.Info("repair floor", slog.Time("repair_floor", floor))

	return nil
}

//nolint:ireturn // The worker accepts either recorder through its interface.
func newRecorder(
	endpoint string,
	meter metric.Meter,
	ledger recorder.Ledger,
	log *slog.Logger,
) (worker.Recorder, error) {
	if endpoint == "" {
		return noopRecorder{}, nil
	}

	consumer, err := recorder.New(meter, ledger, log)
	if err != nil {
		return nil, fmt.Errorf("create recorder: %w", err)
	}

	return consumer, nil
}

type noopRecorder struct{}

func (noopRecorder) Record(_ context.Context, _ string, _ []byte) error { return errNoRecorder }

// probe asks the local health endpoint and returns the exit code.
func probe(addr string) int {
	url, err := receiver.HealthURL(addr)
	if err != nil {
		return 1
	}

	ctx, cancel := context.WithTimeout(context.Background(), probeTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext( //nolint:gosec // The probe uses the operator-configured listener.
		ctx,
		http.MethodGet,
		url,
		nil,
	)
	if err != nil {
		return 1
	}

	resp, err := http.DefaultClient.Do(req) //nolint:gosec // own address
	if err != nil {
		return 1
	}

	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return 1
	}

	return 0
}
