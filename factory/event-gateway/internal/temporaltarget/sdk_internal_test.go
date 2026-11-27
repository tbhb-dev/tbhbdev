// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words serviceerror temporaltarget

package temporaltarget

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"go.temporal.io/api/common/v1"
	"go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"

	sdk "go.temporal.io/sdk/client"
)

const hidden = "hidden-value"

func TestMapSDKError(t *testing.T) {
	t.Parallel()

	permanent := serviceerror.NewInvalidArgument(hidden)
	for _, test := range []struct{ input, want error }{
		{serviceerror.NewWorkflowExecutionAlreadyStarted(hidden, "", ""), ErrAlreadyStarted},
		{&temporal.ChildWorkflowExecutionAlreadyStartedError{}, ErrAlreadyStarted},
		{fmt.Errorf("%s: %w", hidden, serviceerror.NewNotFound(hidden)), ErrNotFound},
		{serviceerror.NewUnavailable(hidden), ErrTransient},
		{serviceerror.NewResourceExhausted(0, hidden), ErrTransient},
		{serviceerror.NewDeadlineExceeded(hidden), ErrTransient},
		{serviceerror.NewCanceled(hidden), ErrTransient},
		{serviceerror.NewAborted(hidden), ErrTransient},
		{serviceerror.NewInternal(hidden), ErrTransient},
		{context.DeadlineExceeded, ErrTransient},
		{context.Canceled, ErrTransient},
		{permanent, permanent},
		{nil, nil},
	} {
		got := mapSDKError(test.input)
		if !errors.Is(got, test.want) ||
			(!errors.Is(test.want, permanent) && got != nil && strings.Contains(got.Error(), hidden)) {
			t.Errorf("mapSDKError(%T) = %v, want %v", test.input, got, test.want)
		}
	}
}

type sdkCall struct {
	options         sdk.StartWorkflowOptions
	workflow        any
	id, run, signal string
	args            []any
}

type sdkSpy struct {
	sdk.Client

	call  func(sdkCall) error
	close func()
}

func (s sdkSpy) Close() { s.close() }

func (s sdkSpy) ExecuteWorkflow(
	_ context.Context,
	options sdk.StartWorkflowOptions,
	name any,
	args ...any,
) (sdk.WorkflowRun, error) {
	return nil, s.call(sdkCall{id: "", run: "", signal: "", options: options, workflow: name, args: args})
}

func (s sdkSpy) SignalWorkflow(_ context.Context, id, run, signal string, arg any) error {
	return s.call(
		sdkCall{options: sdk.StartWorkflowOptions{}, workflow: nil, id: id, run: run, signal: signal, args: []any{arg}},
	)
}

func (s sdkSpy) SignalWithStartWorkflow(
	_ context.Context, id, signal string, arg any, options sdk.StartWorkflowOptions, name any, args ...any,
) (sdk.WorkflowRun, error) {
	return nil, s.call(
		sdkCall{run: "", options: options, workflow: name, id: id, signal: signal, args: append(args, arg)},
	)
}

func TestSDKActions(t *testing.T) {
	t.Parallel()

	const input, workflowName, signalName, queue = `{ "seq": 1 }`, "review", "update", "queue"

	req := StartRequest{
		IDReuse:      0,
		WorkflowID:   "workflow",
		TaskQueue:    queue,
		WorkflowType: workflowName,
		Input:        []byte(input),
	}
	raw := converter.NewRawValue(&common.Payload{
		ExternalPayloads: nil,

		Data: []byte(input), Metadata: map[string][]byte{"encoding": []byte("json/plain")},
	})

	want := sdkCall{
		options:  sdk.StartWorkflowOptions{},
		workflow: nil,
		run:      "",
		id:       req.WorkflowID,
		signal:   signalName,
		args:     []any{raw},
	}

	var adapter Client = &SDKClient{client: sdkSpy{
		Client: nil,
		close:  nil,
		call: func(got sdkCall) error {
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("SDK call = %+v, want %+v", got, want)
			}

			return nil
		},
	}}

	sdkCheck(t, adapter.Signal(t.Context(), SignalRequest{req.WorkflowID, signalName, req.Input}))

	for reuse, expected := range map[IDReuse]enums.WorkflowIdReusePolicy{
		AllowDuplicate:           enums.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
		RejectDuplicate:          enums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
		AllowDuplicateFailedOnly: enums.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY,
	} {
		req.IDReuse = reuse
		options := sdk.StartWorkflowOptions{
			ID: req.WorkflowID, TaskQueue: queue, WorkflowExecutionErrorWhenAlreadyStarted: true,
			WorkflowIDConflictPolicy: enums.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING, WorkflowIDReusePolicy: expected,
		}
		want = sdkCall{id: "", run: "", signal: "", options: options, workflow: workflowName, args: []any{raw}}

		sdkCheck(t, adapter.Start(t.Context(), req))

		want.id, want.signal, want.args = req.WorkflowID, signalName, []any{raw, raw}

		sdkCheck(
			t,
			adapter.SignalWithStart(t.Context(), SignalWithStartRequest{StartRequest: req, SignalName: signalName}),
		)
	}
}

