// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package buildloop runs the commands of a build-loop step.
package buildloop

import (
	"bytes"
	"cmp"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"time"
)

const (
	defaultTimeout = 10 * time.Minute
	waitDelay      = 5 * time.Second
)

// Command describes one command to run. Executable must be an absolute, cleaned
// path. It is matched exactly against the Runner's allow-list and is never run
// through a shell or looked up on PATH. A zero Timeout
// means ten minutes, and an empty Dir means the current directory.
type Command struct {
	Executable string
	Dir        string
	Args       []string
	Timeout    time.Duration
}

// Result is the outcome of a command that ran to completion. Output holds the
// standard output and standard error, interleaved.
type Result struct {
	Output   string
	Duration time.Duration
	ExitCode int
}

// TimeoutError reports a command that did not finish within its timeout.
type TimeoutError struct {
	Timeout time.Duration
}

// Error implements error.
func (e *TimeoutError) Error() string {
	return fmt.Sprintf("command exceeded its timeout of %s", e.Timeout)
}

// NotAllowedError reports an executable that is not on the allow-list or is not
// an absolute, cleaned path.
type NotAllowedError struct {
	Executable string
}

// Error implements error.
func (e *NotAllowedError) Error() string {
	return fmt.Sprintf("command %q is not allowed", e.Executable)
}

// Runner runs commands whose executable is on a fixed allow-list.
type Runner struct {
	allowed map[string]struct{}
}

// NewRunner returns a Runner that runs only the named executables. Each name
// must be an absolute, cleaned path, or NewRunner returns a *NotAllowedError:
// a bare or relative name would be resolved through PATH or the working
// directory and so could select a different binary. A name must match
// Command.Executable exactly: the Runner does not clean paths, fold case, or
// take the base name.
func NewRunner(allowed ...string) (*Runner, error) {
	set := make(map[string]struct{}, len(allowed))

	for _, name := range allowed {
		if !fixedPath(name) {
			return nil, &NotAllowedError{Executable: name}
		}

		set[name] = struct{}{}
	}

	return &Runner{allowed: set}, nil
}

// fixedPath reports whether name is an absolute path that is already cleaned.
func fixedPath(name string) bool {
	return filepath.IsAbs(name) && filepath.Clean(name) == name
}

// Run runs one command and waits for it. A command that exits with a nonzero
// status is not an error: the status is in Result.ExitCode. Run returns a
// *NotAllowedError or *TimeoutError for those failures, and the Result of a
// timeout holds the output captured so far.
func (r *Runner) Run(ctx context.Context, command Command) (Result, error) {
	if _, ok := r.allowed[command.Executable]; !ok {
		return Result{}, &NotAllowedError{Executable: command.Executable}
	}

	timeout := cmp.Or(command.Timeout, defaultTimeout)

	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var out bytes.Buffer

	cmd := exec.CommandContext(ctx, command.Executable, command.Args...)
	cmd.Dir = command.Dir
	cmd.Stdout = &out
	cmd.Stderr = &out
	cmd.WaitDelay = waitDelay

	start := time.Now()
	err := cmd.Run()
	result := Result{
		Output:   out.String(),
		Duration: time.Since(start),
		ExitCode: cmd.ProcessState.ExitCode(),
	}

	var exitErr *exec.ExitError

	switch {
	case err != nil && errors.Is(ctx.Err(), context.DeadlineExceeded):
		return result, &TimeoutError{Timeout: timeout}
	case err != nil && ctx.Err() != nil:
		return result, fmt.Errorf("run %q: %w", command.Executable, ctx.Err())
	case err != nil && !errors.As(err, &exitErr):
		return result, fmt.Errorf("run %q: %w", command.Executable, err)
	default:
		return result, nil
	}
}
