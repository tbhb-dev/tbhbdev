// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	shutdownTimeout = 10 * time.Second
	readTimeout     = 15 * time.Second
	writeTimeout    = 30 * time.Second
)

const (
	readListenFlag    = "read-listen"
	readTokenFileFlag = "read-token-file"
	defaultStoreDir   = "/data/nats"
)

var (
	errUsage      = errors.New("usage: event-gateway serve [flags]")
	errServeFlags = errors.New("invalid serve flags (see event-gateway serve -help)")
	errBodyCap    = errors.New("body cap must be positive and at most 5 MiB")
	errStoreLimit = errors.New("JetStream limits must be nonnegative")
)

var errReadFlags = errors.New("invalid combination of -read-listen, -session-listen and -read-token-file")

type serveConfig struct {
	otlp       otlpConfig
	secret     *string
	secretFile *string
	grafSecret *string
	grafFile   *string
	readListen string
	wsListen   string
	tokenFile  string
	listen     string
	storeDir   string
	targets    string
	parkedPath string
	bodyCap    int
	maxMemory  int64
	maxStore   int64
}

func (cfg *serveConfig) parse(out io.Writer, args []string) error {
	flags := flag.NewFlagSet("event-gateway serve", flag.ContinueOnError)
	// Flag parsing errors can quote argument values. Only print flag help.
	flags.SetOutput(io.Discard)
	flags.StringVar(&cfg.listen, "listen", "127.0.0.1:8080", "HTTP listen address")
	flags.StringVar(&cfg.readListen, readListenFlag, "", "private read API listen address")
	flags.StringVar(&cfg.wsListen, "session-listen", "", "private session listen address")
	flags.StringVar(&cfg.tokenFile, readTokenFileFlag, "", "bearer token hash file for read and session listeners")
	flags.StringVar(&cfg.storeDir, "store-dir", defaultStoreDir, "JetStream data directory")
	flags.StringVar(&cfg.parkedPath, "parked-socket", os.Getenv(parkedSocketEnv), "parked API socket path")
	flags.StringVar(&cfg.targets, "targets", "", "routing targets JSON file")
	flags.Func("github-secret", "GitHub webhook secret (overrides file and environment)", func(value string) error {
		cfg.secret = &value

		return nil
	})
	flags.Func("github-secret-file", "GitHub secret file (before GITHUB_WEBHOOK_SECRET)", func(value string) error {
		cfg.secretFile = &value

		return nil
	})
	flags.Func("grafana-secret", "Grafana webhook secret; enables the Grafana route", func(value string) error {
		cfg.grafSecret = &value

		return nil
	})
	flags.Func("grafana-secret-file", "Grafana secret file; enables the Grafana route", func(value string) error {
		cfg.grafFile = &value

		return nil
	})
	flags.IntVar(&cfg.bodyCap, "body-cap", defaultBodyCap, "maximum retained raw body size in bytes")
	flags.Int64Var(
		&cfg.maxMemory,
		"jetstream-max-memory",
		0,
		"JetStream memory limit in bytes (0 uses the server default)",
	)
	flags.Int64Var(&cfg.maxStore, "jetstream-max-store", 0, "JetStream disk limit in bytes (0 uses the server default)")
	flags.StringVar(&cfg.otlp.Endpoint, "otlp-endpoint", "", "OTLP HTTP collector base URL")
	flags.StringVar(&cfg.otlp.CAFile, "otlp-ca-file", "", "PEM CA file for the collector")

	cfg.otlp.Headers = make(map[string]string)

	flags.Func("otlp-header", "OTLP collector header as name=value (repeatable)", func(header string) error {
		name, value, ok := strings.Cut(header, "=")
		if !ok || name == "" {
			return errServeFlags
		}

		cfg.otlp.Headers[name] = value

		return nil
	})

	err := flags.Parse(args)
	provided := make(map[string]bool)

	flags.Visit(func(option *flag.Flag) { provided[option.Name] = true })

	switch {
	case errors.Is(err, flag.ErrHelp):
		flags.SetOutput(out)
		flags.Usage()

		return flag.ErrHelp
	case err != nil:
		return errServeFlags
	case flags.NArg() != 0:
		return errUsage
	case cfg.bodyCap <= 0, cfg.bodyCap > defaultBodyCap:
		return errBodyCap
	case cfg.maxMemory < 0, cfg.maxStore < 0:
		return errStoreLimit
	case (cfg.readListen != "" || cfg.wsListen != "") != provided[readTokenFileFlag],
		provided[readListenFlag] && cfg.readListen == "":
		return errReadFlags
	default:
		return nil
	}
}

func (cfg *serveConfig) loadSecret() ([]byte, error) {
	return readSecret(cfg.secret, cfg.secretFile, "GITHUB_WEBHOOK_SECRET")
}