func sdkCheck(t *testing.T, err error) {
	t.Helper()

	if err != nil {
		t.Fatal(err)
	}
}

func TestTLSConfig(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	certPath := filepath.Join(dir, "cert.pem")
	keyPath := filepath.Join(dir, "key.pem")
	missing := filepath.Join(dir, "missing.pem")
	invalid := filepath.Join(dir, "invalid.pem")
	sdkCheck(
		t,
		os.WriteFile(invalid, pem.EncodeToMemory(&pem.Block{Headers: nil, Type: hidden, Bytes: []byte(hidden)}), 0o600),
	)

	pub, private, err := ed25519.GenerateKey(rand.Reader)
	sdkCheck(t, err)

	cert := &x509.Certificate{BasicConstraintsValid: true, IsCA: true}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, pub, private)
	sdkCheck(t, err)

	keyDER, err := x509.MarshalPKCS8PrivateKey(private)
	sdkCheck(t, err)
	sdkCheck(
		t,
		os.WriteFile(certPath, pem.EncodeToMemory(&pem.Block{Headers: nil, Type: "CERTIFICATE", Bytes: der}), 0o600),
	)
	sdkCheck(
		t,
		os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Headers: nil, Type: "PRIVATE KEY", Bytes: keyDER}), 0o600),
	)
	config, err := (&TLSFiles{CA: certPath, Cert: certPath, Key: keyPath}).config()
	sdkCheck(t, err)

	if config.MinVersion != tls.VersionTLS12 || config.RootCAs == nil || len(config.Certificates) != 1 {
		t.Fatal("TLS config is missing the CA, key pair, or minimum TLS version")
	}

	for _, test := range []struct {
		files TLSFiles
		path  string
	}{
		{TLSFiles{Cert: "", Key: "", CA: missing}, missing},
		{TLSFiles{Cert: "", Key: "", CA: keyPath}, keyPath},
		{TLSFiles{CA: "", Cert: missing, Key: keyPath}, missing},
		{TLSFiles{CA: "", Cert: certPath, Key: missing}, missing},
		{TLSFiles{CA: "", Cert: invalid, Key: keyPath}, invalid},
	} {
		_, err = test.files.config()
		if err == nil || !strings.Contains(err.Error(), test.path) || strings.Contains(err.Error(), hidden) {
			t.Errorf("TLS file error for %s = %v", test.path, err)
		}
	}
}

func TestDial(t *testing.T) {
	t.Cleanup(func() { dialSDK = sdk.Dial })

	connection := Connection{TLS: nil, HostPort: "[::1]:7233", Namespace: "default"}
	closed := false
	dialSDK = func(options sdk.Options) (sdk.Client, error) {
		if options.HostPort != connection.HostPort || options.Namespace != connection.Namespace {
			t.Fatal("incorrect Temporal endpoint or namespace")
		}

		if options.ConnectionOptions.TLS == nil {
			return nil, ErrTransient
		}

		return sdkSpy{Client: nil, call: nil, close: func() { closed = true }}, nil
	}

	_, err := Dial(connection)
	if !errors.Is(err, ErrTransient) {
		t.Fatalf("dial error = %v", err)
	}

	connection.TLS = new(TLSFiles)

	client, err := Dial(connection)
	sdkCheck(t, err)
	client.Close()

	if !closed {
		t.Fatal("SDK client was not closed")
	}

	connection.TLS.CA = filepath.Join(t.TempDir(), "missing.pem")

	_, err = Dial(connection)
	if !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("TLS file error = %v", err)
	}
}
