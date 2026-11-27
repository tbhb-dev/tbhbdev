// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

//go:build !unix || aix || (solaris && !illumos)

package main

import "context"

func (*serveConfig) withParked(_ context.Context, _ []runnerTarget, run func(parkedService) error) error {
	return run(noParkedService)
}
