// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:words eventfields temporaltarget

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/eventfields"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/routing"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/temporaltarget"
)

type targetFactory func(routing.Target) (actor, func(), error)

//nolint:gochecknoglobals // Serve uses the registry of supported target types.
var targetTypes = map[string]targetFactory{"temporal": newTemporalTarget}

var (
	errTargetsFile       = errors.New("invalid targets file")
	errUnknownTargetType = errors.New("unknown target type")
	errTemporalDial      = errors.New("dial Temporal failed")
	errTemporalAction    = errors.New("invalid Temporal rule action")
)

type temporalClient interface {
	temporaltarget.Client
	Close()
}

//nolint:gochecknoglobals // Tests substitute a client without network access.
var dialTemporal = func(connection temporaltarget.Connection) (temporalClient, error) {
	return temporaltarget.Dial(connection)
}

//nolint:ireturn // The registry accepts actors from each target type.
func newTemporalTarget(config routing.Target) (actor, func(), error) {
	connection, err := temporaltarget.ParseConnection(config.Connection)
	if err != nil {
		return nil, nil, fmt.Errorf("parse Temporal connection: %w", err)
	}

	client, err := dialTemporal(connection)
	if err != nil {
		return nil, nil, errTemporalDial
	}

	target, err := temporaltarget.New(config, client)
	if err != nil {
		client.Close()

		return nil, nil, errTemporalAction
	}

	return target, client.Close, nil
}

type actor interface {
	// Act performs one rule's action for one event. The error must not contain
	// event payload or envelope text. An error with a `Permanent() bool` method
	// that returns true parks the event; any other error is retried.
	Act(ctx context.Context, rule routing.Rule, fields eventfields.Fields, envelope []byte) error
}

type permanentActorError interface {
	error
	Permanent() bool
}

// retryActorError keeps the actor's message and drops the runner's sentinel.
type retryActorError struct{ error }

type ruleTarget struct {
	actor  actor
	config routing.Target
}

func (target ruleTarget) Name() string { return target.config.Name }

func (target ruleTarget) Deliver(ctx context.Context, event Event) error {
	fields, err := eventfields.FromEnvelope(event.Seq, event.Envelope)
	if err != nil {
		return fmt.Errorf("%w: %w", ErrPermanent, err)
	}

	payload := sessionJSON[sessionObject](event.Envelope)["payload"]
	matchFields := filter.Extract(fields.Event, payload)

	for _, rule := range target.config.Rules {
		if !rule.Matches(fields.Provider, fields.Event, fields.Repository, matchFields) {
			continue
		}

		err = target.actor.Act(ctx, rule, fields, event.Envelope)
		if err != nil {
			permanent, ok := errors.AsType[permanentActorError](err)
			if ok && permanent.Permanent() {
				err = fmt.Errorf("%w: %w", ErrPermanent, err)
			} else if errors.Is(err, ErrPermanent) {
				err = retryActorError{err}
			}

			return RuleError{Rule: rule.Name, Err: err}
		}
	}

	return nil
}

func loadTargets(path string, registry map[string]targetFactory) ([]runnerTarget, func(), error) {
	var closers []func()

	closeTargets := func() {
		for _, closeTarget := range closers {
			closeTarget()
		}
	}

	config, err := readTargetsConfig(path)
	if err != nil {
		return nil, nil, err
	}

	if len(config.Targets) == 0 {
		return nil, closeTargets, nil
	}

	targets := make([]runnerTarget, 0, len(config.Targets))
	for _, target := range config.Targets {
		create, ok := registry[target.Type]
		if !ok {
			closeTargets()

			return nil, nil, fmt.Errorf("%w %q", errUnknownTargetType, target.Type)
		}

		actions, closeTarget, createErr := create(target)
		if createErr != nil {
			closeTargets()

			return nil, nil, fmt.Errorf("configure target %q: %w", target.Name, createErr)
		}

		if closeTarget != nil {
			closers = append(closers, closeTarget)
		}

		start := startPosition{all: target.Start.Kind == routing.All, at: nil}
		if target.Start.Kind == routing.Time {
			start.at = &target.Start.Time
		}

		targets = append(targets, runnerTarget{
			Target: ruleTarget{actor: actions, config: target},
			Start:  start, Subjects: target.Subjects(),
		})
	}

	return targets, closeTargets, nil
}

func readTargetsConfig(path string) (routing.Config, error) {
	if path == "" {
		return routing.Config{Targets: nil}, nil
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return routing.Config{}, fmt.Errorf("read targets file: %w", err)
	}

	config, err := routing.Load(data)
	if err == nil || emptyTargetConfig(data) {
		return config, nil
	}

	if targetErr, ok := errors.AsType[routing.TargetError](err); ok {
		return routing.Config{}, fmt.Errorf("configure target %q: %w", targetErr.Name, errTargetsFile)
	}

	return routing.Config{}, errTargetsFile
}

// The routing parser requires targets. Serve also accepts an empty config.
func emptyTargetConfig(data []byte) bool {
	object := sessionJSON[sessionObject](data)
	if object == nil {
		return false
	}

	if len(object) == 0 {
		return true
	}

	entries := sessionJSON[[]json.RawMessage](object["targets"])

	return len(object) == 1 && entries != nil && len(entries) == 0
}

func runWithTargets(
	ctx context.Context,
	store eventStore,
	signals telemetry,
	targets []runnerTarget,
	serveHTTP func(context.Context) error,
) error {
	if len(targets) == 0 {
		return serveHTTP(ctx)
	}

	_, err := openParkedStream(ctx, store.jet)
	if err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	//nolint:exhaustruct_v5 // The runner uses the default retry base and a fresh gap counter.
	run := runner{
		stream:      store.stream,
		targets:     targets,
		onPermanent: parkHook(store.jet, signals, 0),
		ready:       make(chan struct{}),
	}
	stopped := make(chan error, 1)

	go func() {
		stopped <- run.run(ctx)

		cancel()
	}()

	select {
	case runErr := <-stopped:
		return runErr
	case <-run.ready:
	}

	// Cancellation can arrive as the runner signals readiness.
	select {
	case <-ctx.Done():
		return <-stopped
	default:
	}

	httpErr := serveHTTP(ctx)

	cancel()

	return errors.Join(httpErr, <-stopped)
}
