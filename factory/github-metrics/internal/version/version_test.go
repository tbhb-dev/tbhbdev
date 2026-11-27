// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package version_test

import (
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/version"
)

func TestName(t *testing.T) {
	t.Parallel()

	if version.Name != "github-metrics" {
		t.Errorf("Name = %q, want %q", version.Name, "github-metrics")
	}
}
