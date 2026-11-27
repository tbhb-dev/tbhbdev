// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel/metric"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/appauth"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/budget"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/ghclient"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/reconcile"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/store"
	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/worker"
)

const (
	appIDEnv          = "GITHUB_APP_ID"
	installationIDEnv = "GITHUB_INSTALLATION_ID"
	appKeyFileEnv     = "GITHUB_APP_KEY_FILE"
	apiURLEnv         = "GITHUB_API_URL"
	defaultGitHubAPI  = "https://api.github.com"
)

var (
	errMissingReconcilerConfig = errors.New("missing reconciler configuration")
	errInvalidAppID            = errors.New(appIDEnv + " must be a positive integer")
	errInvalidInstallationID   = errors.New(installationIDEnv + " must be a positive integer")
	errInvalidBaseURL          = errors.New(apiURLEnv + " must be a valid HTTPS API base URL")
	errInvalidCIBackfill       = errors.New(
		"GITHUB_CI_BACKFILL must name a repository and days from 1 to 7",
	)
	errReadAppKey = errors.New("read " + appKeyFileEnv)
)

type reconcileConfig struct {
	keyFile        string
	apiURL         string
	appID          int64
	installationID int64
}

func reconcilerConfig(env func(string) string) (reconcileConfig, error) {
	app, installation, keyFile := env(appIDEnv), env(installationIDEnv), env(appKeyFileEnv)
	credentials := [...]struct{ name, value string }{
		{appIDEnv, app}, {installationIDEnv, installation}, {appKeyFileEnv, keyFile},
	}

	var (
		disabled reconcileConfig
		missing  []string
	)

	for _, credential := range credentials {
		if credential.value == "" {
			missing = append(missing, credential.name)
		}
	}

	if len(missing) == len(credentials) {
		return disabled, nil
	}

	if len(missing) != 0 {
		return reconcileConfig{}, fmt.Errorf(
			"%w: %s",
			errMissingReconcilerConfig,
			strings.Join(missing, ", "),
		)
	}

	appID, err := strconv.ParseInt(app, 10, 64)
	if err != nil || appID <= 0 {
		return reconcileConfig{}, errInvalidAppID
	}

	installationID, err := strconv.ParseInt(installation, 10, 64)
	if err != nil || installationID <= 0 {
		return reconcileConfig{}, errInvalidInstallationID
	}

	return reconcileConfig{
		keyFile:        keyFile,
		apiURL:         cmp.Or(env(apiURLEnv), defaultGitHubAPI),
		appID:          appID,
		installationID: installationID,
	}, nil
}

// ciBackfillConfig validates the trigger even when reconciliation is disabled.
func ciBackfillConfig(env func(string) string) (reconcile.CIConfig, error) {
	const name = "GITHUB_CI_BACKFILL"

	var cfg reconcile.CIConfig

	value := env(name)
	if value == "" {
		return cfg, nil
	}

	match := regexp.MustCompile(`^([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9_.-]+):([1-7])$`).
		FindStringSubmatch(value)
	if match == nil || strings.HasSuffix(match[1], "/.") || strings.HasSuffix(match[1], "/..") {
		return cfg, errInvalidCIBackfill
	}

	cfg.Repository = match[1]
	cfg.Days = int(match[2][0] - '0')

	return cfg, nil
}

//nolint:ireturn // The worker accepts either recorder through its interface.
func newConsumers(
	ctx context.Context,
	endpoint string,
	cfg reconcileConfig,
	meter metric.Meter,
	repo *store.Bolt,
	log *slog.Logger,
) (worker.Recorder, *reconcile.Loop, *ghclient.Client, error) {
	// cspell:ignore contextcheck
	consumer, err := newRecorder( //nolint:contextcheck // Recorder setup seeds its counters.
		endpoint,
		meter,
		repo,
		log,
	)
	if err != nil {
		return nil, nil, nil, err
	}

	log.InfoContext(ctx, "recorder mode", "configured", endpoint != "")

	record, configured := consumer.(*recorder.Recorder)
	if !configured || cfg.keyFile == "" {
		log.InfoContext(ctx, "reconciler disabled")

		return consumer, nil, nil, nil
	}

	loop, client, err := newReconciler(cfg, meter, record, repo, log)

	return consumer, loop, client, err
}

func newReconciler(
	cfg reconcileConfig,
	meter metric.Meter,
	record *recorder.Recorder,
	repo reconcile.Watermarks,
	log *slog.Logger,
) (*reconcile.Loop, *ghclient.Client, error) {
	// Relative request paths must resolve inside the API base path.
	base, err := url.Parse(strings.TrimRight(cfg.apiURL, "/") + "/")
	if err != nil || base.Scheme != "https" {
		return nil, nil, errInvalidBaseURL
	}

	key, err := readConfiguredFile(cfg.keyFile)
	if err != nil {
		return nil, nil, errReadAppKey
	}

	source, err := appauth.New(appauth.Config{
		HTTPClient: nil, Now: nil, Logger: log, BaseURL: base.String(), PrivateKeyPEM: key,
		AppID: cfg.appID, InstallationID: cfg.installationID,
	})
	if err != nil {
		return nil, nil, fmt.Errorf("create installation token source: %w", err)
	}

	tags := ghclient.NewETags()

	client, err := ghclient.New(base.String(), http.DefaultClient, budget.New(), source, tags)
	if err != nil {
		return nil, nil, errInvalidBaseURL
	}

	lister := reconcile.NewLister(client, tags, base)
	pass := func(ctx context.Context) (reconcile.RunResult, error) {
		return reconcile.Run(ctx, lister, lister, record, repo, reconcileClock{})
	}

	loop, err := reconcile.NewLoop(pass, meter, log, nil)
	if err != nil {
		return nil, nil, fmt.Errorf("create reconciler loop: %w", err)
	}

	return loop, client, nil
}

func startReconciler(
	ctx context.Context,
	loop *reconcile.Loop,
	stop context.CancelFunc,
) <-chan error {
	done := make(chan error, 1)
	if loop == nil {
		close(done)

		return done
	}

	go func() {
		err := loop.Start(ctx)
		done <- err

		if err != nil {
			stop()
		}
	}()

	return done
}

type reconcileClock struct{}

func (reconcileClock) Now() time.Time { return time.Now() }
