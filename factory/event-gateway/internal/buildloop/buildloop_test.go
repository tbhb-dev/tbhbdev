// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package buildloop_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/buildloop"
)

const (
	helperArg   = "buildloop-helper"
	modeExit    = "exit"
	modeSleep   = "sleep"
	modeWD      = "wd"
	missingBase = "buildloop-missing-executable"
	bothStreams = "to stdout\nto stderr\n"
)

// TestMain turns the test binary into the command the tests run.
func TestMain(m *testing.M) {
	if len(os.Args) > 2 && os.Args[1] == helperArg {
		os.Exit(helper(os.Args[2], os.Args[3:]))
	}

	os.Exit(m.Run())
}

func helper(mode string, args []string) int {
	switch mode {
	case modeExit:
		_, _ = os.Stdout.WriteString("to stdout\n")
		_, _ = os.Stderr.WriteString("to stderr\n")

		return number(args)
	case modeSleep:
		time.Sleep(time.Minute)
	case modeWD:
		dir, err := os.Getwd()
		if err != nil {
			return 1
		}

		_, _ = os.Stdout.WriteString(dir)
	}

	return 0
}

func number(args []string) int {
	value, err := strconv.Atoi(args[0])
	if err != nil {
		return 1
	}

	return value
}

func self(t *testing.T) string {
	t.Helper()

	path, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}

	return path
}

func uncleaned(t *testing.T) string {
	t.Helper()

	sep := string(filepath.Separator)

	return filepath.Dir(self(t)) + sep + "." + sep + filepath.Base(self(t))
}

func missing() string {
	return filepath.Join(os.TempDir(), missingBase)
}

func newRunner(t *testing.T, allowed ...string) *buildloop.Runner {
	t.Helper()

	runner, err := buildloop.NewRunner(allowed...)
	if err != nil {
		t.Fatal(err)
	}

	return runner
}

func helperCommand(t *testing.T, timeout time.Duration, args ...string) buildloop.Command {
	t.Helper()

	return buildloop.Command{
		Executable: self(t),
		Dir:        "",
		Args:       append([]string{helperArg}, args...),
		Timeout:    timeout,
	}
}

func sameDir(t *testing.T, got, want string) bool {
	t.Helper()

	gotInfo, err := os.Stat(got)
	if err != nil {
		t.Fatal(err)
	}

	wantInfo, err := os.Stat(want)
	if err != nil {
		t.Fatal(err)
	}

	return os.SameFile(gotInfo, wantInfo)
}

func TestRun(t *testing.T) {
	t.Parallel()

	tempDir := t.TempDir()
	workDir := helperCommand(t, 0, modeWD)
	workDir.Dir = tempDir

	tests := []struct {
		check   func(buildloop.Result) bool
		name    string
		command buildloop.Command
	}{
		{
			name:    "success",
			command: helperCommand(t, 0, modeExit, "0"),
			check:   func(r buildloop.Result) bool { return r.ExitCode == 0 && r.Output == bothStreams },
		},
		{
			name:    "nonzero exit",
			command: helperCommand(t, time.Minute, modeExit, "3"),
			check:   func(r buildloop.Result) bool { return r.ExitCode == 3 && r.Output == bothStreams },
		},
		{
			name:    "working directory",
			command: workDir,
			check:   func(r buildloop.Result) bool { return sameDir(t, r.Output, tempDir) },
		},
	}

	runner := newRunner(t, self(t))

	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()

			got, err := runner.Run(t.Context(), testCase.command)
			if err != nil {
				t.Fatalf("Run() error = %v", err)
			}

			if !testCase.check(got) || got.Duration <= 0 {
				t.Errorf("Run() = exit %d, %d bytes, duration %s", got.ExitCode, len(got.Output), got.Duration)
			}
		})
	}
}

func isNotAllowed(err error) bool {
	var target *buildloop.NotAllowedError

	return errors.As(err, &target) && target.Error() != ""
}

func TestNewRunnerRejectsUnfixedPaths(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		allowed string
	}{
		{name: "bare name", allowed: "sh"},
		{name: "dot relative", allowed: "./tool"},
		{name: "nested relative", allowed: "bin/tool"},
		{name: "parent relative", allowed: "../tool"},
		{name: "uncleaned absolute", allowed: uncleaned(t)},
		{name: "trailing separator", allowed: self(t) + string(filepath.Separator)},
		{name: "empty", allowed: ""},
	}

	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()

			runner, err := buildloop.NewRunner(self(t), testCase.allowed)
			if !isNotAllowed(err) || runner != nil {
				t.Fatalf("NewRunner() = %v, %v, want a *NotAllowedError", runner, err)
			}
		})
	}
}

func TestRunErrors(t *testing.T) {
	t.Parallel()

	timeout := 200 * time.Millisecond
	tests := []struct {
		check   func(error) bool
		name    string
		command buildloop.Command
	}{
		{
			name:    "timeout",
			command: helperCommand(t, timeout, modeSleep),
			check: func(err error) bool {
				var target *buildloop.TimeoutError

				return errors.As(err, &target) && target.Timeout == timeout && target.Error() != ""
			},
		},
		{
			name:    "disallowed command",
			command: buildloop.Command{Executable: "sh", Dir: "", Args: []string{"-c", "true"}, Timeout: 0},
			check:   isNotAllowed,
		},
		{
			name:    "relative path of an allowed binary",
			command: buildloop.Command{Executable: "./" + filepath.Base(self(t)), Dir: "", Args: nil, Timeout: 0},
			check:   isNotAllowed,
		},
		{
			name:    "bare name of an allowed binary",
			command: buildloop.Command{Executable: filepath.Base(self(t)), Dir: "", Args: nil, Timeout: 0},
			check:   isNotAllowed,
		},
		{
			name:    "uncleaned path of an allowed binary",
			command: buildloop.Command{Executable: uncleaned(t), Dir: "", Args: nil, Timeout: 0},
			check:   isNotAllowed,
		},
		{
			name:    "command that cannot start",
			command: buildloop.Command{Executable: missing(), Dir: "", Args: nil, Timeout: 0},
			check: func(err error) bool {
				var notAllowed *buildloop.NotAllowedError

				return err != nil && !errors.As(err, &notAllowed)
			},
		},
	}

	runner := newRunner(t, self(t), missing())

	for _, testCase := range tests {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()

			_, err := runner.Run(t.Context(), testCase.command)
			if !testCase.check(err) {
				t.Fatalf("Run() error = %v", err)
			}
		})
	}
}

func TestRunCancelled(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithCancel(t.Context())
	cancel()

	_, err := newRunner(t, self(t)).Run(ctx, helperCommand(t, 0, modeSleep))
	if !errors.Is(err, context.Canceled) {
		t.Errorf("Run() error = %v, want context.Canceled", err)
	}
}
