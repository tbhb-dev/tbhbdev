// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package ghclient_test

import (
	"context"
	"testing"
	"testing/synctest"
)

// synctestRun runs body on a fake clock. The body must not use sockets.
func synctestRun(t *testing.T, body func(ctx context.Context)) {
	t.Helper()

	synctest.Test(t, func(t *testing.T) {
		body(t.Context())
	})
}
