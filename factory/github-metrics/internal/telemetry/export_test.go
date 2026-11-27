// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package telemetry

import "net/http"

// NewHTTPClient exposes the exporter client for tests.
func NewHTTPClient() (*http.Client, error) {
	return newHTTPClient()
}