// readSecret takes the flag value, then the file, then the environment.
func readSecret(value, file *string, env string) ([]byte, error) {
	switch {
	case value != nil:
		return []byte(*value), nil
	case file != nil:
		secret, err := os.ReadFile(*file)
		if err != nil {
			return nil, fmt.Errorf("read secret file: %w", err)
		}

		return bytes.TrimRight(secret, "\r\n"), nil
	default:
		return []byte(os.Getenv(env)), nil
	}
}

func serve(ctx context.Context, out io.Writer, logHandler slog.Handler, args []string) error {
	var cfg serveConfig

	err := cfg.parse(out, args)
	if errors.Is(err, flag.ErrHelp) {
		return nil
	}

	if err != nil {
		return err
	}

	targets, closeTargets, err := loadTargets(cfg.targets, targetTypes)
	if err != nil {
		return err
	}

	var closeStore func()

	// withParked releases the listener before these deferred closes run.
	defer func() {
		if closeStore != nil {
			closeStore()
		}
	}()
	defer closeTargets()

	var tokens *readTokens
	if cfg.readListen != "" || cfg.wsListen != "" {
		tokens = new(readTokens)
		tokens.path = cfg.tokenFile

		err = tokens.load()
		if err != nil {
			return err
		}
	}

	secret, err := cfg.loadSecret()
	if err != nil {
		return err
	}

	handler, err := newGitHubWebhook(secret, cfg.bodyCap)
	if err != nil {
		return err
	}

	grafana, err := cfg.grafanaHandler()
	if err != nil {
		return err
	}

	return cfg.withParked(ctx, targets, func(parked parkedService) error {
		options := storeOptions(cfg.storeDir)
		options.JetStreamMaxMemory = cfg.maxMemory
		options.JetStreamMaxStore = cfg.maxStore

		store, stop, storeErr := openStore(ctx, options, retention)
		if storeErr != nil {
			return storeErr
		}

		closeStore = stop

		return cfg.start(ctx, logHandler, store, targets, tokens, handler, grafana, parked)
	})
}

func (cfg *serveConfig) start(
	ctx context.Context, logHandler slog.Handler, store eventStore, targets []runnerTarget, tokens *readTokens,
	handler *githubWebhook, grafana *grafanaWebhook, parked parkedService,
) error {
	signals, closeSignals, err := openTelemetry(ctx, logHandler, cfg.otlp)
	if err != nil {
		return err
	}

	defer closeSignals()

	handler.store = store
	handler.telemetry = signals

	webhook := gatewayHTTP(cfg.listen, withGrafana(grafana, handler, store, signals))

	return runWithTargets(ctx, store, signals, targets, func(ctx context.Context) error {
		return cfg.runHTTP(ctx, tokens, store, signals, append(parked(store), webhook))
	})
}

func (cfg *serveConfig) runHTTP(
	ctx context.Context, tokens *readTokens, store eventStore, signals telemetry, servers []httpService,
) error {
	api := readAPI{tokens: tokens, store: store, telemetry: signals}

	if cfg.readListen != "" {
		servers = append(servers, gatewayHTTP(cfg.readListen, api.handler()))
	}

	if cfg.wsListen != "" {
		sessions := newSessionAPI(api, helloTimeout)
		server := gatewayHTTP(cfg.wsListen, http.HandlerFunc(sessions.connect))
		server.RegisterOnShutdown(func() { close(sessions.stop) })
		servers = append(servers, server)

		defer sessions.active.Wait()
	}

	return runHTTP(ctx, tokens, signals.logger, servers...)
}

type httpService struct {
	*http.Server

	serve func() error
}

type parkedService func(eventStore) []httpService

func noParkedService(eventStore) []httpService { return nil }

func gatewayHTTP(address string, handler http.Handler) httpService {
	var server http.Server
	server.Addr = address
	server.Handler = handler
	server.DisableGeneralOptionsHandler = true
	server.ReadHeaderTimeout = readTimeout
	server.ReadTimeout = readTimeout
	server.WriteTimeout = writeTimeout

	return httpService{Server: &server, serve: server.ListenAndServe}
}

func runHTTP(ctx context.Context, tokens *readTokens, logger *slog.Logger, servers ...httpService) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	var tick <-chan time.Time

	if tokens != nil {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()

		tick = ticker.C
	}

	stopped := make(chan error, len(servers))
	for _, server := range servers {
		go func() {
			stopped <- runHTTPServer(ctx, server)

			cancel()
		}()
	}

	var err error

	for remaining := len(servers); remaining > 0; {
		select {
		case result := <-stopped:
			err = errors.Join(err, result)
			remaining--
		case <-tick:
			tokens.reload(ctx, logger)
		}
	}

	return err
}

func runHTTPServer(ctx context.Context, httpServer httpService) error {
	done := make(chan error, 1)
	stop := context.AfterFunc(ctx, func() {
		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownTimeout)
		defer cancel()

		done <- errors.Join(httpServer.Shutdown(shutdown), httpServer.Close())
	})

	err := httpServer.serve()
	if stop() {
		return fmt.Errorf("serve HTTP: %w", err)
	}

	return <-done
}
