// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words devserver eventfields runfile runfiles serviceerror temporaltarget

//nolint:exhaustruct_v5 // Fixtures use SDK defaults for unrelated options.
package temporaltarget

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"

	sdk "go.temporal.io/sdk/client"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/eventfields"
	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/routing"
)

const (
	devQueue     = "dev-test"
	devWorkflow  = "wait-for-event"
	devSignal    = "event"
	devNamespace = "default"
)

func temporalCLI(t *testing.T) string {
	t.Helper()

	key := os.Getenv("TEMPORAL_CLI")
	if key == "" {
		t.Fatal("TEMPORAL_CLI is missing; run the test with its Bazel runfiles")
	}

	manifest := os.Getenv("RUNFILES_MANIFEST_FILE")
	if manifest == "" {
		dir := os.Getenv("RUNFILES_DIR")
		if dir == "" {
			t.Fatal("Temporal CLI runfile requires RUNFILES_DIR or RUNFILES_MANIFEST_FILE")
		}

		return filepath.Join(dir, filepath.FromSlash(key))
	}

	//nolint:gosec // Bazel supplies the path to its runfiles manifest.
	data, err := os.ReadFile(manifest)
	devCheck(t, err)

	for line := range strings.SplitSeq(string(data), "\n") {
		if escaped, ok := strings.CutPrefix(line, " "); ok {
			line = strings.NewReplacer(`\n`, "\n", `\b`, `\`).Replace(escaped)
		}

		if target, ok := strings.CutPrefix(strings.TrimSuffix(line, "\r"), key+" "); ok {
			return filepath.FromSlash(target)
		}
	}

	t.Fatalf("Temporal CLI runfile %q missing from %s", key, manifest)

	return ""
}

func devCheck(t *testing.T, err error) {
	t.Helper()

	if err != nil {
		t.Fatalf("Temporal dev server: %v", err)
	}
}

func startDevServer(t *testing.T) (*SDKClient, func()) {
	t.Helper()

	var listen net.ListenConfig

	listener, listenErr := listen.Listen(t.Context(), "tcp", "127.0.0.1:0")
	devCheck(t, listenErr)

	address := listener.Addr().String()
	host, port, splitErr := net.SplitHostPort(address)
	devCheck(t, splitErr)

	devCheck(t, listener.Close())

	// Cleanup stops the worker before killing the server.
	processCtx := context.WithoutCancel(t.Context())
	//nolint:gosec // The executable is the pinned Bazel runfile.
	cmd := exec.CommandContext(processCtx, temporalCLI(t), "server", "start-dev",
		"--ip", host, "--port", port, "--headless", "--log-level", "error", "--namespace", devNamespace)

	var output bytes.Buffer

	cmd.Stdout, cmd.Stderr = &output, &output

	startErr := cmd.Start()
	if startErr != nil {
		t.Fatalf("start Temporal CLI runfile %q: %v", cmd.Path, startErr)
	}

	exited := make(chan error, 1)

	go func() {
		exited <- cmd.Wait()

		close(exited)
	}()

	stopServer := sync.OnceValue(func() error {
		var exitErr, killErr error

		select {
		case exitErr = <-exited:
		default:
			killErr = cmd.Process.Kill()

			const stopTimeout = 5 * time.Second

			// Wait can reap the process between the exit check and Kill.
			select {
			case exitErr = <-exited:
			case <-time.After(stopTimeout):
				return fmt.Errorf("Temporal dev server did not exit within %s: %w",
					stopTimeout, errors.Join(context.DeadlineExceeded, killErr))
			}
		}

		if exitErr != nil && cmd.ProcessState == nil {
			return fmt.Errorf("wait for Temporal dev server: %w", errors.Join(exitErr, killErr))
		}

		return nil
	})
	stop := func() {
		t.Helper()
		devCheck(t, stopServer())
	}
	t.Cleanup(stop)

	ctx, cancel := context.WithTimeout(t.Context(), 45*time.Second)
	defer cancel()

	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	options := sdk.Options{HostPort: address, Namespace: devNamespace}
	options.ConnectionOptions.GetSystemInfoTimeout = time.Second

	for {
		rpc, dialErr := sdk.DialContext(ctx, options)
		if dialErr == nil {
			adapter := &SDKClient{client: rpc}
			t.Cleanup(adapter.Close)

			return adapter, stop
		}

		select {
		case exitErr := <-exited:
			t.Fatalf("Temporal dev server exited before readiness: %v\n%s", exitErr, &output)
		case <-ctx.Done():
			stop()
			t.Fatalf("Temporal dev server at %s was not ready: %v\n%s", address, dialErr, &output)
		case <-ticker.C:
		}
	}
}

func waitForEvent(ctx workflow.Context, input json.RawMessage) ([]json.RawMessage, error) {
	var signal json.RawMessage

	workflow.GetSignalChannel(ctx, devSignal).Receive(ctx, &signal)

	return []json.RawMessage{input, signal}, nil
}

func actOnDevServer(ctx context.Context, t *testing.T, adapter *SDKClient, delivery, action string, seq uint64) error {
	t.Helper()

	rule := routing.Rule{Name: "dev-rule", Action: json.RawMessage(action)}
	target, err := New(routing.Target{Name: "dev-target", Type: "temporal", Rules: []routing.Rule{rule}}, adapter)
	devCheck(t, err)

	const event = `{"provider":"github","event":"issues","delivery":%q,"payload":{"secret":"omit-me"}}`

	envelope := fmt.Appendf(nil, event, delivery)
	fields, err := eventfields.FromEnvelope(seq, envelope)
	devCheck(t, err)

	return target.Act(ctx, rule, fields, envelope)
}

const (
	devAction = `"workflow_id":"{{.delivery}}"`
	devStart  = `{"kind":"start",` + devAction + `,"task_queue":"` + devQueue + `","workflow_type":"` +
		devWorkflow + `","id_reuse":`
	devSend = `{"kind":"signal",` + devAction + `,"signal":"` + devSignal + `"}`
	devBoth = `{"kind":"signal_with_start",` + devAction + `,"task_queue":"` + devQueue + `","workflow_type":"` +
		devWorkflow + `","id_reuse":"reject_duplicate","signal":"` + devSignal + `"}`
	devFirst = devStart + `"` + rejectReuse + `"}`
)

func TestTemporalDevServer(t *testing.T) {
	t.Parallel()

	adapter, _ := startDevServer(t)
	poller := worker.New(adapter.client, devQueue, worker.Options{})
	poller.RegisterWorkflowWithOptions(waitForEvent, workflow.RegisterOptions{Name: devWorkflow})
	devCheck(t, poller.Start())
	t.Cleanup(poller.Stop)

	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()

	const id, both = "start-then-signal", "signal-with-start"

	devCheck(t, actOnDevServer(ctx, t, adapter, id, devFirst, 1))
	devCheck(t, actOnDevServer(ctx, t, adapter, id, devSend, 2))
	checkDevResult(ctx, t, adapter, id, 1, 2)

	devCheck(t, actOnDevServer(ctx, t, adapter, both, devBoth, 3))
	checkDevResult(ctx, t, adapter, both, 3, 3)

	rawErr := adapter.client.SignalWorkflow(ctx, "missing", "", devSignal, nil)
	if _, ok := errors.AsType[*serviceerror.NotFound](rawErr); !ok || !errors.Is(mapSDKError(rawErr), ErrNotFound) {
		t.Fatalf("missing workflow signal returned %T (%v), want NotFound", rawErr, rawErr)
	}

	got := actOnDevServer(ctx, t, adapter, "missing", devSend, 4)
	if !errors.Is(got, deliveryError{signalKind, permanent}) {
		t.Fatalf("missing workflow signal returned %v, want permanent", got)
	}

	checkDevRepeats(t, adapter.client)
}

func checkDevResult(ctx context.Context, t *testing.T, adapter *SDKClient, workflowID string, sequences ...uint64) {
	t.Helper()

	var result []json.RawMessage

	devCheck(t, adapter.client.GetWorkflow(ctx, workflowID, "").Get(ctx, &result))

	if len(result) != len(sequences) {
		t.Fatalf("workflow returned %d references, want %d", len(result), len(sequences))
	}

	for index, seq := range sequences {
		want := fmt.Sprintf(`{"delivery":%q,"event":"issues","fields":{"action":null,"head_sha":null,"label":null,`+
			`"number":null,"sender":null},"provider":"github","received_at":"","repository":null,`+
			`"rule":"dev-rule","seq":%d,"target":"dev-target"}`, workflowID, seq)
		if string(result[index]) != want || len(result[index]) >= maxReferenceBytes {
			t.Fatalf("workflow reference = %s, want %s", result[index], want)
		}
	}
}

// Observe the real SDK response and vary the SDK-only flag at the call boundary.
type startObserver struct {
	sdk.Client

	run  sdk.WorkflowRun
	err  error
	flag bool
}

//nolint:ireturn // Match the SDK client interface.
func (o *startObserver) ExecuteWorkflow(
	ctx context.Context, options sdk.StartWorkflowOptions, name any, args ...any,
) (sdk.WorkflowRun, error) {
	options.WorkflowExecutionErrorWhenAlreadyStarted = o.flag
	o.run, o.err = o.Client.ExecuteWorkflow(ctx, options, name, args...)

	return o.run, o.err
}

type repeatCase struct {
	policy, state, outcome string
	flag                   bool
}

const (
	existingRun, newRun, delivered = "existing run", "new run", "delivered"
	running, completed             = "running", "completed"
	allowReuse, allowFailedReuse   = "allow_duplicate", "allow_duplicate_failed_only"
	rejectReuse                    = "reject_duplicate"
)

func checkDevRepeats(t *testing.T, rpc sdk.Client) {
	t.Helper()

	for _, testCase := range []repeatCase{
		{allowReuse, running, existingRun, false},
		{allowReuse, running, existingRun, true},
		{allowReuse, completed, newRun, false},
		{allowReuse, completed, newRun, true},
		{allowFailedReuse, running, existingRun, false},
		{allowFailedReuse, running, existingRun, true},
		{allowFailedReuse, completed, existingRun, false},
		{allowFailedReuse, completed, permanent, true},
		{rejectReuse, running, existingRun, false},
		{rejectReuse, running, existingRun, true},
		{rejectReuse, completed, existingRun, false},
		{rejectReuse, completed, delivered, true},
	} {
		name := testCase.policy + "/" + testCase.state + "/flag=" + strconv.FormatBool(testCase.flag)
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			checkDevRepeat(t, rpc, name, testCase)
		})
	}
}

func checkDevRepeat(t *testing.T, rpc sdk.Client, workflowID string, testCase repeatCase) {
	t.Helper()

	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()

	observer := &startObserver{Client: rpc, flag: testCase.flag}
	adapter := &SDKClient{client: observer}
	action := devStart + strconv.Quote(testCase.policy) + "}"
	devCheck(t, actOnDevServer(ctx, t, adapter, workflowID, action, 1))

	first := observer.run
	if testCase.state == completed {
		devCheck(t, rpc.SignalWorkflow(ctx, workflowID, first.GetRunID(), devSignal, nil))
		devCheck(t, first.Get(ctx, nil))
	}

	deliveryErr := actOnDevServer(ctx, t, adapter, workflowID, action, 2)
	t.Logf("repeat SDK error %T; target result %v", observer.err, deliveryErr)

	if testCase.outcome == delivered || testCase.outcome == permanent {
		if _, typed := errors.AsType[*serviceerror.WorkflowExecutionAlreadyStarted](observer.err); !typed ||
			!temporal.IsWorkflowExecutionAlreadyStartedError(observer.err) ||
			!errors.Is(mapSDKError(observer.err), ErrAlreadyStarted) {
			t.Fatalf("repeat returned %T (%v), want WorkflowExecutionAlreadyStarted", observer.err, observer.err)
		}
	} else {
		devCheck(t, observer.err)

		if observer.run == nil || (observer.run.GetRunID() == first.GetRunID()) != (testCase.outcome == existingRun) {
			t.Fatalf("repeat run = %v, want %s", observer.run, testCase.outcome)
		}
	}

	if testCase.outcome != permanent {
		devCheck(t, deliveryErr)
	} else if !errors.Is(deliveryErr, deliveryError{startKind, permanent}) {
		t.Fatalf("repeat target result = %v, want permanent", deliveryErr)
	}
}

func TestTemporalDevServerOutage(t *testing.T) {
	t.Parallel()

	adapter, stop := startDevServer(t)
	stop()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	got := actOnDevServer(ctx, t, adapter, "outage", devFirst, 1)
	if !errors.Is(got, deliveryError{startKind, "transient"}) {
		t.Fatalf("start during outage = %v, want transient", got)
	}
}
