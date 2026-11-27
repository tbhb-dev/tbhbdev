// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words serviceerror temporaltarget

package temporaltarget

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"

	"go.temporal.io/api/common/v1"
	"go.temporal.io/api/enums/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/temporal"

	sdk "go.temporal.io/sdk/client"
)

// SDKClient delivers event references through the Temporal SDK.
type SDKClient struct{ client sdk.Client }

var dialSDK = sdk.Dial

// Dial connects to Temporal using the parsed connection settings.
func Dial(connection Connection) (*SDKClient, error) {
	security, err := connection.TLS.config()
	if err != nil {
		return nil, err
	}

	var options sdk.Options

	options.HostPort, options.Namespace = connection.HostPort, connection.Namespace
	options.ConnectionOptions.TLS = security

	client, err := dialSDK(options)
	if err != nil {
		return nil, fmt.Errorf("dial Temporal: %w", err)
	}

	return &SDKClient{client: client}, nil
}

// Close releases the Temporal connection.
func (a *SDKClient) Close() { a.client.Close() }

func (files *TLSFiles) config() (*tls.Config, error) {
	if files == nil {
		return nil, nil
	}

	config := new(tls.Config)
	config.MinVersion = tls.VersionTLS12

	if files.CA != "" {
		pem, err := os.ReadFile(files.CA)
		if err != nil {
			return nil, fmt.Errorf("read Temporal CA: %w", err)
		}

		config.RootCAs = x509.NewCertPool()
		if !config.RootCAs.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("%w in %s", configError("invalid Temporal CA certificate"), files.CA)
		}
	}

	if files.Cert != "" {
		pair, err := tls.LoadX509KeyPair(files.Cert, files.Key)
		if err != nil {
			if pathError, ok := errors.AsType[*os.PathError](err); ok {
				return nil, fmt.Errorf("read Temporal key pair: %w", pathError)
			}

			return nil, fmt.Errorf("%w for %s and %s", configError("invalid Temporal key pair"), files.Cert, files.Key)
		}

		config.Certificates = []tls.Certificate{pair}
	}

	return config, nil
}

// Start starts a workflow with one event reference.
func (a *SDKClient) Start(ctx context.Context, req StartRequest) error {
	_, err := a.client.ExecuteWorkflow(ctx, startOptions(req), req.WorkflowType, rawInput(req.Input))

	return mapSDKError(err)
}

// Signal sends an event reference to the current run.
func (a *SDKClient) Signal(ctx context.Context, req SignalRequest) error {
	return mapSDKError(a.client.SignalWorkflow(ctx, req.WorkflowID, "", req.SignalName, rawInput(req.Input)))
}

// SignalWithStart starts the workflow if needed and signals it.
func (a *SDKClient) SignalWithStart(ctx context.Context, req SignalWithStartRequest) error {
	input := rawInput(req.Input)
	_, err := a.client.SignalWithStartWorkflow(ctx, req.WorkflowID, req.SignalName, input,
		startOptions(req.StartRequest), req.WorkflowType, input)

	return mapSDKError(err)
}

func startOptions(req StartRequest) sdk.StartWorkflowOptions {
	var options sdk.StartWorkflowOptions

	options.ID, options.TaskQueue = req.WorkflowID, req.TaskQueue
	options.WorkflowExecutionErrorWhenAlreadyStarted = true
	options.WorkflowIDConflictPolicy = enums.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING
	options.WorkflowIDReusePolicy = map[IDReuse]enums.WorkflowIdReusePolicy{
		AllowDuplicate:           enums.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
		AllowDuplicateFailedOnly: enums.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY,
		RejectDuplicate:          enums.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE,
	}[req.IDReuse]

	return options
}

func rawInput(input []byte) converter.RawValue {
	return converter.NewRawValue(&common.Payload{
		Data: input, Metadata: map[string][]byte{converter.MetadataEncoding: []byte(converter.MetadataEncodingJSON)},
		ExternalPayloads: nil,
	})
}

func mapSDKError(err error) error {
	switch {
	case temporal.IsWorkflowExecutionAlreadyStartedError(err):
		return fmt.Errorf("%w: Temporal workflow ID in use", ErrAlreadyStarted)
	case errors.As(err, new(*serviceerror.NotFound)):
		return fmt.Errorf("%w: Temporal workflow missing", ErrNotFound)
	case errors.As(err, new(*serviceerror.Unavailable)), errors.As(err, new(*serviceerror.ResourceExhausted)),
		errors.As(err, new(*serviceerror.DeadlineExceeded)), errors.As(err, new(*serviceerror.Canceled)),
		errors.As(err, new(*serviceerror.Aborted)), errors.As(err, new(*serviceerror.Internal)),
		errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
		return fmt.Errorf("%w: Temporal request failed", ErrTransient)
	default:
		return err
	}
}
